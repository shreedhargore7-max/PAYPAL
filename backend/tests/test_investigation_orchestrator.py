"""Offline tests for the ResolveOS Investigation Orchestrator.

These tests do not call OpenRouter, any carrier API, or PayPal.
Run from backend: python -m pytest -v tests/test_investigation_orchestrator.py
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

TEST_DB = Path(__file__).resolve().parent / "_resolveos_orchestrator_test.sqlite3"
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
        "summary": "Test complaint about an undelivered parcel.",
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


def add_evidence(case_id: str) -> dict[str, Any]:
    response = client.post(
        f"/cases/{case_id}/evidence",
        headers=ADMIN,
        json={
            "tracking_number": "DEMO-MISMATCH-001",
            "latest_carrier_scan": "DEMO ONLY — no real carrier scan",
            "expected_delivery_date": "2026-10-20",
            "delivery_address_confirmed": False,
            "carrier_investigation": "Demo scenario only",
            "source_reference": "https://example.com/test-evidence",
            "evidence_notes": "TEST ONLY — no real evidence attached",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["evidence_record"]


def test_orchestrator_requires_merchant_key(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    response = client.post(f"/cases/{case['case_id']}/investigation/run")
    assert response.status_code == 401


def test_orchestrator_returns_auditable_plan_and_no_payment_action(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    before_case = db.get_case(case["case_id"])
    before_order = db.get_order("ORD-1003")

    response = client.post(f"/cases/{case['case_id']}/investigation/run", headers=ADMIN)
    assert response.status_code == 200, response.text
    result = response.json()["investigation_run"]
    assert result["agent_name"] == "ResolveOS Investigation Orchestrator"
    assert result["policy_preview"]["decision"] == "NEEDS_MORE_INFORMATION"
    assert any(step["tool"] == "read_evidence_history" for step in result["steps"])
    assert any("No PayPal capture is linked" in item for item in result["observations"])
    assert "link a completed paypal capture" in result["recommended_next_action"].lower()
    assert all("refund" not in step.get("tool", "").lower() for step in result["steps"])

    after_case = db.get_case(case["case_id"])
    after_order = db.get_order("ORD-1003")
    assert after_case["status"] == before_case["status"]
    assert after_case["policy_decision"] == before_case["policy_decision"]
    assert after_order["shipping_status"] == before_order["shipping_status"]
    assert after_order["paypal_capture_id"] == before_order["paypal_capture_id"]
    assert after_order["refund_status"] == before_order["refund_status"]

    detail = client.get(f"/cases/{case['case_id']}").json()
    event = next(item for item in reversed(detail["audit_log"]) if item["action"] == "INVESTIGATION_ORCHESTRATED")
    assert event["details"]["run_id"] == result["run_id"]
    assert event["details"]["result"]["recommended_next_action"] == result["recommended_next_action"]


def test_orchestrator_flags_unverified_evidence_and_simulated_tracking(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    evidence = add_evidence(case["case_id"])
    db.create_carrier_tracking_check(
        case["case_id"],
        evidence["evidence_id"],
        "DEMO-MISMATCH-001",
        {
            "provider": "ResolveOS Simulated Demo",
            "source_mode": "SIMULATED_DEMO",
            "tracking_status": "Delivered",
            "estimated_delivery_date": "2026-10-21",
            "comparison": {
                "overall_result": "SIMULATED_DISCREPANCY",
                "shipping_status_comparison": "DIFFERENT_REVIEW_REQUIRED",
                "delivery_date_comparison": "DIFFERENT_REVIEW_REQUIRED",
                "findings": ["SIMULATED DATA ONLY", "Status and date differ"],
                "human_review_required": True,
                "payment_action_taken": False,
            },
        },
    )

    response = client.post(f"/cases/{case['case_id']}/investigation/run", headers=ADMIN)
    assert response.status_code == 200, response.text
    result = response.json()["investigation_run"]
    tracking_step = next(step for step in result["steps"] if step["tool"] == "read_tracking_comparisons")
    assert tracking_step["status"] == "SIMULATED_ONLY"
    assert any("simulated demo data" in item.lower() for item in result["observations"])
    assert any("status difference" in item.lower() for item in result["observations"])
    assert any("delivery-date difference" in item.lower() for item in result["observations"])
    assert "open the official carrier source" in result["recommended_next_action"].lower()


def test_orchestrator_never_executes_payment_and_flags_refund_lock(monkeypatch: pytest.MonkeyPatch):
    case = make_case(monkeypatch)
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET status = 'REFUND_PENDING', refund_status = 'PENDING' WHERE case_id = ?",
            (case["case_id"],),
        )
        conn.execute("UPDATE orders SET refund_status = 'PENDING' WHERE order_id = ?", ("ORD-1003",))

    before_order = db.get_order("ORD-1003")
    response = client.post(f"/cases/{case['case_id']}/investigation/run", headers=ADMIN)
    assert response.status_code == 200, response.text
    result = response.json()["investigation_run"]
    assert "stop further payment actions" in result["recommended_next_action"].lower()
    assert any("No refund is executed" in item for item in result["guardrails"])
    assert db.get_order("ORD-1003")["refund_status"] == before_order["refund_status"] == "PENDING"
    assert db.get_case(case["case_id"])["status"] == "REFUND_PENDING"


def test_orchestrator_returns_404_for_unknown_case():
    response = client.post("/cases/CASE-DOES-NOT-EXIST/investigation/run", headers=ADMIN)
    assert response.status_code == 404


def test_dashboard_contains_orchestrator_controls():
    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Investigation Orchestrator" in dashboard.text
    assert "Run investigation agent" in dashboard.text
    assert "/investigation/run" in dashboard.text
