import unittest
from datetime import datetime, timezone
from types import MappingProxyType

from crm_ai_agent.domain.entities import (
    ApprovalPolicySnapshot,
    Approval,
    EvidenceItem,
    LLMInterpretation,
    PlanAction,
    ResolvedEntity,
    Ticket,
    TicketSnapshot,
)
from crm_ai_agent.domain.enums import ApprovalStatus, RiskLevel, TicketStatus, TrustLevel
from crm_ai_agent.domain.validation import DomainValidationError


class TestEntities(unittest.TestCase):
    def test_ticket_defaults_to_discovered_status_and_default_tenant(self) -> None:
        ticket = Ticket(ticket_id="t1", external_ticket_id="EXT-1")
        self.assertEqual(ticket.status, TicketStatus.DISCOVERED)
        self.assertEqual(ticket.version, 1)
        self.assertEqual(ticket.tenant_id, "default")

    def test_ticket_snapshot_top_level_is_immutable(self) -> None:
        snapshot = TicketSnapshot(
            schema_version="1.0",
            snapshot_id="snap-1",
            ticket_id="t1",
            captured_at=datetime.now(timezone.utc),
            source_hash="abc123",
            status="open",
            title="Test ticket",
            trusted_fields={"assignee": "alice"},
        )
        with self.assertRaises(Exception):
            snapshot.title = "mutated"  # type: ignore[misc]

    def test_ticket_snapshot_trusted_fields_are_deeply_frozen(self) -> None:
        original = {"assignee": "alice"}
        snapshot = TicketSnapshot(
            schema_version="1.0",
            snapshot_id="snap-1",
            ticket_id="t1",
            captured_at=datetime.now(timezone.utc),
            source_hash="abc123",
            status="open",
            title="Test ticket",
            trusted_fields=original,
        )
        # Mutating the dict passed in after construction must not affect the snapshot.
        original["assignee"] = "mallory"
        self.assertEqual(snapshot.trusted_fields["assignee"], "alice")

        self.assertIsInstance(snapshot.trusted_fields, MappingProxyType)
        with self.assertRaises(TypeError):
            snapshot.trusted_fields["assignee"] = "mallory"  # type: ignore[index]

    def test_plan_action_arguments_are_deeply_frozen(self) -> None:
        action = PlanAction(
            action_id="a1",
            action_type="edit_crm_user_name",
            arguments={"user_id": "u1", "first_name": "Ivan"},
            preconditions=["user_exists"],
            expected_before_state={"first_name": "Ivon"},
            expected_after_state={"first_name": "Ivan"},
            risk_level=RiskLevel.MEDIUM,
            idempotency_key="idem-1",
        )
        self.assertEqual(action.risk_level, RiskLevel.MEDIUM)
        self.assertTrue(action.idempotency_key)
        self.assertIsInstance(action.arguments, MappingProxyType)
        self.assertIsInstance(action.preconditions, tuple)
        with self.assertRaises(TypeError):
            action.arguments["user_id"] = "u2"  # type: ignore[index]

    def test_plan_action_rejects_unregistered_action_type(self) -> None:
        with self.assertRaises(DomainValidationError):
            PlanAction(
                action_id="a1",
                action_type="delete_everything",
                arguments={},
                preconditions=[],
                expected_before_state={},
                expected_after_state={},
                risk_level=RiskLevel.HIGH,
                idempotency_key="idem-1",
            )

    def test_llm_interpretation_rejects_out_of_range_confidence(self) -> None:
        with self.assertRaises(DomainValidationError):
            LLMInterpretation(
                schema_version="1.0",
                snapshot_id="snap-1",
                ticket_type="change_user_name",
                classification_confidence=1.5,
                requested_changes={"first_name": "Ivan"},
                subject_reference="user:u1",
            )

    def test_llm_interpretation_rejects_unknown_ticket_type(self) -> None:
        with self.assertRaises(DomainValidationError):
            LLMInterpretation(
                schema_version="1.0",
                snapshot_id="snap-1",
                ticket_type="delete_all_users",
                classification_confidence=0.9,
                requested_changes={"first_name": "Ivan"},
                subject_reference="user:u1",
            )

    def test_llm_interpretation_evidence_is_structured_and_frozen(self) -> None:
        interpretation = LLMInterpretation(
            schema_version="1.0",
            snapshot_id="snap-1",
            ticket_type="change_user_name",
            classification_confidence=0.9,
            requested_changes={"first_name": "Ivan"},
            subject_reference="user:u1",
            evidence=[
                EvidenceItem(
                    section="description",
                    source_reference="ticket:EXT-1#description",
                    trust_level=TrustLevel.REQUESTER_TEXT,
                    text="Please rename me to Ivan",
                )
            ],
        )
        self.assertIsInstance(interpretation.requested_changes, MappingProxyType)
        self.assertEqual(len(interpretation.evidence), 1)
        self.assertEqual(interpretation.evidence[0].trust_level, TrustLevel.REQUESTER_TEXT)

    def test_approval_policy_snapshot_rejects_empty_policy_version(self) -> None:
        with self.assertRaises(DomainValidationError):
            ApprovalPolicySnapshot(policy_version="", allowed_approver_roles=("crm_admin",), ttl_seconds=3600)

    def test_approval_policy_snapshot_rejects_empty_roles(self) -> None:
        with self.assertRaises(DomainValidationError):
            ApprovalPolicySnapshot(policy_version="mvp-1", allowed_approver_roles=(), ttl_seconds=3600)

    def test_approval_policy_snapshot_rejects_blank_role_entry(self) -> None:
        with self.assertRaises(DomainValidationError):
            ApprovalPolicySnapshot(policy_version="mvp-1", allowed_approver_roles=("crm_admin", "  "), ttl_seconds=3600)

    def test_approval_policy_snapshot_rejects_duplicate_roles(self) -> None:
        with self.assertRaises(DomainValidationError):
            ApprovalPolicySnapshot(
                policy_version="mvp-1",
                allowed_approver_roles=("crm_admin", "crm_admin"),
                ttl_seconds=3600,
            )

    def test_approval_policy_snapshot_rejects_non_positive_ttl(self) -> None:
        with self.assertRaises(DomainValidationError):
            ApprovalPolicySnapshot(policy_version="mvp-1", allowed_approver_roles=("crm_admin",), ttl_seconds=0)
        with self.assertRaises(DomainValidationError):
            ApprovalPolicySnapshot(policy_version="mvp-1", allowed_approver_roles=("crm_admin",), ttl_seconds=-1)

    def test_approval_policy_snapshot_is_immutable(self) -> None:
        policy = ApprovalPolicySnapshot(
            policy_version="mvp-1", allowed_approver_roles=("crm_admin",), ttl_seconds=3600
        )
        with self.assertRaises(Exception):
            policy.ttl_seconds = 10  # type: ignore[misc]
        with self.assertRaises(Exception):
            policy.allowed_approver_roles = ("someone_else",)  # type: ignore[misc]
        self.assertIsInstance(policy.allowed_approver_roles, tuple)

    def test_approval_persists_policy_snapshot_and_roles(self) -> None:
        approval = Approval(
            approval_id="appr-1",
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
        self.assertEqual(approval.status, ApprovalStatus.PENDING)
        self.assertEqual(approval.allowed_approver_roles, ("crm_admin",))
        self.assertEqual(approval.policy.policy_version, "mvp-1")


class TestResolvedEntity(unittest.TestCase):
    def test_single_match_requires_entity_id(self) -> None:
        with self.assertRaises(DomainValidationError):
            ResolvedEntity(query="ivan@example.com", match_count=1, entity_id=None)

    def test_zero_matches_must_not_carry_an_entity_id(self) -> None:
        with self.assertRaises(DomainValidationError):
            ResolvedEntity(query="ivan@example.com", match_count=0, entity_id="crm-user-1")

    def test_multiple_matches_must_not_carry_an_entity_id(self) -> None:
        with self.assertRaises(DomainValidationError):
            ResolvedEntity(query="ivan@example.com", match_count=2, entity_id="crm-user-1")

    def test_negative_match_count_is_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            ResolvedEntity(query="ivan@example.com", match_count=-1)

    def test_valid_single_match(self) -> None:
        resolved = ResolvedEntity(query="ivan@example.com", match_count=1, entity_id="crm-user-1")
        self.assertEqual(resolved.entity_id, "crm-user-1")

    def test_valid_zero_matches(self) -> None:
        resolved = ResolvedEntity(query="nobody@example.com", match_count=0)
        self.assertIsNone(resolved.entity_id)


if __name__ == "__main__":
    unittest.main()
