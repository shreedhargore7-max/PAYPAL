"""Offline tests for read-only AfterShip tracking lookup and evidence comparison.

No real carrier API, PayPal, or LLM calls are made by these tests.
Run from backend: python -m pytest -v tests/test_carrier_tracking.py
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import Mock

TEST_DB = Path(__file__).resolve().parent / "_resolveos_test.sqlite3"
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
import app.carrier_tracking as carrier_tracking

client = TestClient(main_module.app)
ADMIN = {"X-Admin-Key": os.environ["ADMIN_API_KEY"]}


def fake_item_not_received(_message: str, _order: dict[str, Any]) -> dict[str, Any]:
    return {
        "issue_type": "ITEM_NOT_RECEIVED",
        "summary": "Test complaint about a missing parcel.",
        "requested_outcome": "resolution",
        "confidence": 0.95,
    }


@pytest.fixture(autouse=True)
def reset_test_database():
    with db.connect() as conn:
        conn.execute("DELETE FROM carrier_tracking_checks")
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM case_evidence")
        conn.execute("DELETE FROM cases")
        conn.execute("DELETE FROM orders")
    db.init_db()
    yield


def make_case_and_evidence(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], dict[str, Any]]:
    monkeypatch.setattr(main_module, "classify_complaint", fake_item_not_received)
    case_response = client.post(
        "/cases",
        json={
            "order_id": "ORD-1003",
            "customer_message": "My webcam has not arrived and tracking says in transit.",
        },
    )
    assert case_response.status_code == 200, case_response.text
    case = case_response.json()
    evidence_response = client.post(
        f"/cases/{case['case_id']}/evidence",
        headers=ADMIN,
        json={
            "tracking_number": "TRACK-12345",
            "carrier_slug": "ups",
            "latest_carrier_scan": "Arrived at sorting facility; 2026-10-12 08:30 UTC",
            "expected_delivery_date": "2026-10-14",
            "source_reference": "https://www.ups.com/track?tracknum=TRACK-12345",
            "evidence_notes": "Fixture data for offline tracking tests only.",
        },
    )
    assert evidence_response.status_code == 200, evidence_response.text
    return case, evidence_response.json()["evidence_record"]


def provider_payload(tracking_number: str = "TRACK-12345") -> dict[str, Any]:
    return {
        "provider": "AfterShip",
        "tracking_number": tracking_number,
        "carrier_slug": "ups",
        "carrier_name": "UPS",
        "provider_tracking_id": "provider-tracking-id",
        "tracking_status": "InTransit",
        "status_message": "Your package is moving through the network.",
        "estimated_delivery_date": "2026-10-14",
        "courier_tracking_link": "https://www.ups.com/track?tracknum=TRACK-12345",
        "latest_checkpoint": {
            "checkpoint_time": "2026-10-12T08:30:00Z",
            "message": "Arrived at sorting facility",
            "tag": "InTransit",
            "location": "Test city",
            "slug": "ups",
        },
        "lookup_state": "EXISTING_TRACKING",
        "carrier_has_checkpoints": True,
    }


def test_tracking_refresh_requires_merchant_key(monkeypatch: pytest.MonkeyPatch):
    case, evidence = make_case_and_evidence(monkeypatch)
    monkeypatch.setenv("AFTERSHIP_API_KEY", "non-secret-test-key")
    response = client.post(
        f"/cases/{case['case_id']}/evidence/{evidence['evidence_id']}/tracking-refresh"
    )
    assert response.status_code == 401


def test_live_aftership_mode_requires_api_key(monkeypatch: pytest.MonkeyPatch):
    case, evidence = make_case_and_evidence(monkeypatch)
    monkeypatch.setenv("CARRIER_TRACKING_MODE", "aftership")
    monkeypatch.delenv("AFTERSHIP_API_KEY", raising=False)
    response = client.post(
        f"/cases/{case['case_id']}/evidence/{evidence['evidence_id']}/tracking-refresh",
        headers=ADMIN,
    )
    assert response.status_code == 503
    assert "AFTERSHIP_API_KEY" in response.json()["detail"]


def test_tracking_refresh_persists_snapshot_and_comparison_without_changing_order_or_policy(monkeypatch: pytest.MonkeyPatch):
    case, evidence = make_case_and_evidence(monkeypatch)
    monkeypatch.setenv("CARRIER_TRACKING_MODE", "aftership")
    monkeypatch.setenv("AFTERSHIP_API_KEY", "offline-test-key")
    monkeypatch.setattr(main_module, "lookup_tracking", lambda key, number, slug=None: provider_payload(number))
    before_order = db.get_order("ORD-1003")
    before_case = db.get_case(case["case_id"])

    response = client.post(
        f"/cases/{case['case_id']}/evidence/{evidence['evidence_id']}/tracking-refresh",
        headers=ADMIN,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["tracking_check"]["provider"] == "AfterShip"
    assert result["tracking_check"]["comparison"]["shipping_status_comparison"] == "MATCH"
    assert result["tracking_check"]["comparison"]["delivery_date_comparison"] == "MATCH"
    assert result["tracking_check"]["comparison"]["human_review_required"] is True
    assert "no shipping state or payment action was changed" in result["message"].lower()

    detail = client.get(f"/cases/{case['case_id']}").json()
    assert len(detail["carrier_tracking_checks"]) == 1
    assert detail["carrier_tracking_checks"][0]["tracking_number"] == "TRACK-12345"
    assert any(event["action"] == "CARRIER_TRACKING_LOOKED_UP" for event in detail["audit_log"])
    after_order = db.get_order("ORD-1003")
    after_case = db.get_case(case["case_id"])
    assert after_order["shipping_status"] == before_order["shipping_status"] == "IN_TRANSIT"
    assert after_order["refund_status"] == before_order["refund_status"]
    assert after_order["paypal_capture_id"] == before_order["paypal_capture_id"]
    assert after_case["policy_decision"] == before_case["policy_decision"]
    assert after_case["status"] == before_case["status"]


def test_tracking_number_mismatch_does_not_save_snapshot(monkeypatch: pytest.MonkeyPatch):
    case, evidence = make_case_and_evidence(monkeypatch)
    monkeypatch.setenv("CARRIER_TRACKING_MODE", "aftership")
    monkeypatch.setenv("AFTERSHIP_API_KEY", "offline-test-key")
    monkeypatch.setattr(main_module, "lookup_tracking", lambda key, number, slug=None: provider_payload("SOME-OTHER-TRACKING"))
    response = client.post(
        f"/cases/{case['case_id']}/evidence/{evidence['evidence_id']}/tracking-refresh",
        headers=ADMIN,
    )
    assert response.status_code == 409
    detail = client.get(f"/cases/{case['case_id']}").json()
    assert detail["carrier_tracking_checks"] == []
    assert any(event["action"] == "CARRIER_TRACKING_NUMBER_MISMATCH" for event in detail["audit_log"])


def test_tracking_refresh_is_locked_after_refund_submission(monkeypatch: pytest.MonkeyPatch):
    case, evidence = make_case_and_evidence(monkeypatch)
    monkeypatch.setenv("CARRIER_TRACKING_MODE", "aftership")
    monkeypatch.setenv("AFTERSHIP_API_KEY", "offline-test-key")
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET status = 'REFUND_PENDING', refund_status = 'PENDING' WHERE case_id = ?",
            (case["case_id"],),
        )
    provider_mock = Mock(side_effect=AssertionError("tracking must not be queried once a refund is pending"))
    monkeypatch.setattr(main_module, "lookup_tracking", provider_mock)
    response = client.post(
        f"/cases/{case['case_id']}/evidence/{evidence['evidence_id']}/tracking-refresh",
        headers=ADMIN,
    )
    assert response.status_code == 409
    assert provider_mock.call_count == 0


def test_demo_tracking_works_without_paid_api_key_and_never_calls_aftership(monkeypatch: pytest.MonkeyPatch):
    case, evidence = make_case_and_evidence(monkeypatch)
    monkeypatch.setenv("CARRIER_TRACKING_MODE", "demo")
    monkeypatch.delenv("AFTERSHIP_API_KEY", raising=False)
    provider_mock = Mock(side_effect=AssertionError("demo mode must not call AfterShip"))
    monkeypatch.setattr(main_module, "lookup_tracking", provider_mock)
    before_order = db.get_order("ORD-1003")
    before_case = db.get_case(case["case_id"])

    response = client.post(
        f"/cases/{case['case_id']}/evidence/{evidence['evidence_id']}/tracking-refresh",
        headers=ADMIN,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    snapshot = result["tracking_check"]
    assert snapshot["source_mode"] == "SIMULATED_DEMO"
    assert "SIMULATED" in snapshot["provider"]
    assert "NOT from a real carrier" in result["message"]
    assert snapshot["tracking_status"] == "InTransit"
    assert snapshot["comparison"]["shipping_status_comparison"] == "MATCH"
    assert snapshot["comparison"]["human_review_required"] is True
    provider_mock.assert_not_called()

    detail = client.get(f"/cases/{case['case_id']}").json()
    assert len(detail["carrier_tracking_checks"]) == 1
    assert any(event["action"] == "CARRIER_TRACKING_SIMULATED" for event in detail["audit_log"])
    assert db.get_order("ORD-1003")["shipping_status"] == before_order["shipping_status"] == "IN_TRANSIT"
    assert db.get_order("ORD-1003")["refund_status"] == before_order["refund_status"]
    assert db.get_case(case["case_id"])["policy_decision"] == before_case["policy_decision"]
    assert db.get_case(case["case_id"])["status"] == before_case["status"]


def test_demo_mismatch_scenario_is_labeled_and_requires_human_review(monkeypatch: pytest.MonkeyPatch):
    case, _ = make_case_and_evidence(monkeypatch)
    monkeypatch.setenv("CARRIER_TRACKING_MODE", "demo")
    evidence_response = client.post(
        f"/cases/{case['case_id']}/evidence",
        headers=ADMIN,
        json={
            "tracking_number": "DEMO-MISMATCH-12345",
            "latest_carrier_scan": "SIMULATED test scan; not real evidence",
            "expected_delivery_date": "2026-10-14",
            "evidence_notes": "Demo comparison only; not carrier proof.",
        },
    )
    assert evidence_response.status_code == 200, evidence_response.text
    evidence = evidence_response.json()["evidence_record"]

    response = client.post(
        f"/cases/{case['case_id']}/evidence/{evidence['evidence_id']}/tracking-refresh",
        headers=ADMIN,
    )
    assert response.status_code == 200, response.text
    snapshot = response.json()["tracking_check"]
    assert snapshot["source_mode"] == "SIMULATED_DEMO"
    assert snapshot["tracking_status"] == "Delivered"
    assert snapshot["comparison"]["shipping_status_comparison"] == "DIFFERENT_REVIEW_REQUIRED"
    assert snapshot["comparison"]["delivery_date_comparison"] == "DIFFERENT_REVIEW_REQUIRED"
    assert snapshot["comparison"]["human_review_required"] is True
    assert "SIMULATED" in snapshot["demo_disclaimer"]


def test_demo_mismatch_persists_human_readable_comparison_findings(monkeypatch: pytest.MonkeyPatch):
    case, _ = make_case_and_evidence(monkeypatch)
    monkeypatch.setenv("CARRIER_TRACKING_MODE", "demo")
    evidence_response = client.post(
        f"/cases/{case['case_id']}/evidence",
        headers=ADMIN,
        json={
            "tracking_number": "DEMO-MISMATCH-001",
            "latest_carrier_scan": "DEMO ONLY — not a real scan",
            "expected_delivery_date": "2026-10-20",
            "evidence_notes": "Demo comparison test only.",
        },
    )
    assert evidence_response.status_code == 200, evidence_response.text
    evidence = evidence_response.json()["evidence_record"]
    refresh = client.post(
        f"/cases/{case['case_id']}/evidence/{evidence['evidence_id']}/tracking-refresh",
        headers=ADMIN,
    )
    assert refresh.status_code == 200, refresh.text
    snapshot = refresh.json()["tracking_check"]
    comparison = snapshot["comparison"]
    assert comparison["overall_result"] == "SIMULATED_DISCREPANCY"
    assert "not a real carrier event" in comparison["overall_summary"].lower()
    assert any("status differs" in item.lower() for item in comparison["findings"])
    assert any("dates differ" in item.lower() for item in comparison["findings"])
    assert comparison["payment_action_taken"] is False

    detail = client.get(f"/cases/{case['case_id']}").json()
    saved = next(item for item in detail["carrier_tracking_checks"] if item["evidence_id"] == evidence["evidence_id"])
    assert saved["comparison"]["overall_result"] == "SIMULATED_DISCREPANCY"
    assert detail["status"] == case["status"]
    assert detail["policy_decision"] == case["policy_decision"]
    assert db.get_order("ORD-1003")["shipping_status"] == "IN_TRANSIT"


def test_adapter_uses_aftership_header_and_reuses_exact_matching_tracker(monkeypatch: pytest.MonkeyPatch):
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "meta": {"code": 200},
        "data": {"trackings": [{
            "id": "provider-id",
            "tracking_number": "TRACK-12345",
            "slug": "ups",
            "tag": "InTransit",
            "subtag_message": "Moving through network",
            "courier": {"name": "UPS"},
            "courier_tracking_link": "https://www.ups.com/track?tracknum=TRACK-12345",
            "checkpoints": [{
                "checkpoint_time": "2026-10-12T08:30:00Z",
                "message": "Arrived at sorting facility",
                "tag": "InTransit",
                "location": "Test city",
            }],
        }]},
    }
    request_mock = Mock(return_value=response)
    monkeypatch.setattr(carrier_tracking.requests, "request", request_mock)

    result = carrier_tracking.lookup_tracking("offline-test-key", "TRACK-12345", "ups")

    assert result["provider"] == "AfterShip"
    assert result["tracking_status"] == "InTransit"
    assert result["latest_checkpoint"]["message"] == "Arrived at sorting facility"
    request_mock.assert_called_once()
    args, kwargs = request_mock.call_args
    assert args[0] == "GET"
    assert args[1].endswith("/trackings")
    assert kwargs["headers"]["as-api-key"] == "offline-test-key"
    assert kwargs["params"]["tracking_numbers"] == "TRACK-12345"


def test_adapter_creates_tracking_when_no_exact_match_exists(monkeypatch: pytest.MonkeyPatch):
    get_response = Mock()
    get_response.status_code = 200
    get_response.json.return_value = {"meta": {"code": 200}, "data": {"trackings": []}}
    post_response = Mock()
    post_response.status_code = 200
    post_response.json.return_value = {
        "meta": {"code": 200},
        "data": {"tracking": {
            "id": "new-provider-id",
            "tracking_number": "TRACK-12345",
            "slug": "ups",
            "tag": "Pending",
            "courier": {"name": "UPS"},
            "checkpoints": [],
        }},
    }
    request_mock = Mock(side_effect=[get_response, post_response])
    monkeypatch.setattr(carrier_tracking.requests, "request", request_mock)

    result = carrier_tracking.lookup_tracking("offline-test-key", "TRACK-12345", "ups")

    assert result["lookup_state"] == "CREATED_WAITING_FOR_UPDATES"
    assert result["tracking_number"] == "TRACK-12345"
    assert request_mock.call_count == 2
    assert request_mock.call_args_list[1].args[0] == "POST"
    assert request_mock.call_args_list[1].kwargs["json"] == {"tracking_number": "TRACK-12345", "slug": "ups"}
