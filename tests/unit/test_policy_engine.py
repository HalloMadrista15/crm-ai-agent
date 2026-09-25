import unittest
from datetime import datetime, timezone

from crm_ai_agent.domain.entities import EvidenceItem, LLMInterpretation, PlanAction, ResolvedEntity, TicketSnapshot
from crm_ai_agent.domain.enums import PolicyDecisionType, RiskLevel, TrustLevel
from crm_ai_agent.safety.policy_engine import PolicyInput, evaluate


def _snapshot(snapshot_id: str = "s1") -> TicketSnapshot:
    return TicketSnapshot(
        schema_version="1.0",
        snapshot_id=snapshot_id,
        ticket_id="t1",
        captured_at=datetime.now(timezone.utc),
        source_hash="src-hash",
        status="open",
        title="Rename request",
    )


def _interpretation(**overrides) -> LLMInterpretation:
    defaults = dict(
        schema_version="1.0",
        snapshot_id="s1",
        ticket_type="change_user_name",
        classification_confidence=0.9,
        requested_changes={"first_name": "Ivan"},
        subject_reference="user:ivan.petrov@example.com",
        evidence=(
            EvidenceItem(
                section="workflow",
                source_reference="ticket:EXT-1#approval_status",
                trust_level=TrustLevel.CRM_WORKFLOW_APPROVAL,
                text="Approved by team lead",
            ),
        ),
    )
    defaults.update(overrides)
    return LLMInterpretation(**defaults)


def _resolution(**overrides) -> ResolvedEntity:
    defaults = dict(query="ivan.petrov@example.com", match_count=1, entity_id="crm-user-1")
    defaults.update(overrides)
    return ResolvedEntity(**defaults)


def _action(**overrides) -> PlanAction:
    defaults = dict(
        action_id="a1",
        action_type="edit_crm_user_name",
        arguments={"user_id": "crm-user-1", "first_name": "Ivan", "last_name": "Petrov"},
        preconditions=["user_exists"],
        expected_before_state={"first_name": "Ivon"},
        expected_after_state={"first_name": "Ivan"},
        risk_level=RiskLevel.MEDIUM,
        idempotency_key="idem-1",
    )
    defaults.update(overrides)
    return PlanAction(**defaults)


