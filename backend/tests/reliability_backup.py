"""Offline reliability tests for ResolveOS.

These tests use a dedicated SQLite database and mock every PayPal/LLM network
call. They will not contact PayPal or spend real/sandbox money.
Run from the backend directory with: python -m pytest -v tests
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Configure an isolated database and safe test credentials BEFORE importing app.main.
TEST_DB = Path(__file__).resolve().parent / "_resolveos_test.sqlite3"
os.environ["RESOLVEOS_DB_PATH"] = str(TEST_DB)
os.environ["ADMIN_API_KEY"] = "test-admin-key-not-a-real-secret"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["PAYPAL_CLIENT_ID"] = "test-sandbox-client-id"
os.environ["PAYPAL_CLIENT_SECRET"] = "test-sandbox-client-secret"

import pytest
from fastapi.testclient import TestClient

from app import db
import app.main as main_module


client = TestClient(main_module.app)
TEST_ADMIN_HEADER = {"X-Admin-Key": os.environ["ADMIN_API_KEY"]}


def fake_item_not_received(message: str, order: dict[str, Any]) -> dict[str, Any]:
    """Deterministic classifier used so tests never need OpenRouter access."""
    return {
        "issue_type": "ITEM_NOT_RECEIVED",
        "summary": f"Test classification for {order['order_id']}",
        "requested_outcome": "refund_or_resolution",
        "confidence": 0.90,
    }


def valid_capture(capture_id: str = "CAPTURE-TEST-1001") -> dict[str, Any]:
    return {
        "id": capture_id,
        "status": "COMPLETED",
        "amount": {"value": "89.99", "currency_code": "USD"},
    }


def setup_linked_order(monkeypatch: pytest.MonkeyPatch) -> str:
    capture_id = "CAPTURE-TEST-1001"
    db.link_capture("ORD-1001", capture_id)
    monkeypatch.setattr(main_module, "get_capture", lambda _capture_id: valid_capture(capture_id))
    monkeypatch.setattr(main_module, "classify_complaint", fake_item_not_received)
    return capture_id


def make_case(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/cases",
        json={
            "order_id": "ORD-1001",
            "customer_message": "My headphones were never shipped; please refund me.",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture(autouse=True)
def reset_test_database():
    """Clear and reseed only this test suite's private database before each test."""
    with db.connect() as conn:
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM cases")
        conn.execute("DELETE FROM orders")
    db.init_db()
    yield


def test_health_endpoint_is_available():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["mode"] == "paypal-sandbox-only"


def test_demo_orders_are_seeded():
    response = client.get("/orders")
    assert response.status_code == 200
    ids = {order["order_id"] for order in response.json()}
    assert ids == {"ORD-1001", "ORD-1002", "ORD-1003"}


def test_unknown_order_cannot_create_case():
    response = client.post(
        "/cases",
        json={"order_id": "ORD-DOES-NOT-EXIST", "customer_message": "My order never arrived."},
    )
    assert response.status_code == 404


def test_missing_capture_blocks_refund_candidate():
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(main_module, "classify_complaint", fake_item_not_received)
        response = client.post(
            "/cases",
            json={"order_id": "ORD-1001", "customer_message": "My headphones were never shipped."},
        )
    finally:
        monkeypatch.undo()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["policy_decision"] == "AWAITING_CAPTURE_LINK"
    assert body["status"] == "AWAITING_CAPTURE_LINK"
    assert body["paypal_refund_id"] is None


def test_delivered_item_requires_human_review():
    from unittest.mock import patch

    with patch.object(main_module, "classify_complaint", side_effect=fake_item_not_received):
        response = client.post(
            "/cases",
            json={"order_id": "ORD-1002", "customer_message": "My keyboard never arrived."},
        )
    assert response.status_code == 200, response.text
    assert response.json()["policy_decision"] == "NEEDS_HUMAN_REVIEW"
    assert response.json()["status"] == "NEEDS_HUMAN_REVIEW"


def test_admin_endpoint_rejects_missing_or_invalid_key():
    missing = client.post("/cases/CASE-NOT-FOUND/approve-refund")
    assert missing.status_code == 401

    invalid = client.post(
        "/cases/CASE-NOT-FOUND/approve-refund",
        headers={"X-Admin-Key": "not-the-test-admin-key"},
    )
    assert invalid.status_code == 401


