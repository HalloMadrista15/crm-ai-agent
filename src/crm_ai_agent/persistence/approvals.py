"""Minimal approval persistence: save/load round-trip only.

This is deliberately not a full repository layer (that belongs to the
application/ports work in a later stage). Its only job right now is to prove
that everything a restarted process needs to judge "who was allowed to
approve this, and did they" survives a write/read cycle through SQLite:
the policy snapshot (version, allowed roles, TTL), the Telegram identity that
consumed it, and the decision.

Transaction boundary
---------------------
``save_approval`` issues an ``INSERT`` and nothing else — no ``commit()``,
no ``BEGIN``. Committing (or rolling back) is the caller's decision, because
in the real flow this insert is one write among several that must succeed or
fail together (e.g. persisting the approval row alongside an audit event and
an outbox notification). If this function committed on its own, a caller
could not compose it into such a unit of work: an earlier write in the same
logical operation could already be durably committed by the time a later
one fails, leaving the database in a state no rollback can undo.

The full transaction-management component (an explicit Unit of Work that
opens, commits, and rolls back around a whole use case) is Stage 2+ work.
Until then, callers — including this module's own tests — must wrap calls
here in their own explicit ``BEGIN``/``COMMIT``/``ROLLBACK``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from crm_ai_agent.domain.entities import Approval, ApprovalPolicySnapshot
from crm_ai_agent.domain.enums import ApprovalStatus


def _to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def save_approval(conn: sqlite3.Connection, approval: Approval) -> None:
    conn.execute(
        """
        INSERT INTO approvals (
            approval_id, ticket_id, plan_id, plan_hash, snapshot_hash,
            policy_version, allowed_approver_roles_json, ttl_seconds,
            status, telegram_user_id, telegram_chat_id,
            created_at, expires_at, consumed_at, decision_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            approval.approval_id,
            approval.ticket_id,
            approval.plan_id,
            approval.plan_hash,
            approval.snapshot_hash,
            approval.policy.policy_version,
            json.dumps(list(approval.policy.allowed_approver_roles)),
            approval.policy.ttl_seconds,
            approval.status.value,
            approval.telegram_user_id,
            approval.telegram_chat_id,
            _to_iso(approval.created_at),
            _to_iso(approval.expires_at),
            _to_iso(approval.consumed_at),
            approval.decision_reason,
        ),
    )


class ApprovalAlreadyConsumedError(Exception):
    pass


def update_decision(conn: sqlite3.Connection, approval: Approval) -> None:
    """Persist the outcome of consuming an approval (see
    safety/telegram_approval.consume_approval).

    Guarded by ``WHERE status = 'PENDING'`` so that even if two callbacks for
    the same approval race past the in-memory PENDING check in
    ``consume_approval`` at the same instant, at most one ``UPDATE`` actually
    changes a row — this is the atomic, one-time-consumption guarantee at
    the database level, not just in application logic. Raises
    ``ApprovalAlreadyConsumedError`` if no row was updated, so the caller
    (who already believed it held a PENDING approval) finds out it lost the
    race instead of silently believing its write succeeded.
    """

    cursor = conn.execute(
        """
        UPDATE approvals
        SET status = ?, telegram_user_id = ?, telegram_chat_id = ?,
            consumed_at = ?, decision_reason = ?
        WHERE approval_id = ? AND status = 'PENDING'
        """,
        (
            approval.status.value,
            approval.telegram_user_id,
            approval.telegram_chat_id,
            _to_iso(approval.consumed_at),
            approval.decision_reason,
            approval.approval_id,
        ),
    )
    if cursor.rowcount == 0:
        raise ApprovalAlreadyConsumedError(
            f"approval {approval.approval_id!r} was not in PENDING status at update time "
            "(concurrent consumption, or it does not exist)"
        )


def load_approval(conn: sqlite3.Connection, approval_id: str) -> Approval | None:
    row = conn.execute(
        "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
    ).fetchone()
    if row is None:
        return None
    return Approval(
        approval_id=row["approval_id"],
        ticket_id=row["ticket_id"],
        plan_id=row["plan_id"],
        plan_hash=row["plan_hash"],
        snapshot_hash=row["snapshot_hash"],
        policy=ApprovalPolicySnapshot(
            policy_version=row["policy_version"],
            allowed_approver_roles=tuple(json.loads(row["allowed_approver_roles_json"])),
            ttl_seconds=row["ttl_seconds"],
        ),
        status=ApprovalStatus(row["status"]),
        telegram_user_id=row["telegram_user_id"],
        telegram_chat_id=row["telegram_chat_id"],
        created_at=_from_iso(row["created_at"]),
        expires_at=_from_iso(row["expires_at"]),
        consumed_at=_from_iso(row["consumed_at"]),
        decision_reason=row["decision_reason"],
    )
