"""The Policy Engine: the single place that decides whether a draft plan may
proceed to Telegram approval.

Per docs/architecture.md section 9 (embedded in the original spec), this
accepts a ``TicketSnapshot``, an ``LLMInterpretation``, a ``ResolvedEntity``,
and a draft list of ``PlanAction``s, and returns exactly one of four
decisions: ``ALLOW_FOR_APPROVAL``, ``MANUAL_REVIEW``, ``DENY``, ``STALE``.

What this module deliberately does NOT do:

- It does not talk to a CRM, Telegram, or an LLM.
- It does not decide who is authorized to approve — that is Stage 6.
- Its rule set here is the Stage 1/2 MVP subset (snapshot/plan consistency,
  entity-resolution ambiguity, action-registry conformance, ticket-type
  restriction, and an approval-evidence trust check). The full hard-deny
  list in the master spec (conflicting data, service-account rights,
  manually-edited target state, etc.) is intentionally not all implemented
  yet; each additional rule can be added as its own function in
  ``_RULES`` without touching the evaluation loop.

Evidence and trust
-------------------
"Approval was found" is only ever accepted from
``EvidenceItem.trust_level == TrustLevel.CRM_WORKFLOW_APPROVAL``.  A
requester's own claim of being approved, or an operator's free-text comment
saying so, is explicitly NOT sufficient — that is the fix for the
architecture review's point 1 ("'Согласование найдено' нельзя считать
решением LLM"). See docs/open_questions.md #3: until the CRM's real
approval-evidence field is confirmed, any interpretation lacking
``CRM_WORKFLOW_APPROVAL`` evidence for a ``required_approval`` action routes
to MANUAL_REVIEW, never DENY — a legitimate approval that hasn't happened
yet is not the same as an invalid request.
"""

from __future__ import annotations

from dataclasses import dataclass

from crm_ai_agent.domain.entities import LLMInterpretation, PlanAction, ResolvedEntity, TicketSnapshot
from crm_ai_agent.domain.enums import PolicyDecisionType, TrustLevel
from crm_ai_agent.safety.action_registry import ActionRegistryError, is_ticket_type_allowed, validate_against_registry


@dataclass(frozen=True)
class PolicyInput:
    snapshot: TicketSnapshot
    interpretation: LLMInterpretation
    resolution: ResolvedEntity
    draft_actions: tuple[PlanAction, ...]
    policy_version: str


@dataclass(frozen=True)
class PolicyEvaluation:
    decision: PolicyDecisionType
    reasons: tuple[str, ...]
    policy_version: str


def _has_workflow_approval_evidence(interpretation: LLMInterpretation) -> bool:
    return any(item.trust_level == TrustLevel.CRM_WORKFLOW_APPROVAL for item in interpretation.evidence)


def evaluate(policy_input: PolicyInput) -> PolicyEvaluation:
    reasons: list[str] = []
    snapshot = policy_input.snapshot
    interpretation = policy_input.interpretation
    resolution = policy_input.resolution
    policy_version = policy_input.policy_version

    # 1. Snapshot/interpretation consistency: the interpretation must be
    #    about the exact snapshot we currently hold, not a stale one.
    if interpretation.snapshot_id != snapshot.snapshot_id:
        return PolicyEvaluation(
            decision=PolicyDecisionType.STALE,
            reasons=(
                f"interpretation.snapshot_id {interpretation.snapshot_id!r} does not match "
                f"current snapshot_id {snapshot.snapshot_id!r}",
            ),
            policy_version=policy_version,
        )

    # 2. Entity resolution must be unambiguous.
    if resolution.match_count == 0:
        return PolicyEvaluation(
            decision=PolicyDecisionType.DENY,
            reasons=(f"no matching subject found for query {resolution.query!r}",),
            policy_version=policy_version,
        )
    if resolution.match_count > 1:
        return PolicyEvaluation(
            decision=PolicyDecisionType.DENY,
            reasons=(
                f"{resolution.match_count} matching subjects found for query {resolution.query!r}; ambiguous",
            ),
            policy_version=policy_version,
        )

    # 3. Every draft action must conform to the Action Registry.
    for action in policy_input.draft_actions:
        try:
            spec = validate_against_registry(action)
        except ActionRegistryError as exc:
            return PolicyEvaluation(
                decision=PolicyDecisionType.DENY,
                reasons=(str(exc),),
                policy_version=policy_version,
            )

        if not is_ticket_type_allowed(spec, interpretation.ticket_type):
            return PolicyEvaluation(
                decision=PolicyDecisionType.DENY,
                reasons=(
                    f"action {action.action_type} is not allowed for ticket_type {interpretation.ticket_type!r}",
                ),
                policy_version=policy_version,
            )

        if spec.required_approval and not _has_workflow_approval_evidence(interpretation):
            reasons.append(
                f"action {action.action_type} requires approval evidence with "
                f"trust_level={TrustLevel.CRM_WORKFLOW_APPROVAL.value}, none found"
            )

    if reasons:
        return PolicyEvaluation(
            decision=PolicyDecisionType.MANUAL_REVIEW,
            reasons=tuple(reasons),
            policy_version=policy_version,
        )

    # 4. Risk flags and unresolved ambiguity/missing data always go to a
    #    human — never silently allowed through regardless of confidence.
    if interpretation.risk_flags:
        return PolicyEvaluation(
            decision=PolicyDecisionType.MANUAL_REVIEW,
            reasons=tuple(f"risk flag: {flag}" for flag in interpretation.risk_flags),
            policy_version=policy_version,
        )
    if interpretation.ambiguities:
        return PolicyEvaluation(
            decision=PolicyDecisionType.MANUAL_REVIEW,
            reasons=tuple(f"ambiguity: {item}" for item in interpretation.ambiguities),
            policy_version=policy_version,
        )
    if interpretation.missing_data:
        return PolicyEvaluation(
            decision=PolicyDecisionType.MANUAL_REVIEW,
            reasons=tuple(f"missing data: {item}" for item in interpretation.missing_data),
            policy_version=policy_version,
        )

    return PolicyEvaluation(
        decision=PolicyDecisionType.ALLOW_FOR_APPROVAL,
        reasons=("all checks passed",),
        policy_version=policy_version,
    )


__all__ = ["PolicyInput", "PolicyEvaluation", "evaluate"]
