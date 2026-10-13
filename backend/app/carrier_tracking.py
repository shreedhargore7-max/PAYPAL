"""AfterShip Tracking API adapter for ResolveOS.

This module only reads tracking information and can subscribe a tracking number
when it is not yet present in AfterShip. It never edits ResolveOS order state or
initiates payment actions.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

BASE_URL = "https://api.aftership.com/tracking/2026-07"


class CarrierTrackingError(RuntimeError):
    """Safe, user-facing carrier tracking error."""


def _request_json(method: str, url: str, api_key: str, **kwargs: Any) -> dict[str, Any]:
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.update({"as-api-key": api_key, "Content-Type": "application/json"})
    try:
        response = requests.request(method, url, headers=headers, timeout=20, **kwargs)
    except requests.Timeout as exc:
        raise CarrierTrackingError("AfterShip tracking lookup timed out. Try again shortly.") from exc
    except requests.RequestException as exc:
        raise CarrierTrackingError("Could not connect to AfterShip. Check your internet connection and try again.") from exc

    if response.status_code in (401, 403):
        raise CarrierTrackingError("AfterShip rejected the API key or this account lacks API access. Check AFTERSHIP_API_KEY and your plan permissions.")
    if response.status_code == 429:
        raise CarrierTrackingError("AfterShip rate limit reached. Wait a moment before trying again.")
    if response.status_code >= 400:
        if response.status_code in (400, 404, 422):
            raise CarrierTrackingError(f"AfterShip could not process this tracking request (HTTP {response.status_code}). Check the tracking number and carrier slug.")
        raise CarrierTrackingError(f"AfterShip tracking request failed (HTTP {response.status_code}). Try again later.")

    try:
        data = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError) as exc:
        raise CarrierTrackingError("AfterShip returned an unreadable response. Try again later.") from exc
    if not isinstance(data, dict):
        raise CarrierTrackingError("AfterShip returned an unexpected response format.")
    # Some API errors may be represented in an envelope even when HTTP succeeds.
    meta = data.get("meta")
    if isinstance(meta, dict):
        code = meta.get("code")
        if isinstance(code, int) and code >= 400:
            if code in (401, 403):
                raise CarrierTrackingError("AfterShip rejected the API key or this account lacks API access.")
            raise CarrierTrackingError(f"AfterShip reported an API error (code {code}). Check the tracking number, carrier, and account permissions.")
    return data


def _get_tracking_list(api_key: str, tracking_number: str, carrier_slug: str | None) -> list[dict[str, Any]]:
    params: dict[str, str] = {"tracking_numbers": tracking_number, "fields": "checkpoints"}
    if carrier_slug:
        params["slug"] = carrier_slug
    response = _request_json("GET", f"{BASE_URL}/trackings", api_key, params=params)
    data = response.get("data") or {}
    trackings = data.get("trackings") if isinstance(data, dict) else None
    if not isinstance(trackings, list):
        return []
    return [item for item in trackings if isinstance(item, dict)]


def _select_matching_tracking(trackings: list[dict[str, Any]], tracking_number: str, carrier_slug: str | None) -> dict[str, Any] | None:
    number = tracking_number.strip().casefold()
    matches = [t for t in trackings if str(t.get("tracking_number") or "").strip().casefold() == number]
    if carrier_slug:
        matches = [t for t in matches if str(t.get("slug") or "").strip().casefold() == carrier_slug.casefold()]
    return matches[0] if matches else None


def _checkpoint_timestamp(item: dict[str, Any]) -> str:
    value = item.get("checkpoint_time") or item.get("created_at") or ""
    return str(value)


def _latest_checkpoint(checkpoints: Any) -> dict[str, Any] | None:
    if not isinstance(checkpoints, list):
        return None
    safe = [c for c in checkpoints if isinstance(c, dict)]
    if not safe:
        return None
    # ISO dates sort lexicographically for the formats used by AfterShip. Keep
    # a defensive fallback for non-standard provider timestamps.
    def sort_key(item: dict[str, Any]) -> tuple[int, str]:
        stamp = _checkpoint_timestamp(item)
        try:
            datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            valid = 1
        except (ValueError, TypeError):
            valid = 0
        return valid, stamp
    point = max(safe, key=sort_key)
    return {
        "checkpoint_time": point.get("checkpoint_time") or point.get("created_at"),
        "message": point.get("message"),
        "tag": point.get("tag"),
        "subtag": point.get("subtag"),
        "location": point.get("location"),
        "slug": point.get("slug"),
    }


def normalize_tracking(tracking: dict[str, Any], lookup_state: str) -> dict[str, Any]:
    courier = tracking.get("courier") if isinstance(tracking.get("courier"), dict) else {}
    checkpoints = tracking.get("checkpoints")
    latest = _latest_checkpoint(checkpoints)
    link = tracking.get("courier_tracking_link")
    # Only return official carrier links over HTTPS.
    if not isinstance(link, str) or not link.startswith("https://"):
        link = None
    return {
        "provider": "AfterShip",
        "source_mode": "LIVE_AFTERSHIP",
        "tracking_number": str(tracking.get("tracking_number") or ""),
        "carrier_slug": tracking.get("slug"),
        "carrier_name": courier.get("name") or tracking.get("slug"),
        "provider_tracking_id": tracking.get("id") or tracking.get("legacy_id"),
        "tracking_status": tracking.get("tag") or "Unknown",
        "status_message": tracking.get("subtag_message") or tracking.get("subtag") or tracking.get("tag") or "No carrier status message returned yet.",
        "estimated_delivery_date": tracking.get("estimated_delivery_date") or tracking.get("aftership_estimated_delivery_date"),
        "courier_tracking_link": link,
        "latest_checkpoint": latest,
        "lookup_state": lookup_state,
        "carrier_has_checkpoints": bool(latest),
    }


def lookup_tracking(api_key: str, tracking_number: str, carrier_slug: str | None = None) -> dict[str, Any]:
    """Look up a tracker; create a subscription only if no exact matching record exists."""
    if not api_key or not api_key.strip():
        raise CarrierTrackingError("Configure AFTERSHIP_API_KEY in backend/.env before using carrier tracking lookup.")
    tracking_number = tracking_number.strip()
    if len(tracking_number) < 5:
        raise CarrierTrackingError("Tracking number must contain at least 5 non-whitespace characters.")
    if carrier_slug:
        carrier_slug = carrier_slug.strip().lower()
        import re
        if not re.fullmatch(r"[a-z0-9-]{1,80}", carrier_slug):
            raise CarrierTrackingError("Carrier slug must contain lowercase letters, numbers, and hyphens only.")

    existing = _select_matching_tracking(_get_tracking_list(api_key, tracking_number, carrier_slug), tracking_number, carrier_slug)
    if existing is not None:
        return normalize_tracking(existing, "EXISTING_TRACKING")

    body: dict[str, Any] = {"tracking_number": tracking_number}
    if carrier_slug:
        body["slug"] = carrier_slug
    response = _request_json("POST", f"{BASE_URL}/trackings", api_key, json=body)
    data = response.get("data") or {}
    created = data.get("tracking") if isinstance(data, dict) else None
    if not isinstance(created, dict):
        raise CarrierTrackingError("AfterShip did not return the created tracking record. Check the tracking number and carrier slug.")
    return normalize_tracking(created, "CREATED_WAITING_FOR_UPDATES")


def simulate_tracking(
    tracking_number: str,
    carrier_slug: str | None = None,
    expected_delivery_date: str | None = None,
) -> dict[str, Any]:
    """Return an explicitly labelled local demo scenario without network calls.

    DEMO-DELIVERED-... simulates a delivered scan; DEMO-EXCEPTION-... simulates
    a carrier exception; DEMO-MISMATCH-... deliberately disagrees with the local
    in-transit status and merchant-entered expected date. All other values
    simulate an in-transit shipment. None of these results are real carrier data.
    """
    if not isinstance(tracking_number, str) or len(tracking_number.strip()) < 5:
        raise CarrierTrackingError("Tracking number must contain at least 5 non-whitespace characters.")

    number = tracking_number.strip()
    upper = number.upper()
    if upper.startswith("DEMO-DELIVERED") or upper.startswith("DEMO-MISMATCH"):
        tag = "Delivered"
        message = "SIMULATED ONLY: demo scenario says delivered; this is not a real carrier event."
    elif upper.startswith("DEMO-EXCEPTION"):
        tag = "Exception"
        message = "SIMULATED ONLY: demo scenario reports a carrier exception; this is not real carrier data."
    else:
        tag = "InTransit"
        message = "SIMULATED ONLY: demo scenario says the parcel is in transit; this is not real carrier data."

    try:
        if isinstance(expected_delivery_date, str):
            base_date = date.fromisoformat(expected_delivery_date.strip()[:10])
        else:
            base_date = date.today() + timedelta(days=3)
    except ValueError:
        base_date = date.today() + timedelta(days=3)

    if upper.startswith("DEMO-MISMATCH"):
        simulated_date = base_date + timedelta(days=1)
    else:
        simulated_date = base_date

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    slug = (carrier_slug or "demo-carrier").strip().lower() or "demo-carrier"
    return {
        "provider": "ResolveOS Demo (SIMULATED)",
        "source_mode": "SIMULATED_DEMO",
        "tracking_number": number,
        "carrier_slug": slug,
        "carrier_name": "Simulated carrier — not a real carrier lookup",
        "provider_tracking_id": None,
        "tracking_status": tag,
        "status_message": message,
        "estimated_delivery_date": simulated_date.isoformat(),
        "courier_tracking_link": None,
        "latest_checkpoint": {
            "checkpoint_time": stamp,
            "message": f"SIMULATED ONLY: generated demo checkpoint for status {tag}; not sourced from a carrier.",
            "tag": tag,
            "subtag": "DEMO_ONLY",
            "location": "ResolveOS local demo environment (simulated)",
            "slug": slug,
        },
        "lookup_state": "SIMULATED_DEMO_DATA",
        "carrier_has_checkpoints": True,
        "demo_disclaimer": "SIMULATED DEMO DATA — not fetched from or verified by a real carrier.",
    }
