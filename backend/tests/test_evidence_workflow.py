"""Offline tests for evidence capture and deterministic case re-evaluation.

Run from backend with: python -m pytest -v tests/test_evidence_workflow.py
No network, PayPal, or LLM calls are made by these tests.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

TEST_DB = Path(__file__).resolve().parent / "_resolveos_test.sqlite3"
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


def test_evidence_submission_requires_merchant_key(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    response = client.post(
        f"/cases/{case['case_id']}/evidence",
        json={"tracking_number": "TRACK-123", "latest_carrier_scan": "In transit"},
    )
    assert response.status_code == 401


def test_evidence_is_saved_unverified_and_visible_in_case(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    response = client.post(
        f"/cases/{case['case_id']}/evidence",
        headers=ADMIN,
        json={
            "tracking_number": "TRACK-123",
            "latest_carrier_scan": "Arrived at regional hub; 2026-10-12 08:30 UTC",
            "expected_delivery_date": "2026-10-14",
            "delivery_address_confirmed": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["evidence_record"]["verification_status"] == "SUBMITTED_UNVERIFIED"
    assert body["evidence_record"]["evidence"]["tracking_number"] == "TRACK-123"

    saved = client.get(f"/cases/{case['case_id']}")
    assert saved.status_code == 200
    detail = saved.json()
    assert len(detail["evidence_records"]) == 1
    assert detail["evidence_records"][0]["verification_status"] == "SUBMITTED_UNVERIFIED"
    assert any(event["action"] == "EVIDENCE_SUBMITTED" for event in detail["audit_log"])
    assert "Verify the submitted tracking number" in " ".join(detail["evidence_investigation"]["checklist"])
    assert "not independently verified" in detail["evidence_investigation"]["explanation"]


def test_empty_evidence_submission_is_rejected(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    response = client.post(
        f"/cases/{case['case_id']}/evidence",
        headers=ADMIN,
        json={"tracking_number": "  ", "evidence_notes": ""},
    )
    assert response.status_code == 422
    assert client.get(f"/cases/{case['case_id']}").json()["evidence_records"] == []


def test_reevaluation_uses_current_order_state_and_never_calls_refund(monkeypatch: pytest.MonkeyPatch):
    from unittest.mock import Mock

    case = make_case(monkeypatch)
    refund_mock = Mock(side_effect=AssertionError("reevaluation must not call PayPal refund"))
    monkeypatch.setattr(main_module, "refund_capture", refund_mock)

    first = client.post(f"/cases/{case['case_id']}/reevaluate", headers=ADMIN)
    assert first.status_code == 200, first.text
    assert first.json()["policy_decision"] == "NEEDS_MORE_INFORMATION"

    with db.connect() as conn:
        conn.execute("UPDATE orders SET shipping_status = 'LOST' WHERE order_id = 'ORD-1003'")

    second = client.post(f"/cases/{case['case_id']}/reevaluate", headers=ADMIN)
    assert second.status_code == 200, second.text
    result = second.json()
    assert result["policy_decision"] == "AWAITING_CAPTURE_LINK"
    assert result["status"] == "AWAITING_CAPTURE_LINK"
    assert any(event["action"] == "CASE_REEVALUATED" for event in result["audit_log"])
    assert refund_mock.call_count == 0


def test_reevaluation_cannot_reset_submitted_refund_state(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    db.update_case(case["case_id"], status="REFUND_SUBMITTING")
    response = client.post(f"/cases/{case['case_id']}/reevaluate", headers=ADMIN)
    assert response.status_code == 409
