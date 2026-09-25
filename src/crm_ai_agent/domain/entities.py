"""Core domain entities.

These are plain dataclasses on purpose: the domain layer must not depend on
persistence, Playwright, Telegram or any LLM SDK. Adapters translate to/from
these shapes.

Entities that participate in hashing or approval (``TicketSnapshot``,
``LLMInterpretation``, ``PlanAction``, ``ExecutionPlan``) deep-freeze their
nested dict/list fields in ``__post_init__`` via ``object.__setattr__`` (the
one legal way to set a field on a frozen dataclass) so that no caller can
mutate a plan's arguments after it has been hashed and approved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType

from crm_ai_agent.domain.enums import (
    ActionOperationStatus,
    ActionRunStatus,
    ApprovalStatus,
    ExecutionStatus,
    PlanStatus,
    RiskLevel,
    TicketStatus,
    TrustLevel,
)
from crm_ai_agent.domain.immutability import deep_freeze
from crm_ai_agent.domain.validation import (
    DomainValidationError,
    validate_action_type,
    validate_confidence,
    validate_non_empty,
    validate_positive_int,
    validate_ticket_type,
    validate_unique_non_empty_strings,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TextFragment:
    """A single piece of ticket text with explicit trust provenance."""

    section: str
    source_reference: str
    captured_at: datetime
    trust_level: TrustLevel
    text: str


@dataclass(frozen=True)
class TicketSnapshot:
    schema_version: str
    snapshot_id: str
    ticket_id: str
    captured_at: datetime
    source_hash: str
    status: str
    title: str
    fragments: tuple[TextFragment, ...] = field(default_factory=tuple)
    trusted_fields: MappingProxyType[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "fragments", tuple(self.fragments))
        object.__setattr__(self, "trusted_fields", deep_freeze(dict(self.trusted_fields)))


@dataclass
class Ticket:
    ticket_id: str
    external_ticket_id: str
    tenant_id: str = "default"
    status: TicketStatus = TicketStatus.DISCOVERED
    version: int = 1
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class EvidenceItem:
    """One structured piece of evidence backing an LLM interpretation.

    Evidence is a pointer into the snapshot, not free text: the policy engine
    (Stage 2) must be able to check ``trust_level`` before treating anything
    as a fact (e.g. "approval found").
    """

    section: str
    source_reference: str
    trust_level: TrustLevel
    text: str


@dataclass(frozen=True)
class LLMInterpretation:
    schema_version: str
    snapshot_id: str
    ticket_type: str
    classification_confidence: float
    requested_changes: MappingProxyType[str, str]
    subject_reference: str
    evidence: tuple[EvidenceItem, ...] = field(default_factory=tuple)
    ambiguities: tuple[str, ...] = field(default_factory=tuple)
    missing_data: tuple[str, ...] = field(default_factory=tuple)
    risk_flags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        validate_ticket_type(self.ticket_type)
        object.__setattr__(
            self, "classification_confidence", validate_confidence(self.classification_confidence)
        )
        validate_non_empty(self.subject_reference, field_name="subject_reference")
        object.__setattr__(self, "requested_changes", deep_freeze(dict(self.requested_changes)))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "ambiguities", tuple(self.ambiguities))
        object.__setattr__(self, "missing_data", tuple(self.missing_data))
        object.__setattr__(self, "risk_flags", tuple(self.risk_flags))


@dataclass(frozen=True)
class PlanAction:
    action_id: str
    action_type: str
    arguments: MappingProxyType[str, str]
    preconditions: tuple[str, ...]
    expected_before_state: MappingProxyType[str, str]
    expected_after_state: MappingProxyType[str, str]
    risk_level: RiskLevel
    idempotency_key: str

    def __post_init__(self) -> None:
        validate_action_type(self.action_type)
        validate_non_empty(self.idempotency_key, field_name="idempotency_key")
        object.__setattr__(self, "arguments", deep_freeze(dict(self.arguments)))
        object.__setattr__(self, "preconditions", tuple(self.preconditions))
        object.__setattr__(self, "expected_before_state", deep_freeze(dict(self.expected_before_state)))
        object.__setattr__(self, "expected_after_state", deep_freeze(dict(self.expected_after_state)))


@dataclass(frozen=True)
class ExecutionPlan:
    plan_id: str
    ticket_id: str
    snapshot_id: str
    snapshot_hash: str
    plan_hash: str
    policy_version: str
    actions: tuple[PlanAction, ...]
    status: PlanStatus = PlanStatus.DRAFT
    created_at: datetime = field(default_factory=utcnow)
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions", tuple(self.actions))


@dataclass(frozen=True)
class ApprovalPolicySnapshot:
    """The approval policy in force at the moment an ``Approval`` was created.

    Persisted alongside the approval so that after a restart it is possible
    to determine *why* a given approver was allowed to consume it, without
    depending on the (possibly since-changed) live policy configuration.
    Frozen and validated at construction: once an approval has been issued
    against a policy snapshot, that snapshot must never be able to drift in
    memory — a mutated ``ttl_seconds`` or role list after the fact would
    silently change who was allowed to approve an already-issued approval.
    """

    policy_version: str
    allowed_approver_roles: tuple[str, ...]
    ttl_seconds: int

    def __post_init__(self) -> None:
        validate_non_empty(self.policy_version, field_name="policy_version")
        object.__setattr__(
            self,
            "allowed_approver_roles",
            validate_unique_non_empty_strings(
                tuple(self.allowed_approver_roles), field_name="allowed_approver_roles"
            ),
        )
        object.__setattr__(
            self, "ttl_seconds", validate_positive_int(self.ttl_seconds, field_name="ttl_seconds")
        )


@dataclass
class Approval:
    approval_id: str
    ticket_id: str
    plan_id: str
    plan_hash: str
    snapshot_hash: str
    policy: ApprovalPolicySnapshot
    status: ApprovalStatus = ApprovalStatus.PENDING
    telegram_user_id: str | None = None
    telegram_chat_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    expires_at: datetime | None = None
    consumed_at: datetime | None = None
    decision_reason: str | None = None

    @property
    def allowed_approver_roles(self) -> tuple[str, ...]:
        return self.policy.allowed_approver_roles


@dataclass
class ActionOperation:
    """The logical, idempotent unit of mutation work.

    Exactly one ``ActionOperation`` exists per ``idempotency_key``. It may
    have many :class:`ActionRun` attempts, but the operation's own status is
    what callers must check before deciding to attempt again — never the
    presence/absence of a run row.
    """

    operation_id: str
    plan_id: str
    action_id: str
    idempotency_key: str
    status: ActionOperationStatus = ActionOperationStatus.PENDING


@dataclass
class ActionRun:
    """A single execution attempt of an :class:`ActionOperation`.

    ``plan_id`` is redundant with ``operation_id``/``execution_id`` in
    memory (both already resolve to the same plan), but is carried
    explicitly here because it is what lets the database enforce, via
    composite foreign keys, that the operation and the execution referenced
    by one run actually belong to the same plan. See migrations/0001_init.sql
    and docs/schema_foundation.md.
    """

    action_run_id: str
    operation_id: str
    execution_id: str
    plan_id: str
    action_id: str
    attempt_no: int
    status: ActionRunStatus = ActionRunStatus.PENDING
    before_state_hash: str | None = None
    after_state_hash: str | None = None
    error_class: str | None = None


@dataclass
class Execution:
    execution_id: str
    plan_id: str
    status: ExecutionStatus = ExecutionStatus.QUEUED
    claimed_by: str | None = None
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True)
class ResolvedEntity:
    """The result of a deterministic, read-only lookup of a subject (e.g. a
    CRM user) referenced by an ``LLMInterpretation``.

    This is the Stage 5 "Entity Resolution" contract's output shape, added
    now as a foundation type so the Policy Engine has something typed to
    require exactly one match against. The actual read-only CRM lookup is
    Stage 3+ work; nothing here talks to a real CRM.
    """

    query: str
    match_count: int
    entity_id: str | None = None
    source: str = "crm"

    def __post_init__(self) -> None:
        validate_non_empty(self.query, field_name="query")
        if self.match_count < 0:
            raise DomainValidationError(f"match_count must be >= 0, got {self.match_count!r}")
        if self.match_count == 1 and not self.entity_id:
            raise DomainValidationError("match_count == 1 requires a non-empty entity_id")
        if self.match_count != 1 and self.entity_id:
            raise DomainValidationError("entity_id must only be set when match_count == 1")


@dataclass(frozen=True)
class AuditEvent:
    """Foundation shape for the append-only audit trail (Stage 2 builds the
    hash-chaining service on top of this). ``sequence_no`` gives a total
    order within a ticket that does not depend on timestamp resolution.
    """

    event_id: str
    ticket_id: str | None
    correlation_id: str
    sequence_no: int
    actor_type: str
    actor_id: str
    event_type: str
    created_at: datetime
    prev_event_hash: str | None
    event_hash: str
    payload: MappingProxyType[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        validate_non_empty(self.actor_type, field_name="actor_type")
        validate_non_empty(self.actor_id, field_name="actor_id")
        validate_non_empty(self.correlation_id, field_name="correlation_id")
        object.__setattr__(self, "payload", deep_freeze(dict(self.payload)))
