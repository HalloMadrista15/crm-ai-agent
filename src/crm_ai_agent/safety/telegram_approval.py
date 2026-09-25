"""Domain contract for consuming a Telegram approval callback.

This module contains NO network code, no Telegram Bot API client, and no
webhook server — that is all Stage 6 work, not done here. What exists here
is the strict, typed decision logic that the real Stage 6 adapter MUST run
every incoming callback through before it is allowed to flip an
``Approval`` from ``PENDING`` to ``APPROVED``/``REJECTED``. Writing this now,
ahead of the adapter, is what lets Stage 6 be "plug a real webhook handler
in front of an already-tested set of checks" instead of "invent the checks
under time pressure once a real bot is live."

Threat model recap (see docs/threat_model.md) — each check below closes one
specific attack:

- ``InvalidWebhookError``: the request didn't come from Telegram at all
  (forged webhook call).
- ``TelegramIdentityMismatchError``: the identity the caller resolved (e.g.
  by looking up an allow-listed Telegram user ID) doesn't match the identity
  actually embedded in the callback — a defensive check against the caller
  wiring the wrong records together.
- ``ApprovalNotPendingError``: replay of an already-consumed callback, or a
  double-tap on the same button.
- ``ApprovalExpiredError``: a stale approval request being actioned long
  after it should have lapsed.
- ``ApproverNotAuthorizedError``: an allow-listed Telegram user who does not
  hold a role this specific approval requires (e.g. anyone can message the
  bot, but not everyone may approve a name change).
- ``ApprovalHashMismatchError``: the ticket or plan changed after the
  approval was issued — approving it now would approve a DIFFERENT plan
  than the one a human actually looked at. Checked on approval only: a
  human is always allowed to reject a now-stale plan without a fresh
  revalidation, but approving one requires proof nothing moved underneath.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime

from crm_ai_agent.domain.entities import Approval
from crm_ai_agent.domain.enums import ApprovalStatus


class ApprovalConsumptionError(Exception):
    pass


class InvalidWebhookError(ApprovalConsumptionError):
    pass


class TelegramIdentityMismatchError(ApprovalConsumptionError):
    pass


class ApprovalNotPendingError(ApprovalConsumptionError):
    pass


class ApprovalExpiredError(ApprovalConsumptionError):
    pass


class ApproverNotAuthorizedError(ApprovalConsumptionError):
    pass


class ApprovalHashMismatchError(ApprovalConsumptionError):
    pass


@dataclass(frozen=True)
class TelegramCallback:
    """What a real webhook handler (Stage 6) must extract and verify BEFORE
    constructing this: ``webhook_secret_valid`` is not computed here — it is
    the caller's attestation that it already checked Telegram's secret
    token header (or equivalent) on the raw HTTP request. This type exists
    so that attestation is a typed, required field instead of an assumption
    buried in the adapter.
    """

    approval_id: str
    telegram_user_id: str
    telegram_chat_id: str
    webhook_secret_valid: bool


@dataclass(frozen=True)
class ApproverIdentity:
    """The result of resolving a Telegram user ID to CRM/policy roles.

    Constructing one of these with fabricated roles is exactly as
    meaningless as constructing a spoofed ``Principal`` in
    ``state_machines.py`` — see that module's security-boundary note. The
    real lookup (an allow-list, or a CRM role table) is Stage 6 work.
    """

    telegram_user_id: str
    roles: frozenset[str]


def consume_approval(
    *,
    approval: Approval,
    callback: TelegramCallback,
    approver: ApproverIdentity,
    decision: ApprovalStatus,
    current_plan_hash: str,
    current_snapshot_hash: str,
    now: datetime,
    decision_reason: str = "",
) -> Approval:
    """Validate a callback against ``approval`` and return the updated
    ``Approval`` to persist (via ``persistence.approvals.update_decision``).

    Does not mutate ``approval`` in place and does not touch the database —
    callers own persistence and the transaction boundary, consistent with
    every other function in this project.
    """

    if not callback.webhook_secret_valid:
        raise InvalidWebhookError("callback failed webhook secret verification")

    if callback.approval_id != approval.approval_id:
        raise TelegramIdentityMismatchError(
            f"callback.approval_id {callback.approval_id!r} does not match approval "
            f"{approval.approval_id!r}"
        )

    if callback.telegram_user_id != approver.telegram_user_id:
        raise TelegramIdentityMismatchError(
            f"callback telegram_user_id {callback.telegram_user_id!r} does not match "
            f"resolved approver identity {approver.telegram_user_id!r}"
        )

    if approval.status != ApprovalStatus.PENDING:
        raise ApprovalNotPendingError(
            f"approval {approval.approval_id!r} is already {approval.status.value}; "
            "a callback cannot consume it again"
        )

    if approval.expires_at is not None and now > approval.expires_at:
        raise ApprovalExpiredError(
            f"approval {approval.approval_id!r} expired at {approval.expires_at.isoformat()}"
        )

    if not (approver.roles & set(approval.allowed_approver_roles)):
        raise ApproverNotAuthorizedError(
            f"approver {approver.telegram_user_id!r} with roles {sorted(approver.roles)} "
            f"does not hold any of the roles required by this approval: "
            f"{list(approval.allowed_approver_roles)}"
        )

    if decision not in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
        raise ValueError(f"decision must be APPROVED or REJECTED, got {decision!r}")

    if decision == ApprovalStatus.APPROVED:
        if current_plan_hash != approval.plan_hash or current_snapshot_hash != approval.snapshot_hash:
            raise ApprovalHashMismatchError(
                f"approval {approval.approval_id!r} was issued for plan_hash={approval.plan_hash!r}/"
                f"snapshot_hash={approval.snapshot_hash!r}, but the current values are "
                f"plan_hash={current_plan_hash!r}/snapshot_hash={current_snapshot_hash!r}; "
                "the ticket or plan changed since this approval was issued"
            )

    return dataclasses.replace(
        approval,
        status=decision,
        telegram_user_id=approver.telegram_user_id,
        telegram_chat_id=callback.telegram_chat_id,
        consumed_at=now,
        decision_reason=decision_reason,
    )
