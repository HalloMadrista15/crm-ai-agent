"""Domain enums shared across entities and state machines."""

from enum import Enum


class TicketStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    READING = "READING"
    ANALYZING = "ANALYZING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTION_QUEUED = "EXECUTION_QUEUED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    FINALIZING = "FINALIZING"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    STALE = "STALE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    CANCELLED = "CANCELLED"
    EXTERNALLY_CLOSED = "EXTERNALLY_CLOSED"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class ExecutionStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIALLY_FAILED = "PARTIALLY_FAILED"
    FAILED = "FAILED"


class ActionOperationStatus(str, Enum):
    """Status of the logical operation identified by an idempotency key.

    One operation may have many :class:`ActionRunStatus` attempts, but the
    operation itself only ever reaches one terminal outcome. ``UNKNOWN_OUTCOME``
    is terminal-for-automation: it routes to reconciliation, never to a blind
    retry of the mutation.
    """

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED_FINAL = "FAILED_FINAL"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    RECONCILING = "RECONCILING"


class ActionRunStatus(str, Enum):
    """Status of a single execution attempt of an operation."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


class PlanStatus(str, Enum):
    DRAFT = "DRAFT"
    COMPILED = "COMPILED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    STALE = "STALE"
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class TrustLevel(str, Enum):
    CRM_SYSTEM_FIELD = "crm_system_field"
    CRM_WORKFLOW_APPROVAL = "crm_workflow_approval"
    OPERATOR_COMMENT = "operator_comment"
    REQUESTER_TEXT = "requester_text"
    HISTORICAL_RECORD = "historical_record"


class KnownActionType(str, Enum):
    """Foundation-level allowlist of action types.

    This is NOT the Action Registry (that is built in Stage 2 with full
    argument schemas, preconditions, verifiers, etc.). It exists so that
    ``PlanAction.action_type`` and ``LLMInterpretation.ticket_type`` cannot
    silently hold an arbitrary, unregistered string at the domain layer.
    """

    EDIT_CRM_USER_NAME = "edit_crm_user_name"
    FIND_CRM_USER = "find_crm_user"
    ADD_TICKET_COMMENT = "add_ticket_comment"
    CLOSE_TICKET = "close_ticket"

    # Real Astana Motors Webitel scenario (see docs/open_questions.md,
    # "Обновление после реальной разведки" and the 2026-09-07 Webitel recon
    # notes). Registry-only design so far: no adapter can execute this yet.
    # Confirmed via read-only recon of a real user's card (login 10078) that
    # provisioning a new Webitel user means cloning Roles, License, and the
    # single "group" Variable from an existing similar ("template") user —
    # General info (name/login/password/extension) is entered fresh, not
    # copied. See safety/action_registry.py's entry for the exact argument
    # shape this implies.
    PROVISION_WEBITEL_USER = "provision_webitel_user"


class KnownTicketType(str, Enum):
    """Foundation-level allowlist of ticket types the LLM may classify into.

    Any ticket type outside this set must be treated as unclassifiable and
    routed to manual review, not passed through to plan compilation.
    """

    CHANGE_USER_NAME = "change_user_name"
    # Creatio schema TsiOfficeNotesISAccessPage ("Доступ к ИС (Информационные
    # системы)"), confirmed on two real production tickets 2026-09-04/06.
    ACCESS_TO_INFORMATION_SYSTEM = "access_to_information_system"
    UNKNOWN = "unknown"


class PolicyDecisionType(str, Enum):
    """The only outcomes the Policy Engine may return (see safety/policy_engine.py)."""

    ALLOW_FOR_APPROVAL = "ALLOW_FOR_APPROVAL"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    DENY = "DENY"
    STALE = "STALE"


class ErrorClass(str, Enum):
    TRANSIENT = "TRANSIENT"
    VALIDATION = "VALIDATION"
    POLICY_DENIED = "POLICY_DENIED"
    AUTHENTICATION = "AUTHENTICATION"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SELECTOR_CHANGED = "SELECTOR_CHANGED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    CONFLICT = "CONFLICT"
    BUSINESS_RULE = "BUSINESS_RULE"
