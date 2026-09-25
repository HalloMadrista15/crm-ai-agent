"""The Plan Compiler: turns a resolved subject + interpretation into an
``ExecutionPlan``, but ONLY if the Policy Engine allows it.

Scope note: this compiles exactly one action type, ``edit_crm_user_name``,
matching Stage 5 of the master plan ("Скомпилируй action edit_crm_user_name
только при выполнении policy"). Compiling other action types is future
work, added the same way: build the draft ``PlanAction``, run it through
``policy_engine.evaluate``, only materialize an ``ExecutionPlan`` on
``ALLOW_FOR_APPROVAL``.

Verification contract: the original spec called for a distinct
``verification_spec`` field per action. This project already carries
``PlanAction.expected_before_state``/``expected_after_state`` (Stage 1) for
exactly that purpose — a verifier's job (Stage 7, not built) is to read the
CRM after mutation and confirm it matches ``expected_after_state``. Adding a
second, separate field would duplicate that contract for no benefit, so this
compiler treats ``expected_before_state``/``expected_after_state`` AS the
verification contract. This is a deliberate design choice, not an omission.

Idempotency key derivation: ``f"{snapshot_id}:{action_type}:{entity_id}"``.
Stable across retries of the same logical change (same snapshot, same
subject, same action type) so a crashed-and-restarted attempt reuses the
same ``ActionOperation`` (see persistence/action_operations.py) instead of
creating an independent one. A NEW snapshot (the ticket changed) or a
different subject correctly produces a different key — that is supposed to
be a different logical operation.
"""

from __future__ import annotations

import dataclasses
import uuid

from crm_ai_agent.domain.entities import ExecutionPlan, LLMInterpretation, PlanAction, ResolvedEntity, TicketSnapshot
from crm_ai_agent.domain.enums import KnownActionType, PlanStatus, PolicyDecisionType
from crm_ai_agent.safety.action_registry import get_action_spec
from crm_ai_agent.safety.canonical_hash import compute_plan_hash, compute_snapshot_hash
from crm_ai_agent.safety.policy_engine import PolicyEvaluation, PolicyInput, evaluate

_EDIT_NAME_FIELDS = ("first_name", "last_name")


class PlanCompilationError(Exception):
    pass


class CompiledPlan:
    __slots__ = ("evaluation", "plan")

    def __init__(self, evaluation: PolicyEvaluation, plan: ExecutionPlan | None) -> None:
        self.evaluation = evaluation
        self.plan = plan


def _build_draft_edit_name_action(
    *, snapshot_id: str, interpretation: LLMInterpretation, resolution: ResolvedEntity, current_state: dict[str, str]
) -> PlanAction:
    spec = get_action_spec(KnownActionType.EDIT_CRM_USER_NAME.value)

    changed_fields = {k: v for k, v in interpretation.requested_changes.items() if k in _EDIT_NAME_FIELDS}
    final_values = dict(current_state)
    final_values.update(changed_fields)

    arguments = {"user_id": resolution.entity_id, **{f: final_values.get(f, "") for f in _EDIT_NAME_FIELDS}}
    expected_before_state = {f: current_state[f] for f in changed_fields}
    expected_after_state = dict(changed_fields)

    idempotency_key = f"{snapshot_id}:{spec.action_type.value}:{resolution.entity_id}"

    return PlanAction(
        action_id=str(uuid.uuid4()),
        action_type=spec.action_type.value,
        arguments=arguments,
        preconditions=("user_exists",),
        expected_before_state=expected_before_state,
        expected_after_state=expected_after_state,
        risk_level=spec.risk_level,
        idempotency_key=idempotency_key,
    )


def compile_edit_user_name_plan(
    *,
    snapshot: TicketSnapshot,
    interpretation: LLMInterpretation,
    resolution: ResolvedEntity,
    current_state: dict[str, str],
    policy_version: str,
) -> CompiledPlan:
    """Compile an ``edit_crm_user_name`` plan for ``resolution``'s subject.

    Requires ``resolution.match_count == 1`` up front — the Plan Compiler
    does not depend on the Policy Engine alone to catch ambiguity, it
    refuses to even attempt compilation for a subject that isn't uniquely
    resolved. Returns a ``CompiledPlan`` whose ``.plan`` is ``None`` unless
    the Policy Engine's decision is ``ALLOW_FOR_APPROVAL`` — every other
    decision (``MANUAL_REVIEW``, ``DENY``, ``STALE``) means there is nothing
    safe to compile yet, only a reason to report back to the ticket.
    """

    if resolution.match_count != 1 or resolution.entity_id is None:
        raise PlanCompilationError(
            f"cannot compile a plan for an unresolved subject (match_count={resolution.match_count})"
        )

    draft_action = _build_draft_edit_name_action(
        snapshot_id=snapshot.snapshot_id,
        interpretation=interpretation,
        resolution=resolution,
        current_state=current_state,
    )

    policy_evaluation = evaluate(
        PolicyInput(
            snapshot=snapshot,
            interpretation=interpretation,
            resolution=resolution,
            draft_actions=(draft_action,),
            policy_version=policy_version,
        )
    )

    if policy_evaluation.decision != PolicyDecisionType.ALLOW_FOR_APPROVAL:
        return CompiledPlan(evaluation=policy_evaluation, plan=None)

    snapshot_hash = compute_snapshot_hash(snapshot)
    plan = ExecutionPlan(
        plan_id=str(uuid.uuid4()),
        ticket_id=snapshot.ticket_id,
        snapshot_id=snapshot.snapshot_id,
        snapshot_hash=snapshot_hash,
        plan_hash="",  # filled in below, after the rest of the plan is fixed
        policy_version=policy_version,
        actions=(draft_action,),
        status=PlanStatus.AWAITING_APPROVAL,
    )
    plan = dataclasses.replace(plan, plan_hash=compute_plan_hash(plan))

    return CompiledPlan(evaluation=policy_evaluation, plan=plan)