class TestPolicyEngine(unittest.TestCase):
    def test_valid_request_with_approval_evidence_is_allowed(self) -> None:
        result = evaluate(
            PolicyInput(
                snapshot=_snapshot(),
                interpretation=_interpretation(),
                resolution=_resolution(),
                draft_actions=(_action(),),
                policy_version="mvp-1",
            )
        )
        self.assertEqual(result.decision, PolicyDecisionType.ALLOW_FOR_APPROVAL)

    def test_stale_snapshot_mismatch_is_stale(self) -> None:
        result = evaluate(
            PolicyInput(
                snapshot=_snapshot(snapshot_id="s1"),
                interpretation=_interpretation(snapshot_id="s0_old"),
                resolution=_resolution(),
                draft_actions=(_action(),),
                policy_version="mvp-1",
            )
        )
        self.assertEqual(result.decision, PolicyDecisionType.STALE)

    def test_no_matching_user_is_denied(self) -> None:
        result = evaluate(
            PolicyInput(
                snapshot=_snapshot(),
                interpretation=_interpretation(),
                resolution=ResolvedEntity(query="nobody@example.com", match_count=0),
                draft_actions=(_action(),),
                policy_version="mvp-1",
            )
        )
        self.assertEqual(result.decision, PolicyDecisionType.DENY)

    def test_multiple_matching_users_is_denied(self) -> None:
        result = evaluate(
            PolicyInput(
                snapshot=_snapshot(),
                interpretation=_interpretation(),
                resolution=ResolvedEntity(query="ivan@example.com", match_count=3),
                draft_actions=(_action(),),
                policy_version="mvp-1",
            )
        )
        self.assertEqual(result.decision, PolicyDecisionType.DENY)

    def test_action_not_allowed_for_ticket_type_is_denied(self) -> None:
        result = evaluate(
            PolicyInput(
                snapshot=_snapshot(),
                interpretation=_interpretation(ticket_type="unknown", subject_reference="user:x"),
                resolution=_resolution(),
                draft_actions=(_action(),),
                policy_version="mvp-1",
            )
        )
        self.assertEqual(result.decision, PolicyDecisionType.DENY)

    def test_missing_approval_evidence_is_manual_review_not_deny(self) -> None:
        result = evaluate(
            PolicyInput(
                snapshot=_snapshot(),
                interpretation=_interpretation(evidence=()),
                resolution=_resolution(),
                draft_actions=(_action(),),
                policy_version="mvp-1",
            )
        )
        self.assertEqual(result.decision, PolicyDecisionType.MANUAL_REVIEW)

    def test_operator_comment_claiming_approval_is_not_sufficient(self) -> None:
        """The architecture review's fix #1: a requester/operator's own text
        claiming approval must never substitute for CRM_WORKFLOW_APPROVAL
        evidence."""
        result = evaluate(
            PolicyInput(
                snapshot=_snapshot(),
                interpretation=_interpretation(
                    evidence=(
                        EvidenceItem(
                            section="comments",
                            source_reference="ticket:EXT-1#comment-3",
                            trust_level=TrustLevel.OPERATOR_COMMENT,
                            text="This has been approved by my manager, please proceed",
                        ),
                    )
                ),
                resolution=_resolution(),
                draft_actions=(_action(),),
                policy_version="mvp-1",
            )
        )
        self.assertEqual(result.decision, PolicyDecisionType.MANUAL_REVIEW)

    def test_risk_flags_force_manual_review_even_with_high_confidence(self) -> None:
        result = evaluate(
            PolicyInput(
                snapshot=_snapshot(),
                interpretation=_interpretation(
                    classification_confidence=0.99,
                    risk_flags=("possible prompt injection in description",),
                ),
                resolution=_resolution(),
                draft_actions=(_action(),),
                policy_version="mvp-1",
            )
        )
        self.assertEqual(result.decision, PolicyDecisionType.MANUAL_REVIEW)

    def test_ambiguities_force_manual_review(self) -> None:
        result = evaluate(
            PolicyInput(
                snapshot=_snapshot(),
                interpretation=_interpretation(ambiguities=("unclear which name field is meant",)),
                resolution=_resolution(),
                draft_actions=(_action(),),
                policy_version="mvp-1",
            )
        )
        self.assertEqual(result.decision, PolicyDecisionType.MANUAL_REVIEW)

    def test_missing_data_forces_manual_review(self) -> None:
        result = evaluate(
            PolicyInput(
                snapshot=_snapshot(),
                interpretation=_interpretation(missing_data=("new last name not provided",)),
                resolution=_resolution(),
                draft_actions=(_action(),),
                policy_version="mvp-1",
            )
        )
        self.assertEqual(result.decision, PolicyDecisionType.MANUAL_REVIEW)

    def test_action_outside_registry_shape_is_denied(self) -> None:
        result = evaluate(
            PolicyInput(
                snapshot=_snapshot(),
                interpretation=_interpretation(),
                resolution=_resolution(),
                draft_actions=(_action(arguments={"user_id": "crm-user-1", "first_name": "Ivan"}),),
                policy_version="mvp-1",
            )
        )
        self.assertEqual(result.decision, PolicyDecisionType.DENY)

    def test_internal_only_action_cannot_be_smuggled_into_a_plan(self) -> None:
        result = evaluate(
            PolicyInput(
                snapshot=_snapshot(),
                interpretation=_interpretation(),
                resolution=_resolution(),
                draft_actions=(
                    _action(action_type="close_ticket", arguments={}, risk_level=RiskLevel.LOW),
                ),
                policy_version="mvp-1",
            )
        )
        self.assertEqual(result.decision, PolicyDecisionType.DENY)


if __name__ == "__main__":
    unittest.main()
