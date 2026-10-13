"""Deterministic resolution summary and explicit case-closure guardrails.

This module does not call AI, external carrier services, or payment providers.
"""
from __future__ import annotations

from typing import Any

from .evidence_integrity import evidence_integrity_counts, evidence_integrity_issues, historical_verification_problem

ACTIVE_REFUND_STATUSES = {
    "REFUND_SUBMITTING", "REFUND_PENDING", "REFUND_REQUIRES_RECONCILIATION"
}
UNRESOLVED_POLICY_DECISIONS = {
    "NEEDS_MORE_INFORMATION", "AWAITING_CAPTURE_LINK", "REFUND_CANDIDATE"
}
OPEN_ACTION_STATUSES = {"OPEN", "IN_PROGRESS"}
COMPLETED_ACTION_STATUSES = {"COMPLETED", "AUTO_COMPLETED"}
CLOSURE_OUTCOMES = {
    "RESOLVED_REFUND_COMPLETED",
    "RESOLVED_NO_REFUND",
    "ESCALATED",
    "CUSTOMER_WITHDREW",
    "DUPLICATE_CASE",
}


def _upper(value: Any, default: str = "UNKNOWN") -> str:
    return str(value or default).strip().upper()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def select_tracking_snapshot_for_current_evidence(
    evidence_records: list[dict[str, Any]],
    tracking_checks: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, str]]]:
    """Select a tracking snapshot linked to the newest eligible evidence record.

    `tracking_checks` is expected newest-first, as returned by the database. A
    snapshot is eligible only if its linked evidence still exists, has not been
    rejected/revoked (and is not a historically contradictory VERIFIED record),
    and its tracking number exactly matches the value saved on that evidence.
    Snapshots for another evidence record are retained in history but excluded
    from the active case summary.

    Returns (selected_snapshot, current_evidence, excluded_snapshot_reasons).
    """
    evidence_by_id = {
        str(record.get("evidence_id")): record
        for record in evidence_records
        if record.get("evidence_id")
    }

    current_evidence: dict[str, Any] | None = None
    current_tracking_number = ""
    for record in evidence_records:  # newest first
        status = _upper(record.get("verification_status"), "SUBMITTED_UNVERIFIED")
        if status in {"VERIFICATION_REVOKED", "REJECTED"}:
            continue
        if status == "VERIFIED" and historical_verification_problem(record):
            continue
        evidence_payload = _as_dict(record.get("evidence"))
        tracking_number = str(evidence_payload.get("tracking_number") or "").strip()
        if tracking_number:
            current_evidence = record
            current_tracking_number = tracking_number
            break

    excluded: list[dict[str, str]] = []
    candidates: list[dict[str, Any]] = []
    for check in tracking_checks:  # newest first
        check_id = str(check.get("check_id") or "unknown check")
        linked_evidence_id = str(check.get("evidence_id") or "")
        linked_evidence = evidence_by_id.get(linked_evidence_id)
        reason: str | None = None

        if linked_evidence is None:
            reason = "LINKED_EVIDENCE_NOT_FOUND"
        else:
            linked_status = _upper(linked_evidence.get("verification_status"), "SUBMITTED_UNVERIFIED")
            linked_payload = _as_dict(linked_evidence.get("evidence"))
            linked_number = str(linked_payload.get("tracking_number") or "").strip()
            check_number = str(check.get("tracking_number") or "").strip()
            if linked_status == "VERIFICATION_REVOKED":
                reason = "LINKED_EVIDENCE_REVOKED"
            elif linked_status == "REJECTED":
                reason = "LINKED_EVIDENCE_REJECTED"
            elif linked_status == "VERIFIED" and historical_verification_problem(linked_evidence):
                reason = "LINKED_EVIDENCE_INTEGRITY_WARNING"
            elif not linked_number or not check_number or linked_number.casefold() != check_number.casefold():
                reason = "TRACKING_NUMBER_MISMATCH"
            elif current_evidence is None or linked_evidence_id != str(current_evidence.get("evidence_id")):
                reason = "NOT_ASSOCIATED_WITH_CURRENT_EVIDENCE"
            else:
                candidates.append(check)

        if reason:
            excluded.append({"check_id": check_id, "evidence_id": linked_evidence_id or "unknown", "reason": reason})

    selected = candidates[0] if candidates else None
    selected_id = str(selected.get("check_id")) if selected else None
    for check in candidates[1:]:
        # Keep older matching snapshots in history, but only the most recent
        # matching snapshot is active in the summary.
        if str(check.get("check_id")) != selected_id:
            excluded.append({
                "check_id": str(check.get("check_id") or "unknown check"),
                "evidence_id": str(check.get("evidence_id") or "unknown"),
                "reason": "OLDER_SNAPSHOT_FOR_CURRENT_EVIDENCE",
            })

    # The display list is informational; sort it back into input order so the
    # dashboard's audit chronology remains intuitive.
    position = {str(item.get("check_id") or "unknown check"): i for i, item in enumerate(tracking_checks)}
    excluded.sort(key=lambda item: position.get(item["check_id"], len(position)))
    return selected, current_evidence, excluded


