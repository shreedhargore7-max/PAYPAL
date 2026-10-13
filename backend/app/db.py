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
            CREATE TABLE IF NOT EXISTS case_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
                evidence_json TEXT NOT NULL,
                submitted_by TEXT NOT NULL DEFAULT 'MERCHANT',
                verification_status TEXT NOT NULL DEFAULT 'SUBMITTED_UNVERIFIED',
                created_at TEXT NOT NULL,
                reviewed_by TEXT,
                review_note TEXT,
                reviewed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_case_evidence_case_id
                ON case_evidence(case_id, id);
            CREATE TABLE IF NOT EXISTS carrier_tracking_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
                evidence_id TEXT NOT NULL,
                tracking_number TEXT NOT NULL,
                provider TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                checked_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_carrier_tracking_case_id
                ON carrier_tracking_checks(case_id, id);
            CREATE TABLE IF NOT EXISTS case_follow_up_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
                action_key TEXT NOT NULL,
                source_run_id TEXT,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                assigned_to TEXT NOT NULL DEFAULT 'MERCHANT',
                priority TEXT NOT NULL DEFAULT 'MEDIUM',
                status TEXT NOT NULL DEFAULT 'OPEN',
                completion_gate TEXT NOT NULL,
                blocked_reason TEXT,
                status_note TEXT,
                completion_note TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_by TEXT,
                completed_at TEXT,
                UNIQUE(case_id, action_key)
            );
            CREATE INDEX IF NOT EXISTS idx_follow_up_actions_case_id
                ON case_follow_up_actions(case_id, id);
            CREATE TABLE IF NOT EXISTS case_customer_message_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
                issue_type TEXT NOT NULL,
                draft_type TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'DRAFT',
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_customer_message_drafts_case_id
                ON case_customer_message_drafts(case_id, id);
            CREATE TABLE IF NOT EXISTS case_resolution_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
                summary_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'DRAFT',
                closure_outcome TEXT,
                closure_note TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_by TEXT,
                closed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_resolution_summaries_case_id
                ON case_resolution_summaries(case_id, id);
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
        # Backward-compatible migration for databases created by earlier versions.
        evidence_columns = {row["name"] for row in conn.execute("PRAGMA table_info(case_evidence)").fetchall()}
        for column, definition in (
            ("reviewed_by", "TEXT"),
            ("review_note", "TEXT"),
            ("reviewed_at", "TEXT"),
            ("verification_revoked_from", "TEXT"),
            ("verification_revoked_by", "TEXT"),
            ("verification_revoked_at", "TEXT"),
            ("verification_revocation_note", "TEXT"),
        ):
            if column not in evidence_columns:
                conn.execute(f"ALTER TABLE case_evidence ADD COLUMN {column} {definition}")

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


def update_case_policy_result(
    case_id: str,
    policy_decision: str,
    reasons: list[str],
    status: str,
) -> bool:
    """Persist a deterministic policy re-evaluation without touching refund fields."""
    with connect() as conn:
        cursor = conn.execute(
            """UPDATE cases
               SET policy_decision = ?, reasons_json = ?, status = ?, updated_at = ?
               WHERE case_id = ?
                 AND status NOT IN ('REFUND_SUBMITTING', 'REFUND_PENDING', 'REFUND_COMPLETED', 'REFUND_REQUIRES_RECONCILIATION')
                 AND (refund_status IS NULL OR refund_status NOT IN ('PENDING', 'COMPLETED'))""",
            (policy_decision, json.dumps(reasons), status, utc_now(), case_id),
        )
        return cursor.rowcount == 1


def create_case_evidence(
    case_id: str,
    evidence: dict[str, Any],
    submitted_by: str = "MERCHANT",
) -> dict[str, Any]:
    """Persist a merchant evidence submission as unverified input."""
    created_at = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """INSERT INTO case_evidence
               (case_id, evidence_json, submitted_by, verification_status, created_at)
               VALUES (?, ?, ?, 'SUBMITTED_UNVERIFIED', ?)""",
            (case_id, json.dumps(evidence, ensure_ascii=False), submitted_by, created_at),
        )
        row_id = cursor.lastrowid
    return {
        "evidence_id": f"EV-{int(row_id):06d}",
        "case_id": case_id,
        "evidence": evidence,
        "submitted_by": submitted_by,
        "verification_status": "SUBMITTED_UNVERIFIED",
        "created_at": created_at,
    }


