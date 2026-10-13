"""Regression tests for tracking snapshot association with evidence records."""
from __future__ import annotations

from typing import Any

from app.resolution_closure import build_resolution_summary, select_tracking_snapshot_for_current_evidence


def make_evidence(evidence_id: str, tracking_number: str, status: str, **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "evidence_id": evidence_id,
        "case_id": "CASE-TEST12345",
        "verification_status": status,
        "created_at": "2026-10-12T12:00:00+00:00",
        "submitted_by": "merchant_admin",
        "review_note": extra.pop("review_note", None),
        "evidence": {
            "tracking_number": tracking_number,
            "latest_carrier_scan": "SIMULATED ONLY test scan",
            "expected_delivery_date": "2026-10-20",
            "source_reference": "https://example.com/test-evidence",
            "evidence_notes": "SIMULATED ONLY test record",
        },
    }
    record.update(extra)
    return record


def make_tracking(
    check_id: str,
    evidence_id: str,
    tracking_number: str,
    *,
    source_mode: str = "SIMULATED_DEMO",
    status: str = "DELIVERED",
    status_comparison: str = "DIFFERENT_REVIEW_REQUIRED",
    date_comparison: str = "DIFFERENT_REVIEW_REQUIRED",
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "case_id": "CASE-TEST12345",
        "evidence_id": evidence_id,
        "tracking_number": tracking_number,
        "provider": "ResolveOS Demo (SIMULATED)" if source_mode == "SIMULATED_DEMO" else "AfterShip",
        "source_mode": source_mode,
        "tracking_status": status,
        "estimated_delivery_date": "2026-10-21",
        "comparison": {
            "overall_result": "SIMULATED_DISCREPANCY" if source_mode == "SIMULATED_DEMO" else "DIFFERENCE_REVIEW_REQUIRED",
            "shipping_status_comparison": status_comparison,
            "delivery_date_comparison": date_comparison,
        },
        "checked_at": "2026-10-12T12:00:00+00:00",
    }


def make_summary(evidence: list[dict[str, Any]], tracking: list[dict[str, Any]]) -> dict[str, Any]:
    return build_resolution_summary(
        case={
            "case_id": "CASE-TEST12345",
            "order_id": "ORD-TEST1001",
            "issue_type": "ITEM_NOT_RECEIVED",
            "customer_message": "The package has not arrived.",
            "summary": "Missing delivery report.",
            "case_status": "NEEDS_MORE_INFORMATION",
            "status": "NEEDS_MORE_INFORMATION",
            "policy_decision": "NEEDS_MORE_INFORMATION",
            "reasons": ["Evidence is needed."],
            "confidence": 0.95,
        },
        order={
            "order_id": "ORD-TEST1001",
            "item_name": "Test item",
            "amount": "59.50",
            "currency": "USD",
            "shipping_status": "IN_TRANSIT",
            "payment_status": "COMPLETED",
            "paypal_capture_id": None,
            "refund_status": "NONE",
        },
        evidence_records=evidence,
        tracking_checks=tracking,
        follow_up_actions=[],
        message_drafts=[],
        audit_log=[],
        generated_at="2026-10-12T12:00:00+00:00",
    )


def test_summary_selects_snapshot_matched_to_current_evidence_not_newer_revoked_record():
    current = make_evidence("EV-000006", "DEMO-MISMATCH-001", "SUBMITTED_UNVERIFIED")
    revoked = make_evidence(
        "EV-000001", "fvadfva", "VERIFICATION_REVOKED",
        verification_revocation_note="Historical test-only verification revoked.",
    )
    old_snapshot = make_tracking("TRK-000002", "EV-000001", "fvadfva", status="InTransit",
                                 status_comparison="MATCH", date_comparison="NOT_AVAILABLE")
    current_snapshot = make_tracking("TRK-000001", "EV-000006", "DEMO-MISMATCH-001")

    summary = make_summary([current, revoked], [old_snapshot, current_snapshot])

    tracking = summary["tracking"]
    assert tracking["latest_check_id"] == "TRK-000001"
    assert tracking["associated_evidence_id"] == "EV-000006"
    assert tracking["current_evidence_id"] == "EV-000006"
    assert tracking["latest_tracking_number"] == "DEMO-MISMATCH-001"
    assert tracking["comparison_outcome"] == "SIMULATED_DISCREPANCY"
    assert tracking["association_status"] == "MATCHED_TO_CURRENT_EVIDENCE"
    assert tracking["excluded_snapshot_count"] == 1
    assert tracking["excluded_snapshots"][0]["check_id"] == "TRK-000002"
    assert tracking["excluded_snapshots"][0]["reason"] == "LINKED_EVIDENCE_REVOKED"
    assert any("matched to EV-000006" in finding for finding in summary["findings"])
    assert any("1 tracking snapshot(s) were excluded" in finding for finding in summary["findings"])
    assert all("TRK-000002" not in finding for finding in summary["findings"] if "Latest tracking result" in finding)


