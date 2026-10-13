"""Bounded, allowlisted read-only tools for ResolveOS investigation runs.

The planner is deterministic and data-aware. It selects a small sequence of
registered Python functions based on the case and persisted evidence. No user
or model-supplied tool name is ever executed. These tools never call external
services or mutate order, case, evidence, or payment state.
"""
from __future__ import annotations

from typing import Any, Callable

MAX_TOOL_CALLS = 5

ToolHandler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def _read_case_context(context: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    case = context["case"]
    issue = str(case.get("issue_type") or "OTHER").upper()
    status = str(case.get("status") or "UNKNOWN").upper()
    data = {
        "case_id": case.get("case_id"),
        "order_id": case.get("order_id"),
        "issue_type": issue,
        "status": status,
        "policy_decision": case.get("policy_decision"),
        "summary": str(case.get("summary") or "")[:500],
        "customer_message": str(case.get("customer_message") or "")[:3000],
    }
    return {
        "data": data,
        "label": "Read complaint and case state",
        "status": "COMPLETED",
        "result": f"Loaded case {data['case_id']} for order {data['order_id']}: {issue}; saved status {status}.",
    }


def _read_order_payment(context: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    order = context["order"]
    capture_linked = bool(str(order.get("paypal_capture_id") or "").strip())
    payment_status = str(order.get("payment_status") or "UNKNOWN").upper()
    data = {
        "order_id": order.get("order_id"),
        "item_name": order.get("item_name"),
        "amount": order.get("amount"),
        "currency": order.get("currency"),
        "shipping_status": str(order.get("shipping_status") or "UNKNOWN").upper(),
        "payment_status": payment_status,
        "capture_linked": capture_linked,
        "refund_status": str(order.get("refund_status") or "NONE").upper(),
    }
    status = "COMPLETED" if payment_status == "COMPLETED" and capture_linked else "ATTENTION_REQUIRED"
    result = (
        f"Payment is {payment_status}; a PayPal capture is {'linked' if capture_linked else 'not linked'}. "
        f"Shipping is {data['shipping_status']}."
    )
    return {"data": data, "label": "Inspect order and payment linkage", "status": status, "result": result}


def _read_evidence_history(context: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    records = context["db"].list_case_evidence(context["case"]["case_id"])
    latest = records[0] if records else None
    evidence = latest.get("evidence") if isinstance(latest, dict) and isinstance(latest.get("evidence"), dict) else {}
    verification = str((latest or {}).get("verification_status") or "NONE").upper()
    tracking_number = str(evidence.get("tracking_number") or "").strip()
    status = "ATTENTION_REQUIRED" if not records or verification != "VERIFIED" else "COMPLETED"
    if not records:
        summary = "No evidence has been submitted yet."
    else:
        summary = (
            f"Read {len(records)} evidence record(s); newest status is {verification}. "
            "A merchant review status does not independently authenticate the source."
        )
    data = {"records": records, "latest": latest, "verification_status": verification, "tracking_number": tracking_number}
    return {"data": data, "label": "Inspect submitted evidence and review status", "status": status, "result": summary}


def resolve_comparison_outcome(latest: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve a saved carrier-comparison outcome, including legacy snapshots.

    Older locally persisted snapshots may have comparison fields but no
    `overall_result`. Derive a safe descriptive label from those saved fields;
    never describe a simulation as real carrier evidence.
    """
    latest = latest if isinstance(latest, dict) else {}
    raw = latest.get("comparison") if isinstance(latest.get("comparison"), dict) else {}
    source_mode = str(latest.get("source_mode") or raw.get("source_mode") or "UNKNOWN").upper()
    provider = str(latest.get("provider") or "").upper()
    simulated = source_mode == "SIMULATED_DEMO" or "SIMULATED" in provider

    status_comparison = str(raw.get("shipping_status_comparison") or latest.get("shipping_status_comparison") or "").upper()
    date_comparison = str(raw.get("delivery_date_comparison") or latest.get("delivery_date_comparison") or "").upper()
    outcome = raw.get("overall_result") or latest.get("overall_result")
    origin = "SAVED" if isinstance(outcome, str) and outcome.strip() else "DERIVED_FROM_SAVED_FIELDS"

    if not isinstance(outcome, str) or not outcome.strip():
        has_difference = "DIFFERENT_REVIEW_REQUIRED" in {status_comparison, date_comparison}
        both_match = status_comparison == "MATCH" and date_comparison == "MATCH"
        if has_difference:
            outcome = "SIMULATED_DISCREPANCY" if simulated else "POTENTIAL_DISCREPANCY"
        elif both_match:
            outcome = "SIMULATED_NO_DIFFERENCE" if simulated else "NO_DIFFERENCE_DETECTED"
        elif simulated:
            outcome = "SIMULATED_INSUFFICIENT_DATA"
        else:
            outcome = "INSUFFICIENT_DATA"

    return {
        "overall_result": str(outcome),
        "outcome_origin": origin,
        "shipping_status_comparison": status_comparison or "NOT_RECORDED",
        "delivery_date_comparison": date_comparison or "NOT_RECORDED",
        "source_mode": source_mode,
        "is_simulated": simulated,
        "findings": raw.get("findings") if isinstance(raw.get("findings"), list) else latest.get("findings", []),
    }


def _read_tracking_comparisons(context: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    checks = context["db"].list_case_carrier_tracking_checks(context["case"]["case_id"])
    latest = checks[0] if checks else None
    resolved = resolve_comparison_outcome(latest)
    mode = resolved["source_mode"]
    comparison = latest.get("comparison") if isinstance(latest, dict) and isinstance(latest.get("comparison"), dict) else {}
    if not checks:
        status = "ACTION_REQUIRED"
        result = "No saved tracking comparison exists yet. The merchant may use Fetch / simulate tracking; demo mode is synthetic."
    else:
        outcome = resolved["overall_result"]
        if resolved["is_simulated"]:
            status = "SIMULATED_ONLY"
        else:
            status = "HUMAN_REVIEW_REQUIRED"
        origin_note = "" if resolved["outcome_origin"] == "SAVED" else " The outcome label was derived from saved status/date comparison fields because this snapshot did not store an overall_result."
        comparison_notes: list[str] = []
        if resolved["shipping_status_comparison"] == "DIFFERENT_REVIEW_REQUIRED":
            comparison_notes.append("status differs")
        if resolved["delivery_date_comparison"] == "DIFFERENT_REVIEW_REQUIRED":
            comparison_notes.append("delivery date differs")
        detail = f" Findings: {', '.join(comparison_notes)}." if comparison_notes else ""
        result = f"Latest saved snapshot {latest.get('check_id') or ''} came from {mode}; comparison outcome is {outcome}.{detail}{origin_note}"
        if resolved["is_simulated"]:
            result += " This is locally simulated data, not real shipment evidence."
        else:
            result += " Review the official source and timestamps manually; this outcome is not proof by itself."
    normalized_comparison = {**comparison, **resolved}
    return {
        "data": {
            "records": checks,
            "latest": latest,
            "source_mode": mode,
            "comparison": normalized_comparison,
            "comparison_outcome": resolved["overall_result"],
            "comparison_outcome_origin": resolved["outcome_origin"],
            "is_simulated": resolved["is_simulated"],
        },
        "label": "Inspect saved carrier comparison",
        "status": status,
        "result": result,
    }


def _preview_deterministic_policy(context: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    case = context["case"]
    order = context["order"]
    preview_decision, preview_reasons = context["evaluate_policy"](
        {"issue_type": str(case.get("issue_type") or "OTHER").upper()}, order
    )
    data = {"decision": preview_decision, "reasons": preview_reasons}
    return {
        "data": data,
        "label": "Preview deterministic policy (read-only)",
        "status": "READ_ONLY_PREVIEW",
        "result": f"Current order inputs produce preview decision {preview_decision}; saved case decision remains {case.get('policy_decision') or 'UNKNOWN'}.",
    }


# The registry is deliberately static. No dynamic import, eval, arbitrary URL,
# generated Python, or model-controlled function names are accepted.
READ_ONLY_TOOL_REGISTRY: dict[str, ToolHandler] = {
    "read_case_context": _read_case_context,
    "read_order_payment": _read_order_payment,
    "read_evidence_history": _read_evidence_history,
    "read_tracking_comparisons": _read_tracking_comparisons,
    "preview_deterministic_policy": _preview_deterministic_policy,
}


def execute_read_only_tool_plan(
    case: dict[str, Any],
    order: dict[str, Any],
    *,
    db_module: Any,
    evaluate_policy: Callable[[dict[str, Any], dict[str, Any]], tuple[str, list[str]]],
) -> dict[str, Any]:
    """Execute a data-aware plan of at most five registered read-only tools.

    Baseline reads establish case/order/evidence context. Follow-up tool choice
    is then based on those results and on whether a refund is already pending,
    completed, or in reconciliation. No external service or payment tool is
    present in the registry.
    """
    context = {"case": case, "order": order, "db": db_module, "evaluate_policy": evaluate_policy}
    results: dict[str, Any] = {}
    steps: list[dict[str, Any]] = []
    planner_trace: list[dict[str, str]] = []
    executed_tools: list[str] = []

    def execute(tool_name: str) -> dict[str, Any]:
        if tool_name not in READ_ONLY_TOOL_REGISTRY:
            raise ValueError(f"Tool is not allowlisted: {tool_name}")
        if len(executed_tools) >= MAX_TOOL_CALLS:
            raise RuntimeError(f"Investigation tool-call limit of {MAX_TOOL_CALLS} was reached")
        output = READ_ONLY_TOOL_REGISTRY[tool_name](context, results)
        results[tool_name] = output.get("data")
        executed_tools.append(tool_name)
        steps.append({
            "tool": tool_name,
            "label": str(output.get("label") or tool_name),
            "status": str(output.get("status") or "COMPLETED"),
            "result": str(output.get("result") or "Tool completed without a summary."),
            "call_number": len(executed_tools),
        })
        return output

    # Stage 1: minimum baseline context. Results from each step feed the planner.
    for tool_name in ("read_case_context", "read_order_payment", "read_evidence_history"):
        execute(tool_name)
        planner_trace.append({
            "event": "TOOL_SELECTED_AND_EXECUTED",
            "tool": tool_name,
            "reason": "Required baseline context to avoid planning from an incomplete case view.",
        })

    issue = str((results.get("read_case_context") or {}).get("issue_type") or "OTHER").upper()
    order_result = results.get("read_order_payment") or {}
    evidence_result = results.get("read_evidence_history") or {}
    payment_status = str(order_result.get("payment_status") or "UNKNOWN").upper()
    capture_linked = bool(order_result.get("capture_linked"))
    latest_evidence_status = str(evidence_result.get("verification_status") or "NONE").upper()
    has_tracking_number = bool(str(evidence_result.get("tracking_number") or "").strip())

    protected_statuses = {
        "REFUND_SUBMITTING", "REFUND_PENDING", "REFUND_COMPLETED",
        "REFUND_REQUIRES_RECONCILIATION",
    }
    refund_status = str(
        case.get("refund_status") or order_result.get("refund_status") or "NONE"
    ).upper()
    payment_locked = (
        str(case.get("status") or "").upper() in protected_statuses
        or refund_status in {"PENDING", "COMPLETED"}
        or bool(case.get("paypal_refund_id"))
    )

    # Stage 2: choose whether tracking inspection is useful and safe for this run.
    should_inspect_tracking = issue == "ITEM_NOT_RECEIVED" or has_tracking_number
    if payment_locked:
        planner_trace.append({
            "event": "TOOL_SKIPPED",
            "tool": "read_tracking_comparisons",
            "reason": "A refund is pending, completed, or requires reconciliation. Avoid generating a new investigative recommendation that could conflict with that protected payment lifecycle; inspect the existing refund/audit state first.",
        })
    elif should_inspect_tracking:
        reasons = []
        if issue == "ITEM_NOT_RECEIVED":
            reasons.append("the complaint concerns a missing item")
        if has_tracking_number:
            reasons.append("the latest evidence has a tracking number")
        reason = "Tracking inspection selected because " + " and ".join(reasons) + "."
        tool_output = execute("read_tracking_comparisons")
        planner_trace.append({
            "event": "CONDITIONAL_TOOL_SELECTED",
            "tool": "read_tracking_comparisons",
            "reason": reason,
        })
        track_data = tool_output.get("data") if isinstance(tool_output.get("data"), dict) else {}
        if not track_data.get("records"):
            planner_trace.append({
                "event": "NEXT_STEP_DERIVED_FROM_RESULT",
                "tool": "read_tracking_comparisons",
                "reason": "No saved carrier snapshot exists. The next useful step is to request/record tracking evidence or run an explicitly labelled demo lookup; the orchestrator did not call a carrier API.",
            })
        else:
            outcome = str(track_data.get("comparison_outcome") or "INSUFFICIENT_DATA")
            origin = str(track_data.get("comparison_outcome_origin") or "UNKNOWN")
            simulated = bool(track_data.get("is_simulated"))
            if "DISCREPANCY" in outcome or outcome == "POTENTIAL_DISCREPANCY":
                reason = (
                    f"Saved comparison outcome is {outcome} (source={origin}). "
                    + ("The snapshot is simulated, not real shipment evidence; treat it only as a UI test. " if simulated else "Treat this as a review signal, not proof. ")
                    + "Recommend manually checking the official source; do not update shipping or payment state."
                )
                event = "COMPARISON_DIFFERENCE_FOUND"
            elif simulated:
                reason = f"Saved outcome is {outcome}, but the snapshot is synthetic and cannot establish real shipment facts."
                event = "SIMULATED_RESULT_NOT_PROOF"
            elif outcome in {"NO_DIFFERENCE_DETECTED", "SIMULATED_NO_DIFFERENCE"}:
                reason = "No difference was recorded in the compared fields; this does not authenticate either source or prove delivery."
                event = "NO_DIFFERENCE_NOT_AUTHENTICATION"
            else:
                reason = f"Saved outcome is {outcome}; relevant fields are missing or not comparable, so request/inspect additional source evidence."
                event = "COMPARISON_INSUFFICIENT_DATA"
            planner_trace.append({
                "event": event,
                "tool": "read_tracking_comparisons",
                "reason": reason,
            })
    else:
        planner_trace.append({
            "event": "TOOL_SKIPPED",
            "tool": "read_tracking_comparisons",
            "reason": f"Issue type is {issue}, no tracking number was found in the latest evidence, and the complaint does not require a delivery investigation. Tracking inspection would not add relevant evidence to this plan.",
        })

    # Stage 3: policy preview is read-only and always included, so the report
    # ends with the deterministic decision without persisting it to the case.
    policy_output = execute("preview_deterministic_policy")
    planner_trace.append({
        "event": "TOOL_SELECTED_AND_EXECUTED",
        "tool": "preview_deterministic_policy",
        "reason": (
            f"Read-only policy preview selected after checking payment status={payment_status}, "
            f"capture_linked={capture_linked}, latest_evidence={latest_evidence_status}, "
            f"refund_locked={payment_locked}. No saved case or payment state is changed."
        ),
    })

    return {
        "steps": steps,
        "results": results,
        "planner_trace": planner_trace,
        "tools_performed": list(executed_tools),
        "max_tool_calls": MAX_TOOL_CALLS,
        "tool_call_count": len(executed_tools),
        "allowlisted_tools": sorted(READ_ONLY_TOOL_REGISTRY),
        "mode": "Adaptive bounded read-only tool-using agent",
        "planning_summary": {
            "issue_type": issue,
            "payment_status": payment_status,
            "capture_linked": capture_linked,
            "latest_evidence_status": latest_evidence_status,
            "tracking_number_available": has_tracking_number,
            "refund_lifecycle_locked": payment_locked,
            "policy_preview_decision": (policy_output.get("data") or {}).get("decision"),
        },
    }

