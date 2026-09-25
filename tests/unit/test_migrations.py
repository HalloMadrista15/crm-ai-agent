import sqlite3
import tempfile
import unittest
from pathlib import Path

from crm_ai_agent.persistence.db import MigrationError, run_migrations


class TestMigrations(unittest.TestCase):
    def test_migrations_create_expected_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        applied = run_migrations(conn)
        self.assertIn("0001_init.sql", applied)

        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        expected = {
            "tickets",
            "ticket_snapshots",
            "llm_interpretations",
            "resolutions",
            "plans",
            "plan_actions",
            "action_operations",
            "action_runs",
            "approvals",
            "executions",
            "locks",
            "audit_events",
            "artifacts",
            "outbox_messages",
        }
        self.assertTrue(expected.issubset(tables))

    def test_migrations_are_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        first_run = run_migrations(conn)
        second_run = run_migrations(conn)
        self.assertEqual(first_run, ["0001_init.sql"])
        self.assertEqual(second_run, [])

    def test_failed_migration_is_fully_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            migrations_dir = Path(tmp)
            (migrations_dir / "0001_good.sql").write_text(
                "CREATE TABLE t_ok (id TEXT PRIMARY KEY);",
                encoding="utf-8",
            )
            (migrations_dir / "0002_bad.sql").write_text(
                "CREATE TABLE t_partial (id TEXT PRIMARY KEY);\n"
                "THIS IS NOT VALID SQL;",
                encoding="utf-8",
            )

            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row

            # Both files are pending on this first call; 0001 commits, then
            # 0002 fails and the exception propagates before a value is
            # returned — so we assert on persisted state, not a return value.
            with self.assertRaises(MigrationError):
                run_migrations(conn, migrations_dir=migrations_dir)

            with self.assertRaises(MigrationError):
                run_migrations(conn, migrations_dir=migrations_dir)

            tables = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            # The first statement of the bad migration must NOT have been
            # left behind: the whole file is one transaction.
            self.assertNotIn("t_partial", tables)

            recorded = {
                row["filename"] for row in conn.execute("SELECT filename FROM schema_migrations")
            }
            self.assertEqual(recorded, {"0001_good.sql"})

            # A subsequent call must not re-attempt the already-applied
            # migration, only retry the still-failing one.
            with self.assertRaises(MigrationError):
                run_migrations(conn, migrations_dir=migrations_dir)

    def test_idempotency_key_allows_multiple_attempts_but_one_operation(self) -> None:
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
        conn.execute(
            "INSERT INTO executions (execution_id, plan_id, status) VALUES ('e1', 'p1', 'QUEUED')"
        )
        conn.execute(
            "INSERT INTO action_operations (operation_id, plan_id, action_id, idempotency_key, status, created_at, updated_at) "
            "VALUES ('op1', 'p1', 'a1', 'idem-1', 'PENDING', ?, ?)",
            (now, now),
        )

        # A second, independent operation for the SAME idempotency_key must
        # be rejected: this is the invariant that stops two independent
        # mutations from ever being dispatched for one logical change.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO action_operations (operation_id, plan_id, action_id, idempotency_key, status, created_at, updated_at) "
                "VALUES ('op2', 'p1', 'a1', 'idem-1', 'PENDING', ?, ?)",
                (now, now),
            )

        # But multiple attempts (action_runs) against the SAME operation are
        # exactly what retries need, and must be allowed.
        conn.execute(
            "INSERT INTO action_runs (action_run_id, operation_id, execution_id, plan_id, action_id, attempt_no, status) "
            "VALUES ('ar1', 'op1', 'e1', 'p1', 'a1', 1, 'FAILED_RETRYABLE')"
        )
        conn.execute(
            "INSERT INTO action_runs (action_run_id, operation_id, execution_id, plan_id, action_id, attempt_no, status) "
            "VALUES ('ar2', 'op1', 'e1', 'p1', 'a1', 2, 'UNKNOWN_OUTCOME')"
        )
        attempts = conn.execute(
            "SELECT COUNT(*) AS n FROM action_runs WHERE operation_id = 'op1'"
        ).fetchone()["n"]
        self.assertEqual(attempts, 2)

        # A duplicate attempt_no for the same operation is not allowed.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO action_runs (action_run_id, operation_id, execution_id, plan_id, action_id, attempt_no, status) "
                "VALUES ('ar3', 'op1', 'e1', 'p1', 'a1', 2, 'PENDING')"
            )


if __name__ == "__main__":
    unittest.main()
