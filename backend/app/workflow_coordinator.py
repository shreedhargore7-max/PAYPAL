"""End-to-end investigation workflow coordinator for ResolveOS.

This module turns saved local records into a bounded stage plan. It is read-only:
it does not call payment/carrier providers, update case/order/evidence state, send
messages, close cases, or execute refunds. Any run is persisted in the existing
case audit log by the API layer.
"""
from __future__ import annotations

from typing import Any

from .evidence_integrity import historical_verification_problem


PROTECTED_CASE_STATUSES = {
    "REFUND_SUBMITTING", "REFUND_PENDING", "REFUND_COMPLETED",
    "REFUND_REQUIRES_RECONCILIATION", "CLOSED_NO_ACTION", "CLOSED_RESOLVED",
    "CLOSED_ESCALATED", "CLOSED_CUSTOMER_WITHDREW", "CLOSED_DUPLICATE",
}


def _upper(value: Any, default: str = "UNKNOWN") -> str:
    return str(value or default).strip().upper()


def _record_evidence_payload(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("evidence")
    return value if isinstance(value, dict) else {}


def _is_simulated(check: dict[str, Any]) -> bool:
    source = _upper(check.get("source_mode"), "")
    provider = _upper(check.get("provider"), "")
    lookup = _upper(check.get("lookup_state"), "")
    return (
        source == "SIMULATED_DEMO"
        or lookup == "SIMULATED_DEMO_DATA"
        or "SIMULATED" in source
        or "SIMULATED" in provider
        or "SIMULATED" in lookup
    )


def _latest_active_evidence(evidence_records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Records are newest-first; revoked records are historical, not active proof."""
    for record in evidence_records:
        if _upper(record.get("verification_status")) != "VERIFICATION_REVOKED":
            return record
    return None


def _matching_tracking_snapshot(
    evidence_record: dict[str, Any] | None,
    tracking_checks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Select only a snapshot tied to the currently active evidence record and number."""
    if not evidence_record:
        return None
    if _upper(evidence_record.get("verification_status")) in {"REJECTED", "VERIFICATION_REVOKED"}:
        return None
    payload = _record_evidence_payload(evidence_record)
    evidence_id = str(evidence_record.get("evidence_id") or "")
    tracking_number = str(payload.get("tracking_number") or "").strip()
    if not evidence_id or not tracking_number:
        return None
    for check in tracking_checks:
        if str(check.get("evidence_id") or "") != evidence_id:
            continue
        if str(check.get("tracking_number") or "").strip() != tracking_number:
            continue
        return check
    return None


def build_end_to_end_workflow(
    *,
    case: dict[str, Any],
    order: dict[str, Any],
    evidence_records: list[dict[str, Any]],
    tracking_checks: list[dict[str, Any]],
    follow_up_actions: list[dict[str, Any]],
    message_drafts: list[dict[str, Any]],
    resolution_summaries: list[dict[str, Any]],
    workflow_id: str,
    generated_at: str,
) -> dict[str, Any]:
    """Return a deterministic workflow snapshot based on the supplied saved state."""
    case_id = str(case.get("case_id") or "")
    issue = _upper(case.get("issue_type"), "OTHER")
    policy = _upper(case.get("policy_decision"), "NEEDS_HUMAN_REVIEW")
    case_status = _upper(case.get("status"), "UNKNOWN")
    payment_status = _upper(order.get("payment_status"), "UNKNOWN")
    shipping_status = _upper(order.get("shipping_status"), "UNKNOWN")
    capture_id = str(order.get("paypal_capture_id") or "").strip()
    refund_status = _upper(case.get("refund_status") or order.get("refund_status"), "NONE")
    refund_locked = (
        case_status in PROTECTED_CASE_STATUSES
        or refund_status in {"PENDING", "COMPLETED"}
        or bool(case.get("paypal_refund_id"))
    )

    active_evidence = _latest_active_evidence(evidence_records)
    evidence_status = _upper(active_evidence.get("verification_status"), "NONE") if active_evidence else "NONE"
    evidence_payload = _record_evidence_payload(active_evidence or {})
    evidence_integrity_problem = (
        historical_verification_problem(active_evidence)
        if active_evidence and evidence_status == "VERIFIED"
        else None
    )
    evidence_is_verified = evidence_status == "VERIFIED" and not evidence_integrity_problem
    tracking_number = str(evidence_payload.get("tracking_number") or "").strip()
    tracking = _matching_tracking_snapshot(active_evidence, tracking_checks)
    tracking_simulated = _is_simulated(tracking) if tracking else False
    comparison = tracking.get("comparison") if tracking and isinstance(tracking.get("comparison"), dict) else {}
    overall_result = _upper(comparison.get("overall_result"), "NOT_RECORDED")
    status_difference = _upper(comparison.get("shipping_status_comparison"), "NOT_COMPARABLE") == "DIFFERENT_REVIEW_REQUIRED"
    date_difference = _upper(comparison.get("delivery_date_comparison"), "NOT_COMPARABLE") == "DIFFERENT_REVIEW_REQUIRED"
    tracking_discrepancy = status_difference or date_difference or "DISCREPANCY" in overall_result

    follow_up_counts = {"open": 0, "blocked": 0, "completed": 0, "total": len(follow_up_actions)}
    for action in follow_up_actions:
        state = _upper(action.get("status"), "OPEN")
        if state in {"OPEN", "IN_PROGRESS"}:
            follow_up_counts["open"] += 1
        elif state == "BLOCKED":
            follow_up_counts["blocked"] += 1
        elif state in {"COMPLETED", "AUTO_COMPLETED"}:
            follow_up_counts["completed"] += 1

    drafts = [draft for draft in message_drafts if _upper(draft.get("status"), "DRAFT") == "DRAFT"]
    latest_summary = resolution_summaries[0] if resolution_summaries else None
    summary_obj = latest_summary.get("summary") if latest_summary and isinstance(latest_summary.get("summary"), dict) else {}
    readiness = summary_obj.get("closure_readiness") if isinstance(summary_obj.get("closure_readiness"), dict) else {}
    closure_status = _upper(readiness.get("status"), "NOT_ASSESSED")

    closed = case_status.startswith("CLOSED_") or case_status == "CLOSED_NO_ACTION"

    stages: list[dict[str, Any]] = []

    def add_stage(key: str, title: str, status: str, owner: str, summary: str,
                  next_action: str | None = None, blocks_resolution: bool = True) -> None:
        stages.append({
            "stage_key": key,
            "title": title,
            "status": status,
            "owner": owner,
            "summary": summary,
            "next_action": next_action,
            "blocks_resolution": blocks_resolution,
        })

    add_stage(
        "CASE_INTAKE", "Complaint and case context", "COMPLETED", "SYSTEM",
        f"Loaded {issue} complaint {case_id} with saved policy {policy}.",
        blocks_resolution=False,
    )

    if refund_locked:
        add_stage("PAYMENT_INTEGRITY", "Payment and refund lifecycle", "PROTECTED", "SYSTEM",
                  f"Refund lifecycle is {refund_status}; avoid duplicate or new payment action.",
                  "Inspect the existing PayPal/refund record through the protected reconciliation workflow.")
    elif payment_status != "COMPLETED":
        add_stage("PAYMENT_INTEGRITY", "Payment and refund lifecycle", "BLOCKED", "MERCHANT",
                  f"Payment status is {payment_status}; do not consider a refund until payment is confirmed.",
                  "Confirm the actual payment status in PayPal Sandbox before any payment decision.")
    elif not capture_id:
        add_stage("PAYMENT_INTEGRITY", "Payment and refund lifecycle", "ACTION_REQUIRED", "MERCHANT",
                  "Local payment status is COMPLETED, but no verified PayPal capture ID is linked to this order.",
                  "Only link a genuine completed PayPal capture with matching amount/currency. Do not invent a capture ID.")
    else:
        add_stage("PAYMENT_INTEGRITY", "Payment and refund lifecycle", "COMPLETED", "SYSTEM",
                  f"A capture ID is linked ({capture_id}); the protected refund endpoint must still re-verify it before a refund.",
                  blocks_resolution=False)

    if active_evidence:
        add_stage("EVIDENCE_COLLECTION", "Collect complaint evidence", "COMPLETED", "MERCHANT",
                  f"Latest non-revoked evidence record is {active_evidence.get('evidence_id')}; submission alone is not verification.",
                  blocks_resolution=False)
    else:
        add_stage("EVIDENCE_COLLECTION", "Collect complaint evidence", "ACTION_REQUIRED", "MERCHANT",
                  "No non-revoked evidence record is available.",
                  "Submit relevant evidence through Evidence Collection and keep it unverified until manually reviewed.")

    if evidence_is_verified:
        add_stage("EVIDENCE_REVIEW", "Review evidence source", "COMPLETED", "MERCHANT",
                  f"Current evidence {active_evidence.get('evidence_id')} is marked VERIFIED and has no heuristic integrity warning. This is still a merchant attestation, not independent source authentication.",
                  blocks_resolution=False)
    else:
        if evidence_status == "SUBMITTED_UNVERIFIED":
            review_summary = f"Current evidence {active_evidence.get('evidence_id')} is SUBMITTED_UNVERIFIED." if active_evidence else "No evidence is available for review."
        elif evidence_status == "REJECTED":
            review_summary = f"Current evidence {active_evidence.get('evidence_id')} was REJECTED."
        elif evidence_status == "VERIFICATION_REVOKED":
            review_summary = "Current evidence verification was revoked."
        elif evidence_integrity_problem:
            review_summary = f"Current evidence has a verification integrity warning: {evidence_integrity_problem}"
        else:
            review_summary = "No current evidence record is eligible to be treated as verified."
        add_stage("EVIDENCE_REVIEW", "Review evidence source", "ACTION_REQUIRED", "MERCHANT", review_summary,
                  "Open the source yourself; correct placeholder/test details, submit genuine evidence, and record a truthful verification decision only after review.")

    if issue != "ITEM_NOT_RECEIVED":
        add_stage("CARRIER_COMPARISON", "Compare carrier tracking", "NOT_APPLICABLE", "SYSTEM",
                  "Carrier tracking comparison is not a primary workflow step for this issue type.", blocks_resolution=False)
    elif not tracking_number:
        add_stage("CARRIER_COMPARISON", "Compare carrier tracking", "ACTION_REQUIRED", "MERCHANT",
                  "The current evidence record has no tracking number.",
                  "Request a real tracking number and inspect its official carrier source; demo tracking cannot establish shipment facts.")
    elif not tracking:
        add_stage("CARRIER_COMPARISON", "Compare carrier tracking", "ACTION_REQUIRED", "MERCHANT",
                  f"No saved tracking snapshot matches the active evidence {active_evidence.get('evidence_id') if active_evidence else 'record'} and tracking number.",
                  "Retrieve or inspect the official carrier result for this exact tracking number and associate it with the current evidence record.")
    elif tracking_simulated:
        add_stage("CARRIER_COMPARISON", "Compare carrier tracking", "SIMULATED_ONLY", "MERCHANT",
                  f"Matching snapshot {tracking.get('check_id')} is simulated and cannot support a factual shipment conclusion.",
                  "Open the official carrier source and compare status, timestamp, and delivery date manually. Keep the local simulation labelled demo-only.")
    else:
        add_stage("CARRIER_COMPARISON", "Compare carrier tracking", "REVIEW_REQUIRED", "MERCHANT",
                  f"Matching snapshot {tracking.get('check_id')} is available, but its source and status/date differences still require merchant review.",
                  "Inspect the official source and record the result; a tracking response does not authorize payment.")

    prerequisites_ready = evidence_is_verified and bool(capture_id) and not tracking_simulated
    if refund_locked:
        add_stage("POLICY_REEVALUATION", "Re-evaluate deterministic policy", "PROTECTED", "SYSTEM",
                  "A submitted/completed/reconciliation refund lifecycle is protected from ordinary workflow transitions.",
                  "Use the existing reconciliation workflow; never retry or issue a second refund automatically.")
    elif prerequisites_ready and policy not in {"NEEDS_MORE_INFORMATION", "NEEDS_HUMAN_REVIEW"}:
        add_stage("POLICY_REEVALUATION", "Re-evaluate deterministic policy", "READY_FOR_REVIEW", "MERCHANT",
                  "Core capture/evidence prerequisites appear satisfied; this snapshot is not itself a new policy decision.",
                  "Run the existing deterministic Re-evaluate case action, then review its saved result.")
    else:
        reasons = []
        if not capture_id:
            reasons.append("verified capture is not linked")
        if not evidence_is_verified:
            reasons.append("current evidence is not verified")
        if tracking_simulated:
            reasons.append("tracking snapshot is simulated")
        if policy == "NEEDS_MORE_INFORMATION":
            reasons.append("saved policy still needs information")
        add_stage("POLICY_REEVALUATION", "Re-evaluate deterministic policy", "BLOCKED", "MERCHANT",
                  "Prerequisites remain: " + "; ".join(reasons or ["human review is required"]) + ".",
                  "Complete the applicable evidence/payment prerequisites first. Re-evaluation never submits a refund by itself.")

    if drafts:
        latest_draft = drafts[0]
        add_stage("CUSTOMER_COMMUNICATION", "Customer follow-up communication", "DRAFT_ONLY", "MERCHANT",
                  f"Draft {latest_draft.get('draft_id')} is saved but unsent.",
                  "Review and edit the draft, then copy it to your support channel manually if appropriate.",
                  blocks_resolution=False)
    else:
        add_stage("CUSTOMER_COMMUNICATION", "Customer follow-up communication", "OPTIONAL", "MERCHANT",
                  "No saved customer message draft exists.",
                  "Optionally prepare a customer information request if that is the appropriate next action.",
                  blocks_resolution=False)

    if closed:
        add_stage("MERCHANT_RESOLUTION", "Resolution and closeout", "CLOSED", "SYSTEM",
                  f"Case is already in status {case_status}; coordinator will not change it.", blocks_resolution=False)
    else:
        closure_blockers: list[str] = []
        if policy in {"NEEDS_MORE_INFORMATION", "NEEDS_HUMAN_REVIEW"}:
            closure_blockers.append(f"policy decision is {policy}")
        if follow_up_counts["open"] or follow_up_counts["blocked"]:
            closure_blockers.append(f"{follow_up_counts['open']} action(s) open/in progress and {follow_up_counts['blocked']} blocked")
        if not evidence_is_verified:
            closure_blockers.append("current evidence is not marked verified")
        if issue == "ITEM_NOT_RECEIVED" and (not tracking or tracking_simulated or tracking_discrepancy):
            closure_blockers.append("tracking source is missing, simulated, or requires review")
        if not capture_id:
            closure_blockers.append("no PayPal capture is linked")
        add_stage("MERCHANT_RESOLUTION", "Resolution review and explicit closeout", "BLOCKED" if closure_blockers else "MERCHANT_REVIEW_REQUIRED", "MERCHANT",
                  "Closure blockers: " + "; ".join(closure_blockers) + "." if closure_blockers else f"Latest summary status is {closure_status}; merchant must review and choose a truthful disposition.",
                  "Keep the case open until evidence and policy blockers are resolved. Closing a case is separate from a refund.")

    active_attention = [stage for stage in stages if stage["status"] in {"ACTION_REQUIRED", "BLOCKED", "SIMULATED_ONLY", "REVIEW_REQUIRED", "READY_FOR_REVIEW"}]
    if closed:
        next_step = {
            "stage_key": "CASE_CLOSED", "title": "Case is closed", "owner": "NONE",
            "instruction": "This workflow snapshot is read-only. Review the saved audit history; do not reopen or mutate the case from this coordinator.",
            "reason": f"Case status is {case_status}.",
        }
    elif refund_locked:
        next_step = {
            "stage_key": "PAYMENT_INTEGRITY", "title": "Respect the protected refund lifecycle", "owner": "MERCHANT",
            "instruction": "Inspect the saved PayPal/refund state using the existing reconciliation workflow. Do not issue or retry a refund from this coordinator.",
            "reason": f"Refund lifecycle status is {refund_status}; duplicate payment activity is prohibited.",
        }
    elif issue == "ITEM_NOT_RECEIVED" and (not evidence_is_verified or not tracking or tracking_simulated or tracking_discrepancy):
        next_step = {
            "stage_key": "EVIDENCE_REVIEW", "title": "Verify genuine carrier evidence", "owner": "MERCHANT",
            "instruction": "Open the official carrier tracking source and compare the real tracking number, timestamped scan, delivery estimate, and delivery details. Save any genuine evidence as unverified first, then record a truthful merchant review. Do not treat simulated snapshots as proof.",
            "reason": "The current evidence is unverified and the current tracking record is missing, simulated, or reports a discrepancy.",
        }
    elif not capture_id:
        next_step = {
            "stage_key": "PAYMENT_INTEGRITY", "title": "Check PayPal capture linkage", "owner": "MERCHANT",
            "instruction": "Link only an actual completed PayPal Sandbox capture whose amount and currency match this order. If no such capture exists, leave this step blocked; never invent a capture ID.",
            "reason": "The local payment status says COMPLETED but no capture is linked to the order.",
        }
    elif policy in {"NEEDS_MORE_INFORMATION", "NEEDS_HUMAN_REVIEW"}:
        next_step = {
            "stage_key": "POLICY_REEVALUATION", "title": "Complete prerequisites, then re-evaluate", "owner": "MERCHANT",
            "instruction": "Finish the required evidence review and payment checks, then use the existing deterministic Re-evaluate case action.",
            "reason": f"The saved policy remains {policy}; the coordinator does not override it.",
        }
    else:
        next_step = {
            "stage_key": "MERCHANT_RESOLUTION", "title": "Review the documented resolution", "owner": "MERCHANT",
            "instruction": "Review the current summary and follow-up plan, then choose a truthful closeout disposition if all prerequisites are satisfied.",
            "reason": "Any final disposition remains an explicit merchant decision.",
        }

    status_counts: dict[str, int] = {}
    for stage in stages:
        key = stage["status"]
        status_counts[key] = status_counts.get(key, 0) + 1
    if closed:
        overall_status = "CLOSED_READ_ONLY"
    elif refund_locked:
        overall_status = "PROTECTED_NO_NEW_PAYMENT_ACTION"
    elif active_attention or any(s["status"] == "ACTION_REQUIRED" for s in stages):
        overall_status = "ACTION_REQUIRED"
    else:
        overall_status = "READY_FOR_MERCHANT_REVIEW"
    trace = [
        f"Loaded case {case_id}, issue {issue}, saved policy {policy}, order {order.get('order_id')}.",
        f"Selected active evidence {active_evidence.get('evidence_id') if active_evidence else 'none'}; revoked records were excluded from active evidence selection.",
        f"Selected tracking snapshot {tracking.get('check_id') if tracking else 'none'} only when its evidence ID and tracking number matched the active evidence record.",
        f"Capture linked={bool(capture_id)}; evidence marked verified and without detected integrity warning={evidence_is_verified}; simulated tracking={tracking_simulated}.",
        f"Computed {len(stages)} workflow stages from saved local state. No external APIs or payment actions were executed.",
    ]

    return {
        "workflow_id": workflow_id,
        "generated_at": generated_at,
        "case_id": case_id,
        "order_id": order.get("order_id"),
        "workflow_name": "ResolveOS End-to-End Investigation Workflow",
        "version": "1.0",
        "overall_status": overall_status,
        "current_case_status": case_status,
        "saved_policy_decision": policy,
        "active_evidence_id": active_evidence.get("evidence_id") if active_evidence else None,
        "active_tracking_check_id": tracking.get("check_id") if tracking else None,
        "active_tracking_number": tracking.get("tracking_number") if tracking else (tracking_number or None),
        "active_tracking_source_mode": tracking.get("source_mode") if tracking else None,
        "follow_up_counts": follow_up_counts,
        "saved_summary_id": latest_summary.get("summary_id") if latest_summary else None,
        "closure_readiness": closure_status,
        "stage_counts": status_counts,
        "stages": stages,
        "next_safe_step": next_step,
        "decision_trace": trace,
        "guardrails": [
            "This coordinator reads saved local state and creates an auditable workflow snapshot only.",
            "It does not execute payment actions, approve or submit refunds, close a case, or send a customer message.",
            "Evidence status is a merchant-entered review record and is not independent authentication.",
            "Simulated tracking is never treated as genuine carrier evidence.",
            "Only a tracking snapshot matching the active non-revoked evidence ID and exact tracking number is selected.",
            "A real refund still requires the existing protected PayPal capture verification, amount/currency match, and merchant authorization.",
        ],
    }
