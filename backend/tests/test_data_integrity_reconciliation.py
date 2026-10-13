"""Tests for audited evidence correction and reconciled resolution summaries."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

TEST_DB = Path(__file__).resolve().parent / "_resolveos_integrity_reconciliation_test.sqlite3"
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
from app.evidence_integrity import evidence_integrity_counts, historical_verification_problem
import app.main as main_module
from app.resolution_closure import build_resolution_summary, validate_closure

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
    response = client.post("/cases", json={
        "order_id": "ORD-1003",
        "customer_message": "My webcam has not arrived; tracking says in transit.",
    })
    assert response.status_code == 200, response.text
    return response.json()


def add_historically_suspicious_verified_record(case_id: str) -> dict[str, Any]:
    created = db.create_case_evidence(case_id, {
        "tracking_number": "FAKE-TRACK-001",
        "latest_carrier_scan": "DEMO ONLY — no real carrier scan",
        "expected_delivery_date": "2026-10-20",
        "source_reference": "https://example.com/test-evidence",
        "evidence_notes": "test only placeholder record",
    }, submitted_by="merchant_admin")
    review_note = "Test only: no verifiable evidence was attached."
    assert db.review_case_evidence(
        case_id, created["evidence_id"], "VERIFIED", "merchant_admin", review_note,
    )
    db.log_audit(case_id, "merchant_admin", "EVIDENCE_REVIEWED", {
        "evidence_id": created["evidence_id"],
        "verification_status": "VERIFIED",
        "reviewed_by": "merchant_admin",
        "review_note": review_note,
        "note": "Synthetic historic audit event to model the already-saved case history.",
    })
    return db.get_case_evidence(case_id, created["evidence_id"])


def test_historical_verified_with_contradictory_note_is_flagged_and_excluded_from_verified_count():
    record = {
        "evidence_id": "EV-000001",
        "verification_status": "VERIFIED",
        "review_note": "Test only: no verifiable evidence was attached.",
        "evidence": {"source_reference": "https://example.com/test-evidence"},
    }
    assert historical_verification_problem(record)
    counts = evidence_integrity_counts([record])
    assert counts["historically_marked_verified"] == 1
    assert counts["verified"] == 0
    assert counts["historical_verified_needing_review"] == 1


def test_revoke_verification_requires_merchant_key(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    record = add_historically_suspicious_verified_record(case["case_id"])
    response = client.post(
        f"/cases/{case['case_id']}/evidence/{record['evidence_id']}/revoke-verification",
        json={"reason": "Historical status needs correction after review."},
    )
    assert response.status_code == 401


def test_revoke_verification_preserves_original_review_and_payment_case_state(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    case_id = case["case_id"]
    record = add_historically_suspicious_verified_record(case_id)
    original_case = db.get_case(case_id)
    original_order = db.get_order("ORD-1003")
    original_review_note = record["review_note"]
    response = client.post(
        f"/cases/{case_id}/evidence/{record['evidence_id']}/revoke-verification",
        headers=ADMIN,
        json={"reason": "Historical note admits test-only data; revoke its verified label."},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["verification_status"] == "VERIFICATION_REVOKED"
    assert payload["previous_status"] == "VERIFIED"
    updated = db.get_case_evidence(case_id, record["evidence_id"])
    assert updated["verification_status"] == "VERIFICATION_REVOKED"
    assert updated["review_note"] == original_review_note
    assert updated["reviewed_by"] == "merchant_admin"
    assert updated["verification_revoked_from"] == "VERIFIED"
    assert updated["verification_revoked_by"] == "merchant_admin"
    assert updated["verification_revoked_at"]
    assert "test-only data" in updated["verification_revocation_note"]
    events = db.get_audit(case_id)
    assert any(e["action"] == "EVIDENCE_REVIEWED" for e in events)
    assert any(e["action"] == "EVIDENCE_VERIFICATION_REVOKED" for e in events)
    assert db.get_case(case_id)["status"] == original_case["status"]
    assert db.get_case(case_id)["policy_decision"] == original_case["policy_decision"]
    assert db.get_order("ORD-1003")["refund_status"] == original_order["refund_status"]
    assert db.get_order("ORD-1003")["paypal_capture_id"] == original_order["paypal_capture_id"]


def test_cannot_revoke_evidence_that_is_not_currently_verified(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    record = db.create_case_evidence(case["case_id"], {"evidence_notes": "An unverified record"}, "merchant_admin")
    response = client.post(
        f"/cases/{case['case_id']}/evidence/{record['evidence_id']}/revoke-verification",
        headers=ADMIN,
        json={"reason": "Correcting a previous evidence verification decision."},
    )
    assert response.status_code == 409
    assert db.get_case_evidence(case["case_id"], record["evidence_id"])["verification_status"] == "SUBMITTED_UNVERIFIED"


def test_revocation_requires_meaningful_reason(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    record = add_historically_suspicious_verified_record(case["case_id"])
    response = client.post(
        f"/cases/{case['case_id']}/evidence/{record['evidence_id']}/revoke-verification",
        headers=ADMIN,
        json={"reason": "bad"},
    )
    assert response.status_code == 422


def test_summary_counts_suspicious_legacy_verification_and_follow_up_statuses_correctly(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    case_id = case["case_id"]
    add_historically_suspicious_verified_record(case_id)
    actions = [
        {"action_key": "OPEN-A", "title": "Open A", "description": "open", "assigned_to": "MERCHANT", "priority": "HIGH", "status": "OPEN", "completion_gate": "MERCHANT_NOTE", "blocked_reason": None},
        {"action_key": "OPEN-B", "title": "Open B", "description": "open", "assigned_to": "MERCHANT", "priority": "HIGH", "status": "IN_PROGRESS", "completion_gate": "MERCHANT_NOTE", "blocked_reason": None},
        {"action_key": "BLOCK-A", "title": "Blocked A", "description": "blocked", "assigned_to": "MERCHANT", "priority": "HIGH", "status": "BLOCKED", "completion_gate": "MERCHANT_NOTE", "blocked_reason": "Prerequisite A"},
        {"action_key": "BLOCK-B", "title": "Blocked B", "description": "blocked", "assigned_to": "MERCHANT", "priority": "HIGH", "status": "BLOCKED", "completion_gate": "MERCHANT_NOTE", "blocked_reason": "Prerequisite B"},
    ]
    db.upsert_case_follow_up_actions(case_id, "TEST-RUN", actions, created_by="merchant_admin", satisfied_gates=set())
    summary = client.post(f"/cases/{case_id}/resolution-summary", headers=ADMIN)
    assert summary.status_code == 200, summary.text
    saved = summary.json()["summary_record"]["summary"]
    counts = saved["evidence"]["counts"]
    assert counts["verified"] == 0
    assert counts["historical_verified_needing_review"] == 1
    assert saved["follow_up"]["total"] == 4
    assert saved["follow_up"]["open_or_in_progress"] == 2
    assert saved["follow_up"]["blocked"] == 2
    assert saved["follow_up"]["completed"] == 0
    assert any("historical VERIFIED" in blocker for blocker in saved["closure_readiness"]["blockers"])


def test_summary_counts_revoked_evidence_separately(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    case_id = case["case_id"]
    record = add_historically_suspicious_verified_record(case_id)
    response = client.post(
        f"/cases/{case_id}/evidence/{record['evidence_id']}/revoke-verification",
        headers=ADMIN,
        json={"reason": "Historical note admits test-only data; revoke its verified label."},
    )
    assert response.status_code == 200
    summary_resp = client.post(f"/cases/{case_id}/resolution-summary", headers=ADMIN)
    assert summary_resp.status_code == 200, summary_resp.text
    counts = summary_resp.json()["summary_record"]["summary"]["evidence"]["counts"]
    assert counts["verified"] == 0
    assert counts["historical_verified_needing_review"] == 0
    assert counts["verification_revoked"] == 1


def test_no_refund_close_is_blocked_by_suspicious_historical_verified_evidence(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    case_id = case["case_id"]
    add_historically_suspicious_verified_record(case_id)
    db.update_case_policy_result(case_id, "NEEDS_HUMAN_REVIEW", ["Human review required."], "NEEDS_HUMAN_REVIEW")
    order = db.get_order("ORD-1003") or {}
    evidence = db.list_case_evidence(case_id)
    reason = validate_closure("RESOLVED_NO_REFUND", db.get_case(case_id) or {}, order, evidence, [], [])
    assert reason is not None
    assert "contradictory review metadata" in reason.lower()


def test_dashboard_exposes_audited_revoke_control_and_reconciled_counts():
    dashboard = Path(__file__).resolve().parents[1] / "app" / "dashboard.html"
    html = dashboard.read_text(encoding="utf-8")
    assert 'data-revoke-verification' in html
    assert 'evidence-revoke-note-' in html
    assert 'historical VERIFIED needing correction' in html
    assert 'open / in progress' in html


def test_migration_has_revocation_metadata_columns():
    db.init_db()
    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(case_evidence)").fetchall()}
    assert {
        "verification_revoked_from", "verification_revoked_by",
        "verification_revoked_at", "verification_revocation_note",
    }.issubset(columns)
