import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

import dataclasses

from crm_ai_agent.domain.entities import Approval, ApprovalPolicySnapshot
from crm_ai_agent.domain.enums import ApprovalStatus
from crm_ai_agent.persistence.approvals import (
    ApprovalAlreadyConsumedError,
    load_approval,
    save_approval,
    update_decision,
)
from crm_ai_agent.persistence.db import run_migrations


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    run_migrations(conn)  # leaves conn.isolation_level = None (autocommit); see db.py

    now = "2026-09-03T00:00:00Z"
    conn.execute(
        "INSERT INTO tickets (ticket_id, external_ticket_id, status, created_at, updated_at) "
        "VALUES ('t1', 'EXT-1', 'AWAITING_APPROVAL', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO ticket_snapshots (snapshot_id, ticket_id, schema_version, captured_at, source_hash, raw_json) "
        "VALUES ('s1', 't1', '1.0', ?, 'hash1', '{}')",
        (now,),
    )
    conn.execute(
        "INSERT INTO plans (plan_id, ticket_id, snapshot_id, snapshot_hash, plan_hash, policy_version, status, created_at) "
        "VALUES ('p1', 't1', 's1', 'hash1', 'planhash1', 'mvp-1', 'AWAITING_APPROVAL', ?)",
        (now,),
    )
    return conn


