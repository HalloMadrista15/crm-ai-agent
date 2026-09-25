"""Negative tests proving SQLite refuses to link a plan/approval to a
snapshot/plan that belongs to a DIFFERENT ticket than the one it claims.

Exercises the composite foreign keys added to migrations/0001_init.sql:
plans.(ticket_id, snapshot_id) -> ticket_snapshots.(ticket_id, snapshot_id),
approvals.(ticket_id, plan_id) -> plans.(ticket_id, plan_id).
"""

import sqlite3
import unittest

from crm_ai_agent.persistence.db import run_migrations


def _seed_two_tickets(conn: sqlite3.Connection) -> None:
    now = "2026-09-03T00:00:00Z"
    for ticket_id, external_id in (("tA", "EXT-A"), ("tB", "EXT-B")):
        conn.execute(
            "INSERT INTO tickets (ticket_id, external_ticket_id, status, created_at, updated_at) "
            "VALUES (?, ?, 'DISCOVERED', ?, ?)",
            (ticket_id, external_id, now, now),
        )
        conn.execute(
            "INSERT INTO ticket_snapshots (snapshot_id, ticket_id, schema_version, captured_at, source_hash, raw_json) "
            "VALUES (?, ?, '1.0', ?, ?, '{}')",
            (f"s_{ticket_id}", ticket_id, now, f"hash_{ticket_id}"),
        )


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    run_migrations(conn)
    _seed_two_tickets(conn)
    return conn


class TestCrossTicketIntegrity(unittest.TestCase):
    def test_plan_cannot_claim_a_snapshot_from_a_different_ticket(self) -> None:
        conn = _make_conn()
        now = "2026-09-03T00:00:00Z"
        # s_tB belongs to ticket tB; a plan claiming ticket_id='tA' with
        # snapshot_id='s_tB' must fail because no (ticket_id='tA',
        # snapshot_id='s_tB') row exists in ticket_snapshots.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO plans (plan_id, ticket_id, snapshot_id, snapshot_hash, plan_hash, policy_version, status, created_at) "
                "VALUES ('p_bad', 'tA', 's_tB', 'hash_tB', 'planhash1', 'v1', 'DRAFT', ?)",
                (now,),
            )

    def test_plan_with_matching_ticket_and_snapshot_succeeds(self) -> None:
        conn = _make_conn()
        now = "2026-09-03T00:00:00Z"
        conn.execute(
            "INSERT INTO plans (plan_id, ticket_id, snapshot_id, snapshot_hash, plan_hash, policy_version, status, created_at) "
            "VALUES ('p_ok', 'tA', 's_tA', 'hash_tA', 'planhash1', 'v1', 'DRAFT', ?)",
            (now,),
        )
        row = conn.execute("SELECT * FROM plans WHERE plan_id = 'p_ok'").fetchone()
        self.assertIsNotNone(row)

    def test_approval_cannot_claim_a_plan_from_a_different_ticket(self) -> None:
        conn = _make_conn()
        now = "2026-09-03T00:00:00Z"
        conn.execute(
            "INSERT INTO plans (plan_id, ticket_id, snapshot_id, snapshot_hash, plan_hash, policy_version, status, created_at) "
            "VALUES ('p_tA', 'tA', 's_tA', 'hash_tA', 'planhash_tA', 'v1', 'DRAFT', ?)",
            (now,),
        )
        # p_tA belongs to ticket tA; an approval claiming ticket_id='tB'
        # with plan_id='p_tA' must fail because no (ticket_id='tB',
        # plan_id='p_tA') row exists in plans.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO approvals ("
                "  approval_id, ticket_id, plan_id, plan_hash, snapshot_hash,"
                "  policy_version, allowed_approver_roles_json, ttl_seconds, status, created_at"
                ") VALUES ('appr_bad', 'tB', 'p_tA', 'planhash_tA', 'hash_tA', 'v1', '[]', 3600, 'PENDING', ?)",
                (now,),
            )

    def test_approval_with_matching_ticket_and_plan_succeeds(self) -> None:
        conn = _make_conn()
        now = "2026-09-03T00:00:00Z"
        conn.execute(
            "INSERT INTO plans (plan_id, ticket_id, snapshot_id, snapshot_hash, plan_hash, policy_version, status, created_at) "
            "VALUES ('p_tA', 'tA', 's_tA', 'hash_tA', 'planhash_tA', 'v1', 'DRAFT', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO approvals ("
            "  approval_id, ticket_id, plan_id, plan_hash, snapshot_hash,"
            "  policy_version, allowed_approver_roles_json, ttl_seconds, status, created_at"
            ") VALUES ('appr_ok', 'tA', 'p_tA', 'planhash_tA', 'hash_tA', 'v1', '[]', 3600, 'PENDING', ?)",
            (now,),
        )
        row = conn.execute("SELECT * FROM approvals WHERE approval_id = 'appr_ok'").fetchone()
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
