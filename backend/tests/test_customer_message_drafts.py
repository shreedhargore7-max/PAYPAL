"""Offline tests for the customer follow-up message composer.

No email, SMS, external API, PayPal, or live AI calls are made.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

TEST_DB = Path(__file__).resolve().parent / "_resolveos_customer_message_drafts_test.sqlite3"
os.environ["RESOLVEOS_DB_PATH"] = str(TEST_DB)
os.environ["ADMIN_API_KEY"] = "test-admin-key-not-a-real-secret"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["AFTERSHIP_API_KEY"] = ""
os.environ["CARRIER_TRACKING_MODE"] = "demo"
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
    db.init_db()
    with db.connect() as conn:
        conn.execute("DELETE FROM case_customer_message_drafts")
        conn.execute("DELETE FROM case_follow_up_actions")
        conn.execute("DELETE FROM carrier_tracking_checks")
        conn.execute("DELETE FROM case_evidence")
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM cases")
        conn.execute("DELETE FROM orders")
    db.init_db()
    yield


def make_case(monkeypatch: pytest.MonkeyPatch, issue_message: str = "My webcam has not arrived") -> dict[str, Any]:
    monkeypatch.setattr(main_module, "classify_complaint", fake_item_not_received)
    response = client.post("/cases", json={"order_id": "ORD-1003", "customer_message": issue_message})
    assert response.status_code == 200, response.text
    return response.json()


def create_draft(case_id: str):
    return client.post(f"/cases/{case_id}/customer-message-drafts", headers=ADMIN)


def test_generation_requires_merchant_authorization(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    response = client.post(f"/cases/{case['case_id']}/customer-message-drafts")
    assert response.status_code == 401


def test_missing_delivery_draft_is_saved_and_not_sent(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    before_case = db.get_case(case["case_id"])
    before_order = db.get_order("ORD-1003")
    response = create_draft(case["case_id"])
    assert response.status_code == 200, response.text
    draft = response.json()["draft"]
    assert draft["draft_id"].startswith("MSG-")
    assert draft["status"] == "DRAFT"
    assert "confirm the delivery address" in draft["body"].lower()
    assert "not been sent" in response.json()["message"].lower()
    saved = client.get(f"/cases/{case['case_id']}").json()
    assert len(saved["customer_message_drafts"]) == 1
    assert saved["customer_message_drafts"][0]["draft_id"] == draft["draft_id"]
    assert any(event["action"] == "CUSTOMER_MESSAGE_DRAFT_CREATED" for event in saved["audit_log"])
    assert db.get_case(case["case_id"])["policy_decision"] == before_case["policy_decision"]
    assert db.get_order("ORD-1003")["paypal_capture_id"] == before_order["paypal_capture_id"]
    assert db.get_order("ORD-1003")["refund_status"] == before_order["refund_status"]


def test_draft_avoids_promising_refund_or_claiming_tracking_is_verified(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    response = create_draft(case["case_id"])
    body = response.json()["draft"]["body"].lower()
    assert "no final decision has been made yet" in body
    assert "delivered" not in body
    assert "refund has been approved" not in body


def test_editing_draft_persists_and_records_audit_without_sending(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    draft = create_draft(case["case_id"]).json()["draft"]
    response = client.put(
        f"/cases/{case['case_id']}/customer-message-drafts/{draft['draft_id']}",
        headers=ADMIN,
        json={"subject": "Update about your order", "body": "We are reviewing your report. We will follow up when we have an update."},
    )
    assert response.status_code == 200, response.text
    assert response.json()["draft"]["status"] == "DRAFT"
    detail = client.get(f"/cases/{case['case_id']}").json()
    assert detail["customer_message_drafts"][0]["subject"] == "Update about your order"
    assert any(event["action"] == "CUSTOMER_MESSAGE_DRAFT_EDITED" for event in detail["audit_log"])
    assert "not been sent" in response.json()["message"].lower()


def test_invalid_draft_edits_are_rejected(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    draft = create_draft(case["case_id"]).json()["draft"]
    response = client.put(
        f"/cases/{case['case_id']}/customer-message-drafts/{draft['draft_id']}",
        headers=ADMIN,
        json={"subject": "Hi", "body": "Short"},
    )
    assert response.status_code == 422


def test_unknown_case_or_draft_is_rejected(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    assert client.post("/cases/CASE-UNKNOWN/customer-message-drafts", headers=ADMIN).status_code == 404
    response = client.put(
        f"/cases/{case['case_id']}/customer-message-drafts/MSG-999999",
        headers=ADMIN,
        json={"subject": "Subject ok", "body": "This is long enough for validation."},
    )
    assert response.status_code == 404


def test_drafts_are_case_scoped(monkeypatch: pytest.MonkeyPatch):
    first = make_case(monkeypatch)
    draft = create_draft(first["case_id"]).json()["draft"]
    assert db.get_case_customer_message_draft(first["case_id"], draft["draft_id"]) is not None
    assert db.get_case_customer_message_draft("CASE-OTHER", draft["draft_id"]) is None