class TestApprovalPersistence(unittest.TestCase):
    def test_approval_round_trips_with_policy_and_decision(self) -> None:
        conn = _make_conn()
        created_at = datetime(2026, 9, 3, tzinfo=timezone.utc)
        expires_at = created_at + timedelta(hours=1)

        approval = Approval(
            approval_id="appr-1",
            ticket_id="t1",
            plan_id="p1",
            plan_hash="planhash1",
            snapshot_hash="hash1",
            policy=ApprovalPolicySnapshot(
                policy_version="mvp-1",
                allowed_approver_roles=("crm_admin", "team_lead"),
                ttl_seconds=3600,
            ),
            status=ApprovalStatus.APPROVED,
            telegram_user_id="tg-42",
            telegram_chat_id="tg-chat-1",
            created_at=created_at,
            expires_at=expires_at,
            consumed_at=created_at + timedelta(minutes=5),
            decision_reason="Approved by team lead",
        )
        # save_approval() does not commit on its own (see its docstring): the
        # caller — here, standing in for the future Unit of Work — owns the
        # transaction boundary.
        conn.execute("BEGIN IMMEDIATE")
        save_approval(conn, approval)
        conn.commit()

        # Simulate a process restart: reload from a fresh read of the row,
        # nothing kept in memory.
        reloaded = load_approval(conn, "appr-1")

        self.assertIsNotNone(reloaded)
        assert reloaded is not None
        self.assertEqual(reloaded.status, ApprovalStatus.APPROVED)
        self.assertEqual(reloaded.telegram_user_id, "tg-42")
        self.assertEqual(reloaded.telegram_chat_id, "tg-chat-1")
        self.assertEqual(reloaded.decision_reason, "Approved by team lead")
        # The policy that governed this approval must be recoverable exactly,
        # even if the live policy config has since changed.
        self.assertEqual(reloaded.policy.policy_version, "mvp-1")
        self.assertEqual(reloaded.allowed_approver_roles, ("crm_admin", "team_lead"))
        self.assertEqual(reloaded.policy.ttl_seconds, 3600)
        self.assertEqual(reloaded.consumed_at, created_at + timedelta(minutes=5))

    def test_missing_approval_returns_none(self) -> None:
        conn = _make_conn()
        self.assertIsNone(load_approval(conn, "does-not-exist"))

    def test_save_approval_does_not_commit_and_can_be_rolled_back(self) -> None:
        conn = _make_conn()
        approval = Approval(
            approval_id="appr-2",
            ticket_id="t1",
            plan_id="p1",
            plan_hash="planhash1",
            snapshot_hash="hash1",
            policy=ApprovalPolicySnapshot(
                policy_version="mvp-1",
                allowed_approver_roles=("crm_admin",),
                ttl_seconds=3600,
            ),
        )
        conn.execute("BEGIN IMMEDIATE")
        save_approval(conn, approval)
        # Visible within the same, still-open transaction/connection...
        self.assertIsNotNone(load_approval(conn, "appr-2"))
        # ...but rolling back must undo it, proving save_approval() never
        # issued a hidden commit of its own.
        conn.rollback()
        self.assertIsNone(load_approval(conn, "appr-2"))

    def test_save_approval_participates_in_a_multi_statement_unit_of_work(self) -> None:
        """Stand-in for the future Unit of Work: one transaction covering an
        approval insert plus another write, committed or rolled back as one.
        """
        conn = _make_conn()
        approval = Approval(
            approval_id="appr-3",
            ticket_id="t1",
            plan_id="p1",
            plan_hash="planhash1",
            snapshot_hash="hash1",
            policy=ApprovalPolicySnapshot(
                policy_version="mvp-1",
                allowed_approver_roles=("crm_admin",),
                ttl_seconds=3600,
            ),
        )
        conn.execute("BEGIN IMMEDIATE")
        save_approval(conn, approval)
        conn.execute(
            "INSERT INTO outbox_messages (message_id, channel, status, payload_json, created_at) "
            "VALUES ('msg-1', 'telegram', 'PENDING', '{}', '2026-09-03T00:00:00Z')"
        )
        conn.commit()

        self.assertIsNotNone(load_approval(conn, "appr-3"))
        outbox_row = conn.execute(
            "SELECT * FROM outbox_messages WHERE message_id = 'msg-1'"
        ).fetchone()
        self.assertIsNotNone(outbox_row)


    def test_update_decision_persists_consumption_and_is_visible_after_reload(self) -> None:
        conn = _make_conn()
        approval = Approval(
            approval_id="appr-4",
            ticket_id="t1",
            plan_id="p1",
            plan_hash="planhash1",
            snapshot_hash="hash1",
            policy=ApprovalPolicySnapshot(
                policy_version="mvp-1", allowed_approver_roles=("crm_admin",), ttl_seconds=3600
            ),
        )
        conn.execute("BEGIN IMMEDIATE")
        save_approval(conn, approval)
        conn.commit()

        consumed = dataclasses.replace(
            approval,
            status=ApprovalStatus.APPROVED,
            telegram_user_id="tg-42",
            telegram_chat_id="tg-chat-1",
            consumed_at=datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc),
            decision_reason="Approved by team lead",
        )
        conn.execute("BEGIN IMMEDIATE")
        update_decision(conn, consumed)
        conn.commit()

        reloaded = load_approval(conn, "appr-4")
        assert reloaded is not None
        self.assertEqual(reloaded.status, ApprovalStatus.APPROVED)
        self.assertEqual(reloaded.telegram_user_id, "tg-42")
        self.assertEqual(reloaded.decision_reason, "Approved by team lead")

    def test_update_decision_raises_if_approval_is_no_longer_pending(self) -> None:
        """This is the DB-level half of one-time consumption: even if two
        callbacks both pass the in-memory PENDING check in
        safety.telegram_approval.consume_approval at the same instant, only
        the first UPDATE here can actually change a row."""
        conn = _make_conn()
        approval = Approval(
            approval_id="appr-5",
            ticket_id="t1",
            plan_id="p1",
            plan_hash="planhash1",
            snapshot_hash="hash1",
            policy=ApprovalPolicySnapshot(
                policy_version="mvp-1", allowed_approver_roles=("crm_admin",), ttl_seconds=3600
            ),
        )
        conn.execute("BEGIN IMMEDIATE")
        save_approval(conn, approval)
        conn.commit()

        first_decision = dataclasses.replace(
            approval, status=ApprovalStatus.APPROVED, telegram_user_id="tg-42",
            telegram_chat_id="tg-chat-1", consumed_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
        )
        conn.execute("BEGIN IMMEDIATE")
        update_decision(conn, first_decision)
        conn.commit()

        second_decision = dataclasses.replace(
            approval, status=ApprovalStatus.REJECTED, telegram_user_id="tg-999",
            telegram_chat_id="tg-chat-2", consumed_at=datetime(2026, 9, 3, 0, 1, tzinfo=timezone.utc),
        )
        conn.execute("BEGIN IMMEDIATE")
        with self.assertRaises(ApprovalAlreadyConsumedError):
            update_decision(conn, second_decision)
        conn.rollback()

        reloaded = load_approval(conn, "appr-5")
        assert reloaded is not None
        self.assertEqual(reloaded.status, ApprovalStatus.APPROVED)
        self.assertEqual(reloaded.telegram_user_id, "tg-42")


if __name__ == "__main__":
    unittest.main()
