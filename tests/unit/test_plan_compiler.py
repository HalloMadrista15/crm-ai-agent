import unittest
from datetime import datetime, timezone

from crm_ai_agent.domain.entities import EvidenceItem, LLMInterpretation, ResolvedEntity, TicketSnapshot
from crm_ai_agent.domain.enums import PolicyDecisionType, TrustLevel
from crm_ai_agent.safety.canonical_hash import compute_plan_hash, compute_snapshot_hash
from crm_ai_agent.safety.plan_compiler import PlanCompilationError, compile_edit_user_name_plan


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
        requested_changes={"last_name": "Ivanova"},
        subject_reference="ivan.petrov@example.com",
        evidence=(
            EvidenceItem(
                section="workflow",
                source_reference="ticket:EXT-1#approval_status",
                trust_level=TrustLevel.CRM_WORKFLOW_APPROVAL,
                text="Approved by HR",
            ),
        ),
    )
    defaults.update(overrides)
    return LLMInterpretation(**defaults)


CURRENT_STATE = {"first_name": "Ivan", "last_name": "Petrova"}
RESOLUTION = ResolvedEntity(query="ivan.petrov@example.com", match_count=1, entity_id="crm-user-1")


class TestPlanCompiler(unittest.TestCase):
    def test_compiles_a_plan_when_policy_allows(self) -> None:
        result = compile_edit_user_name_plan(
            snapshot=_snapshot(),
            interpretation=_interpretation(),
            resolution=RESOLUTION,
            current_state=CURRENT_STATE,
            policy_version="mvp-1",
        )
        self.assertEqual(result.evaluation.decision, PolicyDecisionType.ALLOW_FOR_APPROVAL)
        self.assertIsNotNone(result.plan)
        plan = result.plan
        assert plan is not None
        self.assertEqual(len(plan.actions), 1)
        action = plan.actions[0]
        self.assertEqual(action.arguments["user_id"], "crm-user-1")
        self.assertEqual(action.arguments["first_name"], "Ivan")
        self.assertEqual(action.arguments["last_name"], "Ivanova")
        self.assertEqual(action.expected_before_state, {"last_name": "Petrova"})
        self.assertEqual(action.expected_after_state, {"last_name": "Ivanova"})

    def test_plan_hash_matches_independent_recomputation(self) -> None:
        result = compile_edit_user_name_plan(
            snapshot=_snapshot(),
            interpretation=_interpretation(),
            resolution=RESOLUTION,
            current_state=CURRENT_STATE,
            policy_version="mvp-1",
        )
        plan = result.plan
        assert plan is not None
        self.assertEqual(plan.plan_hash, compute_plan_hash(plan))
        self.assertEqual(plan.snapshot_hash, compute_snapshot_hash(_snapshot()))

    def test_idempotency_key_is_stable_for_the_same_snapshot_and_subject(self) -> None:
        result1 = compile_edit_user_name_plan(
            snapshot=_snapshot(), interpretation=_interpretation(), resolution=RESOLUTION,
            current_state=CURRENT_STATE, policy_version="mvp-1",
        )
        result2 = compile_edit_user_name_plan(
            snapshot=_snapshot(), interpretation=_interpretation(), resolution=RESOLUTION,
            current_state=CURRENT_STATE, policy_version="mvp-1",
        )
        assert result1.plan is not None and result2.plan is not None
        self.assertEqual(
            result1.plan.actions[0].idempotency_key, result2.plan.actions[0].idempotency_key
        )

    def test_idempotency_key_changes_for_a_different_snapshot(self) -> None:
        result1 = compile_edit_user_name_plan(
            snapshot=_snapshot(snapshot_id="s1"), interpretation=_interpretation(snapshot_id="s1"),
            resolution=RESOLUTION, current_state=CURRENT_STATE, policy_version="mvp-1",
        )
        result2 = compile_edit_user_name_plan(
            snapshot=_snapshot(snapshot_id="s2"), interpretation=_interpretation(snapshot_id="s2"),
            resolution=RESOLUTION, current_state=CURRENT_STATE, policy_version="mvp-1",
        )
        assert result1.plan is not None and result2.plan is not None
        self.assertNotEqual(
            result1.plan.actions[0].idempotency_key, result2.plan.actions[0].idempotency_key
        )

    def test_no_plan_is_compiled_when_policy_requires_manual_review(self) -> None:
        result = compile_edit_user_name_plan(
            snapshot=_snapshot(),
            interpretation=_interpretation(evidence=()),  # no approval evidence
            resolution=RESOLUTION,
            current_state=CURRENT_STATE,
            policy_version="mvp-1",
        )
        self.assertEqual(result.evaluation.decision, PolicyDecisionType.MANUAL_REVIEW)
        self.assertIsNone(result.plan)

    def test_no_plan_is_compiled_when_policy_denies(self) -> None:
        # ticket_type "unknown" is a valid KnownTicketType value, but
        # edit_crm_user_name's registry entry only allows "change_user_name"
        # — this reaches the Policy Engine (match_count==1) and is denied
        # there, distinct from the compiler's own resolution-count guard.
        result = compile_edit_user_name_plan(
            snapshot=_snapshot(),
            interpretation=_interpretation(ticket_type="unknown", subject_reference="user:x"),
            resolution=RESOLUTION,
            current_state=CURRENT_STATE,
            policy_version="mvp-1",
        )
        self.assertEqual(result.evaluation.decision, PolicyDecisionType.DENY)
        self.assertIsNone(result.plan)

    def test_unresolved_subject_raises_before_reaching_policy(self) -> None:
        with self.assertRaises(PlanCompilationError):
            compile_edit_user_name_plan(
                snapshot=_snapshot(),
                interpretation=_interpretation(),
                resolution=ResolvedEntity(query="nobody@example.com", match_count=0),
                current_state=CURRENT_STATE,
                policy_version="mvp-1",
            )

    def test_ambiguous_resolution_raises_before_reaching_policy(self) -> None:
        with self.assertRaises(PlanCompilationError):
            compile_edit_user_name_plan(
                snapshot=_snapshot(),
                interpretation=_interpretation(),
                resolution=ResolvedEntity(query="dup@example.com", match_count=2),
                current_state=CURRENT_STATE,
                policy_version="mvp-1",
            )

    def test_only_the_requested_field_is_included_in_before_after_state(self) -> None:
        result = compile_edit_user_name_plan(
            snapshot=_snapshot(),
            interpretation=_interpretation(requested_changes={"last_name": "Ivanova"}),
            resolution=RESOLUTION,
            current_state=CURRENT_STATE,
            policy_version="mvp-1",
        )
        plan = result.plan
        assert plan is not None
        action = plan.actions[0]
        self.assertNotIn("first_name", action.expected_before_state)
        self.assertNotIn("first_name", action.expected_after_state)
        # But the argument set still carries both, since the registry
        # requires both fields regardless of which one is changing.
        self.assertIn("first_name", action.arguments)


if __name__ == "__main__":
    unittest.main()
