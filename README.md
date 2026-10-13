# ResolveOS — starter backend

ResolveOS is an AI-assisted payment-resolution prototype. This starter covers the first vertical slice:

1. Create a support case from a complaint and an order ID.
2. Classify the complaint with Gemini (or a deterministic fallback if no Gemini key is set).
3. Apply **deterministic policy rules**; the LLM itself cannot approve or issue a refund.
4. Verify a PayPal Sandbox capture and link it to an order.
5. Require merchant approval before issuing a full refund through PayPal Sandbox.
6. Save a case timeline/audit trail in SQLite.

**Sandbox only.** The PayPal API base URL is hard-coded to `https://api-m.sandbox.paypal.com`. The included order/shipping rows are synthetic demo records. A refund can only be sent when you link a real, completed Sandbox capture whose amount and currency exactly match the demo order. Never use this prototype with live credentials or real customer data.

## 1. Create PayPal Sandbox credentials

1. Sign in at [PayPal Developer](https://developer.paypal.com/).
2. Open **Apps & Credentials**, choose **Sandbox**, and create/select an app.
3. Copy the Sandbox Client ID and Secret. PayPal's REST APIs use OAuth 2.0 to exchange these credentials for a bearer token: [official guide](https://developer.paypal.com/api/rest/authentication).
4. Sandbox buyer and business accounts can be managed in the [Sandbox Accounts dashboard](https://developer.paypal.com/sandbox-testing/accounts).

## 2. Set up on Windows PowerShell

Use Python 3.11 if that is the interpreter you already use for your other AI project.

```powershell
cd path\to\resolveos_starter
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r backend\requirements.txt
Copy-Item backend\.env.example backend\.env
```

Edit `backend\.env` and set:

- `PAYPAL_CLIENT_ID`
- `PAYPAL_CLIENT_SECRET`
- `ADMIN_API_KEY` to a long random secret of your own
- optionally `GEMINI_API_KEY`

The app works without a Gemini key by using a small deterministic classifier, which is useful for checking API and payment flow first.

## 3. Run the backend

```powershell
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/docs> for the interactive API docs. The database is created at `backend\resolveos.db` on first run. The requirements also include `requests`, used by the PayPal API client.

## 4. Try the investigation workflow

### List demo orders

Call `GET /orders`. Seeded orders:

- `ORD-1001`: payment marked complete in demo data; not shipped. Intended to test the refund-candidate branch.
- `ORD-1002`: delivered. A non-receipt complaint should be sent for human review.
- `ORD-1003`: in transit. A non-receipt complaint should request more information.

Example body for `POST /cases`:

```json
{
  "order_id": "ORD-1001",
  "customer_message": "I paid but my headphones were never shipped. Please refund me."
}
```

Copy the returned `case_id`. Without a linked PayPal capture, the expected decision is `AWAITING_CAPTURE_LINK`, not a refund.

## 5. Link a real PayPal Sandbox capture

The refund endpoint requires a **capture ID**, not an order ID. Create and approve a Sandbox payment using a separate checkout/test flow, capture it, then use the resulting capture ID. PayPal documents the capture/refund lifecycle here: [Orders API](https://developer.paypal.com/api/rest/integration/orders-api) and [Refund captured payment](https://developer.paypal.com/api/payments/v2/captures-refund).

The capture's value and currency must exactly match a seeded order. For the current sample orders, use `$89.99 USD`, `$129.00 USD`, or `$59.50 USD` as appropriate. In the local interactive docs, call:

`POST /orders/ORD-1001/link-capture`

Body:

```json
{"capture_id": "YOUR_COMPLETED_SANDBOX_CAPTURE_ID"}
```

Set request header `X-Admin-Key` to the `ADMIN_API_KEY` value from `backend\.env`. The server checks the capture status, amount, and currency against PayPal before linking it. Alternatively, update the order creation flow later to save the capture ID automatically after checkout.

After linking a capture, create a new case for the matching order. If the policy engine returns `REFUND_CANDIDATE`, call `POST /cases/{case_id}/approve-refund` with the same `X-Admin-Key` header. This sends a full refund through the PayPal Sandbox API, using a stable idempotency key. A successful API response is saved in the case and audit log.

## Important behavior / prototype limitations

- **No automatic money movement:** AI only extracts the complaint; deterministic rules produce a recommendation. A merchant must approve a refund.
- **No duplicate-charge automation:** such cases go to human review until distinct captured payment IDs are verified.
- **Synthetic fulfillment data:** the demo shipment statuses are local fixture data. In the final product, connect to a real order/shipping provider or label seeded evidence visibly as demo data.
- **Authentication:** the shared `X-Admin-Key` protects the two merchant-only prototype routes. Replace it with proper user authentication, roles, and session management before deployment.
- **Webhooks are not implemented in this starter:** add PayPal webhook signature verification, event deduplication, and reconciliation before treating events as authoritative. PayPal documentation: [Webhooks](https://developer.paypal.com/api/rest/webhooks/rest/).
- **Not production-ready:** add unit/integration tests, rate limits, structured logging, strict access control, secret management, idempotent database transitions, and policy/legal review before using real customer data or live payments.

## Suggested next build milestones

1. **Frontend:** Next.js merchant case dashboard + customer complaint chat.
2. **Actual checkout:** use the PayPal JS SDK / Orders API to create and approve a Sandbox transaction, then persist the capture ID automatically.
3. **Verified events:** register PayPal webhook events and verify each signature before updating states.
4. **Disputes:** read dispute details and prepare supporting evidence; only submit when PayPal indicates the case is eligible and the merchant approves it. See [Disputes API](https://developer.paypal.com/api/disputes/).
5. **Evaluation:** write scenario tests for delivered vs. not shipped, refund already present, amount mismatch, repeated approval, PayPal timeout, adversarial customer message, and model outage.

## API map

- `GET /health` — health check
- `GET /orders` — seeded demo orders
- `POST /orders/{order_id}/link-capture` — merchant-only: validate and link a Sandbox capture
- `POST /cases` — investigate complaint and create a case
- `GET /cases/{case_id}` — inspect case and audit timeline
- `POST /cases/{case_id}/approve-refund` — merchant-only: attempt a full Sandbox refund for an eligible case