def _evidence_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["evidence_id"] = f"EV-{int(item.pop('id')):06d}"
    item["evidence"] = json.loads(item.pop("evidence_json"))
    return item


def get_case_evidence(case_id: str, evidence_id: str) -> dict[str, Any] | None:
    """Fetch one evidence item by public ID (EV-000001)."""
    try:
        raw_id = evidence_id.removeprefix("EV-")
        if not raw_id.isdigit() or not evidence_id.startswith("EV-"):
            return None
        internal_id = int(raw_id)
    except (AttributeError, ValueError):
        return None
    with connect() as conn:
        row = conn.execute(
            """SELECT id, case_id, evidence_json, submitted_by,
                      verification_status, created_at, reviewed_by, review_note, reviewed_at,
                      verification_revoked_from, verification_revoked_by,
                      verification_revoked_at, verification_revocation_note
               FROM case_evidence WHERE case_id = ? AND id = ?""",
            (case_id, internal_id),
        ).fetchone()
    return _evidence_row_to_dict(row) if row is not None else None


def review_case_evidence(
    case_id: str,
    evidence_id: str,
    verification_status: str,
    reviewed_by: str,
    review_note: str,
) -> bool:
    """Atomically review a previously unverified record; does not alter case/payment state."""
    raw_id = evidence_id.removeprefix("EV-") if isinstance(evidence_id, str) else ""
    if not evidence_id.startswith("EV-") or not raw_id.isdigit():
        return False
    internal_id = int(raw_id)
    reviewed_at = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """UPDATE case_evidence
               SET verification_status = ?, reviewed_by = ?, review_note = ?, reviewed_at = ?
               WHERE case_id = ? AND id = ? AND verification_status = 'SUBMITTED_UNVERIFIED'""",
            (verification_status, reviewed_by, review_note, reviewed_at, case_id, internal_id),
        )
        return cursor.rowcount == 1


def revoke_case_evidence_verification(
    case_id: str,
    evidence_id: str,
    revoked_by: str,
    revocation_note: str,
) -> bool:
    """Revoke a prior VERIFIED label while preserving the original review metadata."""
    raw_id = evidence_id.removeprefix("EV-") if isinstance(evidence_id, str) else ""
    if not evidence_id.startswith("EV-") or not raw_id.isdigit():
        return False
    internal_id = int(raw_id)
    revoked_at = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """UPDATE case_evidence
               SET verification_status = 'VERIFICATION_REVOKED',
                   verification_revoked_from = 'VERIFIED',
                   verification_revoked_by = ?,
                   verification_revoked_at = ?,
                   verification_revocation_note = ?
               WHERE case_id = ? AND id = ? AND verification_status = 'VERIFIED'
                 AND verification_revoked_at IS NULL""",
            (revoked_by, revoked_at, revocation_note, case_id, internal_id),
        )
        return cursor.rowcount == 1


def list_case_evidence(case_id: str) -> list[dict[str, Any]]:
    """Return evidence history newest-first with submission and review metadata."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, case_id, evidence_json, submitted_by,
                      verification_status, created_at, reviewed_by, review_note, reviewed_at,
                      verification_revoked_from, verification_revoked_by,
                      verification_revoked_at, verification_revocation_note
               FROM case_evidence WHERE case_id = ? ORDER BY id DESC""",
            (case_id,),
        ).fetchall()
    return [_evidence_row_to_dict(row) for row in rows]


