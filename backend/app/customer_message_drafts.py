"""Deterministic, editable customer follow-up drafts for ResolveOS.

This module only creates drafts. It never sends email, SMS, chat messages, or
payment actions. Templates deliberately avoid claiming that demo tracking data
is real or promising a particular resolution.
"""
from __future__ import annotations

from typing import Any


def build_customer_message_draft(case: dict[str, Any], order: dict[str, Any]) -> dict[str, str]:
    """Return a safe, issue-specific message draft with no unsupported claims."""
    issue = str(case.get("issue_type") or "OTHER").upper()
    item = " ".join(str(order.get("item_name") or "your order").split())[:120]
    order_id = " ".join(str(order.get("order_id") or case.get("order_id") or "").split())[:40]
    order_line = f" (order {order_id})" if order_id else ""

    if issue == "ITEM_NOT_RECEIVED":
        subject = f"Update on your delivery inquiry{order_line}"
        body = (
            f"Hello,\n\n"
            f"Thank you for letting us know that {item} has not arrived. We're reviewing your report and the available order information so we can provide an accurate update.\n\n"
            "Could you please confirm the delivery address shown on your order and let us know whether anyone at that address, a neighbour, building reception, or a designated safe place may have received the parcel? If you received a delivery notice, you can share its details too.\n\n"
            "Please do not send passwords, one-time codes, or full payment-card details. We will review the information and follow up when we have an update. No final decision has been made yet.\n\n"
            "Thank you for your patience,\nCustomer Support"
        )
        draft_type = "MISSING_DELIVERY_INFORMATION_REQUEST"
    elif issue == "WRONG_ITEM":
        subject = f"Help us review your item concern{order_line}"
        body = (
            f"Hello,\n\n"
            f"Thank you for contacting us about the item received for {item}. We'd like to review what arrived and compare it with the order details.\n\n"
            "Could you share clear photos of the item you received, its packaging, and the shipping label or packing slip if available? Please keep the item and packaging while we review your report.\n\n"
            "Please avoid including passwords or full payment-card details. We will review the information before confirming the next step.\n\n"
            "Thank you,\nCustomer Support"
        )
        draft_type = "WRONG_ITEM_EVIDENCE_REQUEST"
    elif issue == "DUPLICATE_CHARGE":
        subject = f"Information needed to review a possible duplicate charge{order_line}"
        body = (
            "Hello,\n\n"
            "Thank you for reporting a possible duplicate charge. We need to compare the relevant transaction records before drawing a conclusion.\n\n"
            "Could you share the dates, amounts, currencies, and transaction references shown for the two entries, and let us know whether both entries are marked completed or one is still pending? Please do not send your password, one-time codes, or full card number.\n\n"
            "We will review the details and follow up with the next step.\n\n"
            "Thank you,\nCustomer Support"
        )
        draft_type = "DUPLICATE_CHARGE_DETAILS_REQUEST"
    elif issue == "REFUND_NOT_RECEIVED":
        subject = f"Information needed to check your refund{order_line}"
        body = (
            "Hello,\n\n"
            "Thank you for letting us know that you have not received your refund. We'll review the existing refund record and its latest status.\n\n"
            "If available, could you share the refund confirmation reference and the date the refund was initiated? Please do not send account passwords, one-time codes, or full payment-card details.\n\n"
            "We will check the record and follow up with an update.\n\n"
            "Thank you,\nCustomer Support"
        )
        draft_type = "REFUND_STATUS_INFORMATION_REQUEST"
    else:
        subject = f"More information needed to review your request{order_line}"
        body = (
            "Hello,\n\n"
            "Thank you for contacting us. We are reviewing your request and would like to make sure we understand the issue correctly.\n\n"
            "Could you share a few more details about what happened and when you first noticed the problem? Please include any relevant order confirmation or receipt details, but do not send passwords, one-time codes, or full payment-card details.\n\n"
            "We will review the information and follow up with the next step.\n\n"
            "Thank you,\nCustomer Support"
        )
        draft_type = "GENERAL_INFORMATION_REQUEST"

    return {
        "subject": subject[:180],
        "body": body[:5000],
        "issue_type": issue,
        "draft_type": draft_type,
        "status": "DRAFT",
    }
