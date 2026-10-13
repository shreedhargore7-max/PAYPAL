"""Offline integration tests for the end-to-end workflow coordinator."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

TEST_DB = Path(__file__).resolve().parent / "_resolveos_end_to_end_workflow_test.sqlite3"
os.environ["RESOLVEOS_DB_PATH"] = str(TEST_DB)
os.environ["ADMIN_API_KEY"] = "test-admin-key-not-a-real-secret"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["AFTERSHIP_API_KEY"] = ""
os.environ["CARRIER_TRACKING_MODE"] = "demo"
os.environ["PAYPAL_CLIENT_ID"] = "test-sandbox-client-id"
os.environ["PAYPAL_CLIENT_SECRET"] = "test-sandbox-client-secret"

from fastapi.testclient import TestClient

from app import db
import app.main as main_module

client = TestClient(main_module.app)
ADMIN = {"X-Admin-Key": os.environ["ADMIN_API_KEY"]}


def fake_item_not_received(_message: str, _order: dict[str, Any]) -> dict[str, Any]:
    return {
        "issue_type": "ITEM_NOT_RECEIVED",
        "summary": "Test complaint used for end-to-end workflow tests.",
        "requested_outcome": "resolution",
        "confidence": 0.95,
    }


def reset_db() -> None:
    db.init_db()
    with db.connect() as conn:
        for table in (
            "case_resolution_summaries", "case_customer_message_drafts",
            "case_follow_up_actions", "carrier_tracking_checks", "case_evidence",
            "audit_log", "cases", "orders",
        ):
            conn.execute(f"DELETE FROM {table}")
    db.init_db()


def make_case(monkeypatch) -> dict[str, Any]:
    monkeypatch.setattr(main_module, "classify_complaint", fake_item_not_received)
    response = client.post(
        "/cases",
        json={
            "order_id": "ORD-1003",
            "customer_message": "My webcam has not arrived and tracking says in transit.",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def add_evidence(case_id: str, tracking_number: str, note: str = "Merchant submitted data for testing.") -> dict[str, Any]:
    return db.create_case_evidence(
        case_id,
        {
            "tracking_number": tracking_number,
            "latest_carrier_scan": "Demo scan; timestamp not independently verified.",
            "expected_delivery_date": "2026-10-20",
            "delivery_address_confirmed": False,
            "source_reference": "https://example.com/test-evidence",
            "evidence_notes": note,
        },
        submitted_by="merchant_admin",
    )


def test_workflow_coordinate_requires_merchant_key(monkeypatch):
    reset_db()
    case = make_case(monkeypatch)
    response = client.post(f"/cases/{case['case_id']}/workflow/coordinate")
    assert response.status_code == 401


def test_workflow_run_records_dynamic_stages_next_safe_step_and_no_mutations(monkeypatch):
    reset_db()
    case = make_case(monkeypatch)
    evidence = add_evidence(case["case_id"], "DEMO-MISMATCH-001", "TEST ONLY — no real evidence attached")
    db.create_carrier_tracking_check(
        case["case_id"], evidence["evidence_id"], "DEMO-MISMATCH-001",
        {
            "provider": "ResolveOS Demo (SIMULATED)",
            "source_mode": "SIMULATED_DEMO",
            "lookup_state": "SIMULATED_DEMO_DATA",
            "tracking_status": "Delivered",
            "estimated_delivery_date": "2026-10-21",
            "comparison": {
                "overall_result": "SIMULATED_DISCREPANCY",
                "shipping_status_comparison": "DIFFERENT_REVIEW_REQUIRED",
                "delivery_date_comparison": "DIFFERENT_REVIEW_REQUIRED",
                "human_review_required": True,
            },
        },
    )
    before_case = db.get_case(case["case_id"])
    before_order = db.get_order("ORD-1003")

    response = client.post(f"/cases/{case['case_id']}/workflow/coordinate", headers=ADMIN)
    assert response.status_code == 200, response.text
    result = response.json()["workflow"]
    assert result["workflow_name"] == "ResolveOS End-to-End Investigation Workflow"
    assert result["active_evidence_id"] == evidence["evidence_id"]
    assert result["active_tracking_number"] == "DEMO-MISMATCH-001"
    assert result["active_tracking_source_mode"] == "SIMULATED_DEMO"
    assert result["saved_policy_decision"] == "NEEDS_MORE_INFORMATION"
    assert result["next_safe_step"]["stage_key"] == "EVIDENCE_REVIEW"
    assert any(stage["status"] == "SIMULATED_ONLY" for stage in result["stages"])
    assert any("no verified PayPal capture ID is linked" in stage["summary"] for stage in result["stages"])
    assert any("simulated tracking=True" in trace for trace in result["decision_trace"])

    after_case = db.get_case(case["case_id"])
    after_order = db.get_order("ORD-1003")
    assert after_case["status"] == before_case["status"]
    assert after_case["policy_decision"] == before_case["policy_decision"]
    assert after_order["shipping_status"] == before_order["shipping_status"]
    assert after_order["paypal_capture_id"] == before_order["paypal_capture_id"]
    assert after_order["refund_status"] == before_order["refund_status"]

    detail = client.get(f"/cases/{case['case_id']}").json()
    event = next(e for e in reversed(detail["audit_log"]) if e["action"] == "END_TO_END_WORKFLOW_COORDINATED")
    assert event["details"]["workflow_id"] == result["workflow_id"]
    assert event["details"]["workflow"]["next_safe_step"] == result["next_safe_step"]


def test_workflow_selects_tracking_only_for_active_evidence_and_exact_number(monkeypatch):
    reset_db()
    case = make_case(monkeypatch)
    old = add_evidence(case["case_id"], "DEMO-OLD-001", "TEST ONLY — older placeholder")
    # Create and then revoke a historically verified record so it is no longer active.
    with db.connect() as conn:
        conn.execute(
            "UPDATE case_evidence SET verification_status='VERIFIED', review_note=?, reviewed_by=?, reviewed_at=? WHERE id=?",
            ("Test only: no verifiable evidence was attached.", "merchant_admin", db.utc_now(), int(old["evidence_id"].split("-")[1])),
        )
    assert db.revoke_case_evidence_verification(
        case["case_id"], old["evidence_id"], "merchant_admin", "Revoked placeholder test evidence."
    )
    db.create_carrier_tracking_check(
        case["case_id"], old["evidence_id"], "DEMO-OLD-001",
        {"provider": "ResolveOS Demo (SIMULATED)", "source_mode": "SIMULATED_DEMO", "tracking_status": "Delivered"},
    )
    current = add_evidence(case["case_id"], "DEMO-CURRENT-002", "Current submitted evidence; not yet verified.")
    db.create_carrier_tracking_check(
        case["case_id"], current["evidence_id"], "DEMO-CURRENT-002",
        {"provider": "ResolveOS Demo (SIMULATED)", "source_mode": "SIMULATED_DEMO", "tracking_status": "InTransit"},
    )

    response = client.post(f"/cases/{case['case_id']}/workflow/coordinate", headers=ADMIN)
    assert response.status_code == 200, response.text
    result = response.json()["workflow"]
    assert result["active_evidence_id"] == current["evidence_id"]
    assert result["active_tracking_number"] == "DEMO-CURRENT-002"
    assert result["active_tracking_check_id"] is not None
    assert all("DEMO-OLD-001" not in stage["summary"] for stage in result["stages"])


def test_workflow_returns_404_for_unknown_case():
    reset_db()
    response = client.post("/cases/CASE-DOES-NOT-EXIST/workflow/coordinate", headers=ADMIN)
    assert response.status_code == 404


def test_workflow_cannot_coordinate_closed_case(monkeypatch):
    reset_db()
    case = make_case(monkeypatch)
    with db.connect() as conn:
        conn.execute("UPDATE cases SET status='CLOSED_NO_ACTION' WHERE case_id=?", (case["case_id"],))
    response = client.post(f"/cases/{case['case_id']}/workflow/coordinate", headers=ADMIN)
    assert response.status_code == 409


def test_dashboard_has_end_to_end_workflow_controls():
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "End-to-End Investigation Workflow" in response.text
    assert "Coordinate end-to-end workflow" in response.text
    assert "/workflow/coordinate" in response.text
