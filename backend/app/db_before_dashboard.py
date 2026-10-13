import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(os.getenv("RESOLVEOS_DB_PATH", Path(__file__).resolve().parent.parent / "resolveos.db"))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                customer_email TEXT NOT NULL,
                item_name TEXT NOT NULL,
                amount TEXT NOT NULL,
                currency TEXT NOT NULL,
                shipping_status TEXT NOT NULL,
                carrier TEXT,
                tracking_number TEXT,
                payment_status TEXT NOT NULL,
                paypal_capture_id TEXT,
                refund_status TEXT NOT NULL DEFAULT 'NONE',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES orders(order_id),
                customer_message TEXT NOT NULL,
                issue_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                policy_decision TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence REAL NOT NULL,
                paypal_request_id TEXT,
                paypal_refund_id TEXT,
                refund_status TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        existing = conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
        if existing == 0:
            now = datetime.now(timezone.utc)
            seed_orders = [
                ("ORD-1001", "buyer1@example.test", "Wireless Headphones", "89.99", "USD", "NOT_SHIPPED", None, None, "COMPLETED", None, "NONE", (now - timedelta(days=4)).isoformat()),
                ("ORD-1002", "buyer2@example.test", "Mechanical Keyboard", "129.00", "USD", "DELIVERED", "DemoShip", "DEMO-TRACK-2002", "COMPLETED", None, "NONE", (now - timedelta(days=8)).isoformat()),
                ("ORD-1003", "buyer3@example.test", "Webcam", "59.50", "USD", "IN_TRANSIT", "DemoShip", "DEMO-TRACK-2003", "COMPLETED", None, "NONE", (now - timedelta(days=2)).isoformat()),
            ]
            conn.executemany(
                """INSERT INTO orders (order_id, customer_email, item_name, amount, currency,
                   shipping_status, carrier, tracking_number, payment_status, paypal_capture_id,
                   refund_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                seed_orders,
            )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def list_orders() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_order(order_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone())


def link_capture(order_id: str, capture_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE orders SET paypal_capture_id = ? WHERE order_id = ?", (capture_id, order_id))


def create_case(case: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO cases (case_id, order_id, customer_message, issue_type, summary,
               policy_decision, reasons_json, status, confidence, paypal_request_id,
               paypal_refund_id, refund_status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (case["case_id"], case["order_id"], case["customer_message"], case["issue_type"],
             case["summary"], case["policy_decision"], json.dumps(case["reasons"]), case["status"],
             case["confidence"], case.get("paypal_request_id"), case.get("paypal_refund_id"),
             case.get("refund_status"), case["created_at"], case["updated_at"]),
        )


def list_cases(limit: int = 100) -> list[dict[str, Any]]:
    """Return recent cases ordered by creation time, newest first."""
    safe_limit = max(1, min(int(limit), 500))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cases ORDER BY created_at DESC LIMIT ?", (safe_limit,)
        ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["reasons"] = json.loads(item.pop("reasons_json"))
            output.append(item)
        return output


def get_case(case_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["reasons"] = json.loads(result.pop("reasons_json"))
        return result


def update_case(case_id: str, **fields: Any) -> None:
    if not fields:
        return
    allowed = {"status", "policy_decision", "paypal_request_id", "paypal_refund_id", "refund_status", "updated_at"}
    if not set(fields).issubset(allowed):
        raise ValueError("Unsupported case update field")
    fields["updated_at"] = utc_now()
    assignment = ", ".join(f"{field} = ?" for field in fields)
    values = list(fields.values()) + [case_id]
    with connect() as conn:
        conn.execute(f"UPDATE cases SET {assignment} WHERE case_id = ?", values)


def claim_refund_submission(case_id: str, request_id: str) -> bool:
    """Atomically reserve a refund submission for one eligible case.

    The conditional UPDATE is the concurrency guard: only the request that
    changes AWAITING_MERCHANT_APPROVAL to REFUND_SUBMITTING may call PayPal.
    """
    with connect() as conn:
        cursor = conn.execute(
            """UPDATE cases
               SET status = 'REFUND_SUBMITTING', paypal_request_id = ?, updated_at = ?
               WHERE case_id = ?
                 AND status = 'AWAITING_MERCHANT_APPROVAL'
                 AND (refund_status IS NULL OR refund_status NOT IN ('COMPLETED', 'PENDING'))
            """,
            (request_id, utc_now(), case_id),
        )
        return cursor.rowcount == 1


def log_audit(case_id: str, actor: str, action: str, details: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_log (case_id, actor, action, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (case_id, actor, action, json.dumps(details, default=str), utc_now()),
        )


def get_audit(case_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM audit_log WHERE case_id = ? ORDER BY id", (case_id,)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            output.append(item)
        return output
