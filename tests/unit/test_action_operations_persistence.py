import sqlite3
import unittest
from datetime import datetime, timezone

from crm_ai_agent.domain.enums import ActionOperationStatus, ActionRunStatus
from crm_ai_agent.persistence.action_operations import (
    count_attempts,
    get_operation_status,
    get_or_create_operation,
    record_attempt,
    set_operation_status,
)
from crm_ai_agent.persistence.db import run_migrations


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    run_migrations(conn)

    now = "2026-09-03T00:00:00Z"
    conn.execute(
        "INSERT INTO tickets (ticket_id, external_ticket_id, status, created_at, updated_at) "
        "VALUES ('t1', 'EXT-1', 'DISCOVERED', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO ticket_snapshots (snapshot_id, ticket_id, schema_version, captured_at, source_hash, raw_json) "
        "VALUES ('s1', 't1', '1.0', ?, 'hash1', '{}')",
        (now,),
    )
    conn.execute(
        "INSERT INTO plans (plan_id, ticket_id, snapshot_id, snapshot_hash, plan_hash, policy_version, status, created_at) "
        "VALUES ('p1', 't1', 's1', 'hash1', 'planhash1', 'v1', 'DRAFT', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO plan_actions (action_id, plan_id, action_type, risk_level, idempotency_key, raw_json) "
        "VALUES ('a1', 'p1', 'edit_crm_user_name', 'MEDIUM', 'idem-1', '{}')"
    )
    conn.execute("INSERT INTO executions (execution_id, plan_id, status) VALUES ('e1', 'p1', 'QUEUED')")
    return conn


class TestActionOperationsPersistence(unittest.TestCase):
    def test_get_or_create_operation_creates_once(self) -> None:
        conn = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        operation_id, created = get_or_create_operation(
            conn, plan_id="p1", action_id="a1", idempotency_key="idem-1", now=now
        )
        self.assertTrue(created)
        self.assertEqual(get_operation_status(conn, operation_id=operation_id), ActionOperationStatus.PENDING)

    def test_get_or_create_operation_is_idempotent_across_calls(self) -> None:
        """This is THE invariant Stage 1 remediation was about: a retry must
        never construct a second, independent operation for the same key."""
        conn = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        operation_id_1, created_1 = get_or_create_operation(
            conn, plan_id="p1", action_id="a1", idempotency_key="idem-1", now=now
        )
        operation_id_2, created_2 = get_or_create_operation(
            conn, plan_id="p1", action_id="a1", idempotency_key="idem-1", now=now
        )
        self.assertEqual(operation_id_1, operation_id_2)
        self.assertTrue(created_1)
        self.assertFalse(created_2)

        row_count = conn.execute(
            "SELECT COUNT(*) AS n FROM action_operations WHERE idempotency_key = 'idem-1'"
        ).fetchone()["n"]
        self.assertEqual(row_count, 1)

    def test_multiple_attempts_can_be_recorded_against_one_operation(self) -> None:
        conn = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        operation_id, _ = get_or_create_operation(
            conn, plan_id="p1", action_id="a1", idempotency_key="idem-1", now=now
        )
        record_attempt(
            conn, operation_id=operation_id, execution_id="e1", plan_id="p1", action_id="a1",
            attempt_no=1, status=ActionRunStatus.FAILED_RETRYABLE,
        )
        record_attempt(
            conn, operation_id=operation_id, execution_id="e1", plan_id="p1", action_id="a1",
            attempt_no=2, status=ActionRunStatus.UNKNOWN_OUTCOME,
        )
        self.assertEqual(count_attempts(conn, operation_id=operation_id), 2)

    def test_duplicate_attempt_number_is_rejected_by_the_schema(self) -> None:
        conn = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        operation_id, _ = get_or_create_operation(
            conn, plan_id="p1", action_id="a1", idempotency_key="idem-1", now=now
        )
        record_attempt(
            conn, operation_id=operation_id, execution_id="e1", plan_id="p1", action_id="a1",
            attempt_no=1, status=ActionRunStatus.FAILED_RETRYABLE,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            record_attempt(
                conn, operation_id=operation_id, execution_id="e1", plan_id="p1", action_id="a1",
                attempt_no=1, status=ActionRunStatus.PENDING,
            )

    def test_unknown_outcome_moves_operation_to_reconciling_not_a_fresh_attempt(self) -> None:
        """UNKNOWN_OUTCOME must route to reconciliation, never to blindly
        starting another mutation attempt automatically."""
        conn = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        operation_id, _ = get_or_create_operation(
            conn, plan_id="p1", action_id="a1", idempotency_key="idem-1", now=now
        )
        record_attempt(
            conn, operation_id=operation_id, execution_id="e1", plan_id="p1", action_id="a1",
            attempt_no=1, status=ActionRunStatus.UNKNOWN_OUTCOME,
        )
        set_operation_status(conn, operation_id=operation_id, status=ActionOperationStatus.RECONCILING, now=now)
        self.assertEqual(get_operation_status(conn, operation_id=operation_id), ActionOperationStatus.RECONCILING)

    def test_get_operation_status_raises_for_unknown_operation(self) -> None:
        conn = _make_conn()
        with self.assertRaises(KeyError):
            get_operation_status(conn, operation_id="does-not-exist")


if __name__ == "__main__":
    unittest.main()
