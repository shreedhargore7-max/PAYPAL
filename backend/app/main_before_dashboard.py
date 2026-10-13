import html
import json
import os
from pathlib import Path
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import db
from .agent import classify_complaint, evaluate_policy
from .paypal_client import (
    PayPalError, capture_checkout_order, create_checkout_order, get_capture,
    get_checkout_order, refund_capture,
)

load_dotenv()
db.init_db()
app = FastAPI(title="ResolveOS API", version="0.1.0", description="AI-assisted payment support and human-approved PayPal sandbox refunds.")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CreateCaseRequest(BaseModel):
    order_id: str = Field(min_length=4, max_length=40)
    customer_message: str = Field(min_length=5, max_length=3000)


class LinkCaptureRequest(BaseModel):
    capture_id: str = Field(min_length=5, max_length=80)


class CheckoutCaptureRequest(BaseModel):
    paypal_order_id: str = Field(min_length=5, max_length=80)


def require_admin(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> str:
    expected = os.getenv("ADMIN_API_KEY", "").strip()
    if not expected or expected.startswith("replace-with-"):
        raise HTTPException(status_code=503, detail="Set a strong ADMIN_API_KEY in backend/.env before using merchant-only endpoints.")
    if not x_admin_key or not secrets.compare_digest(x_admin_key, expected):
        raise HTTPException(status_code=401, detail="Valid X-Admin-Key header required for this prototype merchant action.")
    return "merchant_admin"


def public_case(case: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in case.items() if k not in {"paypal_request_id"}}


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
    db.create_case(case)
    db.log_audit(case_id, "AI_AGENT", "CASE_ANALYZED", {"issue_type": case["issue_type"], "policy_decision": decision, "reasons": reasons})
    result = {**public_case(case), "order": order}
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
    return public_case(case)


@app.post("/cases/{case_id}/approve-refund")
def approve_refund(case_id: str, actor: str = Depends(require_admin)) -> dict[str, Any]:
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
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
