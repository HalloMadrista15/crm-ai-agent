"""Canonical hashing for snapshots and plans.

``docs/schema_foundation.md`` flagged this as the fixed target for Stage 2:
``crm_ai_agent.domain.immutability.canonicalize`` already produces a
deterministic, key-order-independent projection of nested dict/list/tuple
data. This module turns that projection into a stable hex digest, and
defines the exact payload shape hashed for a ``TicketSnapshot`` and an
``ExecutionPlan`` — the values that become ``snapshot_hash``/``plan_hash``
and are re-checked at approval-consumption and pre-execution revalidation
time (Stage 6/7) to detect a ticket or plan that changed underneath an
in-flight approval.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from crm_ai_agent.domain.entities import ExecutionPlan, PlanAction, TextFragment, TicketSnapshot
from crm_ai_agent.domain.immutability import canonicalize


def _json_safe(value: Any) -> Any:
    """Convert values canonicalize() doesn't already handle (datetimes) to
    a JSON-serializable, order-stable form. Applied before canonicalize().
    """

    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def compute_hash(value: Any) -> str:
    """Hash an arbitrary JSON-safe, canonicalizable structure.

    Two structurally-equal inputs with different key insertion order or a
    mix of dict/MappingProxyType always hash identically — that is the
    entire point of routing through ``canonicalize`` first.
    """

    canonical = canonicalize(_json_safe(value))
    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _fragment_payload(fragment: TextFragment) -> dict[str, Any]:
    return {
        "section": fragment.section,
        "source_reference": fragment.source_reference,
        "captured_at": fragment.captured_at,
        "trust_level": fragment.trust_level.value,
        "text": fragment.text,
    }


def snapshot_hash_payload(snapshot: TicketSnapshot) -> dict[str, Any]:
    """The exact fields that participate in ``snapshot_hash``.

    Deliberately excludes ``source_hash`` itself (that is an input from the
    CRM adapter describing what was read, not part of what we hash here) and
    ``snapshot_id`` (an identifier, not content — two snapshots with
    identical content should hash identically regardless of their assigned
    ID, which is what lets a caller detect "nothing actually changed").
    """

    return {
        "schema_version": snapshot.schema_version,
        "ticket_id": snapshot.ticket_id,
        "status": snapshot.status,
        "title": snapshot.title,
        "trusted_fields": dict(snapshot.trusted_fields),
        "fragments": [_fragment_payload(f) for f in snapshot.fragments],
    }


def compute_snapshot_hash(snapshot: TicketSnapshot) -> str:
    return compute_hash(snapshot_hash_payload(snapshot))


def _plan_action_payload(action: PlanAction) -> dict[str, Any]:
    return {
        "action_id": action.action_id,
        "action_type": action.action_type,
        "arguments": dict(action.arguments),
        "preconditions": list(action.preconditions),
        "expected_before_state": dict(action.expected_before_state),
        "expected_after_state": dict(action.expected_after_state),
        "risk_level": action.risk_level.value,
        "idempotency_key": action.idempotency_key,
    }


def plan_hash_payload(plan: ExecutionPlan) -> dict[str, Any]:
    """The exact fields that participate in ``plan_hash``.

    Includes ``snapshot_hash`` so that a plan's hash changes if the snapshot
    it was compiled from changes, even if no field of the plan itself was
    touched — a plan is only meaningful paired with the snapshot it was
    reasoned about.
    """

    return {
        "ticket_id": plan.ticket_id,
        "snapshot_hash": plan.snapshot_hash,
        "policy_version": plan.policy_version,
        "actions": [_plan_action_payload(a) for a in plan.actions],
    }


def compute_plan_hash(plan: ExecutionPlan) -> str:
    return compute_hash(plan_hash_payload(plan))