def build_resolution_summary(
    case: dict[str, Any],
    order: dict[str, Any],
    evidence_records: list[dict[str, Any]],
    tracking_checks: list[dict[str, Any]],
    follow_up_actions: list[dict[str, Any]],
    message_drafts: list[dict[str, Any]],
    audit_log: list[dict[str, Any]],
    generated_at: str,
) -> dict[str, Any]:
    """Build a transparent summary from saved data only; no facts are inferred as verified."""
    latest_evidence = evidence_records[0] if evidence_records else None
    latest_tracking, tracking_evidence, excluded_tracking_snapshots = select_tracking_snapshot_for_current_evidence(
        evidence_records, tracking_checks
    )
    latest_draft = message_drafts[0] if message_drafts else None
    issue = _upper(case.get("issue_type"), "OTHER")
    policy = _upper(case.get("policy_decision"), "NEEDS_HUMAN_REVIEW")
    case_status = _upper(case.get("status"), "UNKNOWN")
    payment_status = _upper(order.get("payment_status"), "UNKNOWN")
    refund_status = _upper(case.get("refund_status") or order.get("refund_status"), "NONE")
    evidence_status = _upper((latest_evidence or {}).get("verification_status"), "NONE")
    tracking = _as_dict(latest_tracking)
    comparison = _as_dict(tracking.get("comparison"))
    simulated = (
        _upper(tracking.get("source_mode"), "") == "SIMULATED_DEMO"
        or "SIMULATED" in _upper(tracking.get("provider"), "")
    ) if latest_tracking else False
    actions_open = [a for a in follow_up_actions if _upper(a.get("status")) in OPEN_ACTION_STATUSES]
    actions_done = [a for a in follow_up_actions if _upper(a.get("status")) in COMPLETED_ACTION_STATUSES]
    actions_blocked = [a for a in follow_up_actions if _upper(a.get("status")) == "BLOCKED"]
    # Reconcile the stored review labels with contradictory notes/source metadata.
    # This does not silently rewrite history; suspicious legacy records need an explicit audited correction.
    evidence_counts = evidence_integrity_counts(evidence_records)
    integrity_issues = evidence_integrity_issues(evidence_records)
    draft_counts = {
        "total": len(message_drafts),
        "unsent_drafts": sum(1 for d in message_drafts if _upper(d.get("status"), "DRAFT") == "DRAFT"),
    }

    findings: list[str] = [
        f"Issue classified as {issue}; saved policy decision is {policy}.",
        f"Order payment status is {payment_status}; a PayPal capture {'is linked' if order.get('paypal_capture_id') else 'is not linked'}.",
        f"Evidence history contains {evidence_counts['total']} record(s): {evidence_counts['verified']} VERIFIED without detected integrity warnings, {evidence_counts['historical_verified_needing_review']} historically VERIFIED needing correction, {evidence_counts['verification_revoked']} verification(s) revoked, {evidence_counts['rejected']} rejected, and {evidence_counts['unverified']} unverified.",
        f"Follow-up plan contains {len(follow_up_actions)} task(s): {len(actions_open)} open/in progress, {len(actions_blocked)} blocked, and {len(actions_done)} completed.",
        f"Customer communication history contains {draft_counts['total']} draft(s); ResolveOS does not send these messages.",
    ]
    if latest_evidence:
        findings.append(
            f"Latest evidence {latest_evidence.get('evidence_id', 'unknown')} has review status {evidence_status}; this status reflects merchant review, not independent source authentication."
        )
    else:
        findings.append("No evidence submissions are saved for this case yet.")
    if evidence_counts["historical_verified_needing_review"]:
        findings.append(
            f"{evidence_counts['historical_verified_needing_review']} historically VERIFIED evidence record(s) have contradictory review notes or placeholder/missing source metadata. They are excluded from the current verified count until the merchant records an audited correction."
        )
    if evidence_counts["verification_revoked"]:
        findings.append(
            f"{evidence_counts['verification_revoked']} evidence verification decision(s) were explicitly revoked with an audit record; revoked records no longer count as verified."
        )
    if latest_tracking:
        finding_prefix = "Latest tracking result for the current evidence is simulated demo data" if simulated else "Latest tracking result for the current evidence is saved provider data"
        status_text = tracking.get("tracking_status") or tracking.get("provider_shipping_status") or "unknown status"
        linked_evidence_id = latest_tracking.get("evidence_id") or "unknown evidence"
        findings.append(
            f"{finding_prefix} ({latest_tracking.get('check_id', 'unknown check')}) with status {status_text}; matched to {linked_evidence_id} and tracking number {latest_tracking.get('tracking_number') or 'unknown'}."
        )
        if comparison.get("shipping_status_comparison") == "DIFFERENT_REVIEW_REQUIRED":
            findings.append("Saved status comparison reports a difference that requires human review.")
        if comparison.get("delivery_date_comparison") == "DIFFERENT_REVIEW_REQUIRED":
            findings.append("Saved delivery-date comparison reports a difference that requires human review.")
        if not comparison:
            findings.append("The selected tracking record has no saved comparison object.")
    elif tracking_checks:
        target_id = (tracking_evidence or {}).get("evidence_id", "none")
        findings.append(
            f"Saved tracking snapshots exist, but none is matched to the current eligible evidence record {target_id}; no unrelated or revoked-evidence snapshot is treated as current."
        )
    else:
        findings.append("No carrier tracking lookup or demo simulation is saved for this case.")
    if excluded_tracking_snapshots:
        reason_counts: dict[str, int] = {}
        for item in excluded_tracking_snapshots:
            reason_counts[item["reason"]] = reason_counts.get(item["reason"], 0) + 1
        reason_text = ", ".join(f"{count} {reason}" for reason, count in sorted(reason_counts.items()))
        findings.append(
            f"{len(excluded_tracking_snapshots)} tracking snapshot(s) were excluded from the active summary because they did not match the current eligible evidence: {reason_text}. Historical snapshots remain saved."
        )
    if latest_draft:
        findings.append(
            f"Latest customer message {latest_draft.get('draft_id', 'unknown')} is { _upper(latest_draft.get('status'), 'DRAFT') }; it has not been sent by ResolveOS."
        )
    latest_investigation_id = next((
        (event.get("details") or {}).get("run_id")
        for event in reversed(audit_log or [])
        if event.get("action") == "INVESTIGATION_ORCHESTRATED"
        and (event.get("details") or {}).get("run_id")
    ), None)

    blockers: list[str] = []
    if case_status in ACTIVE_REFUND_STATUSES or refund_status == "PENDING":
        blockers.append("A refund request is pending, submitting, or requires reconciliation; reconcile the PayPal state before closing.")
    if policy in UNRESOLVED_POLICY_DECISIONS:
        blockers.append(f"The current policy decision is {policy}; the case still needs investigation or an authorized decision.")
    if actions_open:
        blockers.append(f"{len(actions_open)} follow-up action(s) remain open or in progress.")
    if actions_blocked:
        blockers.append(f"{len(actions_blocked)} follow-up action(s) remain blocked by prerequisites.")
    if evidence_status == "SUBMITTED_UNVERIFIED":
        blockers.append("The latest evidence is still SUBMITTED_UNVERIFIED.")
    elif evidence_status == "VERIFICATION_REVOKED":
        blockers.append("The latest evidence verification was revoked; review new genuine evidence before resolving the case.")
    if evidence_counts["historical_verified_needing_review"]:
        blockers.append(
            f"{evidence_counts['historical_verified_needing_review']} historical VERIFIED evidence record(s) have contradictory review metadata and require an audited correction before closure."
        )
    if issue == "ITEM_NOT_RECEIVED" and simulated:
        blockers.append("The latest tracking result is simulated and cannot support a factual shipment resolution.")
    if not order.get("paypal_capture_id") and policy not in {"NO_NEW_REFUND"} and refund_status != "COMPLETED":
        blockers.append("No PayPal capture is linked to the order; do not infer refund eligibility from the summary.")
    blockers = list(dict.fromkeys(blockers))

    if case_status.startswith("CLOSED_") or case_status == "CLOSED_NO_ACTION":
        readiness = "ALREADY_CLOSED"
    else:
        readiness = "READY_FOR_MERCHANT_DECISION" if not blockers else "NOT_READY_TO_CLOSE"

    return {
        "generated_at": generated_at,
        "source": "DETERMINISTIC_SAVED_STATE_SUMMARY",
        "case": {
            "case_id": case.get("case_id"),
            "order_id": case.get("order_id"),
            "issue_type": issue,
            "customer_message": case.get("customer_message", ""),
            "investigation_summary": case.get("summary", ""),
            "case_status": case_status,
            "policy_decision": policy,
            "policy_reasons": case.get("reasons") if isinstance(case.get("reasons"), list) else [],
            "confidence": case.get("confidence"),
        },
        "order": {
            "item_name": order.get("item_name"),
            "amount": order.get("amount"),
            "currency": order.get("currency"),
            "shipping_status": order.get("shipping_status"),
            "payment_status": order.get("payment_status"),
            "capture_linked": bool(order.get("paypal_capture_id")),
            # Do not expose raw PayPal IDs in this narrative summary.
            "refund_status": refund_status,
        },
        "evidence": {
            "counts": evidence_counts,
            "latest_evidence_id": (latest_evidence or {}).get("evidence_id"),
            "latest_status": evidence_status,
            "integrity_issues": integrity_issues,
            "verified_count_note": "The verified count excludes records with explicit contradictory review notes or obvious placeholder/missing source metadata. This is a local integrity check, not independent authentication of any carrier source.",
        },
        "tracking": {
            "latest_check_id": tracking.get("check_id"),
            "latest_tracking_number": tracking.get("tracking_number"),
            "provider": tracking.get("provider"),
            "simulated": simulated,
            "status": tracking.get("tracking_status") or tracking.get("provider_shipping_status"),
            "current_evidence_id": (tracking_evidence or {}).get("evidence_id"),
            "associated_evidence_id": tracking.get("evidence_id"),
            "association_status": "MATCHED_TO_CURRENT_EVIDENCE" if latest_tracking else "NO_MATCHING_SNAPSHOT_FOR_CURRENT_EVIDENCE",
            "excluded_snapshot_count": len(excluded_tracking_snapshots),
            "excluded_snapshots": excluded_tracking_snapshots,
            "comparison_outcome": comparison.get("overall_result") or (
                "SIMULATED_DISCREPANCY" if simulated and (
                    comparison.get("shipping_status_comparison") == "DIFFERENT_REVIEW_REQUIRED"
                    or comparison.get("delivery_date_comparison") == "DIFFERENT_REVIEW_REQUIRED"
                ) else "DERIVED_FROM_SAVED_FIELDS" if comparison else "NOT_RECORDED"
            ),
            "shipping_status_comparison": comparison.get("shipping_status_comparison"),
            "delivery_date_comparison": comparison.get("delivery_date_comparison"),
        },
        "follow_up": {
            "total": len(follow_up_actions),
            "open_or_in_progress": len(actions_open),
            "blocked": len(actions_blocked),
            "completed": len(actions_done),
            "actions": [{"action_id": a.get("action_id"), "title": a.get("title"), "status": a.get("status"), "blocked_reason": a.get("blocked_reason")} for a in follow_up_actions],
        },
        "customer_messages": {
            **draft_counts,
            "latest_draft_id": (latest_draft or {}).get("draft_id"),
            "latest_draft_status": (latest_draft or {}).get("status"),
            "latest_draft_updated_at": (latest_draft or {}).get("updated_at"),
            "sent_by_resolveos": False,
        },
        "latest_investigation_run_id": latest_investigation_id,
        "findings": findings,
        "closure_readiness": {"status": readiness, "ready": not blockers, "blockers": blockers},
        "disclaimer": "Generated from saved local records. Merchant-submitted evidence and simulated tracking are not independently authenticated by this summary. Summary generation does not close the case, change policy, or execute a payment action.",
    }


