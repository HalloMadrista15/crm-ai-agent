import unittest

from crm_ai_agent.domain.enums import TicketStatus
from crm_ai_agent.domain.state_machines import (
    ActorType,
    Capability,
    InvalidTransitionError,
    Principal,
    RevalidationResult,
    TicketStateMachine,
    TransitionGuardError,
    VerificationResult,
    validate_transition,
)

FINALIZER = Principal(
    actor_type=ActorType.FINALIZER_SERVICE, actor_id="finalizer-1", capabilities=frozenset({Capability.FINALIZER})
)
OPERATOR_WITH_CLOSE = Principal(
    actor_type=ActorType.OPERATOR,
    actor_id="op-1",
    capabilities=frozenset({Capability.OPERATOR_MANUAL_CLOSE}),
)
PLAIN_OPERATOR = Principal(actor_type=ActorType.OPERATOR, actor_id="op-2")
PASSED_REVALIDATION = RevalidationResult(passed=True, checked_snapshot_hash="h1", checked_plan_hash="p1")
FAILED_REVALIDATION = RevalidationResult(
    passed=False, checked_snapshot_hash="h1", checked_plan_hash="p1", reason="ticket changed"
)
PASSED_VERIFICATION = VerificationResult(passed=True, verified_action_run_id="ar1")
FAILED_VERIFICATION = VerificationResult(passed=False, verified_action_run_id="ar1", reason="postcondition not met")


class TestTicketStateMachine(unittest.TestCase):
    def test_happy_path_transitions_are_allowed_with_required_guards(self) -> None:
        machine = TicketStateMachine()
        machine.transition_to(TicketStatus.READING)
        machine.transition_to(TicketStatus.ANALYZING)
        machine.transition_to(TicketStatus.AWAITING_APPROVAL)
        machine.transition_to(TicketStatus.APPROVED)
        machine.transition_to(TicketStatus.EXECUTION_QUEUED, revalidation=PASSED_REVALIDATION)
        machine.transition_to(TicketStatus.EXECUTING)
        machine.transition_to(TicketStatus.VERIFYING)
        machine.transition_to(TicketStatus.FINALIZING, verification=PASSED_VERIFICATION)
        machine.transition_to(TicketStatus.CLOSED, principal=FINALIZER)
        self.assertEqual(machine.status, TicketStatus.CLOSED)

    def test_cannot_skip_from_discovered_to_closed(self) -> None:
        machine = TicketStateMachine()
        with self.assertRaises(InvalidTransitionError):
            machine.transition_to(TicketStatus.CLOSED, principal=FINALIZER)

    def test_cannot_leave_terminal_status(self) -> None:
        with self.assertRaises(InvalidTransitionError):
            validate_transition(TicketStatus.CLOSED, TicketStatus.ANALYZING)
        with self.assertRaises(InvalidTransitionError):
            validate_transition(TicketStatus.REJECTED, TicketStatus.APPROVED)
        with self.assertRaises(InvalidTransitionError):
            validate_transition(TicketStatus.EXTERNALLY_CLOSED, TicketStatus.ANALYZING)

    def test_manual_review_reachable_from_active_states(self) -> None:
        for status in (
            TicketStatus.READING,
            TicketStatus.ANALYZING,
            TicketStatus.AWAITING_APPROVAL,
            TicketStatus.EXECUTING,
        ):
            validate_transition(status, TicketStatus.MANUAL_REVIEW)

    def test_manual_review_cannot_transition_directly_to_closed(self) -> None:
        with self.assertRaises(InvalidTransitionError):
            validate_transition(TicketStatus.MANUAL_REVIEW, TicketStatus.CLOSED)

    def test_manual_review_can_become_externally_closed_only_with_capability(self) -> None:
        machine = TicketStateMachine(initial_status=TicketStatus.MANUAL_REVIEW)
        with self.assertRaises(TransitionGuardError):
            machine.transition_to(TicketStatus.EXTERNALLY_CLOSED, principal=PLAIN_OPERATOR)
        machine.transition_to(TicketStatus.EXTERNALLY_CLOSED, principal=OPERATOR_WITH_CLOSE)
        self.assertEqual(machine.status, TicketStatus.EXTERNALLY_CLOSED)

    def test_approved_plan_can_become_stale_and_return_to_analyzing(self) -> None:
        machine = TicketStateMachine(initial_status=TicketStatus.APPROVED)
        machine.transition_to(TicketStatus.STALE)
        machine.transition_to(TicketStatus.ANALYZING)
        self.assertEqual(machine.status, TicketStatus.ANALYZING)

    def test_recovery_required_cannot_jump_directly_to_closed(self) -> None:
        with self.assertRaises(InvalidTransitionError):
            validate_transition(TicketStatus.RECOVERY_REQUIRED, TicketStatus.CLOSED)

    def test_execution_queued_requires_a_passed_revalidation_result(self) -> None:
        machine = TicketStateMachine(initial_status=TicketStatus.APPROVED)
        with self.assertRaises(TransitionGuardError):
            machine.transition_to(TicketStatus.EXECUTION_QUEUED)
        with self.assertRaises(TransitionGuardError):
            machine.transition_to(TicketStatus.EXECUTION_QUEUED, revalidation=FAILED_REVALIDATION)
        machine.transition_to(TicketStatus.EXECUTION_QUEUED, revalidation=PASSED_REVALIDATION)
        self.assertEqual(machine.status, TicketStatus.EXECUTION_QUEUED)

    def test_finalizing_requires_a_passed_verification_result(self) -> None:
        machine = TicketStateMachine(initial_status=TicketStatus.VERIFYING)
        with self.assertRaises(TransitionGuardError):
            machine.transition_to(TicketStatus.FINALIZING)
        with self.assertRaises(TransitionGuardError):
            machine.transition_to(TicketStatus.FINALIZING, verification=FAILED_VERIFICATION)
        machine.transition_to(TicketStatus.FINALIZING, verification=PASSED_VERIFICATION)
        self.assertEqual(machine.status, TicketStatus.FINALIZING)

    def test_closed_requires_finalizer_capability_not_just_an_actor_label(self) -> None:
        machine = TicketStateMachine(initial_status=TicketStatus.FINALIZING)
        # An operator principal, even one with a different real capability,
        # must not be able to close a ticket.
        with self.assertRaises(TransitionGuardError):
            machine.transition_to(TicketStatus.CLOSED, principal=OPERATOR_WITH_CLOSE)
        # A principal merely labeled as a finalizer actor type but without
        # the FINALIZER capability must also be rejected.
        unprivileged_finalizer_actor = Principal(actor_type=ActorType.FINALIZER_SERVICE, actor_id="finalizer-2")
        with self.assertRaises(TransitionGuardError):
            machine.transition_to(TicketStatus.CLOSED, principal=unprivileged_finalizer_actor)
        machine.transition_to(TicketStatus.CLOSED, principal=FINALIZER)
        self.assertEqual(machine.status, TicketStatus.CLOSED)


if __name__ == "__main__":
    unittest.main()
