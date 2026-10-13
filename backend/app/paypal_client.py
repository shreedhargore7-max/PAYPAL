import hashlib
import os

import requests

BASE_URL = "https://api-m.sandbox.paypal.com"  # Intentionally Sandbox-only.
TIMEOUT_SECONDS = 20


class PayPalError(RuntimeError):
    pass


def _credentials() -> tuple[str, str]:
    client_id = os.getenv("PAYPAL_CLIENT_ID", "").strip()
    secret = os.getenv("PAYPAL_CLIENT_SECRET", "").strip()
    if not client_id or not secret or client_id.startswith("your_"):
        raise PayPalError("Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in backend/.env using Sandbox credentials.")
    return client_id, secret


def access_token() -> str:
    client_id, secret = _credentials()
    response = requests.post(
        f"{BASE_URL}/v1/oauth2/token",
        auth=(client_id, secret),
        headers={"Accept": "application/json", "Accept-Language": "en_US"},
        data={"grant_type": "client_credentials"},
        timeout=TIMEOUT_SECONDS,
    )
    if not response.ok:
        # Avoid logging credentials; PayPal's response body is useful for sandbox debugging.
        raise PayPalError(f"PayPal OAuth failed ({response.status_code}): {response.text[:400]}")
    token = response.json().get("access_token")
    if not token:
        raise PayPalError("PayPal did not return an access token.")
    return token


def _api_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def create_checkout_order(order_id: str, item_name: str, amount: str, currency: str) -> dict:
    token = access_token()
    payload = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": order_id,
                "custom_id": order_id,
                "description": item_name[:127],
                "amount": {"currency_code": currency, "value": str(amount)},
            }
        ],
        "application_context": {
            "brand_name": "ResolveOS Demo",
            "user_action": "PAY_NOW",
            "shipping_preference": "NO_SHIPPING",
        },
    }
    response = requests.post(
        f"{BASE_URL}/v2/checkout/orders",
        headers=_api_headers(token),
        json=payload,
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code not in (200, 201):
        raise PayPalError(f"PayPal create-order failed ({response.status_code}): {response.text[:500]}")
    result = response.json()
    if not result.get("id"):
        raise PayPalError("PayPal create-order response did not include an order ID.")
    return result


def get_checkout_order(paypal_order_id: str) -> dict:
    token = access_token()
    response = requests.get(
        f"{BASE_URL}/v2/checkout/orders/{paypal_order_id}",
        headers=_api_headers(token),
        timeout=TIMEOUT_SECONDS,
    )
    if not response.ok:
        raise PayPalError(f"PayPal checkout-order lookup failed ({response.status_code}): {response.text[:500]}")
    return response.json()


def capture_checkout_order(paypal_order_id: str) -> dict:
    token = access_token()
    # Stable idempotency key for this order avoids accidentally issuing duplicate capture calls.
    request_id = hashlib.sha256(paypal_order_id.encode("utf-8")).hexdigest()[:32]
    response = requests.post(
        f"{BASE_URL}/v2/checkout/orders/{paypal_order_id}/capture",
        headers={**_api_headers(token), "PayPal-Request-Id": request_id},
        json={},
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code not in (200, 201, 202):
        raise PayPalError(f"PayPal capture-order failed ({response.status_code}): {response.text[:500]}")
    return response.json()


def get_capture(capture_id: str) -> dict:
    token = access_token()
    response = requests.get(
        f"{BASE_URL}/v2/payments/captures/{capture_id}",
        headers=_api_headers(token),
        timeout=TIMEOUT_SECONDS,
    )
    if not response.ok:
        raise PayPalError(f"PayPal capture lookup failed ({response.status_code}): {response.text[:400]}")
    return response.json()


def refund_capture(capture_id: str, request_id: str) -> dict:
    token = access_token()
    response = requests.post(
        f"{BASE_URL}/v2/payments/captures/{capture_id}/refund",
        headers={**_api_headers(token), "PayPal-Request-Id": request_id},
        json={},
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code not in (200, 201, 202):
        raise PayPalError(f"PayPal refund failed ({response.status_code}): {response.text[:500]}")
    return response.json()
