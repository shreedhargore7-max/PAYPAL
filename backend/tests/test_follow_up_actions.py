"""Offline tests for the persistent Investigation Actions & Follow-up feature.

No live AI, carrier API, or PayPal calls are used by these tests.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

TEST_DB = Path(__file__).resolve().parent / "_resolveos_follow_up_actions_test.sqlite3"
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
        "summary": "Test case used for offline follow-up action tests.",
        "requested_outcome": "resolution",
        "confidence": 0.95,
    }


@pytest.fixture(autouse=True)
def reset_test_database():
    db.init_db()
    with db.connect() as conn:
        conn.execute("DELETE FROM case_follow_up_actions")
        conn.execute("DELETE FROM carrier_tracking_checks")
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM case_evidence")
        conn.execute("DELETE FROM cases")
        conn.execute("DELETE FROM orders")
    db.init_db()
    yield


def make_case(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
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


def run_investigation(case_id: str) -> dict[str, Any]:
    response = client.post(f"/cases/{case_id}/investigation/run", headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


def add_reviewable_evidence(case_id: str) -> dict[str, Any]:
    response = client.post(
        f"/cases/{case_id}/evidence",
        headers=ADMIN,
        json={
            "tracking_number": "TRACK123456",
            "latest_carrier_scan": "At regional facility; 2026-10-12 08:30 UTC",
            "expected_delivery_date": "2026-10-20",
            "delivery_address_confirmed": True,
            "carrier_investigation": "Reference recorded on the official carrier page.",
            "evidence_notes": "Merchant reviewed the official carrier tracking page.",
            "source_reference": "https://www.ups.com/track?tracknum=TRACK123456",
        },
    )
    assert response.status_code == 200, response.text
    record = response.json()["evidence_record"]
    review = client.post(
        f"/cases/{case_id}/evidence/{record['evidence_id']}/review",
        headers=ADMIN,
        json={
            "decision": "VERIFIED",
            "review_note": "Opened official carrier tracking page and checked the tracking number and timestamp.",
        },
    )
    assert review.status_code == 200, review.text
    return record


def refresh_plan(case_id: str) -> dict[str, Any]:
    response = client.post(f"/cases/{case_id}/follow-up-actions/refresh", headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()["follow_up_plan"]["actions"]


def find_action(case_id: str, key: str) -> dict[str, Any]:
    detail = client.get(f"/cases/{case_id}").json()
    return next(item for item in detail["follow_up_actions"] if item["action_key"] == key)


def update_action(case_id: str, action: dict[str, Any], status: str, note: str) -> Any:
    return client.post(
        f"/cases/{case_id}/follow-up-actions/{action['action_id']}/update",
        headers=ADMIN,
        json={"status": status, "note": note},
    )


def test_investigation_run_persists_follow_up_plan_and_audit(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    before_case = db.get_case(case["case_id"])
    before_order = db.get_order("ORD-1003")

    run_investigation(case["case_id"])
    detail = client.get(f"/cases/{case['case_id']}").json()
    actions = detail["follow_up_actions"]
    keys = {item["action_key"] for item in actions}

    assert {"VERIFY_CAPTURE_LINKED", "COLLECT_VERIFY_EVIDENCE", "REVIEW_OFFICIAL_TRACKING_SOURCE", "RE_EVALUATE_POLICY"}.issubset(keys)
    assert find_action(case["case_id"], "RE_EVALUATE_POLICY")["status"] == "BLOCKED"
    assert any(event["action"] == "FOLLOW_UP_PLAN_REFRESHED" for event in detail["audit_log"])
    assert db.get_case(case["case_id"])["policy_decision"] == before_case["policy_decision"]
    assert db.get_order("ORD-1003")["paypal_capture_id"] == before_order["paypal_capture_id"]
    assert db.get_order("ORD-1003")["refund_status"] == before_order["refund_status"]


def test_follow_up_refresh_requires_merchant_authorization(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    response = client.post(f"/cases/{case['case_id']}/follow-up-actions/refresh")
    assert response.status_code == 401


def test_blocked_action_cannot_be_changed_until_prerequisites_are_met(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    run_investigation(case["case_id"])
    action = find_action(case["case_id"], "RE_EVALUATE_POLICY")
    assert action["status"] == "BLOCKED"
    response = update_action(case["case_id"], action, "IN_PROGRESS", "Starting deterministic policy review now.")
    assert response.status_code == 409


def test_capture_action_cannot_be_completed_without_linked_completed_capture(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    run_investigation(case["case_id"])
    action = find_action(case["case_id"], "VERIFY_CAPTURE_LINKED")
    response = update_action(case["case_id"], action, "COMPLETED", "The payment capture was checked and linked.")
    assert response.status_code == 422
    assert "existing PayPal flow verifies and links" in response.json()["detail"]
    assert db.get_order("ORD-1003")["paypal_capture_id"] is None


def test_test_only_note_cannot_complete_action(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    run_investigation(case["case_id"])
    action = find_action(case["case_id"], "VERIFY_CAPTURE_LINKED")
    response = update_action(case["case_id"], action, "COMPLETED", "Test only: fake data used for checking.")
    assert response.status_code == 422
    assert "test or placeholder" in response.json()["detail"]


def test_action_progress_is_persisted_and_audited_without_payment_change(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    run_investigation(case["case_id"])
    action = find_action(case["case_id"], "VERIFY_CAPTURE_LINKED")
    before = db.get_order("ORD-1003")
    response = update_action(case["case_id"], action, "IN_PROGRESS", "Started checking the PayPal capture link.")
    assert response.status_code == 200, response.text
    updated = find_action(case["case_id"], "VERIFY_CAPTURE_LINKED")
    assert updated["status"] == "IN_PROGRESS"
    detail = client.get(f"/cases/{case['case_id']}").json()
    assert any(e["action"] == "FOLLOW_UP_ACTION_UPDATED" for e in detail["audit_log"])
    assert db.get_order("ORD-1003")["paypal_capture_id"] == before["paypal_capture_id"]
    assert db.get_order("ORD-1003")["refund_status"] == before["refund_status"]


def test_evidence_task_requires_latest_record_to_be_verified(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    run_investigation(case["case_id"])
    action = find_action(case["case_id"], "COLLECT_VERIFY_EVIDENCE")
    response = update_action(case["case_id"], action, "COMPLETED", "I collected carrier evidence for this delivery issue.")
    assert response.status_code == 422
    assert "newest evidence must be reviewed as VERIFIED" in response.json()["detail"]


def test_action_plan_unblocks_source_review_after_real_structured_evidence_is_reviewed(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    run_investigation(case["case_id"])
    add_reviewable_evidence(case["case_id"])
    refresh_plan(case["case_id"])

    evidence_action = find_action(case["case_id"], "COLLECT_VERIFY_EVIDENCE")
    assert evidence_action["status"] == "AUTO_COMPLETED"
    source_action = find_action(case["case_id"], "REVIEW_OFFICIAL_TRACKING_SOURCE")
    assert source_action["status"] == "OPEN"

    response = update_action(
        case["case_id"],
        source_action,
        "COMPLETED",
        "Checked official carrier tracking page; latest timestamped checkpoint shows in transit on 2026-10-12 08:30 UTC.",
    )
    assert response.status_code == 200, response.text
    assert find_action(case["case_id"], "REVIEW_OFFICIAL_TRACKING_SOURCE")["status"] == "COMPLETED"
    assert find_action(case["case_id"], "RE_EVALUATE_POLICY")["status"] == "BLOCKED"  # capture still missing


def test_re_evaluation_action_is_completed_only_after_successful_policy_endpoint(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    run_investigation(case["case_id"])
    add_reviewable_evidence(case["case_id"])
    # Simulate a capture which was already linked by the existing verified capture-link flow.
    db.link_capture("ORD-1003", "CAPTURE-VERIFIED-BY-TEST")
    refresh_plan(case["case_id"])
    source_action = find_action(case["case_id"], "REVIEW_OFFICIAL_TRACKING_SOURCE")
    assert source_action["status"] == "OPEN"
    source_complete = update_action(
        case["case_id"], source_action, "COMPLETED",
        "Checked official carrier tracking page; latest timestamped checkpoint shows in transit on 2026-10-12 08:30 UTC.",
    )
    assert source_complete.status_code == 200, source_complete.text
    refresh_plan(case["case_id"])

    reevaluate_action = find_action(case["case_id"], "RE_EVALUATE_POLICY")
    assert reevaluate_action["status"] == "OPEN"
    before_refund = db.get_case(case["case_id"]).get("paypal_refund_id")
    result = client.post(f"/cases/{case['case_id']}/reevaluate", headers=ADMIN)
    assert result.status_code == 200, result.text
    assert find_action(case["case_id"], "RE_EVALUATE_POLICY")["status"] == "COMPLETED"
    assert db.get_case(case["case_id"]).get("paypal_refund_id") == before_refund
    assert db.get_order("ORD-1003")["refund_status"] == "NONE"
    assert find_action(case["case_id"], "REVIEW_UPDATED_FINDINGS")["status"] == "OPEN"


def test_follow_up_action_unknown_id_is_not_found(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    response = client.post(
        f"/cases/{case['case_id']}/follow-up-actions/FU-999999/update",
        headers=ADMIN,
        json={"status": "COMPLETED", "note": "I checked the official source and recorded the result."},
    )
    assert response.status_code == 404
