import sqlite3
import unittest
from datetime import datetime, timezone

from crm_ai_agent.persistence.db import run_migrations
from crm_ai_agent.safety.audit import append_event, compute_event_hash, verify_chain


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
    return conn


class TestAudit(unittest.TestCase):
    def test_first_event_has_sequence_one_and_no_prev_hash(self) -> None:
        conn = _make_conn()
        event = append_event(
            conn,
            ticket_id="t1",
            correlation_id="corr-1",
            actor_type="SYSTEM",
            actor_id="watcher-1",
            event_type="TICKET_DISCOVERED",
            payload={"external_ticket_id": "EXT-1"},
            now=datetime(2026, 9, 3, tzinfo=timezone.utc),
        )
        self.assertEqual(event.sequence_no, 1)
        self.assertIsNone(event.prev_event_hash)

    def test_chain_links_sequential_events(self) -> None:
        conn = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        e1 = append_event(
            conn, ticket_id="t1", correlation_id="corr-1", actor_type="SYSTEM", actor_id="w1",
            event_type="TICKET_DISCOVERED", payload={"a": 1}, now=now,
        )
        e2 = append_event(
            conn, ticket_id="t1", correlation_id="corr-1", actor_type="SYSTEM", actor_id="w1",
            event_type="TICKET_ANALYZED", payload={"b": 2}, now=now,
        )
        self.assertEqual(e2.sequence_no, 2)
        self.assertEqual(e2.prev_event_hash, e1.event_hash)
        self.assertTrue(verify_chain(conn, "t1"))

    def test_events_for_different_tickets_do_not_share_a_chain(self) -> None:
        conn = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        conn.execute(
            "INSERT INTO tickets (ticket_id, external_ticket_id, status, created_at, updated_at) "
            "VALUES ('t2', 'EXT-2', 'DISCOVERED', ?, ?)",
            (now.isoformat(), now.isoformat()),
        )
        e_t1 = append_event(
            conn, ticket_id="t1", correlation_id="c1", actor_type="SYSTEM", actor_id="w1",
            event_type="TICKET_DISCOVERED", payload={}, now=now,
        )
        e_t2 = append_event(
            conn, ticket_id="t2", correlation_id="c2", actor_type="SYSTEM", actor_id="w1",
            event_type="TICKET_DISCOVERED", payload={}, now=now,
        )
        self.assertEqual(e_t1.sequence_no, 1)
        self.assertEqual(e_t2.sequence_no, 1)
        self.assertIsNone(e_t2.prev_event_hash)

    def test_canonicalized_payload_is_hash_stable_regardless_of_key_order(self) -> None:
        conn1 = _make_conn()
        conn2 = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        e1 = append_event(
            conn1, ticket_id="t1", correlation_id="c1", actor_type="SYSTEM", actor_id="w1",
            event_type="X", payload={"a": 1, "b": 2}, now=now,
        )
        e2 = append_event(
            conn2, ticket_id="t1", correlation_id="c1", actor_type="SYSTEM", actor_id="w1",
            event_type="X", payload={"b": 2, "a": 1}, now=now,
        )
        self.assertEqual(e1.event_hash, e2.event_hash)

    def test_verify_chain_detects_tampered_event_hash(self) -> None:
        conn = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        append_event(
            conn, ticket_id="t1", correlation_id="c1", actor_type="SYSTEM", actor_id="w1",
            event_type="TICKET_DISCOVERED", payload={"a": 1}, now=now,
        )
        append_event(
            conn, ticket_id="t1", correlation_id="c1", actor_type="SYSTEM", actor_id="w1",
            event_type="TICKET_ANALYZED", payload={"b": 2}, now=now,
        )
        self.assertTrue(verify_chain(conn, "t1"))

        # Out-of-band tamper: rewrite the payload of the first event without
        # updating its stored hash or the second event's prev_event_hash.
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE ticket_id = 't1' AND sequence_no = 1",
            ('{"a":999}',),
        )
        self.assertFalse(verify_chain(conn, "t1"))

    def test_verify_chain_detects_a_deleted_middle_event(self) -> None:
        conn = _make_conn()
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        for i in range(3):
            append_event(
                conn, ticket_id="t1", correlation_id="c1", actor_type="SYSTEM", actor_id="w1",
                event_type=f"STEP_{i}", payload={"i": i}, now=now,
            )
        self.assertTrue(verify_chain(conn, "t1"))

        conn.execute("DELETE FROM audit_events WHERE ticket_id = 't1' AND sequence_no = 2")
        self.assertFalse(verify_chain(conn, "t1"))

    def test_verify_chain_is_true_for_a_ticket_with_no_events(self) -> None:
        conn = _make_conn()
        self.assertTrue(verify_chain(conn, "t1"))

    def test_compute_event_hash_is_deterministic(self) -> None:
        self.assertEqual(
            compute_event_hash("prev", '{"a":1}'),
            compute_event_hash("prev", '{"a":1}'),
        )
        self.assertNotEqual(
            compute_event_hash("prev", '{"a":1}'),
            compute_event_hash(None, '{"a":1}'),
        )


if __name__ == "__main__":
    unittest.main()
