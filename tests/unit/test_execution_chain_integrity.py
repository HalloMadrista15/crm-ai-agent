"""Negative tests proving SQLite itself — not just application code — refuses
to link an action_operation/action_run to the wrong plan/execution chain.

These exercise the composite foreign keys added to migrations/0001_init.sql:
action_operations.(plan_id, action_id) -> plan_actions.(plan_id, action_id),
action_runs.(operation_id, plan_id, action_id) -> action_operations.(...),
action_runs.(execution_id, plan_id) -> executions.(execution_id, plan_id).
"""

import sqlite3
import unittest

from crm_ai_agent.persistence.db import run_migrations


def _seed_two_plans(conn: sqlite3.Connection) -> None:
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
    # Two independent plans for the same ticket (e.g. a superseding re-plan).
    for plan_id, plan_hash in (("pA", "planhashA"), ("pB", "planhashB")):
        conn.execute(
            "INSERT INTO plans (plan_id, ticket_id, snapshot_id, snapshot_hash, plan_hash, policy_version, status, created_at) "
            "VALUES (?, 't1', 's1', 'hash1', ?, 'v1', 'DRAFT', ?)",
            (plan_id, plan_hash, now),
        )
        conn.execute(
            "INSERT INTO plan_actions (action_id, plan_id, action_type, risk_level, idempotency_key, raw_json) "
            "VALUES (?, ?, 'edit_crm_user_name', 'MEDIUM', ?, '{}')",
            (f"a_{plan_id}", plan_id, f"idem_{plan_id}"),
        )
        conn.execute(
            "INSERT INTO executions (execution_id, plan_id, status) VALUES (?, ?, 'QUEUED')",
            (f"e_{plan_id}", plan_id),
        )


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    run_migrations(conn)
    _seed_two_plans(conn)
    return conn


class TestExecutionChainIntegrity(unittest.TestCase):
    def test_operation_cannot_claim_a_plan_action_from_a_different_plan(self) -> None:
        conn = _make_conn()
        now = "2026-09-03T00:00:00Z"
        # a_pA belongs to plan pA; claiming plan_id='pB' for it must fail
        # because no (plan_id='pB', action_id='a_pA') row exists in
        # plan_actions.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO action_operations (operation_id, plan_id, action_id, idempotency_key, status, created_at, updated_at) "
                "VALUES ('op_bad', 'pB', 'a_pA', 'idem_cross_1', 'PENDING', ?, ?)",
                (now, now),
            )

    def test_operation_with_correct_plan_action_pair_succeeds(self) -> None:
        conn = _make_conn()
        now = "2026-09-03T00:00:00Z"
        conn.execute(
            "INSERT INTO action_operations (operation_id, plan_id, action_id, idempotency_key, status, created_at, updated_at) "
            "VALUES ('op_ok', 'pA', 'a_pA', 'idem_pA', 'PENDING', ?, ?)",
            (now, now),
        )
        row = conn.execute(
            "SELECT * FROM action_operations WHERE operation_id = 'op_ok'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_action_run_cannot_pair_an_operation_with_an_execution_from_another_plan(self) -> None:
        conn = _make_conn()
        now = "2026-09-03T00:00:00Z"
        conn.execute(
            "INSERT INTO action_operations (operation_id, plan_id, action_id, idempotency_key, status, created_at, updated_at) "
            "VALUES ('op_pA', 'pA', 'a_pA', 'idem_pA', 'PENDING', ?, ?)",
            (now, now),
        )
        # op_pA belongs to plan pA, e_pB belongs to plan pB. A run claiming
        # plan_id='pB' here fails the operation-side FK (no such
        # (op_pA, pB, a_pA) row); a run claiming plan_id='pA' with
        # execution_id='e_pB' fails the execution-side FK (no such
        # (e_pB, pA) row). Either way, cross-plan pairing is impossible.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO action_runs (action_run_id, operation_id, execution_id, plan_id, action_id, attempt_no, status) "
                "VALUES ('ar_bad', 'op_pA', 'e_pB', 'pB', 'a_pA', 1, 'PENDING')"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO action_runs (action_run_id, operation_id, execution_id, plan_id, action_id, attempt_no, status) "
                "VALUES ('ar_bad2', 'op_pA', 'e_pB', 'pA', 'a_pA', 1, 'PENDING')"
            )

    def test_action_run_with_consistent_chain_succeeds(self) -> None:
        conn = _make_conn()
        now = "2026-09-03T00:00:00Z"
        conn.execute(
            "INSERT INTO action_operations (operation_id, plan_id, action_id, idempotency_key, status, created_at, updated_at) "
            "VALUES ('op_pA', 'pA', 'a_pA', 'idem_pA', 'PENDING', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO action_runs (action_run_id, operation_id, execution_id, plan_id, action_id, attempt_no, status) "
            "VALUES ('ar_ok', 'op_pA', 'e_pA', 'pA', 'a_pA', 1, 'PENDING')"
        )
        row = conn.execute("SELECT * FROM action_runs WHERE action_run_id = 'ar_ok'").fetchone()
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
