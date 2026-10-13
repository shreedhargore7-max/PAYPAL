"""Offline tests for resolution summaries and explicit, guarded case closure."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

TEST_DB = Path(__file__).resolve().parent / "_resolveos_resolution_summary_closure_test.sqlite3"
os.environ["RESOLVEOS_DB_PATH"] = str(TEST_DB)
os.environ["ADMIN_API_KEY"] = "test-admin-key-not-a-real-secret"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["AFTERSHIP_API_KEY"] = ""
os.environ["CARRIER_TRACKING_MODE"] = "demo"
os.environ["PAYPAL_CLIENT_ID"] = "test-sandbox-client-id"
os.environ["PAYPAL_CLIENT_SECRET"] = "test-sandbox-client-secret"

import pytest
from fastapi.testclient import TestClient

from app import db
import app.main as main_module

client = TestClient(main_module.app)
ADMIN = {"X-Admin-Key": os.environ["ADMIN_API_KEY"]}


def fake_item_not_received(_message: str, _order: dict[str, Any]) -> dict[str, Any]:
    return {
        "issue_type": "ITEM_NOT_RECEIVED",
        "summary": "Customer says the webcam has not arrived.",
        "requested_outcome": "resolution",
        "confidence": 0.95,
    }


@pytest.fixture(autouse=True)
def reset_test_database():
    db.init_db()
    with db.connect() as conn:
        conn.execute("DELETE FROM case_resolution_summaries")
        conn.execute("DELETE FROM case_customer_message_drafts")
        conn.execute("DELETE FROM case_follow_up_actions")
        conn.execute("DELETE FROM carrier_tracking_checks")
        conn.execute("DELETE FROM case_evidence")
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM cases")
        conn.execute("DELETE FROM orders")
    db.init_db()
    yield


def make_case(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(main_module, "classify_complaint", fake_item_not_received)
    response = client.post(
        "/cases",
        json={"order_id": "ORD-1003", "customer_message": "My webcam has not arrived; tracking says in transit."},
    )
    assert response.status_code == 200, response.text
    return response.json()


def generate_summary(case_id: str):
    return client.post(f"/cases/{case_id}/resolution-summary", headers=ADMIN)


def close(case_id: str, outcome: str, note: str, summary_id: str | None = None):
    body: dict[str, Any] = {"closure_outcome": outcome, "closure_note": note}
    if summary_id:
        body["summary_id"] = summary_id
    return client.post(f"/cases/{case_id}/close", headers=ADMIN, json=body)


def test_summary_generation_requires_merchant_key(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    assert client.post(f"/cases/{case['case_id']}/resolution-summary").status_code == 401


def test_summary_is_saved_and_includes_saved_case_evidence_and_closure_blockers(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    case_id = case["case_id"]
    before_case = db.get_case(case_id)
    before_order = db.get_order("ORD-1003")
    evidence_record = db.create_case_evidence(case_id, {"tracking_number": "DEMO-MISMATCH-001", "evidence_notes": "Demo only"})
    db.create_carrier_tracking_check(case_id, evidence_record["evidence_id"], "DEMO-MISMATCH-001", {
        "provider": "SIMULATED_DEMO", "source_mode": "SIMULATED_DEMO", "tracking_status": "DELIVERED",
        "comparison": {"shipping_status_comparison": "DIFFERENT_REVIEW_REQUIRED", "delivery_date_comparison": "DIFFERENT_REVIEW_REQUIRED"},
    })
    response = generate_summary(case_id)
    assert response.status_code == 200, response.text
    saved = response.json()["summary_record"]
    assert saved["summary_id"].startswith("RS-")
    assert saved["status"] == "DRAFT"
    summary = saved["summary"]
    assert summary["case"]["policy_decision"] == "NEEDS_MORE_INFORMATION"
    assert summary["evidence"]["counts"]["unverified"] == 1
    assert summary["tracking"]["comparison_outcome"] == "SIMULATED_DISCREPANCY"
    assert summary["closure_readiness"]["status"] == "NOT_READY_TO_CLOSE"
    assert summary["closure_readiness"]["blockers"]
    latest = client.get(f"/cases/{case_id}/resolution-summary")
    assert latest.status_code == 200
    assert latest.json()["latest_summary"]["summary_id"] == saved["summary_id"]
    detail = client.get(f"/cases/{case_id}").json()
    assert detail["resolution_summaries"][0]["summary_id"] == saved["summary_id"]
    assert db.get_case(case_id)["status"] == before_case["status"]
    assert db.get_case(case_id)["policy_decision"] == before_case["policy_decision"]
    assert db.get_order("ORD-1003")["paypal_capture_id"] == before_order["paypal_capture_id"]
    assert db.get_order("ORD-1003")["refund_status"] == before_order["refund_status"]
    assert any(e["action"] == "RESOLUTION_SUMMARY_GENERATED" for e in detail["audit_log"])


def test_close_requires_saved_summary(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    response = close(case["case_id"], "ESCALATED", "Escalated to senior support for manual review.")
    assert response.status_code == 409
    assert db.get_case(case["case_id"])["status"] == case["status"]


def test_unresolved_case_cannot_close_as_no_refund(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    assert generate_summary(case["case_id"]).status_code == 200
    before_order = db.get_order("ORD-1003")
    response = close(case["case_id"], "RESOLVED_NO_REFUND", "Merchant decided not to refund this customer.")
    assert response.status_code == 409
    assert "NEEDS_MORE_INFORMATION" in response.json()["detail"]
    assert db.get_case(case["case_id"])["status"] == "NEEDS_MORE_INFORMATION"
    assert db.get_order("ORD-1003")["refund_status"] == before_order["refund_status"]
    assert any(e["action"] == "CASE_CLOSURE_BLOCKED" for e in client.get(f"/cases/{case['case_id']}").json()["audit_log"])


def test_explicit_escalation_can_close_unresolved_case_without_payment_changes(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    case_id = case["case_id"]
    generate_summary(case_id)
    before_order = db.get_order("ORD-1003")
    response = close(case_id, "ESCALATED", "Handed off to the senior merchant support queue for manual review.")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["case_status"] == "CLOSED_ESCALATED"
    assert payload["resolution_record"]["status"] == "CLOSED"
    assert payload["resolution_record"]["closed_by"] == "merchant_admin"
    assert db.get_case(case_id)["policy_decision"] == "NEEDS_MORE_INFORMATION"
    assert db.get_order("ORD-1003")["paypal_capture_id"] == before_order["paypal_capture_id"]
    assert db.get_order("ORD-1003")["refund_status"] == before_order["refund_status"]
    detail = client.get(f"/cases/{case_id}").json()
    assert any(event["action"] == "CASE_CLOSED" for event in detail["audit_log"])


def test_case_cannot_be_closed_twice_or_mutated_after_closure(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    case_id = case["case_id"]
    generate_summary(case_id)
    closed = close(case_id, "CUSTOMER_WITHDREW", "Customer withdrew the request after support review.")
    assert closed.status_code == 200, closed.text
    assert close(case_id, "ESCALATED", "Handed off to another merchant support team.").status_code == 409
    evidence_response = client.post(
        f"/cases/{case_id}/evidence", headers=ADMIN,
        json={"evidence_notes": "An attempted post-closure evidence edit."},
    )
    assert evidence_response.status_code == 409
    summary_response = generate_summary(case_id)
    assert summary_response.status_code == 409


def test_completed_refund_outcome_requires_completed_paypal_refund(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    generate_summary(case["case_id"])
    response = close(case["case_id"], "RESOLVED_REFUND_COMPLETED", "Merchant confirms the refund is complete and recorded.")
    assert response.status_code == 409
    assert "could not be confirmed" in response.json()["detail"].lower()


def test_no_refund_close_blocked_by_open_follow_up_tasks(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    case_id = case["case_id"]
    # Move policy to a reviewed state for isolating the open-task completion gate.
    db.update_case_policy_result(case_id, "NEEDS_HUMAN_REVIEW", ["Human review decision recorded for this test."], "NEEDS_HUMAN_REVIEW")
    db.upsert_case_follow_up_actions(case_id, "TEST-RUN", [{
        "action_key": "TEST_OPEN_ACTION", "title": "Review case records",
        "description": "Merchant needs to review the records.", "assigned_to": "MERCHANT", "priority": "MEDIUM",
        "status": "OPEN", "completion_gate": "MERCHANT_NOTE", "blocked_reason": None,
    }], created_by="merchant_admin", satisfied_gates=set())
    generate_summary(case_id)
    response = close(case_id, "RESOLVED_NO_REFUND", "Merchant reviewed the case and decided there is no refund.")
    assert response.status_code == 409
    assert "follow-up action" in response.json()["detail"].lower()


def test_bad_closure_note_is_rejected_by_request_validation(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    generate_summary(case["case_id"])
    response = close(case["case_id"], "ESCALATED", "short")
    assert response.status_code == 422


def test_unknown_case_summary_not_found(monkeypatch: pytest.MonkeyPatch):
    assert client.post("/cases/CASE-UNKNOWN/resolution-summary", headers=ADMIN).status_code == 404
    assert client.get("/cases/CASE-UNKNOWN/resolution-summary").status_code == 404


def test_closure_requires_summary_to_be_refreshed_after_new_evidence(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    case_id = case["case_id"]
    generate_summary(case_id)
    # This investigation event makes the saved summary stale and must force a refresh.
    db.log_audit(case_id, "merchant_admin", "EVIDENCE_SUBMITTED", {"evidence_id": "EV-FAKE", "note": "test stale summary gate"})
    response = close(case_id, "ESCALATED", "Handed off to the senior merchant support queue for manual review.")
    assert response.status_code == 409
    assert "fresh resolution summary" in response.json()["detail"].lower()
    assert not db.get_case(case_id)["status"].startswith("CLOSED_")


def test_resolved_no_refund_can_close_after_review_when_prerequisites_are_clear(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_module, "classify_complaint", fake_item_not_received)
    created = client.post(
        "/cases",
        json={"order_id": "ORD-1002", "customer_message": "My order says delivered but I cannot locate it."},
    )
    assert created.status_code == 200, created.text
    case = created.json()
    case_id = case["case_id"]
    evidence = client.post(
        f"/cases/{case_id}/evidence", headers=ADMIN,
        json={
            "tracking_number": "TRACK-TEST-002",
            "latest_carrier_scan": "Test carrier delivery scan at 2026-10-12 10:30 UTC",
            "expected_delivery_date": "2026-10-12",
            "source_reference": "https://www.ups.com/track?tracknum=TRACK-TEST-002",
            "evidence_notes": "Merchant reviewed the source for this offline test.",
        },
    )
    assert evidence.status_code == 200, evidence.text
    evidence_id = evidence.json()["evidence_record"]["evidence_id"]
    rejected = client.post(
        f"/cases/{case_id}/evidence/{evidence_id}/review", headers=ADMIN,
        json={"decision": "REJECTED", "review_note": "Source did not substantiate the customer's missing-item report."},
    )
    assert rejected.status_code == 200, rejected.text
    summary = generate_summary(case_id)
    assert summary.status_code == 200, summary.text
    closed = close(case_id, "RESOLVED_NO_REFUND", "Evidence was reviewed and the merchant decided no refund is warranted.")
    assert closed.status_code == 200, closed.text
    assert closed.json()["case_status"] == "CLOSED_RESOLVED_NO_REFUND"
    assert db.get_order("ORD-1002")["refund_status"] == "NONE"
