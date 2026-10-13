import html
import json
import os
import re
from pathlib import Path
import secrets
import uuid
from datetime import date, datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import db
from .agent import classify_complaint, evaluate_policy
from .carrier_tracking import CarrierTrackingError, lookup_tracking, simulate_tracking
from .investigation_orchestrator import build_investigation_run
from .investigation_tools import execute_read_only_tool_plan
from .follow_up_actions import build_follow_up_action_plan, validate_action_completion
from .customer_message_drafts import build_customer_message_draft
from .resolution_closure import build_resolution_summary, validate_closure, status_for_closure, CLOSURE_OUTCOMES
from .evidence_integrity import historical_verification_problem
from .workflow_coordinator import build_end_to_end_workflow
from .paypal_client import (
    PayPalError, capture_checkout_order, create_checkout_order, get_capture,
    get_checkout_order, refund_capture,
)

load_dotenv()
db.init_db()
app = FastAPI(title="ResolveOS API", version="0.1.0", description="AI-assisted payment support and human-approved PayPal sandbox refunds.")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_evidence_investigation(
    issue_type: str,
    order: dict[str, Any],
    policy_decision: str,
    reasons: list[str] | None = None,
    evidence_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a transparent, policy-guided evidence checklist; this never authorizes payment."""
    issue = (issue_type or "OTHER").upper()
    shipping = str(order.get("shipping_status") or "UNKNOWN").upper()
    has_capture = bool(order.get("paypal_capture_id"))
    refund_status = str(order.get("refund_status") or "NONE").upper()
    latest_evidence: dict[str, Any] = {}
    # Records are newest first. Apply oldest to newest so the latest non-empty
    # submission for each field is the one used for recommendations.
    for record in reversed(evidence_records or []):
        values = record.get("evidence") or {}
        if isinstance(values, dict):
            for key, value in values.items():
                if value is not None and (not isinstance(value, str) or value.strip()):
                    latest_evidence[key] = value

    evidence_by_issue = {
        "ITEM_NOT_RECEIVED": [
            "Carrier tracking number and the latest timestamped scan.",
            "Promised delivery date and the delivery address on the order.",
            "Carrier investigation or loss confirmation if the parcel is overdue.",
        ],
        "WRONG_ITEM": [
            "Customer photos of the received item and its packaging/label.",
            "Order confirmation, SKU, packing slip, and item actually shipped.",
            "Merchant return/replacement policy and any return tracking.",
        ],
        "DUPLICATE_CHARGE": [
            "PayPal activity records for both suspected charges.",
            "Distinct capture/transaction IDs, timestamps, amounts, and currencies.",
            "Confirm whether one entry is only an authorization or a completed capture.",
        ],
        "REFUND_NOT_RECEIVED": [
            "Existing PayPal refund ID and its latest status.",
            "Original capture ID and the refund amount/currency.",
            "PayPal activity details and the date the refund was initiated.",
        ],
        "OTHER": [
            "Order confirmation and the customer's requested resolution.",
            "Relevant messages, screenshots, receipts, or transaction references.",
            "Clarify the specific problem and the date it occurred.",
        ],
    }
    checklist = list(evidence_by_issue.get(issue, evidence_by_issue["OTHER"]))
    if issue == "ITEM_NOT_RECEIVED":
        if latest_evidence.get("tracking_number") and latest_evidence.get("latest_carrier_scan"):
            checklist = [
                "Verify the submitted tracking number and latest timestamped scan against the carrier's official tracking page."
                if item == "Carrier tracking number and the latest timestamped scan." else item
                for item in checklist
            ]
        if latest_evidence.get("expected_delivery_date") and latest_evidence.get("delivery_address_confirmed") is not None:
            checklist = [
                "Verify the submitted delivery date and confirm the delivery address matches the order."
                if item == "Promised delivery date and the delivery address on the order." else item
                for item in checklist
            ]
        if latest_evidence.get("carrier_investigation"):
            checklist = [
                "Corroborate the submitted carrier investigation/loss notes with the carrier's own records."
                if item == "Carrier investigation or loss confirmation if the parcel is overdue." else item
                for item in checklist
            ]
    if not has_capture:
        checklist.insert(0, "Verify and link a completed PayPal capture that matches this order's amount and currency.")
    if issue == "ITEM_NOT_RECEIVED" and shipping == "IN_TRANSIT" and latest_evidence.get("tracking_number") and latest_evidence.get("latest_carrier_scan"):
        next_action = "Verify the submitted tracking number and timestamped scan against the carrier's official tracking page; keep the case open while the order remains in transit."
        explanation = "The submitted evidence is recorded but not independently verified. Merchant-entered tracking details do not change the order's shipping state or qualify the case for an automatic refund."
    elif issue == "ITEM_NOT_RECEIVED" and shipping == "IN_TRANSIT":
        next_action = "Request the latest carrier scan and expected delivery date; keep the case open while the shipment is in transit."
        explanation = "An in-transit status does not establish that the item is lost or undelivered permanently. Do not treat this as refund approval."
    elif issue == "ITEM_NOT_RECEIVED" and shipping == "DELIVERED":
        next_action = "Ask the customer to confirm the delivery address and review proof of delivery with the carrier."
        explanation = "Tracking reports delivery, so delivery evidence needs human review before deciding the complaint."
    elif policy_decision == "AWAITING_CAPTURE_LINK":
        next_action = "Verify the PayPal Sandbox capture and link it to the matching order before considering any payment action."
        explanation = "The policy requires a verified completed capture linked to this order; the evidence checklist cannot bypass that requirement."
    elif policy_decision == "REFUND_CANDIDATE":
        next_action = "Review the evidence, then have an authorized merchant explicitly approve the eligible Sandbox refund."
        explanation = "This is only a policy candidate. Backend checks and merchant authorization remain mandatory before a PayPal refund."
    elif policy_decision == "NO_NEW_REFUND" or refund_status == "REFUNDED":
        next_action = "Check the existing refund record and PayPal status; do not submit another refund."
        explanation = "The order is already marked refunded, so a second refund must not be initiated."
    elif issue == "DUPLICATE_CHARGE":
        next_action = "Compare distinct completed PayPal captures and escalate for human review if two charges are verified."
        explanation = "A duplicate charge cannot be established from the complaint text alone; separate payment records are needed."
    elif issue == "REFUND_NOT_RECEIVED":
        next_action = "Inspect the existing refund ID/status and PayPal activity before deciding whether escalation is needed."
        explanation = "A missing refund report is not a reason to issue a second refund blindly."
    elif issue == "WRONG_ITEM":
        next_action = "Collect item/packaging photos and compare the received SKU with the order and packing records."
        explanation = "Eligibility depends on evidence and the merchant's policy; this case requires human review."
    else:
        next_action = "Collect the missing evidence and route the case to a merchant reviewer."
        explanation = "The current policy result does not authorize an automatic payment action."

    return {
        "title": "Evidence Investigator",
        "mode": "Policy-guided recommendations",
        "checklist": checklist,
        "recommended_next_action": next_action,
        "explanation": explanation,
        "guardrails": [
            "Recommendations only — no payment action is executed by the investigator.",
            "Refunds require a verified completed PayPal capture, amount/currency checks, and merchant authorization.",
        ],
    }


def ai_status_from_classification(classification: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": classification.get("_ai_provider") or "Local rules fallback",
        "model": classification.get("_ai_model"),
        "fallback": bool(classification.get("_ai_fallback", True)),
        "fallback_reason": classification.get("_ai_fallback_reason"),
    }


def ai_status_from_audit(audit_log: list[dict[str, Any]]) -> dict[str, Any]:
    for event in audit_log:
        details = event.get("details") or {}
        status = details.get("ai_status")
        if isinstance(status, dict):
            return status
    return {"provider": "Legacy case", "model": None, "fallback": True, "fallback_reason": "AI status was not recorded for this case"}


class CreateCaseRequest(BaseModel):
    order_id: str = Field(min_length=4, max_length=40)
    customer_message: str = Field(min_length=5, max_length=3000)


class LinkCaptureRequest(BaseModel):
    capture_id: str = Field(min_length=5, max_length=80)


class CheckoutCaptureRequest(BaseModel):
    paypal_order_id: str = Field(min_length=5, max_length=80)


class EvidenceSubmissionRequest(BaseModel):
    tracking_number: str | None = Field(default=None, max_length=120)
    latest_carrier_scan: str | None = Field(default=None, max_length=1500)
    expected_delivery_date: str | None = Field(default=None, max_length=80)
    delivery_address_confirmed: bool | None = None
    carrier_investigation: str | None = Field(default=None, max_length=1500)
    evidence_notes: str | None = Field(default=None, max_length=3000)
    source_reference: str | None = Field(default=None, max_length=500)
    carrier_slug: str | None = Field(default=None, max_length=80, pattern=r"^[a-z0-9-]+$")


class EvidenceReviewRequest(BaseModel):
    decision: Literal["VERIFIED", "REJECTED"]
    review_note: str = Field(min_length=5, max_length=1000)


class EvidenceVerificationRevocationRequest(BaseModel):
    reason: str = Field(min_length=12, max_length=1000)


class FollowUpActionUpdateRequest(BaseModel):
    status: Literal["IN_PROGRESS", "COMPLETED"]
    note: str = Field(min_length=5, max_length=1000)


class CustomerMessageDraftEditRequest(BaseModel):
    subject: str = Field(min_length=4, max_length=180)
    body: str = Field(min_length=10, max_length=5000)


class CloseCaseRequest(BaseModel):
    closure_outcome: Literal[
        "RESOLVED_REFUND_COMPLETED", "RESOLVED_NO_REFUND", "ESCALATED",
        "CUSTOMER_WITHDREW", "DUPLICATE_CASE",
    ]
    closure_note: str = Field(min_length=12, max_length=2000)
    summary_id: str | None = Field(default=None, max_length=24)


def require_admin(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> str:
    expected = os.getenv("ADMIN_API_KEY", "").strip()
    if not expected or expected.startswith("replace-with-"):
        raise HTTPException(status_code=503, detail="Set a strong ADMIN_API_KEY in backend/.env before using merchant-only endpoints.")
    if not x_admin_key or not secrets.compare_digest(x_admin_key, expected):
        raise HTTPException(status_code=401, detail="Valid X-Admin-Key header required for this prototype merchant action.")
    return "merchant_admin"


def public_case(case: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in case.items() if k not in {"paypal_request_id"}}


def ensure_case_not_closed(case: dict[str, Any]) -> None:
    """Prevent ordinary case mutations after an explicit merchant closeout."""
    status = str(case.get("status") or "").upper()
    if status.startswith("CLOSED_") or status == "CLOSED_NO_ACTION":
        raise HTTPException(status_code=409, detail="This case is closed. Reopening is not supported by this workflow.")


@app.get("/checkout/{order_id}", response_class=HTMLResponse)
def sandbox_checkout_page(order_id: str) -> HTMLResponse:
    # A minimal Sandbox checkout page; amount comes from the server-side demo order.
    order = db.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    client_id = os.getenv("PAYPAL_CLIENT_ID", "").strip()
    if not client_id or client_id.startswith("your_"):
        raise HTTPException(status_code=503, detail="Set PayPal Sandbox credentials in backend/.env first.")
    if order.get("paypal_capture_id"):
        raise HTTPException(status_code=409, detail="This demo order already has a linked capture.")

    template = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ResolveOS Sandbox Checkout</title>
  <style>
    body{font-family:system-ui,sans-serif;max-width:680px;margin:48px auto;padding:0 18px;color:#17202a}
    main{border:1px solid #d9dee7;border-radius:16px;padding:24px}
    .muted{color:#566573}.amount{font-size:1.7rem;font-weight:700;margin:12px 0 24px}
    #status{margin-top:18px;overflow-wrap:anywhere}
  </style>
  <script src="https://www.paypal.com/sdk/js?client-id=__CLIENT_ID__&currency=__CURRENCY__&intent=capture"></script>
</head>
<body><main>
  <h1>ResolveOS Sandbox Checkout</h1>
  <p class="muted">This is a test payment using the PayPal Sandbox. No real money should move.</p>
  <h2>__ITEM_NAME__</h2>
  <div class="amount">__AMOUNT__ __CURRENCY__</div>
  <div id="paypal-button-container"></div>
  <p id="status" role="status">Choose PayPal to approve the Sandbox payment.</p>
</main>
<script>
const ORDER_ID = __ORDER_ID_JSON__;
const BASE_URL = "/checkout/" + encodeURIComponent(ORDER_ID);
const statusBox = document.getElementById("status");
paypal.Buttons({
  createOrder: async function () {
    statusBox.textContent = "Creating Sandbox order…";
    const response = await fetch(BASE_URL + "/create", { method: "POST" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Could not create PayPal order.");
    return result.paypal_order_id;
  },
  onApprove: async function (data) {
    statusBox.textContent = "Buyer approved. Capturing payment…";
    const response = await fetch(BASE_URL + "/capture", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paypal_order_id: data.orderID })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Capture failed.");
    statusBox.textContent = "Sandbox capture verified and linked! Capture ID: " + result.capture_id + ". You may now close this page and return to ResolveOS.";
  },
  onCancel: function () { statusBox.textContent = "Payment cancelled. No capture was linked."; },
  onError: function (error) {
    console.error(error);
    statusBox.textContent = "Checkout error: " + (error && error.message ? error.message : "See the backend terminal for details.");
  }
}).render("#paypal-button-container");
</script>
</body></html>'''
    page = (template
            .replace("__CLIENT_ID__", html.escape(client_id, quote=True))
            .replace("__CURRENCY__", html.escape(order["currency"], quote=True))
            .replace("__ITEM_NAME__", html.escape(order["item_name"]))
            .replace("__AMOUNT__", html.escape(str(order["amount"])))
            .replace("__ORDER_ID_JSON__", json.dumps(order_id)))
    return HTMLResponse(page)


@app.post("/checkout/{order_id}/create")
def create_sandbox_checkout(order_id: str) -> dict[str, Any]:
    order = db.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    if order.get("paypal_capture_id"):
        raise HTTPException(status_code=409, detail="This demo order already has a linked capture.")
    try:
        result = create_checkout_order(order_id, order["item_name"], order["amount"], order["currency"])
    except PayPalError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"order_id": order_id, "paypal_order_id": result["id"], "paypal_status": result.get("status")}


@app.post("/checkout/{order_id}/capture")
def capture_sandbox_checkout(order_id: str, body: CheckoutCaptureRequest) -> dict[str, Any]:
    order = db.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    if order.get("paypal_capture_id"):
        raise HTTPException(status_code=409, detail="This order already has a linked capture; refusing a second capture.")

    # Verify the PayPal order belongs to this local order before capturing it.
    try:
        paypal_order = get_checkout_order(body.paypal_order_id)
    except PayPalError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    units = paypal_order.get("purchase_units") or []
    matching_unit = next((u for u in units if u.get("custom_id") == order_id and u.get("reference_id") == order_id), None)
    if not matching_unit:
        raise HTTPException(status_code=409, detail="This PayPal order was not created for this ResolveOS order.")
    expected_amount = matching_unit.get("amount") or {}
    if str(expected_amount.get("value")) != str(order["amount"]) or expected_amount.get("currency_code") != order["currency"]:
        raise HTTPException(status_code=409, detail="PayPal checkout amount/currency does not match the demo order.")
    if paypal_order.get("status") != "APPROVED":
        raise HTTPException(status_code=409, detail=f"PayPal order must be APPROVED by the buyer first; current status is {paypal_order.get('status')}.")

    try:
        capture_response = capture_checkout_order(body.paypal_order_id)
    except PayPalError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if capture_response.get("status") != "COMPLETED":
        raise HTTPException(status_code=409, detail=f"PayPal checkout capture is not completed: {capture_response.get('status')}.")
    capture_units = capture_response.get("purchase_units") or []
    capture_list = []
    for unit in capture_units:
        capture_list.extend(((unit.get("payments") or {}).get("captures") or []))
    completed_capture = next((c for c in capture_list if c.get("status") == "COMPLETED"), None)
    if not completed_capture or not completed_capture.get("id"):
        raise HTTPException(status_code=502, detail="PayPal order completed but no completed capture ID was returned. Check Sandbox activity before retrying.")
    capture_id = completed_capture["id"]

    # Independently fetch the capture as the payment source of truth.
    try:
        verified_capture = get_capture(capture_id)
    except PayPalError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    capture_amount = verified_capture.get("amount") or {}
    if verified_capture.get("status") != "COMPLETED":
        raise HTTPException(status_code=409, detail="PayPal capture did not verify as COMPLETED; order was not linked.")
    if str(capture_amount.get("value")) != str(order["amount"]) or capture_amount.get("currency_code") != order["currency"]:
        raise HTTPException(status_code=409, detail="Verified capture amount/currency does not match the order; capture was not linked.")

    db.link_capture(order_id, capture_id)
    db.log_audit(f"ORDER-{order_id}", "PAYPAL_SANDBOX", "PAYPAL_CHECKOUT_CAPTURED_AND_LINKED", {
        "order_id": order_id, "paypal_order_id": body.paypal_order_id,
        "capture_id": capture_id, "capture_status": verified_capture.get("status"),
        "amount": capture_amount,
    })
    return {"linked": True, "order_id": order_id, "paypal_order_id": body.paypal_order_id,
            "capture_id": capture_id, "capture_status": verified_capture.get("status"),
            "amount": capture_amount}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "ResolveOS", "mode": "paypal-sandbox-only"}


@app.get("/dashboard", response_class=HTMLResponse)
def merchant_dashboard() -> HTMLResponse:
    """Serve the local merchant dashboard from the same origin as the API."""
    dashboard_path = Path(__file__).with_name("dashboard.html")
    if not dashboard_path.exists():
        raise HTTPException(status_code=404, detail="dashboard.html is missing from backend/app.")
    return HTMLResponse(dashboard_path.read_text(encoding="utf-8"))


@app.get("/cases")
def list_support_cases() -> list[dict[str, Any]]:
    """Return recent demo cases for the merchant dashboard."""
    output = []
    for case in db.list_cases():
        case["order"] = db.get_order(case["order_id"])
        output.append(public_case(case))
    return output


@app.get("/orders")
def orders() -> list[dict[str, Any]]:
    """Seeded order/shipping rows are synthetic demo evidence, not PayPal records."""
    return db.list_orders()


@app.post("/orders/{order_id}/link-capture")
def link_paypal_capture(order_id: str, body: LinkCaptureRequest, _actor: str = Depends(require_admin)) -> dict[str, Any]:
    order = db.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    try:
        capture = get_capture(body.capture_id)
    except PayPalError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    amount = (capture.get("amount") or {})
    if capture.get("status") != "COMPLETED":
        raise HTTPException(status_code=409, detail=f"Capture must have COMPLETED status; current status is {capture.get('status')}.")
    if str(amount.get("value")) != str(order["amount"]) or amount.get("currency_code") != order["currency"]:
        raise HTTPException(status_code=409, detail="Capture amount/currency does not match this demo order. Use a matching Sandbox capture.")

    db.link_capture(order_id, body.capture_id)
    db.log_audit("SYSTEM-LINK", _actor, "PAYPAL_CAPTURE_LINKED", {"order_id": order_id, "capture_id": body.capture_id, "status": capture.get("status")})
    return {"linked": True, "order_id": order_id, "capture_id": body.capture_id, "paypal_status": capture.get("status"), "amount": amount}


@app.post("/cases")
def create_support_case(body: CreateCaseRequest) -> dict[str, Any]:
    order = db.get_order(body.order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Unknown order_id. Try one of the seeded orders returned by GET /orders.")

    # When a capture is linked, fetch its current state from PayPal rather than trusting local state.
    paypal_note = None
    if order.get("paypal_capture_id"):
        try:
            capture = get_capture(order["paypal_capture_id"])
            order["payment_status"] = capture.get("status", order["payment_status"])
            amount = capture.get("amount") or {}
            if str(amount.get("value")) != str(order["amount"]) or amount.get("currency_code") != order["currency"]:
                raise HTTPException(status_code=409, detail="Linked PayPal capture no longer matches order amount/currency.")
        except PayPalError as exc:
            paypal_note = str(exc)

    classification = classify_complaint(body.customer_message, order)
    decision, reasons = evaluate_policy(classification, order)
    case_id = f"CASE-{uuid.uuid4().hex[:10].upper()}"
    status_by_decision = {
        "REFUND_CANDIDATE": "AWAITING_MERCHANT_APPROVAL",
        "AWAITING_CAPTURE_LINK": "AWAITING_CAPTURE_LINK",
        "NEEDS_MORE_INFORMATION": "NEEDS_MORE_INFORMATION",
        "NEEDS_HUMAN_REVIEW": "NEEDS_HUMAN_REVIEW",
        "NO_NEW_REFUND": "CLOSED_NO_ACTION",
    }
    case = {
        "case_id": case_id,
        "order_id": body.order_id,
        "customer_message": body.customer_message,
        "issue_type": classification["issue_type"],
        "summary": classification["summary"],
        "policy_decision": decision,
        "reasons": reasons,
        "status": status_by_decision.get(decision, "NEEDS_HUMAN_REVIEW"),
        "confidence": classification["confidence"],
        "paypal_request_id": None,
        "paypal_refund_id": None,
        "refund_status": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    ai_status = ai_status_from_classification(classification)
    evidence_investigation = build_evidence_investigation(case["issue_type"], order, decision, reasons)
    db.create_case(case)
    db.log_audit(case_id, "AI_AGENT", "CASE_ANALYZED", {
        "issue_type": case["issue_type"],
        "policy_decision": decision,
        "reasons": reasons,
        "ai_status": ai_status,
    })
    result = {**public_case(case), "order": order,
              "ai_status": ai_status, "evidence_records": [],
              "evidence_investigation": evidence_investigation}
    if paypal_note:
        result["paypal_lookup_warning"] = paypal_note
    return result


@app.get("/cases/{case_id}")
def get_support_case(case_id: str) -> dict[str, Any]:
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    case["order"] = db.get_order(case["order_id"])
    case["audit_log"] = db.get_audit(case_id)
    case["evidence_records"] = db.list_case_evidence(case_id)
    case["carrier_tracking_checks"] = db.list_case_carrier_tracking_checks(case_id)
    case["follow_up_actions"] = db.list_case_follow_up_actions(case_id)
    case["customer_message_drafts"] = db.list_case_customer_message_drafts(case_id)
    case["resolution_summaries"] = db.list_case_resolution_summaries(case_id)
    audit_log = case["audit_log"] if isinstance(case["audit_log"], list) else []
    case["ai_status"] = ai_status_from_audit(audit_log)
    case["evidence_investigation"] = build_evidence_investigation(
        case.get("issue_type", "OTHER"),
        case["order"] or {},
        case.get("policy_decision", "NEEDS_HUMAN_REVIEW"),
        case.get("reasons") if isinstance(case.get("reasons"), list) else [],
        case["evidence_records"],
    )
    return public_case(case)


@app.post("/cases/{case_id}/customer-message-drafts")
def create_customer_message_draft(
    case_id: str,
    actor: str = Depends(require_admin),
) -> dict[str, Any]:
    """Generate and persist an issue-specific message draft; never sends it."""
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    ensure_case_not_closed(case)
    order = db.get_order(str(case.get("order_id") or ""))
    if not order:
        raise HTTPException(status_code=404, detail="The order linked to this case no longer exists.")

    draft = build_customer_message_draft(case, order)
    saved = db.create_case_customer_message_draft(
        case_id,
        issue_type=draft["issue_type"],
        draft_type=draft["draft_type"],
        subject=draft["subject"],
        body=draft["body"],
        created_by=actor,
    )
    db.log_audit(case_id, actor, "CUSTOMER_MESSAGE_DRAFT_CREATED", {
        "draft_id": saved["draft_id"],
        "draft_type": saved["draft_type"],
        "issue_type": saved["issue_type"],
        "status": "DRAFT",
        "note": "Customer communication draft was generated and saved. It was not sent.",
    })
    return {
        "draft": saved,
        "message": "Customer follow-up draft generated and saved. It has not been sent.",
    }


@app.put("/cases/{case_id}/customer-message-drafts/{draft_id}")
def edit_customer_message_draft(
    case_id: str,
    draft_id: str,
    body: CustomerMessageDraftEditRequest,
    actor: str = Depends(require_admin),
) -> dict[str, Any]:
    """Save merchant edits to an unsent draft. No external message is sent."""
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    ensure_case_not_closed(case)
    draft = db.get_case_customer_message_draft(case_id, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Customer message draft not found.")
    if draft.get("status") != "DRAFT":
        raise HTTPException(status_code=409, detail="Only unsent drafts can be edited.")

    subject = body.subject.strip()
    message_body = body.body.strip()
    if len(subject) < 4 or len(message_body) < 10:
        raise HTTPException(status_code=422, detail="Subject must be at least 4 characters and message body at least 10 characters.")
    if not db.update_case_customer_message_draft(
        case_id, draft_id, subject=subject, body=message_body, updated_by=actor
    ):
        raise HTTPException(status_code=409, detail="Draft changed concurrently. Refresh and try again.")
    updated = db.get_case_customer_message_draft(case_id, draft_id)
    db.log_audit(case_id, actor, "CUSTOMER_MESSAGE_DRAFT_EDITED", {
        "draft_id": draft_id,
        "status": "DRAFT",
        "subject_length": len(subject),
        "body_length": len(message_body),
        "note": "Merchant edited the draft. It remains unsent.",
    })
    return {
        "draft": updated,
        "message": "Draft edits saved. The message has not been sent.",
    }


@app.post("/cases/{case_id}/resolution-summary")
def generate_resolution_summary(
    case_id: str,
    actor: str = Depends(require_admin),
) -> dict[str, Any]:
    """Build and persist a deterministic closeout summary from saved case history."""
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    ensure_case_not_closed(case)
    order = db.get_order(case.get("order_id", ""))
    if not order:
        raise HTTPException(status_code=404, detail="The order linked to this case no longer exists.")
    summary_data = build_resolution_summary(
        case=case,
        order=order,
        evidence_records=db.list_case_evidence(case_id),
        tracking_checks=db.list_case_carrier_tracking_checks(case_id),
        follow_up_actions=db.list_case_follow_up_actions(case_id),
        message_drafts=db.list_case_customer_message_drafts(case_id),
        audit_log=db.get_audit(case_id),
        generated_at=now_iso(),
    )
    saved = db.create_case_resolution_summary(case_id, summary_data, created_by=actor)
    db.log_audit(case_id, actor, "RESOLUTION_SUMMARY_GENERATED", {
        "summary_id": saved["summary_id"],
        "closure_readiness": summary_data["closure_readiness"]["status"],
        "blocker_count": len(summary_data["closure_readiness"]["blockers"]),
        "note": "Resolution summary compiled from saved local records. Case status, policy, evidence, order and payment were not changed.",
    })
    return {
        "summary_record": saved,
        "case": get_support_case(case_id),
        "message": "Resolution summary generated and saved. The case remains open; no payment action was taken.",
    }


@app.get("/cases/{case_id}/resolution-summary")
def get_resolution_summary(case_id: str) -> dict[str, Any]:
    """Read the latest saved resolution summary, if present."""
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    summaries = db.list_case_resolution_summaries(case_id)
    if not summaries:
        raise HTTPException(status_code=404, detail="No resolution summary has been generated for this case yet.")
    return {"case_id": case_id, "latest_summary": summaries[0], "summaries": summaries}


@app.post("/cases/{case_id}/close")
def close_case(
    case_id: str,
    body: CloseCaseRequest,
    actor: str = Depends(require_admin),
) -> dict[str, Any]:
    """Explicit merchant closeout. Never calls PayPal or alters order/payment state."""
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    ensure_case_not_closed(case)
    note = body.closure_note.strip()
    if len(note) < 12:
        raise HTTPException(status_code=422, detail="Add a meaningful closure note of at least 12 characters.")
    summaries = db.list_case_resolution_summaries(case_id)
    if not summaries:
        raise HTTPException(status_code=409, detail="Generate and review a resolution summary before closing the case.")
    summary_id = body.summary_id or summaries[0]["summary_id"]
    if summary_id != summaries[0]["summary_id"]:
        raise HTTPException(status_code=409, detail="Use the latest saved resolution summary to close this case. Generate a fresh summary if the case changed.")
    summary_record = db.get_case_resolution_summary(case_id, summary_id)
    if not summary_record:
        raise HTTPException(status_code=404, detail="Resolution summary not found for this case.")
    if summary_record.get("status") != "DRAFT":
        raise HTTPException(status_code=409, detail="Only an open resolution summary can be used for case closure.")
    # Do not close from a stale snapshot after investigation inputs have changed.
    stale_actions = {
        "CASE_REEVALUATED", "EVIDENCE_SUBMITTED", "EVIDENCE_REVIEWED", "EVIDENCE_VERIFICATION_REVOKED",
        "CARRIER_TRACKING_SIMULATED", "CARRIER_TRACKING_LOOKED_UP",
        "FOLLOW_UP_ACTION_UPDATED", "FOLLOW_UP_PLAN_REFRESHED",
        "INVESTIGATION_ORCHESTRATED", "CUSTOMER_MESSAGE_DRAFT_CREATED",
        "CUSTOMER_MESSAGE_DRAFT_EDITED",
    }
    snapshot_created_at = str(summary_record.get("created_at") or "")
    changed_after_summary = [
        event for event in db.get_audit(case_id)
        if event.get("action") in stale_actions
        and str(event.get("created_at") or "") > snapshot_created_at
    ]
    if changed_after_summary:
        raise HTTPException(status_code=409, detail="Case evidence or investigation state changed after this summary was generated. Generate and review a fresh resolution summary before closing.")
    order = db.get_order(case.get("order_id", "")) or {}
    evidence_records = db.list_case_evidence(case_id)
    tracking_checks = db.list_case_carrier_tracking_checks(case_id)
    follow_up_actions = db.list_case_follow_up_actions(case_id)
    blocking_reason = validate_closure(
        body.closure_outcome, case, order, evidence_records, tracking_checks, follow_up_actions
    )
    if blocking_reason:
        db.log_audit(case_id, actor, "CASE_CLOSURE_BLOCKED", {
            "summary_id": summary_id,
            "closure_outcome_attempted": body.closure_outcome,
            "reason": blocking_reason,
            "note": "Closure was blocked by a deterministic guardrail. No case or payment state was changed.",
        })
        raise HTTPException(status_code=409, detail=blocking_reason)
    new_status = status_for_closure(body.closure_outcome)
    closed_record = db.close_case_with_resolution(
        case_id, summary_id,
        new_case_status=new_status,
        closure_outcome=body.closure_outcome,
        closure_note=note,
        actor=actor,
    )
    if not closed_record:
        raise HTTPException(status_code=409, detail="The case or summary changed concurrently. Refresh and review the latest saved state.")
    return {
        "case_id": case_id,
        "case_status": new_status,
        "resolution_record": closed_record,
        "case": get_support_case(case_id),
        "message": f"Case closed with outcome {body.closure_outcome}. No payment action was executed by case closure.",
    }


def _refresh_follow_up_plan(case: dict[str, Any], actor: str, source_run_id: str) -> dict[str, Any]:
    """Rebuild a deterministic action checklist from the latest saved records."""
    case_id = str(case["case_id"])
    order = db.get_order(str(case.get("order_id") or "")) or {}
    evidence_records = db.list_case_evidence(case_id)
    tracking_checks = db.list_case_carrier_tracking_checks(case_id)
    existing_actions = db.list_case_follow_up_actions(case_id)
    plan = build_follow_up_action_plan(
        case,
        order,
        evidence_records,
        tracking_checks,
        existing_actions,
        run_id=source_run_id,
    )
    old_by_key = {str(item.get("action_key")): item for item in existing_actions}
    persisted = db.upsert_case_follow_up_actions(
        case_id,
        source_run_id,
        plan.get("actions", []),
        created_by=actor,
        satisfied_gates=plan.get("satisfied_gates", set()),
    )
    for item in persisted:
        old = old_by_key.get(str(item.get("action_key")))
        if old and old.get("status") in {"OPEN", "IN_PROGRESS", "BLOCKED"} and item.get("status") == "AUTO_COMPLETED":
            db.log_audit(case_id, "SYSTEM", "FOLLOW_UP_ACTION_AUTO_COMPLETED", {
                "action_id": item.get("action_id"),
                "action_key": item.get("action_key"),
                "completion_gate": item.get("completion_gate"),
                "note": "A machine-checkable prerequisite was observed in the saved local state. This does not authenticate an external source.",
            })
    return {
        "run_id": source_run_id,
        "summary": plan.get("summary", "Follow-up plan refreshed from saved case data."),
        "blockers": plan.get("blockers", []),
        "actions": persisted,
    }


@app.post("/cases/{case_id}/follow-up-actions/refresh")
def refresh_follow_up_action_plan(
    case_id: str,
    actor: str = Depends(require_admin),
) -> dict[str, Any]:
    """Refresh the persisted follow-up checklist from current local records."""
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    ensure_case_not_closed(case)
    if not db.get_order(case.get("order_id", "")):
        raise HTTPException(status_code=404, detail="The order linked to this case no longer exists.")
    latest_run = next((
        (event.get("details") or {}).get("run_id")
        for event in reversed(db.get_audit(case_id))
        if event.get("action") == "INVESTIGATION_ORCHESTRATED"
        and (event.get("details") or {}).get("run_id")
    ), None)
    plan_id = str(latest_run or f"PLAN-{uuid.uuid4().hex[:10].upper()}")
    plan = _refresh_follow_up_plan(case, actor, plan_id)
    db.log_audit(case_id, actor, "FOLLOW_UP_PLAN_REFRESHED", {
        "run_id": plan_id,
        "action_count": len(plan["actions"]),
        "open_count": sum(1 for item in plan["actions"] if item.get("status") == "OPEN"),
        "blocked_count": sum(1 for item in plan["actions"] if item.get("status") == "BLOCKED"),
        "note": "Follow-up checklist refreshed from saved evidence, order, payment, and tracking state. No case policy or payment action was changed.",
    })
    return {
        "case_id": case_id,
        "follow_up_plan": plan,
        "case": get_support_case(case_id),
        "message": f"Follow-up plan refreshed with {len(plan['actions'])} action(s). No case policy or payment action was changed.",
    }


@app.post("/cases/{case_id}/follow-up-actions/{action_id}/update")
def update_follow_up_action(
    case_id: str,
    action_id: str,
    body: FollowUpActionUpdateRequest,
    actor: str = Depends(require_admin),
) -> dict[str, Any]:
    """Record merchant progress; completion gates prevent unsupported claims."""
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    ensure_case_not_closed(case)
    action = db.get_case_follow_up_action(case_id, action_id)
    if not action:
        raise HTTPException(status_code=404, detail="Follow-up action not found for this case.")
    if action.get("status") == "BLOCKED":
        raise HTTPException(status_code=409, detail=f"This action is blocked. {action.get('blocked_reason') or 'Complete its prerequisites and refresh the plan first.'}")
    protected_case = _protected_case_for_evidence_or_reevaluation(case)
    if protected_case and action.get("action_key") != "REFUND_RECONCILIATION":
        raise HTTPException(status_code=409, detail="This case has a submitted or completed refund. Only the refund-reconciliation follow-up is available.")

    note = body.note.strip()
    if len(note) < 12:
        raise HTTPException(status_code=422, detail="Add a meaningful follow-up note of at least 12 characters.")
    if body.status == "COMPLETED":
        order = db.get_order(case.get("order_id", "")) or {}
        evidence_records = db.list_case_evidence(case_id)
        tracking_checks = db.list_case_carrier_tracking_checks(case_id)
        block_reason = validate_action_completion(action, case, order, evidence_records, tracking_checks, note)
        if block_reason:
            raise HTTPException(status_code=422, detail=block_reason)

    updated = db.update_case_follow_up_action(case_id, action_id, body.status, actor, note)
    if not updated:
        raise HTTPException(status_code=409, detail="This follow-up action changed concurrently or is already complete. Refresh the case.")
    db.log_audit(case_id, actor, "FOLLOW_UP_ACTION_UPDATED", {
        "action_id": updated.get("action_id"),
        "action_key": updated.get("action_key"),
        "status": updated.get("status"),
        "assigned_to": updated.get("assigned_to"),
        "note": note,
        "completion_gate": updated.get("completion_gate"),
        "message": "Follow-up checklist progress only; no case policy, order, evidence verification, or payment state was changed.",
    })
    # Refresh after progress changes so newly satisfied gates unblock the next step.
    refreshed = db.get_case(case_id) or case
    plan = _refresh_follow_up_plan(refreshed, actor, str(updated.get("source_run_id") or f"PLAN-{uuid.uuid4().hex[:10].upper()}"))
    db.log_audit(case_id, actor, "FOLLOW_UP_PLAN_REFRESHED", {
        "run_id": plan["run_id"],
        "action_count": len(plan["actions"]),
        "note": "Plan refreshed after a merchant progress update. No payment action was taken.",
    })
    current_case = get_support_case(case_id)
    return {
        "case_id": case_id,
        "action": db.get_case_follow_up_action(case_id, action_id),
        "follow_up_actions": plan["actions"],
        "case": current_case,
        "message": f"Follow-up action {action_id} updated to {body.status}. No payment action was taken.",
    }


@app.post("/cases/{case_id}/investigation/run")
def run_case_investigation(
    case_id: str,
    actor: str = Depends(require_admin),
) -> dict[str, Any]:
    """Run a bounded, data-aware sequence of allowlisted read-only tools.

    The endpoint reads local case/order/evidence/tracking records, conditionally
    selects tracking inspection when relevant, and previews deterministic policy.
    It does not call a carrier API, modify case/order state, verify evidence, or
    submit any payment action.
    """
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    ensure_case_not_closed(case)
    order = db.get_order(case.get("order_id"))
    if not order:
        raise HTTPException(status_code=404, detail="The order linked to this case no longer exists.")

    # Execute an adaptive but strictly allowlisted sequence of local read-only tools.
    # Tool selection is data-aware and bounded; it never accepts model-supplied names.
    tool_execution = execute_read_only_tool_plan(
        case,
        order,
        db_module=db,
        evaluate_policy=evaluate_policy,
    )
    execution_results = tool_execution["results"]
    evidence_result = execution_results.get("read_evidence_history") or {}
    tracking_result = execution_results.get("read_tracking_comparisons") or {}
    evidence_records = evidence_result.get("records", []) if isinstance(evidence_result, dict) else []
    tracking_checks = tracking_result.get("records", []) if isinstance(tracking_result, dict) else []
    run_id = f"INV-{uuid.uuid4().hex[:10].upper()}"
    run = build_investigation_run(
        case,
        order,
        evidence_records,
        tracking_checks,
        run_id=run_id,
        ran_at=now_iso(),
        carrier_mode=os.getenv("CARRIER_TRACKING_MODE", "demo").strip().lower() or "demo",
        tool_execution=tool_execution,
    )
    follow_up_plan = _refresh_follow_up_plan(case, actor, run_id)
    run["follow_up_plan"] = follow_up_plan
    db.log_audit(case_id, actor, "FOLLOW_UP_PLAN_REFRESHED", {
        "run_id": run_id,
        "action_count": len(follow_up_plan["actions"]),
        "open_count": sum(1 for item in follow_up_plan["actions"] if item.get("status") == "OPEN"),
        "blocked_count": sum(1 for item in follow_up_plan["actions"] if item.get("status") == "BLOCKED"),
        "note": "Structured follow-up checklist generated from the investigation findings. No case policy or payment action was changed.",
    })
    db.log_audit(case_id, actor, "INVESTIGATION_ORCHESTRATED", {
        "run_id": run_id,
        "result": run,
        "note": "Read-only investigation run completed. No order, case policy/status, evidence, or payment state was changed.",
    })
    return {
        "case_id": case_id,
        "investigation_run": run,
        "message": f"Investigation run completed using {run.get('tool_execution', {}).get('call_count', 0)} allowlisted read-only tools. Findings and next action were recorded; no case or payment action was taken.",
    }


@app.post("/cases/{case_id}/workflow/coordinate")
def coordinate_end_to_end_workflow(
    case_id: str,
    actor: str = Depends(require_admin),
) -> dict[str, Any]:
    """Create an auditable end-to-end workflow assessment from saved local records.

    This coordinator composes existing case, payment, evidence, tracking, follow-up,
    message-draft, and summary state. It does not run provider calls or mutate those
    records, and it never closes the case or performs a payment action.
    """
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    ensure_case_not_closed(case)
    order = db.get_order(str(case.get("order_id") or ""))
    if not order:
        raise HTTPException(status_code=404, detail="The order linked to this case no longer exists.")

    workflow_id = f"WF-{uuid.uuid4().hex[:10].upper()}"
    workflow = build_end_to_end_workflow(
        case=case,
        order=order,
        evidence_records=db.list_case_evidence(case_id),
        tracking_checks=db.list_case_carrier_tracking_checks(case_id),
        follow_up_actions=db.list_case_follow_up_actions(case_id),
        message_drafts=db.list_case_customer_message_drafts(case_id),
        resolution_summaries=db.list_case_resolution_summaries(case_id),
        workflow_id=workflow_id,
        generated_at=now_iso(),
    )
    db.log_audit(case_id, actor, "END_TO_END_WORKFLOW_COORDINATED", {
        "workflow_id": workflow_id,
        "workflow": workflow,
        "next_stage": (workflow.get("next_safe_step") or {}).get("stage_key"),
        "overall_status": workflow.get("overall_status"),
        "note": "End-to-end workflow assessment generated from saved local state. No case, order, evidence, shipping, policy, or payment state was changed.",
    })
    return {
        "case_id": case_id,
        "workflow": workflow,
        "case": get_support_case(case_id),
        "message": "End-to-end workflow coordinated and recorded. Follow the next safe step; no case or payment action was taken.",
    }


def _protected_case_for_evidence_or_reevaluation(case: dict[str, Any]) -> bool:
    protected_statuses = {
        "REFUND_SUBMITTING", "REFUND_PENDING", "REFUND_COMPLETED",
        "REFUND_REQUIRES_RECONCILIATION",
    }
    return (
        case.get("status") in protected_statuses
        or case.get("refund_status") in {"PENDING", "COMPLETED"}
        or bool(case.get("paypal_refund_id"))
    )


@app.post("/cases/{case_id}/evidence")
def submit_case_evidence(
    case_id: str,
    body: EvidenceSubmissionRequest,
    actor: str = Depends(require_admin),
) -> dict[str, Any]:
    """Save merchant-entered evidence as unverified; never changes payment eligibility."""
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    ensure_case_not_closed(case)
    if _protected_case_for_evidence_or_reevaluation(case):
        raise HTTPException(status_code=409, detail="Evidence collection is locked for a case with a submitted or completed refund. Review the existing PayPal refund state instead.")

    evidence = body.model_dump(exclude_none=True)
    for key, value in list(evidence.items()):
        if isinstance(value, str):
            value = value.strip()
            if not value:
                evidence.pop(key)
            else:
                evidence[key] = value
    # A boolean false is a meaningful statement (e.g. address not confirmed).
    if not evidence:
        raise HTTPException(status_code=422, detail="Enter at least one evidence field before saving.")

    evidence_record = db.create_case_evidence(case_id, evidence, submitted_by=actor)
    db.log_audit(case_id, actor, "EVIDENCE_SUBMITTED", {
        "evidence_id": evidence_record["evidence_id"],
        "fields": sorted(evidence.keys()),
        "verification_status": "SUBMITTED_UNVERIFIED",
    })
    return {
        "case_id": case_id,
        "evidence_record": evidence_record,
        "evidence_records": db.list_case_evidence(case_id),
        "message": "Evidence saved as unverified. It does not change the payment decision by itself.",
    }


def _provider_shipping_status(value: Any) -> str | None:
    tag = str(value or "").replace("_", "").replace(" ", "").lower()
    mapping = {
        "pending": "NOT_SHIPPED",
        "inforeceived": "NOT_SHIPPED",
        "intransit": "IN_TRANSIT",
        "outfordelivery": "OUT_FOR_DELIVERY",
        "delivered": "DELIVERED",
        "attemptfail": "EXCEPTION",
        "exception": "EXCEPTION",
        "expired": "EXPIRED",
        "availableforpickup": "AVAILABLE_FOR_PICKUP",
    }
    return mapping.get(tag)


def _date_prefix(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("datetime") or value.get("datetime_min") or value.get("value")
    if not isinstance(value, str) or len(value.strip()) < 10:
        return None
    candidate = value.strip()[:10]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def build_tracking_comparison(
    order: dict[str, Any], evidence: dict[str, Any], provider: dict[str, Any]
) -> dict[str, Any]:
    """Compare provider observations with submitted/local data; never change policy state."""
    local_status = str(order.get("shipping_status") or "UNKNOWN").upper()
    provider_status = _provider_shipping_status(provider.get("tracking_status"))
    known_local = {"NOT_SHIPPED", "IN_TRANSIT", "DELIVERED", "OUT_FOR_DELIVERY", "EXCEPTION", "EXPIRED", "AVAILABLE_FOR_PICKUP"}
    if provider_status and local_status in known_local:
        status_match = "MATCH" if provider_status == local_status else "DIFFERENT_REVIEW_REQUIRED"
    else:
        status_match = "NOT_COMPARABLE"

    merchant_date = _date_prefix(evidence.get("expected_delivery_date"))
    provider_date = _date_prefix(provider.get("estimated_delivery_date"))
    if merchant_date and provider_date:
        date_match = "MATCH" if merchant_date == provider_date else "DIFFERENT_REVIEW_REQUIRED"
    else:
        date_match = "NOT_AVAILABLE"

    checkpoint = provider.get("latest_checkpoint") if isinstance(provider.get("latest_checkpoint"), dict) else None
    source_mode = str(provider.get("source_mode") or "UNKNOWN").upper()
    is_simulated = source_mode == "SIMULATED_DEMO" or "SIMULATED" in str(provider.get("provider") or "").upper()

    findings: list[str] = []
    has_discrepancy = False
    has_comparable_match = False
    if status_match == "DIFFERENT_REVIEW_REQUIRED":
        has_discrepancy = True
        findings.append(
            f"Shipping status differs: the local order record says {local_status}, while this tracking snapshot says {provider_status}. Check the official source before drawing a conclusion."
        )
    elif status_match == "MATCH":
        has_comparable_match = True
        findings.append(f"Shipping status matches for the fields compared: {local_status}.")
    else:
        findings.append("Shipping status could not be compared because one side has no recognized status.")

    if date_match == "DIFFERENT_REVIEW_REQUIRED":
        has_discrepancy = True
        findings.append(
            f"Delivery dates differ: submitted expected date is {merchant_date}, while the tracking snapshot estimates {provider_date}. Confirm which date each value represents."
        )
    elif date_match == "MATCH":
        has_comparable_match = True
        findings.append(f"Delivery dates match: {merchant_date}.")
    else:
        findings.append("Delivery date comparison is unavailable because at least one date is missing or invalid.")

    if is_simulated:
        if has_discrepancy:
            overall_result = "SIMULATED_DISCREPANCY"
            overall_summary = "The demo scenario deliberately generates a difference for testing the comparison interface. This is not a real carrier event or a real-world discrepancy."
        elif has_comparable_match:
            overall_result = "SIMULATED_NO_DIFFERENCE"
            overall_summary = "The demo scenario shows no difference for the fields that could be compared. This is simulated data, not a confirmation about the real shipment."
        else:
            overall_result = "SIMULATED_INSUFFICIENT_DATA"
            overall_summary = "The simulated result does not contain enough comparable information. No real carrier source was queried."
    elif has_discrepancy:
        overall_result = "POTENTIAL_DISCREPANCY"
        overall_summary = "One or more fields differ. This is a review signal, not proof that either source is correct. Manually inspect the official carrier source."
    elif status_match == "MATCH" and date_match == "MATCH":
        overall_result = "NO_DIFFERENCE_DETECTED"
        overall_summary = "No difference was detected in the compared status and date fields. This does not independently authenticate the evidence."
    else:
        overall_result = "INSUFFICIENT_DATA"
        overall_summary = "There is not enough comparable information to determine whether these fields agree."

    if is_simulated:
        findings.insert(0, "SIMULATED DATA ONLY: every status and date in this comparison was generated locally; no real carrier was contacted.")

    return {
        "local_shipping_status": local_status,
        "provider_shipping_status": provider_status or "UNKNOWN",
        "shipping_status_comparison": status_match,
        "merchant_expected_delivery_date": merchant_date,
        "provider_estimated_delivery_date": provider_date,
        "delivery_date_comparison": date_match,
        "merchant_entered_scan": evidence.get("latest_carrier_scan"),
        "provider_latest_checkpoint": checkpoint,
        "overall_result": overall_result,
        "overall_summary": overall_summary,
        "findings": findings,
        "source_mode": source_mode,
        "scan_review_note": "Compare the two scan descriptions and timestamps manually; wording differences are not proof of a contradiction.",
        "human_review_required": True,
        "payment_action_taken": False,
    }


@app.post("/cases/{case_id}/evidence/{evidence_id}/tracking-refresh")
def refresh_carrier_tracking(
    case_id: str,
    evidence_id: str,
    actor: str = Depends(require_admin),
) -> dict[str, Any]:
    """Fetch read-only carrier tracking and compare it with submitted evidence."""
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    ensure_case_not_closed(case)
    if _protected_case_for_evidence_or_reevaluation(case):
        raise HTTPException(status_code=409, detail="Tracking refresh is locked for a case with a submitted or completed refund.")
    record = db.get_case_evidence(case_id, evidence_id)
    if not record:
        raise HTTPException(status_code=404, detail="Evidence record not found for this case.")
    evidence = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
    tracking_number = str(evidence.get("tracking_number") or "").strip()
    if not tracking_number:
        raise HTTPException(status_code=422, detail="This evidence record has no tracking number. Save one before fetching carrier data.")
    # Default to local simulated data so the prototype is usable without a paid API.
    # Live AfterShip requests require an explicit opt-in as well as account access.
    mode = os.getenv("CARRIER_TRACKING_MODE", "demo").strip().lower() or "demo"
    api_key = os.getenv("AFTERSHIP_API_KEY", "").strip()
    try:
        if mode == "demo":
            provider_result = simulate_tracking(
                tracking_number,
                evidence.get("carrier_slug"),
                evidence.get("expected_delivery_date"),
            )
        elif mode == "aftership":
            if not api_key:
                raise HTTPException(
                    status_code=503,
                    detail="Live AfterShip mode needs AFTERSHIP_API_KEY in backend/.env and an AfterShip plan with API access. Set CARRIER_TRACKING_MODE=demo to use the no-cost simulated mode.",
                )
            provider_result = lookup_tracking(api_key, tracking_number, evidence.get("carrier_slug"))
        else:
            raise HTTPException(
                status_code=503,
                detail="Invalid CARRIER_TRACKING_MODE. Use 'demo' (no payment, simulated results) or 'aftership' (live API access required).",
            )
    except CarrierTrackingError as exc:
        db.log_audit(case_id, actor, "CARRIER_TRACKING_LOOKUP_FAILED", {
            "evidence_id": evidence_id,
            "tracking_number": tracking_number,
            "reason": str(exc),
            "mode": mode,
            "note": "No order shipping state, case policy, or payment state was changed.",
        })
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    provider_result.setdefault("source_mode", "SIMULATED_DEMO" if mode == "demo" else "LIVE_AFTERSHIP")

    returned_number = str(provider_result.get("tracking_number") or "").strip()
    if returned_number and returned_number.casefold() != tracking_number.casefold():
        db.log_audit(case_id, actor, "CARRIER_TRACKING_NUMBER_MISMATCH", {
            "evidence_id": evidence_id,
            "submitted_tracking_number": tracking_number,
            "provider_tracking_number": returned_number,
            "note": "Snapshot rejected because provider tracking number did not match the submitted number.",
        })
        raise HTTPException(status_code=409, detail="AfterShip returned a different tracking number. No snapshot was saved; verify the carrier and tracking number.")

    order = db.get_order(case["order_id"]) or {}
    comparison = build_tracking_comparison(order, evidence, provider_result)
    summary = {**provider_result, "comparison": comparison}
    saved = db.create_carrier_tracking_check(case_id, evidence_id, tracking_number, summary)
    audit_action = "CARRIER_TRACKING_SIMULATED" if provider_result.get("source_mode") == "SIMULATED_DEMO" else "CARRIER_TRACKING_LOOKED_UP"
    is_demo = provider_result.get("source_mode") == "SIMULATED_DEMO"
    db.log_audit(case_id, actor, audit_action, {
        "check_id": saved["check_id"],
        "evidence_id": evidence_id,
        "tracking_number": tracking_number,
        "provider": provider_result.get("provider", "Unknown"),
        "source_mode": provider_result.get("source_mode"),
        "provider_status": provider_result.get("tracking_status"),
        "lookup_state": provider_result.get("lookup_state"),
        "shipping_status_comparison": comparison["shipping_status_comparison"],
        "delivery_date_comparison": comparison["delivery_date_comparison"],
        "note": (
            "SIMULATED demo data generated locally; not real carrier evidence. "
            "No shipping, case policy, or payment state was changed."
            if is_demo else
            "Read-only live carrier lookup. No shipping, case policy, or payment state was changed; merchant review remains required."
        ),
    })
    message = (
        "SIMULATED DEMO tracking result saved. This data was generated locally and is NOT from a real carrier. No shipping state, case policy, or payment action was changed."
        if is_demo else
        "Carrier tracking result saved. Review the live provider comparison; no shipping state or payment action was changed."
    )
    return {"case_id": case_id, "tracking_check": saved, "message": message}



def _is_https_source_reference(value: Any) -> bool:
    """Accept an HTTPS source URL for manual merchant corroboration; never fetch it here."""
    if not isinstance(value, str) or not value.strip() or any(ch.isspace() for ch in value.strip()):
        return False
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme.lower() == "https" and bool(parsed.hostname) and parsed.username is None and parsed.password is None
    except ValueError:
        return False


def _placeholder_or_contradictory_note(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.lower().split())
    exact_placeholders = {"test", "test only", "placeholder", "dummy", "dummy data", "fake", "fake data", "sample", "sample only", "lorem ipsum", "n/a", "na", "unknown", "not available"}
    return normalized in exact_placeholders or any(phrase in normalized for phrase in (
        "test only", "placeholder", "dummy data", "fake data", "not actual evidence",
        "no verifiable", "not verifiable", "not verified", "could not verify",
        "cannot verify", "unable to verify", "not corroborated", "not attached",
    ))


def _verification_block_reason(case: dict[str, Any], evidence_record: dict[str, Any], review_note: str) -> str | None:
    """Require a source reference and minimally structured evidence before a VERIFIED decision.

    This is input-quality validation, not external authentication: the merchant still has
    to open and compare the source themselves.
    """
    evidence = evidence_record.get("evidence") or {}
    if not isinstance(evidence, dict):
        return "Evidence payload is invalid. Submit a new evidence record before marking it verified."
    if _placeholder_or_contradictory_note(review_note):
        return "The review note indicates test, placeholder, or unverified information. Reject this record or provide a truthful note after checking real evidence."
    for field_name, value in evidence.items():
        if isinstance(value, str) and _placeholder_or_contradictory_note(value):
            return f"The {field_name.replace('_', ' ')} field appears to contain test or placeholder text. Provide genuine evidence or reject this record."
    source_reference = evidence.get("source_reference")
    if not _is_https_source_reference(source_reference):
        return "To mark evidence VERIFIED, add an HTTPS link to the official carrier/source and submit a new evidence record. ResolveOS does not fetch or authenticate the URL automatically."

    if str(case.get("issue_type") or "").upper() == "ITEM_NOT_RECEIVED":
        tracking_number = evidence.get("tracking_number")
        scan = evidence.get("latest_carrier_scan")
        expected_date = evidence.get("expected_delivery_date")
        if not isinstance(tracking_number, str) or len(tracking_number.strip()) < 5:
            return "To verify a missing-delivery record, provide the carrier tracking number."
        if not isinstance(scan, str) or not re.search(r"(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s+\d{1,2}:\d{2})", scan):
            return "The latest carrier scan must include a timestamp (for example, 2026-10-12 08:30 UTC)."
        if not isinstance(expected_date, str):
            return "Expected delivery date is required to verify a missing-delivery record; use YYYY-MM-DD."
        normalized_date = expected_date.strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized_date):
            return "Expected delivery date must be a valid calendar date in YYYY-MM-DD format."
        try:
            date.fromisoformat(normalized_date)
        except ValueError:
            return "Expected delivery date must be a valid calendar date in YYYY-MM-DD format."
    return None


@app.post("/cases/{case_id}/evidence/{evidence_id}/review")
def review_case_evidence(
    case_id: str,
    evidence_id: str,
    body: EvidenceReviewRequest,
    actor: str = Depends(require_admin),
) -> dict[str, Any]:
    """Record an explicit merchant review of one evidence record. No policy/payment action occurs."""
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    ensure_case_not_closed(case)
    if _protected_case_for_evidence_or_reevaluation(case):
        raise HTTPException(status_code=409, detail="Evidence review is locked for a case with a submitted or completed refund.")

    evidence_record = db.get_case_evidence(case_id, evidence_id)
    if not evidence_record:
        raise HTTPException(status_code=404, detail="Evidence record not found for this case.")
    if evidence_record.get("verification_status") != "SUBMITTED_UNVERIFIED":
        raise HTTPException(status_code=409, detail="This evidence record has already been reviewed and cannot be reviewed again.")

    review_note = body.review_note.strip()
    if len(review_note) < 5:
        raise HTTPException(status_code=422, detail="Add a review note of at least 5 non-whitespace characters.")

    if body.decision == "VERIFIED":
        block_reason = _verification_block_reason(case, evidence_record, review_note)
        if block_reason:
            db.log_audit(case_id, actor, "EVIDENCE_VERIFICATION_BLOCKED", {
                "evidence_id": evidence_id,
                "decision_attempted": body.decision,
                "reason": block_reason,
                "note": "No evidence status or payment state was changed.",
            })
            raise HTTPException(status_code=422, detail=block_reason)

    if not db.review_case_evidence(case_id, evidence_id, body.decision, actor, review_note):
        raise HTTPException(status_code=409, detail="Evidence was reviewed concurrently or is no longer unverified. Refresh the case.")

    db.log_audit(case_id, actor, "EVIDENCE_REVIEWED", {
        "evidence_id": evidence_id,
        "verification_status": body.decision,
        "reviewed_by": actor,
        "review_note": review_note,
        "note": "Evidence review only; case policy and payment state were not changed.",
    })
    return {
        "case_id": case_id,
        "evidence_id": evidence_id,
        "verification_status": body.decision,
        "reviewed_by": actor,
        "review_note": review_note,
        "message": f"Evidence marked {body.decision.lower()}. No payment action was taken.",
        "case": get_support_case(case_id),
    }


@app.post("/cases/{case_id}/evidence/{evidence_id}/revoke-verification")
def revoke_case_evidence_verification(
    case_id: str,
    evidence_id: str,
    body: EvidenceVerificationRevocationRequest,
    actor: str = Depends(require_admin),
) -> dict[str, Any]:
    """Revoke a historical VERIFIED label after merchant review, preserving its original audit data."""
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    ensure_case_not_closed(case)
    if _protected_case_for_evidence_or_reevaluation(case):
        raise HTTPException(status_code=409, detail="Evidence verification corrections are locked for a case with a submitted or completed refund.")

    record = db.get_case_evidence(case_id, evidence_id)
    if not record:
        raise HTTPException(status_code=404, detail="Evidence record not found for this case.")
    if str(record.get("verification_status") or "").upper() != "VERIFIED":
        raise HTTPException(status_code=409, detail="Only evidence currently marked VERIFIED can have its verification revoked.")

    reason = body.reason.strip()
    if len(reason) < 12:
        raise HTTPException(status_code=422, detail="Enter a meaningful correction reason of at least 12 characters.")
    detected_problem = historical_verification_problem(record)
    if not db.revoke_case_evidence_verification(case_id, evidence_id, actor, reason):
        raise HTTPException(status_code=409, detail="The evidence status changed concurrently. Refresh the case and try again.")

    updated_record = db.get_case_evidence(case_id, evidence_id)
    db.log_audit(case_id, actor, "EVIDENCE_VERIFICATION_REVOKED", {
        "evidence_id": evidence_id,
        "previous_status": "VERIFIED",
        "new_status": "VERIFICATION_REVOKED",
        "detected_integrity_problem": detected_problem,
        "correction_reason": reason,
        "original_reviewed_by": record.get("reviewed_by"),
        "original_reviewed_at": record.get("reviewed_at"),
        "note": "Historical verification was invalidated by an explicit merchant correction. Original review metadata and prior audit events were preserved. No case policy or payment state was changed.",
    })
    return {
        "case_id": case_id,
        "evidence_id": evidence_id,
        "previous_status": "VERIFIED",
        "verification_status": "VERIFICATION_REVOKED",
        "verification_revoked_by": actor,
        "verification_revoked_at": (updated_record or {}).get("verification_revoked_at"),
        "verification_revocation_note": reason,
        "message": "Historical verification revoked and audit trail preserved. This record no longer counts as verified; no case or payment action was taken.",
        "case": get_support_case(case_id),
    }


@app.post("/cases/{case_id}/reevaluate")
def reevaluate_case(
    case_id: str,
    actor: str = Depends(require_admin),
) -> dict[str, Any]:
    """Re-run deterministic policy against current order state without executing payment actions."""
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    ensure_case_not_closed(case)
    if _protected_case_for_evidence_or_reevaluation(case):
        raise HTTPException(status_code=409, detail="This case has a refund submission or completed refund. Re-evaluation is blocked to protect the existing payment state.")
    order = db.get_order(case["order_id"])
    if not order:
        raise HTTPException(status_code=404, detail="The order linked to this case no longer exists.")

    records = db.list_case_evidence(case_id)
    prior_decision = case.get("policy_decision")
    prior_status = case.get("status")
    decision, reasons = evaluate_policy({"issue_type": case.get("issue_type", "OTHER")}, order)

    latest_evidence: dict[str, Any] = {}
    for record in reversed(records):
        values = record.get("evidence") or {}
        if isinstance(values, dict):
            latest_evidence.update(values)
    if (
        case.get("issue_type") == "ITEM_NOT_RECEIVED"
        and str(order.get("shipping_status") or "UNKNOWN").upper() == "IN_TRANSIT"
        and latest_evidence.get("tracking_number")
        and latest_evidence.get("latest_carrier_scan")
    ):
        reasons = [reason for reason in reasons if "Shipment is in transit" not in reason]
        reasons.append(
            "Submitted tracking details are recorded but not independently verified; verify the scan against the carrier's official records while the order remains in transit."
        )

    status_by_decision = {
        "REFUND_CANDIDATE": "AWAITING_MERCHANT_APPROVAL",
        "AWAITING_CAPTURE_LINK": "AWAITING_CAPTURE_LINK",
        "NEEDS_MORE_INFORMATION": "NEEDS_MORE_INFORMATION",
        "NEEDS_HUMAN_REVIEW": "NEEDS_HUMAN_REVIEW",
        "NO_NEW_REFUND": "CLOSED_NO_ACTION",
    }
    new_status = status_by_decision.get(decision, "NEEDS_HUMAN_REVIEW")
    if not db.update_case_policy_result(case_id, decision, reasons, new_status):
        raise HTTPException(status_code=409, detail="Case status changed during re-evaluation or a refund was submitted. Reload the case before continuing.")
    db.log_audit(case_id, actor, "CASE_REEVALUATED", {
        "previous_decision": prior_decision,
        "new_decision": decision,
        "previous_status": prior_status,
        "new_status": new_status,
        "evidence_submission_count": len(records),
        "note": "Evidence submissions remain unverified; no payment action was taken.",
    })
    # Refresh first: a formerly blocked re-evaluation step may now be available,
    # but it is completed only if the refreshed checklist marks it actionable.
    latest_plan_id = next((
        action.get("source_run_id") for action in db.list_case_follow_up_actions(case_id)
        if action.get("action_key") == "RE_EVALUATE_POLICY" and action.get("source_run_id")
    ), None) or f"PLAN-{uuid.uuid4().hex[:10].upper()}"
    first_plan = _refresh_follow_up_plan(db.get_case(case_id) or case, actor, str(latest_plan_id))
    completed_action = db.complete_case_follow_up_action_by_key(
        case_id,
        "RE_EVALUATE_POLICY",
        actor,
        "Completed automatically because the deterministic case re-evaluation succeeded.",
    )
    if completed_action:
        db.log_audit(case_id, actor, "FOLLOW_UP_ACTION_UPDATED", {
            "action_id": completed_action.get("action_id"),
            "action_key": completed_action.get("action_key"),
            "status": "COMPLETED",
            "note": "The existing deterministic re-evaluation endpoint completed successfully. No refund was executed.",
        })
        final_plan = _refresh_follow_up_plan(db.get_case(case_id) or case, actor, str(completed_action.get("source_run_id") or latest_plan_id))
    else:
        final_plan = first_plan
    db.log_audit(case_id, actor, "FOLLOW_UP_PLAN_REFRESHED", {
        "run_id": final_plan["run_id"],
        "action_count": len(final_plan["actions"]),
        "note": "Plan refreshed after deterministic policy re-evaluation. No payment action was taken.",
    })
    return get_support_case(case_id)


@app.post("/cases/{case_id}/approve-refund")
def approve_refund(case_id: str, actor: str = Depends(require_admin)) -> dict[str, Any]:
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    ensure_case_not_closed(case)
    if case["policy_decision"] != "REFUND_CANDIDATE" or case["status"] != "AWAITING_MERCHANT_APPROVAL":
        raise HTTPException(status_code=409, detail=f"This case is not refund-eligible. Current decision: {case['policy_decision']}.")
    if case.get("refund_status") in {"COMPLETED", "PENDING"}:
        raise HTTPException(status_code=409, detail="A refund was already submitted for this case; check the saved PayPal status.")

    order = db.get_order(case["order_id"])
    capture_id = order.get("paypal_capture_id") if order else None
    if not capture_id:
        raise HTTPException(status_code=409, detail="Link a verified completed PayPal Sandbox capture first via POST /orders/{order_id}/link-capture.")

    try:
        capture = get_capture(capture_id)
    except PayPalError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if capture.get("status") != "COMPLETED":
        raise HTTPException(status_code=409, detail=f"PayPal capture is not refundable in its current state: {capture.get('status')}.")
    amount = capture.get("amount") or {}
    if str(amount.get("value")) != str(order["amount"]) or amount.get("currency_code") != order["currency"]:
        raise HTTPException(status_code=409, detail="PayPal capture amount/currency does not match the order; refund blocked.")

    # Persist a stable idempotency key, then atomically claim this case.
    # The conditional database update ensures concurrent approvals cannot both
    # pass through to the PayPal refund call.
    request_id = case.get("paypal_request_id") or f"resolveos-{case_id.lower()}"
    if not db.claim_refund_submission(case_id, request_id):
        latest = db.get_case(case_id)
        current_status = latest.get("status") if latest else "missing"
        raise HTTPException(
            status_code=409,
            detail=f"Refund submission is already in progress or this case is no longer eligible (status: {current_status}).",
        )
    db.log_audit(case_id, actor, "MERCHANT_APPROVED_REFUND", {"capture_id": capture_id, "request_id": request_id, "amount": amount})
    try:
        refund = refund_capture(capture_id, request_id)
    except PayPalError as exc:
        db.update_case(case_id, status="REFUND_REQUIRES_RECONCILIATION")
        db.log_audit(case_id, actor, "REFUND_API_ERROR", {"error": str(exc), "request_id": request_id})
        raise HTTPException(status_code=502, detail=f"PayPal refund request did not return success. Check Sandbox activity before retrying. Request ID: {request_id}. Details: {exc}") from exc

    refund_id = refund.get("id")
    refund_status = refund.get("status", "UNKNOWN")
    new_status = "REFUND_COMPLETED" if refund_status == "COMPLETED" else "REFUND_PENDING"
    db.update_case(case_id, status=new_status, paypal_refund_id=refund_id, refund_status=refund_status)
    if refund_status == "COMPLETED":
        # Local display state only; PayPal remains the payment source of truth.
        with db.connect() as conn:
            conn.execute("UPDATE orders SET refund_status = 'REFUNDED' WHERE order_id = ?", (order["order_id"],))
    db.log_audit(case_id, actor, "PAYPAL_REFUND_RESPONSE", {"refund_id": refund_id, "refund_status": refund_status, "response": refund})
    return {"case_id": case_id, "status": new_status, "refund_id": refund_id, "refund_status": refund_status, "paypal_response": refund}
