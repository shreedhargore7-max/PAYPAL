"""Offline tests for merchant verification/rejection of case evidence.

No network, PayPal, or LLM calls are made by these tests.
Run from backend: python -m pytest -v tests/test_evidence_verification.py
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import Mock

TEST_DB = Path(__file__).resolve().parent / "_resolveos_verification_test.sqlite3"
os.environ["RESOLVEOS_DB_PATH"] = str(TEST_DB)
os.environ["ADMIN_API_KEY"] = "test-admin-key-not-a-real-secret"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ.setdefault("PAYPAL_CLIENT_ID", "test-sandbox-client-id")
os.environ.setdefault("PAYPAL_CLIENT_SECRET", "test-sandbox-client-secret")

import pytest
from fastapi.testclient import TestClient

from app import db
import app.main as main_module

client = TestClient(main_module.app)
ADMIN = {"X-Admin-Key": os.environ["ADMIN_API_KEY"]}


def fake_item_not_received(_message: str, _order: dict[str, Any]) -> dict[str, Any]:
    return {
        "issue_type": "ITEM_NOT_RECEIVED",
        "summary": "Test complaint about an item not received.",
        "requested_outcome": "resolution",
        "confidence": 0.95,
    }


@pytest.fixture(autouse=True)
def reset_test_database():
    with db.connect() as conn:
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM case_evidence")
        conn.execute("DELETE FROM cases")
        conn.execute("DELETE FROM orders")
    db.init_db()
    yield


def make_case_with_evidence(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], dict[str, Any]]:
    monkeypatch.setattr(main_module, "classify_complaint", fake_item_not_received)
    response = client.post(
        "/cases",
        json={
            "order_id": "ORD-1003",
            "customer_message": "My webcam has not arrived and tracking says in transit.",
        },
    )
    assert response.status_code == 200, response.text
    case = response.json()
    submitted = client.post(
        f"/cases/{case['case_id']}/evidence",
        headers=ADMIN,
        json={
            "tracking_number": "TRACK-123",
            "latest_carrier_scan": "Arrived at regional hub; 2026-10-12 08:30 UTC",
            "expected_delivery_date": "2026-10-14",
            "source_reference": "https://tracking.carrier.test/track/TRACK-123",
        },
    )
    assert submitted.status_code == 200, submitted.text
    return case, submitted.json()["evidence_record"]


def test_review_requires_merchant_key(monkeypatch: pytest.MonkeyPatch):
    case, record = make_case_with_evidence(monkeypatch)
    response = client.post(
        f"/cases/{case['case_id']}/evidence/{record['evidence_id']}/review",
        json={"decision": "VERIFIED", "review_note": "Checked the provided carrier details."},
    )
    assert response.status_code == 401


def test_verified_evidence_records_reviewer_and_audit_without_changing_case_or_payment(monkeypatch: pytest.MonkeyPatch):
    case, record = make_case_with_evidence(monkeypatch)
    refund_mock = Mock(side_effect=AssertionError("evidence review must never call a refund"))
    monkeypatch.setattr(main_module, "refund_capture", refund_mock)
    before_case = db.get_case(case["case_id"])
    before_order = db.get_order("ORD-1003")

    response = client.post(
        f"/cases/{case['case_id']}/evidence/{record['evidence_id']}/review",
        headers=ADMIN,
        json={"decision": "VERIFIED", "review_note": "Compared the timestamp and estimate against the carrier source."},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["verification_status"] == "VERIFIED"
    assert result["reviewed_by"] == "merchant_admin"
    assert "No payment action was taken" in result["message"]

    detail = client.get(f"/cases/{case['case_id']}").json()
    reviewed = next(r for r in detail["evidence_records"] if r["evidence_id"] == record["evidence_id"])
    assert reviewed["verification_status"] == "VERIFIED"
    assert reviewed["reviewed_by"] == "merchant_admin"
    assert reviewed["review_note"] == "Compared the timestamp and estimate against the carrier source."
    assert reviewed["reviewed_at"]
    audit = [event for event in detail["audit_log"] if event["action"] == "EVIDENCE_REVIEWED"]
    assert len(audit) == 1
    assert audit[0]["details"]["verification_status"] == "VERIFIED"
    after_case = db.get_case(case["case_id"])
    after_order = db.get_order("ORD-1003")
    assert after_case["policy_decision"] == before_case["policy_decision"]
    assert after_case["status"] == before_case["status"]
    assert after_order["refund_status"] == before_order["refund_status"]
    assert after_order["paypal_capture_id"] == before_order["paypal_capture_id"]
    assert refund_mock.call_count == 0


def test_rejected_evidence_is_persisted_and_audited(monkeypatch: pytest.MonkeyPatch):
    case, record = make_case_with_evidence(monkeypatch)
    response = client.post(
        f"/cases/{case['case_id']}/evidence/{record['evidence_id']}/review",
        headers=ADMIN,
        json={"decision": "REJECTED", "review_note": "Tracking details could not be corroborated."},
    )
    assert response.status_code == 200, response.text
    detail = client.get(f"/cases/{case['case_id']}").json()
    reviewed = next(r for r in detail["evidence_records"] if r["evidence_id"] == record["evidence_id"])
    assert reviewed["verification_status"] == "REJECTED"
    assert reviewed["review_note"] == "Tracking details could not be corroborated."
    assert any(event["action"] == "EVIDENCE_REVIEWED" for event in detail["audit_log"])


def test_review_note_must_contain_non_whitespace_text(monkeypatch: pytest.MonkeyPatch):
    case, record = make_case_with_evidence(monkeypatch)
    response = client.post(
        f"/cases/{case['case_id']}/evidence/{record['evidence_id']}/review",
        headers=ADMIN,
        json={"decision": "VERIFIED", "review_note": "     "},
    )
    assert response.status_code == 422
    assert db.get_case_evidence(case["case_id"], record["evidence_id"])["verification_status"] == "SUBMITTED_UNVERIFIED"


def test_reviewed_evidence_cannot_be_reviewed_a_second_time(monkeypatch: pytest.MonkeyPatch):
    case, record = make_case_with_evidence(monkeypatch)
    path = f"/cases/{case['case_id']}/evidence/{record['evidence_id']}/review"
    first = client.post(path, headers=ADMIN, json={"decision": "VERIFIED", "review_note": "Compared shipment details against the carrier source records."})
    assert first.status_code == 200
    second = client.post(path, headers=ADMIN, json={"decision": "REJECTED", "review_note": "Changing my decision now."})
    assert second.status_code == 409
    saved = db.get_case_evidence(case["case_id"], record["evidence_id"])
    assert saved["verification_status"] == "VERIFIED"
    assert saved["review_note"] == "Compared shipment details against the carrier source records."



def test_verified_evidence_requires_https_source_reference(monkeypatch: pytest.MonkeyPatch):
    case, record = make_case_with_evidence(monkeypatch)
    evidence = record["evidence"].copy()
    evidence.pop("source_reference", None)
    with db.connect() as conn:
        conn.execute("UPDATE case_evidence SET evidence_json = ? WHERE id = ?", (__import__("json").dumps(evidence), int(record["evidence_id"].removeprefix("EV-"))))
    response = client.post(
        f"/cases/{case['case_id']}/evidence/{record['evidence_id']}/review",
        headers=ADMIN,
        json={"decision": "VERIFIED", "review_note": "Checked the available carrier information."},
    )
    assert response.status_code == 422
    assert "HTTPS link" in response.json()["detail"]
    assert db.get_case_evidence(case["case_id"], record["evidence_id"])["verification_status"] == "SUBMITTED_UNVERIFIED"


def test_missing_delivery_verification_requires_timestamp_and_valid_date(monkeypatch: pytest.MonkeyPatch):
    case, record = make_case_with_evidence(monkeypatch)
    evidence = record["evidence"].copy()
    evidence["latest_carrier_scan"] = "fvdfvav"
    evidence["expected_delivery_date"] = "fvfvfv"
    with db.connect() as conn:
        conn.execute("UPDATE case_evidence SET evidence_json = ? WHERE id = ?", (__import__("json").dumps(evidence), int(record["evidence_id"].removeprefix("EV-"))))
    response = client.post(
        f"/cases/{case['case_id']}/evidence/{record['evidence_id']}/review",
        headers=ADMIN,
        json={"decision": "VERIFIED", "review_note": "Compared the source and entered details."},
    )
    assert response.status_code == 422
    assert "timestamp" in response.json()["detail"]
    assert db.get_case_evidence(case["case_id"], record["evidence_id"])["verification_status"] == "SUBMITTED_UNVERIFIED"

def test_verification_is_blocked_when_review_note_admits_unverified_test_data(monkeypatch: pytest.MonkeyPatch):
    case, record = make_case_with_evidence(monkeypatch)
    response = client.post(
        f"/cases/{case['case_id']}/evidence/{record['evidence_id']}/review",
        headers=ADMIN,
        json={"decision": "VERIFIED", "review_note": "Test only: no verifiable carrier evidence was attached."},
    )
    assert response.status_code == 422
    assert "test, placeholder, or unverified" in response.json()["detail"]
    saved = db.get_case_evidence(case["case_id"], record["evidence_id"])
    assert saved["verification_status"] == "SUBMITTED_UNVERIFIED"
    detail = client.get(f"/cases/{case['case_id']}").json()
    assert any(event["action"] == "EVIDENCE_VERIFICATION_BLOCKED" for event in detail["audit_log"])


def test_unknown_evidence_id_is_rejected(monkeypatch: pytest.MonkeyPatch):
    case, _ = make_case_with_evidence(monkeypatch)
    response = client.post(
        f"/cases/{case['case_id']}/evidence/EV-999999/review",
        headers=ADMIN,
        json={"decision": "VERIFIED", "review_note": "Checked this evidence reference."},
    )
    assert response.status_code == 404


def test_review_is_locked_after_refund_submission(monkeypatch: pytest.MonkeyPatch):
    case, record = make_case_with_evidence(monkeypatch)
    db.update_case(case["case_id"], status="REFUND_SUBMITTING")
    response = client.post(
        f"/cases/{case['case_id']}/evidence/{record['evidence_id']}/review",
        headers=ADMIN,
        json={"decision": "VERIFIED", "review_note": "Checked after refund started."},
    )
    assert response.status_code == 409
    assert db.get_case_evidence(case["case_id"], record["evidence_id"])["verification_status"] == "SUBMITTED_UNVERIFIED"