def validate_closure(
    closure_outcome: str,
    case: dict[str, Any],
    order: dict[str, Any],
    evidence_records: list[dict[str, Any]],
    tracking_checks: list[dict[str, Any]],
    follow_up_actions: list[dict[str, Any]],
) -> str | None:
    """Return a blocking explanation, or None when explicit closure is permissible."""
    outcome = _upper(closure_outcome)
    case_status = _upper(case.get("status"))
    refund_status = _upper(case.get("refund_status") or order.get("refund_status"), "NONE")
    policy = _upper(case.get("policy_decision"))
    if case_status.startswith("CLOSED_") or case_status == "CLOSED_NO_ACTION":
        return "This case is already closed. Reopening is not supported by this endpoint."
    if case_status in ACTIVE_REFUND_STATUSES or refund_status == "PENDING":
        return "A refund is submitting, pending, or requires reconciliation. Resolve the PayPal lifecycle before closing this case."
    if refund_status == "COMPLETED" or case_status == "REFUND_COMPLETED" or order.get("refund_status") == "REFUNDED":
        if outcome != "RESOLVED_REFUND_COMPLETED":
            return "A refund is recorded as completed. Use RESOLVED_REFUND_COMPLETED so the closure record matches the payment outcome."
    if outcome == "RESOLVED_REFUND_COMPLETED":
        completed = (
            case_status == "REFUND_COMPLETED"
            and refund_status == "COMPLETED"
            and bool(case.get("paypal_refund_id"))
            and str(order.get("refund_status") or "").upper() == "REFUNDED"
        )
        if not completed:
            return "A completed PayPal refund could not be confirmed in the saved case and order records."
        return None
    if outcome == "RESOLVED_NO_REFUND":
        if policy in UNRESOLVED_POLICY_DECISIONS:
            return f"The current policy decision is {policy}. Finish investigation and policy review before recording a no-refund resolution."
        outstanding = [a for a in follow_up_actions if _upper(a.get("status")) in OPEN_ACTION_STATUSES]
        if outstanding:
            return f"{len(outstanding)} follow-up action(s) are still open or in progress. Complete or explicitly escalate them before resolving without a refund."
        blocked = [a for a in follow_up_actions if _upper(a.get("status")) == "BLOCKED"]
        if blocked:
            return f"{len(blocked)} follow-up action(s) are still blocked. Resolve prerequisites or choose ESCALATED with an explanation."
        if evidence_records and _upper(evidence_records[0].get("verification_status")) in {"SUBMITTED_UNVERIFIED", "VERIFICATION_REVOKED"}:
            return "The latest evidence is unverified or its prior verification was revoked. Review genuine evidence before resolving without a refund."
        integrity_counts = evidence_integrity_counts(evidence_records)
        if integrity_counts["historical_verified_needing_review"]:
            return "Historical VERIFIED evidence has contradictory review metadata. Record an audited verification correction before resolving without a refund."
        latest_tracking, current_tracking_evidence, _excluded_tracking = select_tracking_snapshot_for_current_evidence(
            evidence_records, tracking_checks
        )
        if _upper(case.get("issue_type")) == "ITEM_NOT_RECEIVED" and latest_tracking and (
            _upper(latest_tracking.get("source_mode"), "") == "SIMULATED_DEMO"
            or "SIMULATED" in _upper(latest_tracking.get("provider"), "")
        ) and (not current_tracking_evidence or _upper(current_tracking_evidence.get("verification_status")) != "VERIFIED" or historical_verification_problem(current_tracking_evidence)):
            return "The latest tracking result is simulated and the latest evidence is not currently verified without detected integrity warnings. Do not use demo data to support a real no-refund closure."
    if outcome not in CLOSURE_OUTCOMES:
        return "Unsupported closure outcome."
    return None


def status_for_closure(outcome: str) -> str:
    return {
        "RESOLVED_REFUND_COMPLETED": "CLOSED_RESOLVED_REFUND",
        "RESOLVED_NO_REFUND": "CLOSED_RESOLVED_NO_REFUND",
        "ESCALATED": "CLOSED_ESCALATED",
        "CUSTOMER_WITHDREW": "CLOSED_CUSTOMER_WITHDREW",
        "DUPLICATE_CASE": "CLOSED_DUPLICATE",
    }[outcome]
