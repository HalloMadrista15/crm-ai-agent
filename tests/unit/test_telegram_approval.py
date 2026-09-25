import unittest
from datetime import datetime, timedelta, timezone

from crm_ai_agent.domain.entities import Approval, ApprovalPolicySnapshot
from crm_ai_agent.domain.enums import ApprovalStatus
from crm_ai_agent.safety.telegram_approval import (
    ApprovalExpiredError,
    ApprovalHashMismatchError,
    ApprovalNotPendingError,
    ApproverIdentity,
    ApproverNotAuthorizedError,
    InvalidWebhookError,
    TelegramCallback,
    TelegramIdentityMismatchError,
    consume_approval,
)

NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


def _approval(**overrides) -> Approval:
    defaults = dict(
        approval_id="appr-1",
        ticket_id="t1",
        plan_id="p1",
        plan_hash="planhash1",
        snapshot_hash="hash1",
        policy=ApprovalPolicySnapshot(
            policy_version="mvp-1", allowed_approver_roles=("crm_admin",), ttl_seconds=3600
        ),
        status=ApprovalStatus.PENDING,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    defaults.update(overrides)
    return Approval(**defaults)


def _callback(**overrides) -> TelegramCallback:
    defaults = dict(
        approval_id="appr-1",
        telegram_user_id="tg-42",
        telegram_chat_id="tg-chat-1",
        webhook_secret_valid=True,
    )
    defaults.update(overrides)
    return TelegramCallback(**defaults)


def _approver(**overrides) -> ApproverIdentity:
    defaults = dict(telegram_user_id="tg-42", roles=frozenset({"crm_admin"}))
    defaults.update(overrides)
    return ApproverIdentity(**defaults)


class TestConsumeApproval(unittest.TestCase):
    def test_valid_approval_flips_status_and_records_identity(self) -> None:
        result = consume_approval(
            approval=_approval(),
            callback=_callback(),
            approver=_approver(),
            decision=ApprovalStatus.APPROVED,
            current_plan_hash="planhash1",
            current_snapshot_hash="hash1",
            now=NOW,
        )
        self.assertEqual(result.status, ApprovalStatus.APPROVED)
        self.assertEqual(result.telegram_user_id, "tg-42")
        self.assertEqual(result.telegram_chat_id, "tg-chat-1")
        self.assertEqual(result.consumed_at, NOW)

    def test_valid_rejection_does_not_require_hash_match(self) -> None:
        result = consume_approval(
            approval=_approval(),
            callback=_callback(),
            approver=_approver(),
            decision=ApprovalStatus.REJECTED,
            current_plan_hash="DIFFERENT",
            current_snapshot_hash="DIFFERENT",
            now=NOW,
        )
        self.assertEqual(result.status, ApprovalStatus.REJECTED)

    def test_original_approval_object_is_not_mutated(self) -> None:
        original = _approval()
        consume_approval(
            approval=original,
            callback=_callback(),
            approver=_approver(),
            decision=ApprovalStatus.APPROVED,
            current_plan_hash="planhash1",
            current_snapshot_hash="hash1",
            now=NOW,
        )
        self.assertEqual(original.status, ApprovalStatus.PENDING)
        self.assertIsNone(original.consumed_at)

    def test_invalid_webhook_secret_is_rejected(self) -> None:
        with self.assertRaises(InvalidWebhookError):
            consume_approval(
                approval=_approval(),
                callback=_callback(webhook_secret_valid=False),
                approver=_approver(),
                decision=ApprovalStatus.APPROVED,
                current_plan_hash="planhash1",
                current_snapshot_hash="hash1",
                now=NOW,
            )

    def test_callback_approval_id_mismatch_is_rejected(self) -> None:
        with self.assertRaises(TelegramIdentityMismatchError):
            consume_approval(
                approval=_approval(approval_id="appr-1"),
                callback=_callback(approval_id="appr-DIFFERENT"),
                approver=_approver(),
                decision=ApprovalStatus.APPROVED,
                current_plan_hash="planhash1",
                current_snapshot_hash="hash1",
                now=NOW,
            )

    def test_callback_identity_not_matching_resolved_approver_is_rejected(self) -> None:
        with self.assertRaises(TelegramIdentityMismatchError):
            consume_approval(
                approval=_approval(),
                callback=_callback(telegram_user_id="tg-999"),
                approver=_approver(telegram_user_id="tg-42"),
                decision=ApprovalStatus.APPROVED,
                current_plan_hash="planhash1",
                current_snapshot_hash="hash1",
                now=NOW,
            )

    def test_already_consumed_approval_cannot_be_consumed_again(self) -> None:
        with self.assertRaises(ApprovalNotPendingError):
            consume_approval(
                approval=_approval(status=ApprovalStatus.APPROVED),
                callback=_callback(),
                approver=_approver(),
                decision=ApprovalStatus.APPROVED,
                current_plan_hash="planhash1",
                current_snapshot_hash="hash1",
                now=NOW,
            )

    def test_expired_approval_is_rejected(self) -> None:
        with self.assertRaises(ApprovalExpiredError):
            consume_approval(
                approval=_approval(expires_at=NOW - timedelta(seconds=1)),
                callback=_callback(),
                approver=_approver(),
                decision=ApprovalStatus.APPROVED,
                current_plan_hash="planhash1",
                current_snapshot_hash="hash1",
                now=NOW,
            )

    def test_approver_without_required_role_is_rejected(self) -> None:
        with self.assertRaises(ApproverNotAuthorizedError):
            consume_approval(
                approval=_approval(),
                callback=_callback(),
                approver=_approver(roles=frozenset({"read_only_viewer"})),
                decision=ApprovalStatus.APPROVED,
                current_plan_hash="planhash1",
                current_snapshot_hash="hash1",
                now=NOW,
            )

    def test_approver_with_no_roles_at_all_is_rejected(self) -> None:
        with self.assertRaises(ApproverNotAuthorizedError):
            consume_approval(
                approval=_approval(),
                callback=_callback(),
                approver=_approver(roles=frozenset()),
                decision=ApprovalStatus.APPROVED,
                current_plan_hash="planhash1",
                current_snapshot_hash="hash1",
                now=NOW,
            )

    def test_plan_hash_mismatch_blocks_approval(self) -> None:
        with self.assertRaises(ApprovalHashMismatchError):
            consume_approval(
                approval=_approval(),
                callback=_callback(),
                approver=_approver(),
                decision=ApprovalStatus.APPROVED,
                current_plan_hash="CHANGED",
                current_snapshot_hash="hash1",
                now=NOW,
            )

    def test_snapshot_hash_mismatch_blocks_approval(self) -> None:
        with self.assertRaises(ApprovalHashMismatchError):
            consume_approval(
                approval=_approval(),
                callback=_callback(),
                approver=_approver(),
                decision=ApprovalStatus.APPROVED,
                current_plan_hash="planhash1",
                current_snapshot_hash="CHANGED",
                now=NOW,
            )

    def test_invalid_decision_value_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            consume_approval(
                approval=_approval(),
                callback=_callback(),
                approver=_approver(),
                decision=ApprovalStatus.PENDING,
                current_plan_hash="planhash1",
                current_snapshot_hash="hash1",
                now=NOW,
            )


if __name__ == "__main__":
    unittest.main()
