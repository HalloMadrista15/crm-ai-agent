"""Ticket lifecycle state machine.

Only the transitions listed in ``ALLOWED_TRANSITIONS`` are legal. Some
transitions additionally require a *guard*: a predicate over a
:class:`TransitionContext` that must pass before the transition is allowed,
even though the bare from/to pair is structurally legal.

SECURITY NOTE — this is not a security boundary
-------------------------------------------------
This module enforces *internal consistency*: it stops orchestration code
from accidentally moving a ticket through an impossible sequence of states,
or from finalizing a plan that was never actually revalidated or verified in
memory. It does **not** authenticate anyone. A :class:`Principal` here is
whatever the caller constructs and passes in — nothing about this module
checks that the caller is who they claim to be.

Concretely: Telegram identity verification (that a callback really came from
an allow-listed chat/user), and the Policy Engine's evaluation of who is
*authorized* to approve or finalize a given ticket, are separate concerns
built in Stage 2/6. This state machine's guards exist so that once those
upstream checks have produced a real result — an authenticated approver, an
actual re-read of the CRM, an actual verifier run — that result has to be
threaded through as a typed value to reach a sensitive transition. A caller
that skips the real check and fabricates ``Principal(actor_id="x",
capabilities=frozenset({Capability.FINALIZER}))`` still gets past the guard:
guards make it structurally awkward to *forget* the check, not impossible to
*lie* about having done it. Enforcing that the ``Principal``/``RevalidationResult``/
``VerificationResult`` values passed in were honestly produced is the job of
whatever component constructs them, not of this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from crm_ai_agent.domain.enums import TicketStatus


class InvalidTransitionError(Exception):
    def __init__(self, from_status: TicketStatus, to_status: TicketStatus) -> None:
        super().__init__(f"Illegal ticket transition: {from_status} -> {to_status}")
        self.from_status = from_status
        self.to_status = to_status


class TransitionGuardError(Exception):
    def __init__(self, from_status: TicketStatus, to_status: TicketStatus, reason: str) -> None:
        super().__init__(f"Guard rejected transition {from_status} -> {to_status}: {reason}")
        self.from_status = from_status
        self.to_status = to_status
        self.reason = reason


class ActorType(str, Enum):
    SYSTEM = "SYSTEM"
    OPERATOR = "OPERATOR"
    ADMIN = "ADMIN"
    FINALIZER_SERVICE = "FINALIZER_SERVICE"
    RECOVERY_WORKER = "RECOVERY_WORKER"


class Capability(str, Enum):
    """A capability a :class:`Principal` may hold.

    Deliberately not a free-form string: a typo in a capability name fails
    at construction (``Capability("finalize")`` raises) instead of silently
    never matching the guard's check.
    """

    FINALIZER = "FINALIZER"
    OPERATOR_MANUAL_CLOSE = "OPERATOR_MANUAL_CLOSE"


@dataclass(frozen=True)
class Principal:
    """The actor requesting a transition: who/what, and what it may do.

    This is a domain-level shape, not an authentication result. Stage 6
    (Telegram approval) and the Policy Engine are responsible for populating
    it from something actually verified (an allow-listed Telegram user ID
    mapped to a role, a service identity) rather than caller-supplied intent.
    """

    actor_type: ActorType
    actor_id: str
    capabilities: frozenset[Capability] = field(default_factory=frozenset)

    def has_capability(self, capability: Capability) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True)
class RevalidationResult:
    """The outcome of actually re-checking a plan/snapshot before execution.

    ``passed=True`` must only be constructed by the component that performed
    the re-read (Stage 5/7), never defaulted or inferred by a caller that
    wants to skip the check.
    """

    passed: bool
    checked_snapshot_hash: str
    checked_plan_hash: str
    reason: str = ""


@dataclass(frozen=True)
class VerificationResult:
    """The outcome of an action verifier run (Stage 7), not a claim of one."""

    passed: bool
    verified_action_run_id: str
    reason: str = ""


SYSTEM_PRINCIPAL = Principal(actor_type=ActorType.SYSTEM, actor_id="system")


@dataclass(frozen=True)
class TransitionContext:
    """What a guard is allowed to look at when approving a transition."""

    principal: Principal = SYSTEM_PRINCIPAL
    revalidation: RevalidationResult | None = None
    verification: VerificationResult | None = None


Guard = Callable[[TransitionContext], None]


def _require_revalidated(context: TransitionContext) -> None:
    if context.revalidation is None or not context.revalidation.passed:
        raise ValueError("plan/snapshot must be revalidated (RevalidationResult.passed) before queuing execution")


def _require_verification_passed(context: TransitionContext) -> None:
    if context.verification is None or not context.verification.passed:
        raise ValueError("verification must pass (VerificationResult.passed) before finalizing")


def _require_capability(capability: Capability) -> Guard:
    def guard(context: TransitionContext) -> None:
        if not context.principal.has_capability(capability):
            raise ValueError(
                f"principal {context.principal.actor_type}:{context.principal.actor_id} "
                f"lacks required capability {capability.value}"
            )

    return guard


ALLOWED_TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    TicketStatus.DISCOVERED: {TicketStatus.READING, TicketStatus.MANUAL_REVIEW, TicketStatus.CANCELLED},
    TicketStatus.READING: {TicketStatus.ANALYZING, TicketStatus.MANUAL_REVIEW},
    TicketStatus.ANALYZING: {TicketStatus.AWAITING_APPROVAL, TicketStatus.MANUAL_REVIEW},
    TicketStatus.AWAITING_APPROVAL: {TicketStatus.APPROVED, TicketStatus.REJECTED, TicketStatus.MANUAL_REVIEW},
    TicketStatus.APPROVED: {TicketStatus.STALE, TicketStatus.EXECUTION_QUEUED, TicketStatus.MANUAL_REVIEW},
    TicketStatus.STALE: {TicketStatus.ANALYZING, TicketStatus.MANUAL_REVIEW, TicketStatus.CANCELLED},
    TicketStatus.EXECUTION_QUEUED: {TicketStatus.EXECUTING, TicketStatus.RECOVERY_REQUIRED},
    TicketStatus.EXECUTING: {TicketStatus.VERIFYING, TicketStatus.RECOVERY_REQUIRED, TicketStatus.MANUAL_REVIEW},
    TicketStatus.VERIFYING: {TicketStatus.FINALIZING, TicketStatus.MANUAL_REVIEW, TicketStatus.RECOVERY_REQUIRED},
    TicketStatus.FINALIZING: {TicketStatus.CLOSED, TicketStatus.MANUAL_REVIEW},
    TicketStatus.RECOVERY_REQUIRED: {TicketStatus.VERIFYING, TicketStatus.MANUAL_REVIEW, TicketStatus.ANALYZING},
    # NOTE: MANUAL_REVIEW -> CLOSED is intentionally NOT allowed. A ticket
    # closed by a human directly in the CRM while under manual review is
    # recorded as EXTERNALLY_CLOSED, a distinct terminal state, so that
    # "CLOSED" always means "closed by our finalizer after verification".
    TicketStatus.MANUAL_REVIEW: {TicketStatus.ANALYZING, TicketStatus.CANCELLED, TicketStatus.EXTERNALLY_CLOSED},
    TicketStatus.REJECTED: set(),
    TicketStatus.CLOSED: set(),
    TicketStatus.CANCELLED: set(),
    TicketStatus.EXTERNALLY_CLOSED: set(),
}

GUARDS: dict[tuple[TicketStatus, TicketStatus], Guard] = {
    (TicketStatus.APPROVED, TicketStatus.EXECUTION_QUEUED): _require_revalidated,
    (TicketStatus.VERIFYING, TicketStatus.FINALIZING): _require_verification_passed,
    (TicketStatus.FINALIZING, TicketStatus.CLOSED): _require_capability(Capability.FINALIZER),
    (TicketStatus.MANUAL_REVIEW, TicketStatus.EXTERNALLY_CLOSED): _require_capability(
        Capability.OPERATOR_MANUAL_CLOSE
    ),
}


def validate_transition(
    from_status: TicketStatus,
    to_status: TicketStatus,
    context: TransitionContext | None = None,
) -> None:
    allowed = ALLOWED_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        raise InvalidTransitionError(from_status, to_status)

    guard = GUARDS.get((from_status, to_status))
    if guard is not None:
        if context is None:
            raise TransitionGuardError(from_status, to_status, "transition requires a context but none was provided")
        try:
            guard(context)
        except ValueError as exc:
            raise TransitionGuardError(from_status, to_status, str(exc)) from exc


class TicketStateMachine:
    """Wraps a single ticket's status and enforces legal, guarded transitions."""

    def __init__(self, initial_status: TicketStatus = TicketStatus.DISCOVERED) -> None:
        self._status = initial_status

    @property
    def status(self) -> TicketStatus:
        return self._status

    def transition_to(
        self,
        to_status: TicketStatus,
        *,
        principal: Principal = SYSTEM_PRINCIPAL,
        revalidation: RevalidationResult | None = None,
        verification: VerificationResult | None = None,
    ) -> None:
        context = TransitionContext(
            principal=principal,
            revalidation=revalidation,
            verification=verification,
        )
        validate_transition(self._status, to_status, context)
        self._status = to_status
