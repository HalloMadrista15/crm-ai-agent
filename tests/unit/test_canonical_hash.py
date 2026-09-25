import unittest
from datetime import datetime, timezone

from crm_ai_agent.domain.entities import ExecutionPlan, PlanAction, TicketSnapshot
from crm_ai_agent.domain.enums import PlanStatus, RiskLevel
from crm_ai_agent.safety.canonical_hash import compute_hash, compute_plan_hash, compute_snapshot_hash


def _make_snapshot(**overrides) -> TicketSnapshot:
    defaults = dict(
        schema_version="1.0",
        snapshot_id="s1",
        ticket_id="t1",
        captured_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
        source_hash="src-hash-1",
        status="open",
        title="Rename request",
        trusted_fields={"assignee": "alice", "priority": "high"},
    )
    defaults.update(overrides)
    return TicketSnapshot(**defaults)


def _make_action(**overrides) -> PlanAction:
    defaults = dict(
        action_id="a1",
        action_type="edit_crm_user_name",
        arguments={"user_id": "u1", "first_name": "Ivan", "last_name": "Petrov"},
        preconditions=["user_exists"],
        expected_before_state={"first_name": "Ivon"},
        expected_after_state={"first_name": "Ivan"},
        risk_level=RiskLevel.MEDIUM,
        idempotency_key="idem-1",
    )
    defaults.update(overrides)
    return PlanAction(**defaults)


class TestComputeHash(unittest.TestCase):
    def test_hash_is_deterministic(self) -> None:
        self.assertEqual(compute_hash({"a": 1, "b": 2}), compute_hash({"a": 1, "b": 2}))

    def test_hash_is_insensitive_to_key_order(self) -> None:
        self.assertEqual(compute_hash({"a": 1, "b": 2}), compute_hash({"b": 2, "a": 1}))

    def test_hash_changes_with_content(self) -> None:
        self.assertNotEqual(compute_hash({"a": 1}), compute_hash({"a": 2}))

    def test_hash_serializes_datetimes_consistently(self) -> None:
        dt = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(compute_hash({"when": dt}), compute_hash({"when": dt}))


class TestSnapshotHash(unittest.TestCase):
    def test_same_content_same_hash_regardless_of_snapshot_id(self) -> None:
        snap1 = _make_snapshot(snapshot_id="s1")
        snap2 = _make_snapshot(snapshot_id="s2")
        self.assertEqual(compute_snapshot_hash(snap1), compute_snapshot_hash(snap2))

    def test_same_content_same_hash_regardless_of_trusted_field_insertion_order(self) -> None:
        snap1 = _make_snapshot(trusted_fields={"assignee": "alice", "priority": "high"})
        snap2 = _make_snapshot(trusted_fields={"priority": "high", "assignee": "alice"})
        self.assertEqual(compute_snapshot_hash(snap1), compute_snapshot_hash(snap2))

    def test_changed_title_changes_hash(self) -> None:
        snap1 = _make_snapshot(title="Rename request")
        snap2 = _make_snapshot(title="Rename request (edited)")
        self.assertNotEqual(compute_snapshot_hash(snap1), compute_snapshot_hash(snap2))

    def test_changed_trusted_field_value_changes_hash(self) -> None:
        snap1 = _make_snapshot(trusted_fields={"assignee": "alice"})
        snap2 = _make_snapshot(trusted_fields={"assignee": "bob"})
        self.assertNotEqual(compute_snapshot_hash(snap1), compute_snapshot_hash(snap2))


class TestPlanHash(unittest.TestCase):
    def test_same_content_same_hash_regardless_of_argument_insertion_order(self) -> None:
        action1 = _make_action(arguments={"user_id": "u1", "first_name": "Ivan", "last_name": "Petrov"})
        action2 = _make_action(arguments={"last_name": "Petrov", "first_name": "Ivan", "user_id": "u1"})
        plan1 = ExecutionPlan(
            plan_id="p1", ticket_id="t1", snapshot_id="s1", snapshot_hash="h1",
            plan_hash="unused", policy_version="v1", actions=(action1,), status=PlanStatus.DRAFT,
        )
        plan2 = ExecutionPlan(
            plan_id="p1", ticket_id="t1", snapshot_id="s1", snapshot_hash="h1",
            plan_hash="unused", policy_version="v1", actions=(action2,), status=PlanStatus.DRAFT,
        )
        self.assertEqual(compute_plan_hash(plan1), compute_plan_hash(plan2))

    def test_changed_snapshot_hash_changes_plan_hash(self) -> None:
        action = _make_action()
        plan1 = ExecutionPlan(
            plan_id="p1", ticket_id="t1", snapshot_id="s1", snapshot_hash="h1",
            plan_hash="unused", policy_version="v1", actions=(action,), status=PlanStatus.DRAFT,
        )
        plan2 = ExecutionPlan(
            plan_id="p1", ticket_id="t1", snapshot_id="s1", snapshot_hash="h2",
            plan_hash="unused", policy_version="v1", actions=(action,), status=PlanStatus.DRAFT,
        )
        self.assertNotEqual(compute_plan_hash(plan1), compute_plan_hash(plan2))

    def test_changed_action_arguments_changes_plan_hash(self) -> None:
        plan1 = ExecutionPlan(
            plan_id="p1", ticket_id="t1", snapshot_id="s1", snapshot_hash="h1",
            plan_hash="unused", policy_version="v1",
            actions=(_make_action(arguments={"user_id": "u1", "first_name": "Ivan", "last_name": "Petrov"}),),
            status=PlanStatus.DRAFT,
        )
        plan2 = ExecutionPlan(
            plan_id="p1", ticket_id="t1", snapshot_id="s1", snapshot_hash="h1",
            plan_hash="unused", policy_version="v1",
            actions=(_make_action(arguments={"user_id": "u1", "first_name": "Ivanka", "last_name": "Petrov"}),),
            status=PlanStatus.DRAFT,
        )
        self.assertNotEqual(compute_plan_hash(plan1), compute_plan_hash(plan2))


if __name__ == "__main__":
    unittest.main()