def create_carrier_tracking_check(
    case_id: str,
    evidence_id: str,
    tracking_number: str,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Persist a read-only carrier-provider snapshot and its comparison results."""
    checked_at = utc_now()
    provider = str(summary.get("provider") or "Unknown")
    with connect() as conn:
        cursor = conn.execute(
            """INSERT INTO carrier_tracking_checks
               (case_id, evidence_id, tracking_number, provider, summary_json, checked_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (case_id, evidence_id, tracking_number, provider,
             json.dumps(summary, ensure_ascii=False), checked_at),
        )
        row_id = cursor.lastrowid
    return {
        "check_id": f"TRK-{int(row_id):06d}",
        "case_id": case_id,
        "evidence_id": evidence_id,
        "tracking_number": tracking_number,
        "provider": provider,
        **summary,
        "checked_at": checked_at,
    }


def list_case_carrier_tracking_checks(case_id: str) -> list[dict[str, Any]]:
    """Return saved carrier lookups for a case, newest first."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, case_id, evidence_id, tracking_number, provider, summary_json, checked_at
               FROM carrier_tracking_checks WHERE case_id = ? ORDER BY id DESC""",
            (case_id,),
        ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["check_id"] = f"TRK-{int(item.pop('id')):06d}"
        try:
            summary = json.loads(item.pop("summary_json"))
        except (TypeError, ValueError):
            summary = {}
            item.pop("summary_json", None)
        if isinstance(summary, dict):
            # Keep persisted columns authoritative over duplicated summary fields.
            item.update({k: v for k, v in summary.items() if k not in item})
        output.append(item)
    return output


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


# Follow-up action persistence -------------------------------------------------

def _follow_up_action_row_to_dict(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["action_id"] = f"FU-{int(item.pop('id')):06d}"
    return item


def _follow_up_internal_id(action_id: str) -> int | None:
    if not isinstance(action_id, str) or not action_id.startswith("FU-"):
        return None
    digits = action_id[3:]
    return int(digits) if digits.isdigit() else None


def list_case_follow_up_actions(case_id: str) -> list[dict[str, Any]]:
    """Return persisted merchant follow-up actions in stable plan order."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM case_follow_up_actions
               WHERE case_id = ?
               ORDER BY CASE priority WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END,
                        id ASC""",
            (case_id,),
        ).fetchall()
    return [_follow_up_action_row_to_dict(row) for row in rows if row is not None]


def get_case_follow_up_action(case_id: str, action_id: str) -> dict[str, Any] | None:
    internal_id = _follow_up_internal_id(action_id)
    if internal_id is None:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM case_follow_up_actions WHERE case_id = ? AND id = ?",
            (case_id, internal_id),
        ).fetchone()
    return _follow_up_action_row_to_dict(row)


