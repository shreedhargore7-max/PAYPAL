"""Deterministic follow-up action plans for ResolveOS investigations.

This module turns persisted findings into a small merchant-facing checklist. It
never contacts external services, changes case policy, or initiates payments.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urlparse

PROTECTED_REFUND_STATUSES = {
    "REFUND_SUBMITTING", "REFUND_PENDING", "REFUND_COMPLETED",
    "REFUND_REQUIRES_RECONCILIATION",
}


def _is_https_source(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip() or any(ch.isspace() for ch in value.strip()):
        return False
    try:
        parsed = urlparse(value.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme.lower() != "https" or not host or parsed.username or parsed.password:
            return False
        if host in {"example.com", "www.example.com", "example.org", "example.net", "invalid.test", "localhost"}:
            return False
        if host.endswith(".example.com") or host.endswith(".invalid") or host.endswith(".test"):
            return False
        return True
    except ValueError:
        return False


def _placeholder(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = " ".join(value.lower().split())
    tokens = (
        "test only", "test data", "demo only", "simulated", "placeholder", "dummy data",
        "fake data", "no real carrier", "not real", "not actual evidence", "no verifiable",
        "not verified", "not attached", "example.com/test", "example.com",
    )
    return any(token in text for token in tokens)


def evidence_is_usable_for_follow_up(case: dict[str, Any], record: dict[str, Any] | None) -> bool:
    """Check recorded merchant review and input shape, not independent authenticity."""
    if not isinstance(record, dict) or str(record.get("verification_status") or "").upper() != "VERIFIED":
        return False
    evidence = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
    if not _is_https_source(evidence.get("source_reference")):
        return False
    if any(_placeholder(value) for value in evidence.values() if isinstance(value, str)):
        return False
    if str(case.get("issue_type") or "").upper() == "ITEM_NOT_RECEIVED":
        tracking_number = evidence.get("tracking_number")
        scan = evidence.get("latest_carrier_scan")
        expected_date = evidence.get("expected_delivery_date")
        if not isinstance(tracking_number, str) or len(tracking_number.strip()) < 5:
            return False
        if not isinstance(scan, str) or not re.search(
            r"(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s+\d{1,2}:\d{2})", scan
        ):
            return False
        if not isinstance(expected_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expected_date.strip()):
            return False
        try:
            date.fromisoformat(expected_date.strip())
        except ValueError:
            return False
    return True


def _is_simulated(check: dict[str, Any] | None) -> bool:
    if not isinstance(check, dict):
        return False
    mode = str(check.get("source_mode") or "").upper()
    provider = str(check.get("provider") or "").upper()
    return mode == "SIMULATED_DEMO" or "SIMULATED" in provider


def _has_difference(check: dict[str, Any] | None) -> bool:
    if not isinstance(check, dict):
        return False
    comparison = check.get("comparison") if isinstance(check.get("comparison"), dict) else check
    return any(
        str(comparison.get(key) or "").upper() == "DIFFERENT_REVIEW_REQUIRED"
        for key in ("shipping_status_comparison", "delivery_date_comparison")
    )


def _action_recently_completed(existing: dict[str, dict[str, Any]], key: str, latest_evidence: dict[str, Any] | None,
                               latest_tracking: dict[str, Any] | None) -> bool:
    action = existing.get(key) or {}
    if str(action.get("status") or "").upper() not in {"COMPLETED", "AUTO_COMPLETED"}:
        return False
    completed_at = str(action.get("completed_at") or "")
    if not completed_at:
        return False
    for item, stamp_key in ((latest_evidence, "created_at"), (latest_tracking, "checked_at")):
        stamp = str((item or {}).get(stamp_key) or "")
        if stamp and stamp > completed_at:
            return False
    return True


def build_follow_up_action_plan(
    case: dict[str, Any],
    order: dict[str, Any],
    evidence_records: list[dict[str, Any]],
    tracking_checks: list[dict[str, Any]],
    existing_actions: list[dict[str, Any]],
    *,
    run_id: str,
) -> dict[str, Any]:
    """Create an ordered checklist and gate the next step on verified prerequisites."""
    existing = {str(item.get("action_key")): item for item in existing_actions if item.get("action_key")}
    latest_evidence = evidence_records[0] if evidence_records else None
    latest_evidence_payload = (latest_evidence or {}).get("evidence") if isinstance((latest_evidence or {}).get("evidence"), dict) else {}
    latest_tracking = tracking_checks[0] if tracking_checks else None
    issue = str(case.get("issue_type") or "OTHER").upper()
    case_status = str(case.get("status") or "UNKNOWN").upper()
    refund_status = str(case.get("refund_status") or order.get("refund_status") or "NONE").upper()
    protected = case_status in PROTECTED_REFUND_STATUSES or refund_status in {"PENDING", "COMPLETED"} or bool(case.get("paypal_refund_id"))

    if protected:
        action = {
            "action_key": "REFUND_RECONCILIATION",
            "title": "Inspect the existing refund lifecycle — do not submit another refund",
            "description": "Check the saved PayPal refund ID and current status. If its state is ambiguous, reconcile it through the existing merchant workflow; never retry blindly.",
            "assigned_to": "MERCHANT",
            "priority": "CRITICAL",
            "status": "OPEN",
            "completion_gate": "MANUAL_ACKNOWLEDGEMENT",
            "blocked_reason": None,
            "sort_order": 1,
        }
        return {"run_id": run_id, "summary": "Refund state is protected; the plan is limited to reconciliation and must not initiate another payment action.", "actions": [action], "satisfied_gates": set()}

    verified_evidence = evidence_is_usable_for_follow_up(case, latest_evidence)
    capture_linked = str(order.get("payment_status") or "").upper() == "COMPLETED" and bool(str(order.get("paypal_capture_id") or "").strip())
    latest_tracking_simulated = _is_simulated(latest_tracking)
    has_tracking_check = latest_tracking is not None
    comparison_difference = _has_difference(latest_tracking)
    specs: list[dict[str, Any]] = []
    blockers: list[str] = []

    if not capture_linked:
        specs.append({
            "action_key": "VERIFY_CAPTURE_LINKED",
            "title": "Verify and link the completed PayPal capture",
            "description": f"Payment status is {str(order.get('payment_status') or 'UNKNOWN').upper()} and a capture is {'linked' if order.get('paypal_capture_id') else 'not linked'}. Use the existing protected capture-link flow, which verifies completion and matching amount/currency.",
            "assigned_to": "MERCHANT",
            "priority": "HIGH",
            "status": "OPEN",
            "completion_gate": "CAPTURE_LINKED",
            "blocked_reason": None,
            "reopen": bool((existing.get("VERIFY_CAPTURE_LINKED") or {}).get("status") in {"COMPLETED", "AUTO_COMPLETED"}),
            "sort_order": 1,
        })
        blockers.append("VERIFY_CAPTURE_LINKED")

    if not verified_evidence:
        specs.append({
            "action_key": "COLLECT_VERIFY_EVIDENCE",
            "title": "Collect and review genuine carrier evidence",
            "description": "Save the tracking number, latest timestamped scan, valid expected delivery date, and the official HTTPS source. Open the source yourself; only mark evidence verified after checking it. ResolveOS does not authenticate the URL automatically.",
            "assigned_to": "MERCHANT",
            "priority": "HIGH",
            "status": "OPEN",
            "completion_gate": "LATEST_EVIDENCE_VERIFIED",
            "blocked_reason": None,
            "reopen": bool((existing.get("COLLECT_VERIFY_EVIDENCE") or {}).get("status") in {"COMPLETED", "AUTO_COMPLETED"}),
            "sort_order": 2,
        })
        blockers.append("COLLECT_VERIFY_EVIDENCE")

    if issue == "ITEM_NOT_RECEIVED":
        needs_source_review = (not has_tracking_check) or latest_tracking_simulated
        if needs_source_review:
            if not _action_recently_completed(existing, "REVIEW_OFFICIAL_TRACKING_SOURCE", latest_evidence, latest_tracking):
                specs.append({
                    "action_key": "REVIEW_OFFICIAL_TRACKING_SOURCE",
                    "title": "Inspect the official carrier source and record what it shows",
                    "description": "The saved tracking result is missing or simulated. Open the official carrier tracking page, compare its status and timestamp with the order, and record what you actually observed. A completion note is a merchant attestation, not independent authentication; it does not replace evidence review.",
                    "assigned_to": "MERCHANT",
                    "priority": "HIGH",
                    "status": "OPEN" if verified_evidence else "BLOCKED",
                    "completion_gate": "MANUAL_SOURCE_REVIEW",
                    "blocked_reason": "Submit and review genuine, structured evidence first." if not verified_evidence else None,
                    "reopen": bool((existing.get("REVIEW_OFFICIAL_TRACKING_SOURCE") or {}).get("status") in {"COMPLETED", "AUTO_COMPLETED"}),
                    "sort_order": 3,
                })
                blockers.append("REVIEW_OFFICIAL_TRACKING_SOURCE")
        elif comparison_difference:
            if not _action_recently_completed(existing, "REVIEW_TRACKING_DISCREPANCY", latest_evidence, latest_tracking):
                specs.append({
                    "action_key": "REVIEW_TRACKING_DISCREPANCY",
                    "title": "Review the carrier status/date discrepancy",
                    "description": "Compare the official carrier event timestamp and delivery estimate with the merchant's order data. Document which fields differ and whether they refer to the same event/date concept; do not infer a refund decision from a mismatch alone.",
                    "assigned_to": "MERCHANT",
                    "priority": "HIGH",
                    "status": "OPEN" if verified_evidence else "BLOCKED",
                    "completion_gate": "MANUAL_SOURCE_REVIEW",
                    "blocked_reason": "Submit and review genuine, structured evidence first." if not verified_evidence else None,
                    "reopen": bool((existing.get("REVIEW_TRACKING_DISCREPANCY") or {}).get("status") in {"COMPLETED", "AUTO_COMPLETED"}),
                    "sort_order": 3,
                })
                blockers.append("REVIEW_TRACKING_DISCREPANCY")

    # Mark whether re-evaluation has blockers. The existing endpoint will mark
    # this step complete only when it actually re-runs deterministic policy.
    old_re_evaluate = existing.get("RE_EVALUATE_POLICY") or {}
    re_eval_completed_at = str(old_re_evaluate.get("completed_at") or "")
    newer_inputs_after_re_eval = any(
        str(item.get(stamp_key) or "") > re_eval_completed_at
        for item, stamp_key in ((latest_evidence, "created_at"), (latest_tracking, "checked_at"))
        if isinstance(item, dict) and re_eval_completed_at
    )
    re_evaluated = str(old_re_evaluate.get("status") or "").upper() in {"COMPLETED", "AUTO_COMPLETED"} and not newer_inputs_after_re_eval
    specs.append({
        "action_key": "RE_EVALUATE_POLICY",
        "title": "Re-evaluate the case after investigation prerequisites are complete",
        "description": "Use the existing Re-evaluate case control to apply deterministic policy to the current order state. This records a new policy result but never submits a refund.",
        "assigned_to": "MERCHANT (initiates system action)",
        "priority": "MEDIUM",
        "status": "BLOCKED" if blockers else "OPEN",
        "completion_gate": "CASE_REEVALUATED",
        "blocked_reason": "Complete these prerequisite actions first: " + ", ".join(blockers) if blockers else None,
        "reopen": bool(re_evaluated is False and str(old_re_evaluate.get("status") or "").upper() in {"COMPLETED", "AUTO_COMPLETED"}),
        "sort_order": 4,
    })
    if not blockers and re_evaluated:
        old_review = existing.get("REVIEW_UPDATED_FINDINGS") or {}
        if str(old_review.get("status") or "").upper() not in {"COMPLETED", "AUTO_COMPLETED"}:
            specs.append({
                "action_key": "REVIEW_UPDATED_FINDINGS",
                "title": "Review the refreshed policy decision and record the merchant's next step",
                "description": "Review the current case, the evidence review notes, and the deterministic policy reasons. Decide whether to request more information, keep the case open, or proceed to an allowed merchant decision. This action records review only; it cannot authorize or execute a refund.",
                "assigned_to": "MERCHANT",
                "priority": "MEDIUM",
                "status": "OPEN",
                "completion_gate": "MANUAL_ACKNOWLEDGEMENT",
                "blocked_reason": None,
                "sort_order": 5,
            })

    satisfied_gates = set()
    if capture_linked:
        satisfied_gates.add("CAPTURE_LINKED")
    if verified_evidence:
        satisfied_gates.add("LATEST_EVIDENCE_VERIFIED")
    return {
        "run_id": run_id,
        "summary": (
            f"{len(blockers)} prerequisite action(s) remain before case re-evaluation can proceed."
            if blockers else "Prerequisite checks are complete; the case can be re-evaluated and then reviewed by the merchant."
        ),
        "actions": specs,
        "satisfied_gates": satisfied_gates,
        "blockers": blockers,
    }


def validate_action_completion(
    action: dict[str, Any],
    case: dict[str, Any],
    order: dict[str, Any],
    evidence_records: list[dict[str, Any]],
    tracking_checks: list[dict[str, Any]],
    note: str,
) -> str | None:
    """Return a user-facing reason when an action must not be marked complete."""
    gate = str(action.get("completion_gate") or "").upper()
    note_norm = " ".join(note.lower().split())
    if len(note.strip()) < 12:
        return "Add a meaningful completion note of at least 12 characters."
    if any(token in note_norm for token in ("test only", "placeholder", "dummy data", "fake data", "no verifiable", "not actual evidence", "demo only")):
        return "This completion note describes test or placeholder data. ResolveOS will not mark this action complete from test-only information."

    latest_evidence = evidence_records[0] if evidence_records else None
    evidence_is_verified = evidence_is_usable_for_follow_up(case, latest_evidence)
    latest_tracking = tracking_checks[0] if tracking_checks else None

    if gate == "CAPTURE_LINKED":
        if str(order.get("payment_status") or "").upper() != "COMPLETED" or not str(order.get("paypal_capture_id") or "").strip():
            return "This action can be completed only after the existing PayPal flow verifies and links a completed capture matching amount and currency."
    elif gate == "LATEST_EVIDENCE_VERIFIED":
        if not evidence_is_verified:
            return "The newest evidence must be reviewed as VERIFIED and contain valid structured fields plus a non-placeholder HTTPS source reference. Merchant source checking is still required."
    elif gate == "MANUAL_SOURCE_REVIEW":
        if not evidence_is_verified:
            return "First submit genuine carrier details and mark the latest evidence VERIFIED after manually checking the official source."
        has_source_context = any(word in note_norm for word in ("carrier", "tracking", "official source", "source page", "checkpoint", "delivery scan", "tracking page"))
        if not has_source_context:
            return "Describe what the official carrier source or tracking page showed in your completion note."
        # A simulated local snapshot can coexist with a manually completed source review.
        # The note is an auditable merchant attestation; it does not authenticate the URL
        # or turn simulated values into real carrier data.
    elif gate == "CASE_REEVALUATED":
        return "This action is completed automatically only after the Re-evaluate case endpoint successfully records a new deterministic policy result."
    elif gate == "MANUAL_ACKNOWLEDGEMENT":
        # A merchant note is a recorded attestation, never proof by itself.
        return None
    else:
        return "Unknown completion gate; this action cannot be manually completed."
    return None
