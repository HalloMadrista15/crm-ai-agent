import unittest
from types import MappingProxyType

from crm_ai_agent.domain.entities import ExecutionPlan, PlanAction, TicketSnapshot
from crm_ai_agent.domain.enums import PlanStatus, RiskLevel
from crm_ai_agent.domain.immutability import canonicalize, deep_freeze
from datetime import datetime, timezone


class TestDeepFreeze(unittest.TestCase):
    def test_nested_dict_and_list_are_frozen(self) -> None:
        frozen = deep_freeze({"a": [1, 2, {"b": 3}], "c": {"d": [4, 5]}})
        self.assertIsInstance(frozen, MappingProxyType)
        self.assertIsInstance(frozen["a"], tuple)
        self.assertIsInstance(frozen["a"][2], MappingProxyType)
        self.assertIsInstance(frozen["c"], MappingProxyType)
        self.assertIsInstance(frozen["c"]["d"], tuple)

        with self.assertRaises(TypeError):
            frozen["a"] = ()  # type: ignore[index]
        with self.assertRaises(AttributeError):
            frozen["a"].append(99)  # type: ignore[attr-defined]
        with self.assertRaises(TypeError):
            frozen["c"]["d"] = ()  # type: ignore[index]

    def test_mutating_the_original_source_after_freezing_does_not_affect_result(self) -> None:
        source_inner = {"first_name": "Ivon"}
        source = {"before": source_inner}
        frozen = deep_freeze(source)

        source_inner["first_name"] = "MALLORY"
        source["before"] = {"first_name": "MALLORY2"}
        source["new_key"] = "injected"

        self.assertEqual(frozen["before"]["first_name"], "Ivon")
        self.assertNotIn("new_key", frozen)


class TestCanonicalize(unittest.TestCase):
    def test_canonicalize_is_insensitive_to_key_order(self) -> None:
        a = canonicalize({"b": 2, "a": 1, "c": {"y": 2, "x": 1}})
        b = canonicalize({"a": 1, "c": {"x": 1, "y": 2}, "b": 2})
        self.assertEqual(a, b)

    def test_canonicalize_is_insensitive_to_dict_vs_mappingproxy(self) -> None:
        plain = canonicalize({"a": 1, "b": {"c": 2}})
        frozen = canonicalize(deep_freeze({"b": {"c": 2}, "a": 1}))
        self.assertEqual(plain, frozen)

    def test_canonicalize_preserves_list_order_but_normalizes_frozenset(self) -> None:
        self.assertEqual(canonicalize([3, 1, 2]), [3, 1, 2])
        self.assertEqual(canonicalize(frozenset({3, 1, 2})), [1, 2, 3])

    def test_canonicalize_of_logically_equal_plan_actions_matches_regardless_of_construction_order(self) -> None:
        action1 = PlanAction(
            action_id="a1",
            action_type="edit_crm_user_name",
            arguments={"first_name": "Ivan", "user_id": "u1"},
            preconditions=["user_exists"],
            expected_before_state={"first_name": "Ivon"},
            expected_after_state={"first_name": "Ivan"},
            risk_level=RiskLevel.MEDIUM,
            idempotency_key="idem-1",
        )
        action2 = PlanAction(
            action_id="a1",
            action_type="edit_crm_user_name",
            arguments={"user_id": "u1", "first_name": "Ivan"},
            preconditions=["user_exists"],
            expected_before_state={"first_name": "Ivon"},
            expected_after_state={"first_name": "Ivan"},
            risk_level=RiskLevel.MEDIUM,
            idempotency_key="idem-1",
        )
        self.assertEqual(canonicalize(action1.arguments), canonicalize(action2.arguments))


class TestSnapshotAndPlanDeepImmutability(unittest.TestCase):
    def test_ticket_snapshot_fragments_tuple_cannot_be_appended_to(self) -> None:
        snapshot = TicketSnapshot(
            schema_version="1.0",
            snapshot_id="snap-1",
            ticket_id="t1",
            captured_at=datetime.now(timezone.utc),
            source_hash="hash1",
            status="open",
            title="Test",
        )
        with self.assertRaises(AttributeError):
            snapshot.fragments.append("x")  # type: ignore[attr-defined]

    def test_execution_plan_actions_tuple_is_frozen_even_if_source_list_mutated(self) -> None:
        action = PlanAction(
            action_id="a1",
            action_type="edit_crm_user_name",
            arguments={"user_id": "u1"},
            preconditions=[],
            expected_before_state={},
            expected_after_state={},
            risk_level=RiskLevel.LOW,
            idempotency_key="idem-1",
        )
        source_actions = [action]
        plan = ExecutionPlan(
            plan_id="p1",
            ticket_id="t1",
            snapshot_id="s1",
            snapshot_hash="h1",
            plan_hash="ph1",
            policy_version="v1",
            actions=source_actions,
            status=PlanStatus.DRAFT,
        )
        source_actions.append(action)  # mutate the original list after construction
        self.assertEqual(len(plan.actions), 1)
        with self.assertRaises(AttributeError):
            plan.actions.append(action)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