def upsert_case_follow_up_actions(
    case_id: str,
    source_run_id: str,
    action_specs: list[dict[str, Any]],
    *,
    created_by: str,
    satisfied_gates: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Upsert the current deterministic plan, retaining progress and history.

    Tasks omitted from the refreshed plan are only auto-completed when their
    explicit machine-checkable gate is satisfied. Manual-review tasks require
    an auditable merchant completion note instead.
    """
    now = utc_now()
    satisfied = {str(g).upper() for g in (satisfied_gates or set())}
    incoming_keys = {str(spec.get("action_key") or "") for spec in action_specs}
    with connect() as conn:
        existing_rows = conn.execute(
            "SELECT * FROM case_follow_up_actions WHERE case_id = ? ORDER BY id",
            (case_id,),
        ).fetchall()
        existing_by_key = {str(row["action_key"]): dict(row) for row in existing_rows}

        for spec in action_specs:
            key = str(spec.get("action_key") or "").strip()
            if not key:
                continue
            old = existing_by_key.get(key)
            status = str(spec.get("status") or "OPEN").upper()
            if status not in {"OPEN", "IN_PROGRESS", "BLOCKED"}:
                status = "OPEN"
            fields = (
                source_run_id,
                str(spec.get("title") or key)[:240],
                str(spec.get("description") or "")[:2000],
                str(spec.get("assigned_to") or "MERCHANT")[:80],
                str(spec.get("priority") or "MEDIUM").upper(),
                status,
                str(spec.get("completion_gate") or "MANUAL_ACKNOWLEDGEMENT").upper(),
                str(spec.get("blocked_reason") or "")[:1000] or None,
            )
            if old is None:
                conn.execute(
                    """INSERT INTO case_follow_up_actions
                       (case_id, action_key, source_run_id, title, description, assigned_to, priority,
                        status, completion_gate, blocked_reason, created_by, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (case_id, key, *fields, created_by, now, now),
                )
                continue

            old_status = str(old.get("status") or "OPEN").upper()
            reopen = bool(spec.get("reopen"))
            if old_status in {"COMPLETED", "AUTO_COMPLETED"} and not reopen:
                next_status = old_status
                completed_by = old.get("completed_by")
                completed_at = old.get("completed_at")
                completion_note = old.get("completion_note")
            else:
                next_status = status
                if old_status == "IN_PROGRESS" and status == "OPEN" and not reopen:
                    next_status = "IN_PROGRESS"
                completed_by = None
                completed_at = None
                completion_note = None
            update_fields = list(fields)
            update_fields[5] = next_status
            conn.execute(
                """UPDATE case_follow_up_actions
                   SET source_run_id = ?, title = ?, description = ?, assigned_to = ?, priority = ?,
                       status = ?, completion_gate = ?, blocked_reason = ?, updated_at = ?,
                       completed_by = ?, completed_at = ?, completion_note = ?
                   WHERE case_id = ? AND action_key = ?""",
                (*update_fields, now, completed_by, completed_at, completion_note, case_id, key),
            )

        # Resolve stale open tasks only if their explicit completion gate has
        # become true. This is a system observation, distinct from merchant review.
        for row in existing_rows:
            key = str(row["action_key"])
            status = str(row["status"] or "").upper()
            gate = str(row["completion_gate"] or "").upper()
            if key in incoming_keys or status in {"COMPLETED", "AUTO_COMPLETED"}:
                continue
            if status not in {"OPEN", "IN_PROGRESS", "BLOCKED"}:
                continue
            if gate in satisfied:
                conn.execute(
                    """UPDATE case_follow_up_actions
                       SET status = 'AUTO_COMPLETED', status_note = ?, updated_at = ?,
                           completed_by = 'SYSTEM', completed_at = ?, completion_note = ?, blocked_reason = NULL
                       WHERE case_id = ? AND action_key = ? AND status IN ('OPEN', 'IN_PROGRESS', 'BLOCKED')""",
                    ("Completion gate observed during follow-up plan refresh.", now, now,
                     f"System observed that completion gate {gate} is satisfied in saved state. This is not independent verification of an external source.", case_id, key),
                )
            else:
                conn.execute(
                    """UPDATE case_follow_up_actions
                       SET status = 'BLOCKED', blocked_reason = ?, updated_at = ?
                       WHERE case_id = ? AND action_key = ? AND status IN ('OPEN', 'IN_PROGRESS', 'BLOCKED')""",
                    ("This task is not active in the latest follow-up plan. Refresh the plan after prerequisite or refund-state changes.", now, case_id, key),
                )
    return list_case_follow_up_actions(case_id)


def update_case_follow_up_action(
    case_id: str,
    action_id: str,
    status: str,
    actor: str,
    note: str,
) -> dict[str, Any] | None:
    """Update one non-blocked action with an auditable merchant note."""
    internal_id = _follow_up_internal_id(action_id)
    normalized_status = str(status or "").upper()
    if internal_id is None or normalized_status not in {"IN_PROGRESS", "COMPLETED"}:
        return None
    now = utc_now()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM case_follow_up_actions WHERE case_id = ? AND id = ?",
            (case_id, internal_id),
        ).fetchone()
        if row is None:
            return None
        current_status = str(row["status"] or "").upper()
        if current_status not in {"OPEN", "IN_PROGRESS"}:
            return None
        if normalized_status == "COMPLETED":
            conn.execute(
                """UPDATE case_follow_up_actions
                   SET status = 'COMPLETED', status_note = ?, completion_note = ?, updated_at = ?,
                       completed_by = ?, completed_at = ?
                   WHERE case_id = ? AND id = ? AND status IN ('OPEN', 'IN_PROGRESS')""",
                (note, note, now, actor, now, case_id, internal_id),
            )
        else:
            conn.execute(
                """UPDATE case_follow_up_actions
                   SET status = 'IN_PROGRESS', status_note = ?, updated_at = ?
                   WHERE case_id = ? AND id = ? AND status IN ('OPEN', 'IN_PROGRESS')""",
                (note, now, case_id, internal_id),
            )
        updated = conn.execute(
            "SELECT * FROM case_follow_up_actions WHERE case_id = ? AND id = ?",
            (case_id, internal_id),
        ).fetchone()
    return _follow_up_action_row_to_dict(updated)


