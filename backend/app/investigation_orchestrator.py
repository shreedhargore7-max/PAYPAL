"""ResolveOS rules-first investigation orchestrator.

Coordinates existing read-only internal tools (case/order/evidence/tracking reads)
and a deterministic policy preview. It never calls an external carrier API and
never changes shipping, case-policy, payment, or refund state.
"""
from __future__ import annotations

from typing import Any

from .agent import evaluate_policy
from .investigation_tools import resolve_comparison_outcome


PROTECTED_REFUND_STATUSES = {
    "REFUND_SUBMITTING", "REFUND_PENDING", "REFUND_COMPLETED",
    "REFUND_REQUIRES_RECONCILIATION",
}


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def build_investigation_run(
    case: dict[str, Any],
    order: dict[str, Any],
    evidence_records: list[dict[str, Any]],
    tracking_checks: list[dict[str, Any]],
    *,
    run_id: str,
    ran_at: str,
    carrier_mode: str = "demo",
    tool_execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an auditable investigation plan from persisted project data only."""
    issue = str(case.get("issue_type") or "OTHER").upper()
    case_status = str(case.get("status") or "UNKNOWN").upper()
    policy_decision = str(case.get("policy_decision") or "NEEDS_HUMAN_REVIEW").upper()
    shipping_status = str(order.get("shipping_status") or "UNKNOWN").upper()
    payment_status = str(order.get("payment_status") or "UNKNOWN").upper()
    capture_id = str(order.get("paypal_capture_id") or "").strip()
    refund_status = str(case.get("refund_status") or order.get("refund_status") or "NONE").upper()
    refund_locked = case_status in PROTECTED_REFUND_STATUSES or refund_status in {"PENDING", "COMPLETED"} or bool(case.get("paypal_refund_id"))

    # Database queries return newest-first histories.
    latest_evidence_record = evidence_records[0] if evidence_records else None
    latest_evidence = latest_evidence_record.get("evidence") if latest_evidence_record and isinstance(latest_evidence_record.get("evidence"), dict) else {}
    evidence_status = str((latest_evidence_record or {}).get("verification_status") or "NONE").upper()
    tracking_number = str(latest_evidence.get("tracking_number") or "").strip()
    latest_tracking = tracking_checks[0] if tracking_checks else None
    comparison = latest_tracking.get("comparison") if latest_tracking and isinstance(latest_tracking.get("comparison"), dict) else {}
    resolved_comparison = resolve_comparison_outcome(latest_tracking)
    comparison = {**comparison, **resolved_comparison}
    source_mode = str((latest_tracking or {}).get("source_mode") or resolved_comparison.get("source_mode") or "UNKNOWN").upper()
    is_simulated = bool(resolved_comparison.get("is_simulated"))

    # This is a preview, not a persisted case update. When the bounded tool runner
    # supplies its policy tool result, use that exact output in the report.
    tool_execution = tool_execution or {}
    execution_results = tool_execution.get("results") if isinstance(tool_execution.get("results"), dict) else {}
    policy_tool_result = execution_results.get("preview_deterministic_policy")
    if isinstance(policy_tool_result, dict) and isinstance(policy_tool_result.get("decision"), str):
        preview_decision = policy_tool_result["decision"]
        preview_reasons = policy_tool_result.get("reasons", [])
        if not isinstance(preview_reasons, list):
            preview_reasons = []
    else:
        preview_decision, preview_reasons = evaluate_policy({"issue_type": issue}, order)

    steps: list[dict[str, Any]] = [
        {
            "tool": "read_case_context",
            "label": "Read complaint and case state",
            "status": "COMPLETED",
            "result": f"{issue} complaint; saved case status is {case_status}.",
        },
        {
            "tool": "read_order_payment",
            "label": "Inspect order and payment linkage",
            "status": "COMPLETED" if payment_status == "COMPLETED" and capture_id else "ATTENTION_REQUIRED",
            "result": (
                f"Payment is {payment_status} and a capture ID is linked."
                if payment_status == "COMPLETED" and capture_id
                else f"Payment status is {payment_status}; capture {'is linked' if capture_id else 'is not linked'}."
            ),
        },
        {
            "tool": "read_evidence_history",
            "label": "Inspect submitted evidence and review status",
            "status": "ATTENTION_REQUIRED" if not evidence_records or evidence_status != "VERIFIED" else "COMPLETED",
            "result": (
                "No evidence has been submitted yet."
                if not evidence_records
                else f"Found {len(evidence_records)} evidence record(s); newest record is {evidence_status}. Merchant verification is a recorded review, not independent source authentication."
            ),
        },
        {
            "tool": "read_tracking_comparisons",
            "label": "Inspect saved carrier comparison",
            "status": (
                "NOT_RUN" if not latest_tracking and not tracking_number
                else "ACTION_REQUIRED" if not latest_tracking
                else "SIMULATED_ONLY" if is_simulated
                else "HUMAN_REVIEW_REQUIRED"
            ),
            "result": (
                "No tracking number or saved tracking comparison is available."
                if not latest_tracking and not tracking_number
                else f"Tracking number is present ({tracking_number}), but no saved comparison exists yet."
                if not latest_tracking
                else f"Latest snapshot {latest_tracking.get('check_id') or ''} came from {source_mode}; outcome is {comparison.get('overall_result') or 'not recorded'}."
                + (" This is locally simulated data, not a carrier fact." if is_simulated else "")
            ),
        },
        {
            "tool": "preview_deterministic_policy",
            "label": "Preview deterministic policy (read-only)",
            "status": "READ_ONLY_PREVIEW",
            "result": f"Current order inputs produce preview decision {preview_decision}; saved case decision remains {policy_decision}.",
        },
    ]
    # Prefer actual executed tool steps over static descriptive placeholders.
    if isinstance(tool_execution.get("steps"), list) and tool_execution["steps"]:
        steps = tool_execution["steps"]

    observations: list[str] = []
    if not capture_id:
        observations.append("No PayPal capture is linked to this order. A refund must not be considered until a completed, amount/currency-matching capture is verified and linked.")
    if payment_status != "COMPLETED":
        observations.append(f"Order payment status is {payment_status}; investigate payment state before considering resolution options.")
    if not evidence_records:
        observations.append("No merchant evidence has been submitted for this case.")
    elif evidence_status != "VERIFIED":
        observations.append(f"The newest evidence record is {evidence_status}; it must not be treated as verified proof.")
    if not tracking_number:
        observations.append("No tracking number is available in the latest evidence record.")
    if latest_tracking and is_simulated:
        observations.append("The latest tracking snapshot is simulated demo data. It can test the workflow but cannot establish the real parcel status.")
    if latest_tracking and comparison.get("shipping_status_comparison") == "DIFFERENT_REVIEW_REQUIRED":
        observations.append("The saved tracking comparison reports a shipping-status difference; manually inspect an official source before drawing conclusions.")
    if latest_tracking and comparison.get("delivery_date_comparison") == "DIFFERENT_REVIEW_REQUIRED":
        observations.append("The saved tracking comparison reports a delivery-date difference; confirm that the compared dates represent the same thing.")
    if latest_tracking and comparison.get("outcome_origin") == "DERIVED_FROM_SAVED_FIELDS":
        observations.append(
            f"The comparison outcome was derived as {comparison.get('overall_result')} from saved status/date comparison flags because the stored snapshot did not contain an overall_result field."
        )
    if not observations:
        observations.append("No immediate missing-link or evidence-status issue was detected in the saved local records. Human review is still required before action.")

    if refund_locked:
        next_action = "Stop further payment actions and inspect the existing refund state. This orchestrator is read-only and will not submit or retry a refund."
    elif payment_status != "COMPLETED":
        next_action = "Review the payment record and confirm payment status before deciding the complaint. No payment action was taken by this run."
    elif not capture_id and latest_tracking and is_simulated:
        next_action = "Open the official carrier source to inspect the real status and delivery date; keep this local demo snapshot labelled simulated. Separately verify and link a completed PayPal capture matching amount/currency before any refund consideration. This run cannot authorize a refund."
    elif not capture_id:
        next_action = "Verify and link a completed PayPal capture that matches the order amount and currency. Separately review the shipping evidence; this run cannot authorize a refund."
    elif not evidence_records:
        next_action = "Request and save relevant evidence for this complaint before re-evaluating the case."
    elif not tracking_number and issue == "ITEM_NOT_RECEIVED":
        next_action = "Request the carrier tracking number and latest timestamped scan, then save them as unverified evidence."
    elif latest_tracking and is_simulated:
        next_action = "Open the official carrier source and compare its real tracking status and timestamp with the merchant submission. Keep the demo result labelled simulated and the evidence unverified until honestly reviewed."
    elif latest_tracking and (
        comparison.get("shipping_status_comparison") == "DIFFERENT_REVIEW_REQUIRED"
        or comparison.get("delivery_date_comparison") == "DIFFERENT_REVIEW_REQUIRED"
    ):
        next_action = "Manually review the official carrier source and resolve the status/date discrepancy; do not automatically change order state or approve a refund."
    elif tracking_number and not latest_tracking:
        if str(carrier_mode or "demo").lower() == "demo":
            next_action = "Use Fetch / simulate tracking to create a comparison snapshot. The demo result will be synthetic and will not prove the real shipment status."
        else:
            next_action = "Use Fetch / simulate tracking to query the configured live provider. Review its source and timestamps manually; provider data does not authorize a refund."
    elif evidence_status == "SUBMITTED_UNVERIFIED":
        next_action = "Review the evidence source manually and record a truthful Verified or Rejected decision. A source URL alone does not authenticate the underlying facts."
    elif preview_decision == "REFUND_CANDIDATE":
        next_action = "Present the evidence and policy result to an authorized merchant for explicit review. The orchestrator cannot approve or execute the refund."
    else:
        next_action = "Follow the deterministic policy reasons, request any missing information, and route the final resolution decision to a merchant reviewer."

    return {
        "run_id": run_id,
        "ran_at": ran_at,
        "agent_name": "ResolveOS Investigation Orchestrator",
        "version": "1.1" if tool_execution else "1.0",
        "mode": str(tool_execution.get("mode") or "Rules-first coordinated investigation (read-only)"),
        "case_id": case.get("case_id"),
        "order_id": case.get("order_id"),
        "issue_type": issue,
        "saved_case_status": case_status,
        "saved_policy_decision": policy_decision,
        "policy_preview": {"decision": preview_decision, "reasons": preview_reasons},
        "steps": steps,
        "observations": observations,
        "recommended_next_action": next_action,
        "human_review_required": True,
        "tools_performed": tool_execution.get("tools_performed") or [
            "Read saved case context",
            "Read order/payment fields",
            "Read evidence submission/review history",
            "Read saved carrier comparison snapshots",
            "Preview deterministic policy rules without saving the result",
        ],
        "planner_trace": tool_execution.get("planner_trace") or [],
        "tool_execution": {
            "call_count": int(tool_execution.get("tool_call_count", len(steps))),
            "max_tool_calls": int(tool_execution.get("max_tool_calls", 5)),
            "allowlisted_tools": tool_execution.get("allowlisted_tools") or [],
        },
        "guardrails": [
            "Only registered, allowlisted local read-only tools may run; arbitrary tool names are rejected.",
            "Tool selection branches on the observed issue, evidence status, tracking comparison, and protected refund lifecycle.",
            "A maximum of five tool calls is allowed per investigation run.",
            "No external LLM or carrier API call is made by this run.",
            "No order shipping state, saved case policy/status, evidence status, or payment/refund state is changed.",
            "No refund is executed, approved, or submitted.",
            "Simulated tracking remains demo-only and is not evidence about the real shipment.",
            "An authorized merchant must review the findings and choose the next action.",
        ],
    }
