"""The Action Registry: the single source of truth for what mutation actions
exist and what a valid instance of each looks like.

Per docs/action_catalog.md and the master spec, this is the ONLY place a
plan action's shape is allowed to be defined. A ``PlanAction`` that isn't
registered here, that supplies an argument outside its spec's allowlist, or
that omits a required one, must never reach the Policy Engine's ALLOW
decision — see ``policy_engine.py``, which calls ``validate_against_registry``
before anything else.

``internal_only`` actions (``add_ticket_comment``, ``close_ticket``) exist in
the registry so their shape is fixed, but they are never valid inside a
plan compiled from an LLM interpretation — only the finalizer (Stage 8, not
built yet) may invoke them, and it will do so directly, not through a
Telegram-approved ``ExecutionPlan``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crm_ai_agent.domain.entities import PlanAction
from crm_ai_agent.domain.enums import KnownActionType, RiskLevel


class ActionRegistryError(Exception):
    pass


@dataclass(frozen=True)
class ActionSpec:
    action_type: KnownActionType
    required_argument_keys: frozenset[str]
    risk_level: RiskLevel
    required_approval: bool
    internal_only: bool
    # Empty frozenset means "not restricted to specific ticket types" (used
    # by read-only/internal actions); non-empty means the action may only
    # appear in a plan compiled for one of these ticket types.
    allowed_ticket_types: frozenset[str] = field(default_factory=frozenset)


_REGISTRY: dict[KnownActionType, ActionSpec] = {
    KnownActionType.FIND_CRM_USER: ActionSpec(
        action_type=KnownActionType.FIND_CRM_USER,
        required_argument_keys=frozenset({"query"}),
        risk_level=RiskLevel.LOW,
        required_approval=False,
        internal_only=False,
        allowed_ticket_types=frozenset({"change_user_name"}),
    ),
    KnownActionType.EDIT_CRM_USER_NAME: ActionSpec(
        action_type=KnownActionType.EDIT_CRM_USER_NAME,
        required_argument_keys=frozenset({"user_id", "first_name", "last_name"}),
        risk_level=RiskLevel.MEDIUM,
        required_approval=True,
        internal_only=False,
        allowed_ticket_types=frozenset({"change_user_name"}),
    ),
    KnownActionType.ADD_TICKET_COMMENT: ActionSpec(
        action_type=KnownActionType.ADD_TICKET_COMMENT,
        required_argument_keys=frozenset({"comment_text"}),
        risk_level=RiskLevel.LOW,
        required_approval=False,
        internal_only=True,
    ),
    KnownActionType.CLOSE_TICKET: ActionSpec(
        action_type=KnownActionType.CLOSE_TICKET,
        required_argument_keys=frozenset(),
        risk_level=RiskLevel.LOW,
        required_approval=False,
        internal_only=True,
    ),
    # Confirmed 2026-09-07 via read-only recon of a real Webitel user card
    # (login 10078): provisioning a new user clones Roles, License, and the
    # single "group" Variable from an existing similar ("template") user.
    # General info (name/login/extension) is entered fresh for the new
    # person, never copied. The password is deliberately NOT a plan
    # argument: it is generated live by Webitel's own "generate" control
    # during execution (Stage 7, not built), not typed by a human into
    # someone else's account and not something we bake into an approved
    # plan/hash ahead of time — see adapters/webitel_playwright/selectors.py
    # (user_edit.password_generate_button). ``temporary_password`` is a
    # plan argument (not the password itself) because whether to force a
    # reset on first login is a real decision the approver should see.
    KnownActionType.PROVISION_WEBITEL_USER: ActionSpec(
        action_type=KnownActionType.PROVISION_WEBITEL_USER,
        required_argument_keys=frozenset({
            "template_user_login",
            "new_user_login",
            "new_user_name",
            "new_user_extension",
            "roles",
            "license",
            "group",
            "temporary_password",
        }),
        risk_level=RiskLevel.HIGH,
        required_approval=True,
        internal_only=False,
        allowed_ticket_types=frozenset({"access_to_information_system"}),
    ),
}


def get_action_spec(action_type: str) -> ActionSpec:
    try:
        key = KnownActionType(action_type)
    except ValueError as exc:
        allowed = ", ".join(t.value for t in KnownActionType)
        raise ActionRegistryError(f"Unknown action_type {action_type!r}; must be one of: {allowed}") from exc
    return _REGISTRY[key]


def validate_against_registry(action: PlanAction, *, allow_internal_only: bool = False) -> ActionSpec:
    """Check one plan action against its registered spec.

    Raises ``ActionRegistryError`` on any mismatch. Returns the matched spec
    on success so callers (the Policy Engine) don't have to look it up twice.
    """

    spec = get_action_spec(action.action_type)

    if spec.internal_only and not allow_internal_only:
        raise ActionRegistryError(
            f"{action.action_type} is internal-only; it cannot appear in a plan compiled from an LLM interpretation"
        )

    provided_keys = frozenset(action.arguments.keys())
    missing = spec.required_argument_keys - provided_keys
    if missing:
        raise ActionRegistryError(
            f"action {action.action_type} is missing required arguments: {sorted(missing)}"
        )
    extra = provided_keys - spec.required_argument_keys
    if extra:
        raise ActionRegistryError(
            f"action {action.action_type} has unregistered arguments: {sorted(extra)}"
        )

    if action.risk_level != spec.risk_level:
        raise ActionRegistryError(
            f"action {action.action_type} declares risk_level {action.risk_level.value}, "
            f"but the registry requires {spec.risk_level.value}"
        )

    return spec


def is_ticket_type_allowed(spec: ActionSpec, ticket_type: str) -> bool:
    if not spec.allowed_ticket_types:
        return True
    return ticket_type in spec.allowed_ticket_types
