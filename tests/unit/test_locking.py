import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from crm_ai_agent.persistence.db import run_migrations
from crm_ai_agent.safety.locking import LockError, LockHeldError, acquire_lock, release_lock


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    run_migrations(conn)
    return conn


class TestLocking(unittest.TestCase):
    def test_acquire_fresh_lock_succeeds(self) -> None:
        conn = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        acquire_lock(conn, lock_key="ticket:t1", holder="worker-1", ttl_seconds=60, now=now)
        row = conn.execute("SELECT holder FROM locks WHERE lock_key = 'ticket:t1'").fetchone()
        self.assertEqual(row["holder"], "worker-1")

    def test_acquiring_a_live_lock_held_by_another_worker_raises(self) -> None:
        conn = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        acquire_lock(conn, lock_key="ticket:t1", holder="worker-1", ttl_seconds=60, now=now)
        with self.assertRaises(LockHeldError):
            acquire_lock(conn, lock_key="ticket:t1", holder="worker-2", ttl_seconds=60, now=now)

    def test_same_holder_can_renew_its_own_lock(self) -> None:
        conn = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        acquire_lock(conn, lock_key="ticket:t1", holder="worker-1", ttl_seconds=60, now=now)
        later = now + timedelta(seconds=30)
        acquire_lock(conn, lock_key="ticket:t1", holder="worker-1", ttl_seconds=60, now=later)
        row = conn.execute("SELECT expires_at FROM locks WHERE lock_key = 'ticket:t1'").fetchone()
        self.assertEqual(row["expires_at"], (later + timedelta(seconds=60)).isoformat())

    def test_expired_lock_can_be_stolen_by_another_worker(self) -> None:
        conn = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        acquire_lock(conn, lock_key="ticket:t1", holder="worker-1", ttl_seconds=60, now=now)
        after_expiry = now + timedelta(seconds=61)
        acquire_lock(conn, lock_key="ticket:t1", holder="recovery-worker", ttl_seconds=60, now=after_expiry)
        row = conn.execute("SELECT holder FROM locks WHERE lock_key = 'ticket:t1'").fetchone()
        self.assertEqual(row["holder"], "recovery-worker")

    def test_release_by_non_holder_raises(self) -> None:
        conn = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        acquire_lock(conn, lock_key="ticket:t1", holder="worker-1", ttl_seconds=60, now=now)
        with self.assertRaises(LockError):
            release_lock(conn, lock_key="ticket:t1", holder="worker-2")

    def test_release_by_holder_removes_the_lock(self) -> None:
        conn = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        acquire_lock(conn, lock_key="ticket:t1", holder="worker-1", ttl_seconds=60, now=now)
        release_lock(conn, lock_key="ticket:t1", holder="worker-1")
        row = conn.execute("SELECT * FROM locks WHERE lock_key = 'ticket:t1'").fetchone()
        self.assertIsNone(row)

    def test_release_of_nonexistent_lock_is_a_no_op(self) -> None:
        conn = _make_conn()
        release_lock(conn, lock_key="ticket:does-not-exist", holder="worker-1")  # must not raise

    def test_non_positive_ttl_is_rejected(self) -> None:
        conn = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            acquire_lock(conn, lock_key="ticket:t1", holder="worker-1", ttl_seconds=0, now=now)


if __name__ == "__main__":
    unittest.main()