def test_tracking_snapshot_with_mismatched_number_is_not_selected():
    current = make_evidence("EV-000006", "DEMO-MISMATCH-001", "SUBMITTED_UNVERIFIED")
    wrong_snapshot = make_tracking("TRK-000009", "EV-000006", "UNRELATED-TRACK-999")

    selected, target_evidence, excluded = select_tracking_snapshot_for_current_evidence(
        [current], [wrong_snapshot]
    )

    assert selected is None
    assert target_evidence is current
    assert excluded == [{
        "check_id": "TRK-000009",
        "evidence_id": "EV-000006",
        "reason": "TRACKING_NUMBER_MISMATCH",
    }]
    summary = make_summary([current], [wrong_snapshot])
    assert summary["tracking"]["latest_check_id"] is None
    assert summary["tracking"]["association_status"] == "NO_MATCHING_SNAPSHOT_FOR_CURRENT_EVIDENCE"
    assert summary["tracking"]["comparison_outcome"] == "NOT_RECORDED"


def test_snapshot_for_rejected_evidence_is_excluded_from_active_summary():
    rejected = make_evidence("EV-000006", "DEMO-MISMATCH-001", "REJECTED")
    current = make_evidence("EV-000005", "CURRENT-TRACK-123", "SUBMITTED_UNVERIFIED")
    stale = make_tracking("TRK-000010", "EV-000006", "DEMO-MISMATCH-001")

    selected, target_evidence, excluded = select_tracking_snapshot_for_current_evidence(
        [rejected, current], [stale]
    )

    assert target_evidence is current
    assert selected is None
    assert excluded[0]["reason"] == "LINKED_EVIDENCE_REJECTED"


def test_latest_evidence_without_a_matching_snapshot_does_not_fall_back_to_old_evidence():
    current = make_evidence("EV-000006", "DEMO-MISMATCH-001", "SUBMITTED_UNVERIFIED")
    old = make_evidence("EV-000005", "OLD-TRACK-123", "SUBMITTED_UNVERIFIED")
    old_snapshot = make_tracking("TRK-000009", "EV-000005", "OLD-TRACK-123")

    selected, target_evidence, excluded = select_tracking_snapshot_for_current_evidence(
        [current, old], [old_snapshot]
    )

    assert target_evidence is current
    assert selected is None
    assert excluded[0]["reason"] == "NOT_ASSOCIATED_WITH_CURRENT_EVIDENCE"
    summary = make_summary([current, old], [old_snapshot])
    assert summary["tracking"]["latest_check_id"] is None
    assert summary["tracking"]["current_evidence_id"] == "EV-000006"


def test_summary_reports_no_matching_snapshot_when_all_saved_tracking_is_for_revoked_evidence():
    current = make_evidence("EV-000006", "DEMO-MISMATCH-001", "SUBMITTED_UNVERIFIED")
    revoked = make_evidence("EV-000001", "fvadfva", "VERIFICATION_REVOKED")
    stale = make_tracking("TRK-000002", "EV-000001", "fvadfva")

    summary = make_summary([current, revoked], [stale])

    assert summary["tracking"]["latest_check_id"] is None
    assert summary["tracking"]["excluded_snapshot_count"] == 1
    assert summary["tracking"]["excluded_snapshots"][0]["reason"] == "LINKED_EVIDENCE_REVOKED"
    assert any("none is matched to the current eligible evidence record EV-000006" in f for f in summary["findings"])
    assert summary["closure_readiness"]["status"] == "NOT_READY_TO_CLOSE"


def test_historically_contradictory_verified_evidence_cannot_anchor_tracking_snapshot():
    suspect_verified = make_evidence(
        "EV-000006", "SUSPECT-TRACK-123", "VERIFIED",
        review_note="Test only: no verifiable evidence was attached.",
    )
    newer_eligible = make_evidence("EV-000005", "CURRENT-TRACK-123", "SUBMITTED_UNVERIFIED")
    suspect_snapshot = make_tracking("TRK-000020", "EV-000006", "SUSPECT-TRACK-123")
    active_snapshot = make_tracking("TRK-000019", "EV-000005", "CURRENT-TRACK-123")

    selected, target_evidence, excluded = select_tracking_snapshot_for_current_evidence(
        [suspect_verified, newer_eligible], [suspect_snapshot, active_snapshot]
    )

    assert target_evidence is newer_eligible
    assert selected is active_snapshot
    assert {item["reason"] for item in excluded} == {"LINKED_EVIDENCE_INTEGRITY_WARNING"}