def complete_case_follow_up_action_by_key(
    case_id: str,
    action_key: str,
    actor: str,
    note: str,
) -> dict[str, Any] | None:
    """Complete the re-evaluation action only after a successful policy rerun."""
    now = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """UPDATE case_follow_up_actions
               SET status = 'COMPLETED', status_note = ?, completion_note = ?, updated_at = ?,
                   completed_by = ?, completed_at = ?
               WHERE case_id = ? AND action_key = ? AND completion_gate = 'CASE_REEVALUATED'
                 AND status IN ('OPEN', 'IN_PROGRESS')""",
            (note, note, now, actor, now, case_id, action_key),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM case_follow_up_actions WHERE case_id = ? AND action_key = ?",
            (case_id, action_key),
        ).fetchone()
    return _follow_up_action_row_to_dict(row)



def _message_draft_internal_id(draft_id: str) -> int | None:
    if not isinstance(draft_id, str) or not draft_id.startswith("MSG-"):
        return None
    digits = draft_id[4:]
    return int(digits) if digits.isdigit() else None


def _customer_message_draft_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["draft_id"] = f"MSG-{int(item.pop('id')):06d}"
    return item


def create_case_customer_message_draft(
    case_id: str,
    *,
    issue_type: str,
    draft_type: str,
    subject: str,
    body: str,
    created_by: str,
) -> dict[str, Any]:
    """Persist a customer-facing draft only. There is intentionally no send method."""
    created_at = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """INSERT INTO case_customer_message_drafts
               (case_id, issue_type, draft_type, subject, body, status, created_by, created_at, updated_by, updated_at)
               VALUES (?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?)""",
            (case_id, issue_type, draft_type, subject, body, created_by, created_at, created_by, created_at),
        )
        row_id = cursor.lastrowid
        row = conn.execute("SELECT * FROM case_customer_message_drafts WHERE id = ?", (row_id,)).fetchone()
    return _customer_message_draft_row_to_dict(row)