def test_successful_refund_updates_case_order_and_audit(monkeypatch: pytest.MonkeyPatch):
    from unittest.mock import Mock

    capture_id = setup_linked_order(monkeypatch)
    case = make_case(client)
    assert case["policy_decision"] == "REFUND_CANDIDATE"
    assert case["status"] == "AWAITING_MERCHANT_APPROVAL"

    refund_response = {"id": "REFUND-TEST-1001", "status": "COMPLETED"}
    refund_mock = Mock(return_value=refund_response)
    monkeypatch.setattr(main_module, "refund_capture", refund_mock)

    response = client.post(
        f"/cases/{case['case_id']}/approve-refund",
        headers=TEST_ADMIN_HEADER,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "REFUND_COMPLETED"
    assert response.json()["refund_id"] == "REFUND-TEST-1001"
    assert refund_mock.call_count == 1
    assert refund_mock.call_args.args[0] == capture_id

    saved_case = client.get(f"/cases/{case['case_id']}").json()
    assert saved_case["status"] == "REFUND_COMPLETED"
    assert saved_case["refund_status"] == "COMPLETED"
    assert saved_case["paypal_refund_id"] == "REFUND-TEST-1001"
    assert [event["action"] for event in saved_case["audit_log"]][-2:] == [
        "MERCHANT_APPROVED_REFUND",
        "PAYPAL_REFUND_RESPONSE",
    ]

    saved_order = next(order for order in client.get("/orders").json() if order["order_id"] == "ORD-1001")
    assert saved_order["paypal_capture_id"] == capture_id
    assert saved_order["refund_status"] == "REFUNDED"


def test_repeat_approval_after_completion_does_not_call_paypal_again(monkeypatch: pytest.MonkeyPatch):
    from unittest.mock import Mock

    setup_linked_order(monkeypatch)
    case = make_case(client)
    refund_mock = Mock(return_value={"id": "REFUND-TEST-REPEAT", "status": "COMPLETED"})
    monkeypatch.setattr(main_module, "refund_capture", refund_mock)

    first = client.post(f"/cases/{case['case_id']}/approve-refund", headers=TEST_ADMIN_HEADER)
    second = client.post(f"/cases/{case['case_id']}/approve-refund", headers=TEST_ADMIN_HEADER)

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert refund_mock.call_count == 1


def test_capture_amount_mismatch_blocks_refund(monkeypatch: pytest.MonkeyPatch):
    from unittest.mock import Mock

    setup_linked_order(monkeypatch)
    case = make_case(client)
    monkeypatch.setattr(
        main_module,
        "get_capture",
        lambda _capture_id: {
            "id": "CAPTURE-TEST-1001",
            "status": "COMPLETED",
            "amount": {"value": "1.00", "currency_code": "USD"},
        },
    )
    refund_mock = Mock(return_value={"id": "SHOULD-NOT-HAPPEN", "status": "COMPLETED"})
    monkeypatch.setattr(main_module, "refund_capture", refund_mock)

    response = client.post(f"/cases/{case['case_id']}/approve-refund", headers=TEST_ADMIN_HEADER)
    assert response.status_code == 409
    assert "amount/currency" in response.json()["detail"]
    assert refund_mock.call_count == 0


def test_paypal_error_moves_case_to_reconciliation_and_does_not_blindly_retry(monkeypatch: pytest.MonkeyPatch):
    from unittest.mock import Mock

    from app.paypal_client import PayPalError

    setup_linked_order(monkeypatch)
    case = make_case(client)
    refund_mock = Mock(side_effect=PayPalError("simulated timeout in offline test"))
    monkeypatch.setattr(main_module, "refund_capture", refund_mock)

    first = client.post(f"/cases/{case['case_id']}/approve-refund", headers=TEST_ADMIN_HEADER)
    assert first.status_code == 502, first.text

    saved_case = client.get(f"/cases/{case['case_id']}").json()
    assert saved_case["status"] == "REFUND_REQUIRES_RECONCILIATION"
    assert db.get_case(case["case_id"])["paypal_request_id"] == f"resolveos-{case['case_id'].lower()}"

    second = client.post(f"/cases/{case['case_id']}/approve-refund", headers=TEST_ADMIN_HEADER)
    assert second.status_code == 409, second.text
    assert refund_mock.call_count == 1


def test_concurrent_approvals_submit_only_one_refund(monkeypatch: pytest.MonkeyPatch):
    """Two requests that both read the eligible state must not both call PayPal."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from unittest.mock import Mock

    capture_id = setup_linked_order(monkeypatch)
    case = make_case(client)

    # Force both requests to pass the initial status check and reach capture
    # validation before either can claim the refund submission.
    both_read_case = Barrier(2, timeout=8)

    def synchronized_capture(_capture_id: str) -> dict[str, Any]:
        both_read_case.wait()
        return valid_capture(capture_id)

    monkeypatch.setattr(main_module, "get_capture", synchronized_capture)
    refund_mock = Mock(return_value={"id": "REFUND-CONCURRENT-1", "status": "COMPLETED"})
    monkeypatch.setattr(main_module, "refund_capture", refund_mock)
    url = f"/cases/{case['case_id']}/approve-refund"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, url, headers=TEST_ADMIN_HEADER)
        second = pool.submit(client.post, url, headers=TEST_ADMIN_HEADER)
        responses = [first.result(timeout=15), second.result(timeout=15)]

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert refund_mock.call_count == 1
    assert db.get_case(case["case_id"])["status"] == "REFUND_COMPLETED"
