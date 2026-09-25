"""Idempotency: turning ``action_operations``/``action_runs`` (Stage 1
schema) into the actual "at most one operation per idempotency_key, any
number of attempts" behavior.

Transaction boundary: none of these commit; see ``persistence/approvals.py``
for why. A caller dispatching a mutation attempt should typically wrap
``get_or_create_operation`` and the subsequent ``record_attempt`` in one
transaction so the operation row and its first attempt appear together.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

from crm_ai_agent.domain.enums import ActionOperationStatus, ActionRunStatus


def get_or_create_operation(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    action_id: str,
    idempotency_key: str,
    now: datetime,
) -> tuple[str, bool]:
    """Return ``(operation_id, created)`` for ``idempotency_key``.

    Uses ``INSERT OR IGNORE`` against the ``UNIQUE(idempotency_key)``
    constraint from migrations/0001_init.sql: if a row for this key already
    exists, the insert is a no-op and the existing operation_id is returned
    with ``created=False``. This is what a caller retrying a mutation calls
    before dispatching another attempt — it must never construct a second,
    independent operation for the same logical change.
    """

    candidate_operation_id = str(uuid.uuid4())
    now_iso = now.isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO action_operations (
            operation_id, plan_id, action_id, idempotency_key, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_operation_id,
            plan_id,
            action_id,
            idempotency_key,
            ActionOperationStatus.PENDING.value,
            now_iso,
            now_iso,
        ),
    )
    row = conn.execute(
        "SELECT operation_id FROM action_operations WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    operation_id = row["operation_id"]
    return operation_id, operation_id == candidate_operation_id


def set_operation_status(
    conn: sqlite3.Connection, *, operation_id: str, status: ActionOperationStatus, now: datetime
) -> None:
    conn.execute(
        "UPDATE action_operations SET status = ?, updated_at = ? WHERE operation_id = ?",
        (status.value, now.isoformat(), operation_id),
    )


def get_operation_status(conn: sqlite3.Connection, *, operation_id: str) -> ActionOperationStatus:
    row = conn.execute(
        "SELECT status FROM action_operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no action_operation with operation_id={operation_id!r}")
    return ActionOperationStatus(row["status"])


def record_attempt(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    execution_id: str,
    plan_id: str,
    action_id: str,
    attempt_no: int,
    status: ActionRunStatus,
) -> str:
    """Append one attempt row. Multiple attempts per operation are expected
    (retries); the schema's ``UNIQUE(operation_id, attempt_no)`` constraint
    (Stage 1) is what stops the same attempt number being recorded twice.
    """

    action_run_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO action_runs (
            action_run_id, operation_id, execution_id, plan_id, action_id, attempt_no, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (action_run_id, operation_id, execution_id, plan_id, action_id, attempt_no, status.value),
    )
    return action_run_id


def count_attempts(conn: sqlite3.Connection, *, operation_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM action_runs WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    return row["n"]