def list_case_customer_message_drafts(case_id: str) -> list[dict[str, Any]]:
    """Return persisted message drafts newest-first. Drafts are never sent by ResolveOS."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM case_customer_message_drafts WHERE case_id = ? ORDER BY id DESC",
            (case_id,),
        ).fetchall()
    return [_customer_message_draft_row_to_dict(row) for row in rows]


def get_case_customer_message_draft(case_id: str, draft_id: str) -> dict[str, Any] | None:
    internal_id = _message_draft_internal_id(draft_id)
    if internal_id is None:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM case_customer_message_drafts WHERE case_id = ? AND id = ?",
            (case_id, internal_id),
        ).fetchone()
    return _customer_message_draft_row_to_dict(row) if row is not None else None


def update_case_customer_message_draft(
    case_id: str,
    draft_id: str,
    *,
    subject: str,
    body: str,
    updated_by: str,
) -> bool:
    """Edit only an unsent DRAFT. This function cannot send or mark a message sent."""
    internal_id = _message_draft_internal_id(draft_id)
    if internal_id is None:
        return False
    with connect() as conn:
        cursor = conn.execute(
            """UPDATE case_customer_message_drafts
               SET subject = ?, body = ?, updated_by = ?, updated_at = ?
               WHERE case_id = ? AND id = ? AND status = 'DRAFT'""",
            (subject, body, updated_by, utc_now(), case_id, internal_id),
        )
        return cursor.rowcount == 1


# Resolution summary and explicit case closure ---------------------------------

def _resolution_summary_row_to_dict(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["summary_id"] = f"RS-{int(item.pop('id')):06d}"
    try:
        item["summary"] = json.loads(item.pop("summary_json"))
    except (TypeError, ValueError):
        item["summary"] = {}
        item.pop("summary_json", None)
    return item


def _resolution_summary_internal_id(summary_id: str) -> int | None:
    if not isinstance(summary_id, str) or not summary_id.startswith("RS-"):
        return None
    digits = summary_id[3:]
    return int(digits) if digits.isdigit() else None


def create_case_resolution_summary(
    case_id: str,
    summary: dict[str, Any],
    created_by: str,
) -> dict[str, Any]:
    """Persist a versioned resolution summary draft without closing or mutating the case."""
    stamp = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """INSERT INTO case_resolution_summaries
               (case_id, summary_json, status, created_by, created_at, updated_by, updated_at)
               VALUES (?, ?, 'DRAFT', ?, ?, ?, ?)""",
            (case_id, json.dumps(summary, ensure_ascii=False, default=str), created_by, stamp, created_by, stamp),
        )
        row = conn.execute("SELECT * FROM case_resolution_summaries WHERE id = ?", (cursor.lastrowid,)).fetchone()
    result = _resolution_summary_row_to_dict(row)
    assert result is not None
    return result


def list_case_resolution_summaries(case_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM case_resolution_summaries WHERE case_id = ? ORDER BY id DESC",
            (case_id,),
        ).fetchall()
    return [_resolution_summary_row_to_dict(row) for row in rows if row is not None]


def get_case_resolution_summary(case_id: str, summary_id: str) -> dict[str, Any] | None:
    internal_id = _resolution_summary_internal_id(summary_id)
    if internal_id is None:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM case_resolution_summaries WHERE case_id = ? AND id = ?",
            (case_id, internal_id),
        ).fetchone()
    return _resolution_summary_row_to_dict(row)


def close_case_with_resolution(
    case_id: str,
    summary_id: str,
    *,
    new_case_status: str,
    closure_outcome: str,
    closure_note: str,
    actor: str,
) -> dict[str, Any] | None:
    """Atomically close a case against a saved draft summary and audit the disposition."""
    internal_id = _resolution_summary_internal_id(summary_id)
    if internal_id is None:
        return None
    stamp = utc_now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        case_row = conn.execute("SELECT status FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        if case_row is None or str(case_row["status"] or "").startswith("CLOSED_"):
            return None
        summary_row = conn.execute(
            "SELECT status FROM case_resolution_summaries WHERE case_id = ? AND id = ?",
            (case_id, internal_id),
        ).fetchone()
        if summary_row is None or summary_row["status"] != "DRAFT":
            return None
        updated_case = conn.execute(
            """UPDATE cases SET status = ?, updated_at = ?
               WHERE case_id = ? AND status NOT LIKE 'CLOSED_%' AND status <> 'CLOSED_NO_ACTION'
                 AND status NOT IN ('REFUND_SUBMITTING', 'REFUND_PENDING', 'REFUND_REQUIRES_RECONCILIATION')""",
            (new_case_status, stamp, case_id),
        )
        if updated_case.rowcount != 1:
            return None
        updated_summary = conn.execute(
            """UPDATE case_resolution_summaries
               SET status = 'CLOSED', closure_outcome = ?, closure_note = ?,
                   updated_by = ?, updated_at = ?, closed_by = ?, closed_at = ?
               WHERE case_id = ? AND id = ? AND status = 'DRAFT'""",
            (closure_outcome, closure_note, actor, stamp, actor, stamp, case_id, internal_id),
        )
        if updated_summary.rowcount != 1:
            raise RuntimeError("Resolution summary changed while closing the case")
        conn.execute(
            """INSERT INTO audit_log (case_id, actor, action, details_json, created_at)
               VALUES (?, ?, 'CASE_CLOSED', ?, ?)""",
            (case_id, actor, json.dumps({
                "summary_id": summary_id,
                "closure_outcome": closure_outcome,
                "case_status": new_case_status,
                "note": closure_note,
                "message": "Case closed by explicit merchant disposition. No payment action was executed by case closure.",
            }, ensure_ascii=False), stamp),
        )
        row = conn.execute(
            "SELECT * FROM case_resolution_summaries WHERE case_id = ? AND id = ?",
            (case_id, internal_id),
        ).fetchone()
    return _resolution_summary_row_to_dict(row)
