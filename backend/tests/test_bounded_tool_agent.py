"""Tests for the bounded read-only tool-routing layer."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.investigation_tools import (
    MAX_TOOL_CALLS,
    READ_ONLY_TOOL_REGISTRY,
    execute_read_only_tool_plan,
)


class FakeDB:
    def __init__(self, evidence: list[dict[str, Any]], tracking: list[dict[str, Any]]):
        self.evidence = evidence
        self.tracking = tracking
        self.evidence_reads = 0
        self.tracking_reads = 0

    def list_case_evidence(self, case_id: str):
        self.evidence_reads += 1
        return self.evidence

    def list_case_carrier_tracking_checks(self, case_id: str):
        self.tracking_reads += 1
        return self.tracking


def fake_policy(classification: dict[str, Any], order: dict[str, Any]):
    return "NEEDS_MORE_INFORMATION", ["Demo policy preview only."]


def sample_case(issue_type: str = "OTHER"):
    return {
        "case_id": "CASE-TEST1234",
        "order_id": "ORD-TEST1",
        "issue_type": issue_type,
        "status": "NEEDS_MORE_INFORMATION",
        "policy_decision": "NEEDS_MORE_INFORMATION",
        "summary": "Test complaint",
        "customer_message": "Test only",
    }


def sample_order():
    return {
        "order_id": "ORD-TEST1",
        "item_name": "Test item",
        "amount": "10.00",
        "currency": "USD",
        "shipping_status": "IN_TRANSIT",
        "payment_status": "COMPLETED",
        "paypal_capture_id": None,
        "refund_status": "NONE",
    }


def test_tool_execution_is_bounded_and_uses_only_static_registry():
    fake_db = FakeDB(evidence=[], tracking=[])
    result = execute_read_only_tool_plan(
        sample_case(), sample_order(), db_module=fake_db, evaluate_policy=fake_policy
    )
    executed = result["tools_performed"]
    assert 1 <= len(executed) <= MAX_TOOL_CALLS
    assert set(executed).issubset(set(READ_ONLY_TOOL_REGISTRY))
    assert result["tool_call_count"] == len(executed)
    assert result["max_tool_calls"] == MAX_TOOL_CALLS
    assert executed[-1] == "preview_deterministic_policy"


def test_tracking_tool_is_skipped_for_unrelated_issue_without_tracking_number():
    fake_db = FakeDB(evidence=[], tracking=[])
    result = execute_read_only_tool_plan(
        sample_case("WRONG_ITEM"), sample_order(), db_module=fake_db, evaluate_policy=fake_policy
    )
    assert "read_tracking_comparisons" not in result["tools_performed"]
    assert fake_db.tracking_reads == 0
    assert any(item["event"] == "TOOL_SKIPPED" and item["tool"] == "read_tracking_comparisons" for item in result["planner_trace"])


def test_tracking_tool_is_selected_when_latest_evidence_has_tracking_number():
    fake_db = FakeDB(
        evidence=[{
            "evidence_id": "EV-TEST1",
            "verification_status": "SUBMITTED_UNVERIFIED",
            "evidence": {"tracking_number": "DEMO-TRACK-01"},
        }],
        tracking=[{
            "check_id": "TRK-TEST1",
            "provider": "ResolveOS Simulated Demo",
            "source_mode": "SIMULATED_DEMO",
            "comparison": {"overall_result": "SIMULATED_DISCREPANCY"},
        }],
    )
    result = execute_read_only_tool_plan(
        sample_case("WRONG_ITEM"), sample_order(), db_module=fake_db, evaluate_policy=fake_policy
    )
    assert "read_tracking_comparisons" in result["tools_performed"]
    assert fake_db.tracking_reads == 1
    tracking_step = next(step for step in result["steps"] if step["tool"] == "read_tracking_comparisons")
    assert tracking_step["status"] == "SIMULATED_ONLY"
    assert "not real shipment evidence" in tracking_step["result"].lower()


def test_delivery_complaint_adapts_plan_to_inspect_tracking_even_if_number_missing():
    fake_db = FakeDB(evidence=[], tracking=[])
    result = execute_read_only_tool_plan(
        sample_case("ITEM_NOT_RECEIVED"), sample_order(), db_module=fake_db, evaluate_policy=fake_policy
    )
    assert "read_tracking_comparisons" in result["tools_performed"]
    assert fake_db.tracking_reads == 1
    assert result["tool_call_count"] <= MAX_TOOL_CALLS


def test_legacy_comparison_without_overall_result_derives_discrepancy_and_explains_source():
    fake_db = FakeDB(
        evidence=[{
            "evidence_id": "EV-TEST2",
            "verification_status": "SUBMITTED_UNVERIFIED",
            "evidence": {"tracking_number": "DEMO-MISMATCH-001"},
        }],
        tracking=[{
            "check_id": "TRK-TEST2",
            "provider": "ResolveOS Demo (SIMULATED)",
            "source_mode": "SIMULATED_DEMO",
            "comparison": {
                "shipping_status_comparison": "DIFFERENT_REVIEW_REQUIRED",
                "delivery_date_comparison": "DIFFERENT_REVIEW_REQUIRED",
            },
        }],
    )
    result = execute_read_only_tool_plan(
        sample_case("ITEM_NOT_RECEIVED"), sample_order(), db_module=fake_db, evaluate_policy=fake_policy
    )
    track = result["results"]["read_tracking_comparisons"]
    assert track["comparison_outcome"] == "SIMULATED_DISCREPANCY"
    assert track["comparison_outcome_origin"] == "DERIVED_FROM_SAVED_FIELDS"
    assert "comparison outcome is SIMULATED_DISCREPANCY" in next(
        step["result"] for step in result["steps"] if step["tool"] == "read_tracking_comparisons"
    )
    assert any(
        item["event"] == "COMPARISON_DIFFERENCE_FOUND" and "not real shipment" in item["reason"].lower()
        for item in result["planner_trace"]
    )


def test_adaptive_planner_skips_tracking_when_refund_is_already_pending():
    case = sample_case("ITEM_NOT_RECEIVED")
    case["status"] = "REFUND_PENDING"
    case["refund_status"] = "PENDING"
    fake_db = FakeDB(evidence=[{
        "evidence_id": "EV-TEST3",
        "verification_status": "SUBMITTED_UNVERIFIED",
        "evidence": {"tracking_number": "DEMO-TRACK-01"},
    }], tracking=[])
    result = execute_read_only_tool_plan(
        case, sample_order(), db_module=fake_db, evaluate_policy=fake_policy
    )
    assert "read_tracking_comparisons" not in result["tools_performed"]
    assert fake_db.tracking_reads == 0
    skip = next(item for item in result["planner_trace"] if item["event"] == "TOOL_SKIPPED")
    assert skip["tool"] == "read_tracking_comparisons"
    assert "refund is pending" in skip["reason"].lower()
    assert result["tools_performed"][-1] == "preview_deterministic_policy"


def test_planner_records_why_policy_preview_follows_current_observations():
    fake_db = FakeDB(evidence=[], tracking=[])
    result = execute_read_only_tool_plan(
        sample_case("WRONG_ITEM"), sample_order(), db_module=fake_db, evaluate_policy=fake_policy
    )
    preview = next(item for item in result["planner_trace"] if item["tool"] == "preview_deterministic_policy")
    assert "payment status=COMPLETED" in preview["reason"]
    assert "capture_linked=False" in preview["reason"]
    assert "latest_evidence=NONE" in preview["reason"]
    assert result["planning_summary"]["tracking_number_available"] is False


def test_comparison_outcome_normalizer_supports_legacy_top_level_fields():
    from app.investigation_tools import resolve_comparison_outcome

    result = resolve_comparison_outcome({
        "provider": "AfterShip",
        "source_mode": "LIVE_AFTERSHIP",
        "shipping_status_comparison": "MATCH",
        "delivery_date_comparison": "DIFFERENT_REVIEW_REQUIRED",
    })
    assert result["overall_result"] == "POTENTIAL_DISCREPANCY"
    assert result["outcome_origin"] == "DERIVED_FROM_SAVED_FIELDS"
    assert result["is_simulated"] is False
