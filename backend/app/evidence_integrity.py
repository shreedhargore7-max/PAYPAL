"""Evidence integrity helpers for reconciliation of legacy review decisions.

These rules detect contradictions in saved merchant-entered records. They do not
independently authenticate external evidence or fetch any source URL.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


CONTRADICTORY_REVIEW_PHRASES = (
    "test only",
    "test data only",
    "demo only",
    "placeholder",
    "dummy data",
    "fake data",
    "no verifiable evidence",
    "no verifiable proof",
    "not verifiable",
    "not verified",
    "could not verify",
    "cannot verify",
    "unable to verify",
    "no real evidence",
    "not actual evidence",
    "not real carrier evidence",
    "unverified information",
)

PLACEHOLDER_DATA_PHRASES = (
    "test only",
    "test data only",
    "demo only",
    "placeholder",
    "dummy data",
    "fake data",
    "not actual evidence",
    "no real evidence",
    "not real carrier evidence",
)


def _normalize(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _looks_contradictory_review_note(value: Any) -> bool:
    normalized = _normalize(value)
    return bool(normalized) and any(phrase in normalized for phrase in CONTRADICTORY_REVIEW_PHRASES)


def _is_placeholder_source(value: Any) -> bool:
    normalized = _normalize(value)
    if not normalized:
        return True
    try:
        parsed = urlparse(str(value).strip())
        host = (parsed.hostname or "").lower()
    except ValueError:
        return True
    return (
        host in {"example.com", "example.org", "example.net", "example.test", "invalid.local"}
        or host.endswith(".example")
        or "/test-evidence" in (parsed.path or "").lower()
    )


def _has_placeholder_payload(evidence: Any) -> bool:
    if not isinstance(evidence, dict):
        return False
    source = evidence.get("source_reference")
    if source and _is_placeholder_source(source):
        return True
    for key in ("tracking_number", "latest_carrier_scan", "carrier_investigation", "evidence_notes"):
        value = evidence.get(key)
        normalized = _normalize(value)
        if normalized and any(phrase in normalized for phrase in PLACEHOLDER_DATA_PHRASES):
            return True
        if key == "tracking_number" and normalized.startswith(("test-only-", "test-", "demo-only-")):
            return True
    return False


def historical_verification_problem(record: dict[str, Any]) -> str | None:
    """Return why a VERIFIED record needs correction, or None if no heuristic flag applies.

    A `VERIFIED` status always remains a merchant-entered review decision; this helper
    only flags explicit contradictions or missing/placeholder source references.
    """
    if _normalize(record.get("verification_status")) != "verified":
        return None
    note = record.get("review_note")
    if _looks_contradictory_review_note(note):
        return "The historical review note describes test, placeholder, or unverified information."
    evidence = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
    source = evidence.get("source_reference")
    if not source:
        return "No source reference was saved with the historical VERIFIED decision."
    if _is_placeholder_source(source):
        return "The saved source reference appears to be a placeholder or demonstration URL."
    if _has_placeholder_payload(evidence):
        return "One or more evidence fields contain explicit test or placeholder data."
    return None


def evidence_integrity_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "total": len(records),
        "verified": 0,
        "historically_marked_verified": 0,
        "historical_verified_needing_review": 0,
        "verification_revoked": 0,
        "rejected": 0,
        "unverified": 0,
        "other": 0,
    }
    for record in records:
        status = _normalize(record.get("verification_status")).upper()
        if status == "VERIFIED":
            counts["historically_marked_verified"] += 1
            if historical_verification_problem(record):
                counts["historical_verified_needing_review"] += 1
            else:
                counts["verified"] += 1
        elif status == "VERIFICATION_REVOKED":
            counts["verification_revoked"] += 1
        elif status == "REJECTED":
            counts["rejected"] += 1
        elif status == "SUBMITTED_UNVERIFIED":
            counts["unverified"] += 1
        else:
            counts["other"] += 1
    return counts


def evidence_integrity_issues(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for record in records:
        status = _normalize(record.get("verification_status")).upper()
        evidence_id = str(record.get("evidence_id") or "unknown")
        if status == "VERIFICATION_REVOKED":
            issues.append({
                "evidence_id": evidence_id,
                "status": status,
                "reason": str(record.get("verification_revocation_note") or "A merchant revoked the historical verification decision."),
                "recommended_action": "Review the new evidence record; this invalidated record no longer counts as verified.",
            })
            continue
        reason = historical_verification_problem(record)
        if reason:
            issues.append({
                "evidence_id": evidence_id,
                "status": "HISTORICAL_VERIFIED_NEEDS_REVIEW",
                "reason": reason,
                "recommended_action": "Use the audited Revoke verification action after reviewing the saved record.",
            })
    return issues
