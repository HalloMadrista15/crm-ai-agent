"""A free, deterministic, rule-based ``TicketInterpreter`` — no LLM, no API
key, no network call, no per-request cost.

Why this is a legitimate choice and not a corner cut: the MVP's entire scope
is one ticket type (``change_user_name``) with two possible target fields
(``first_name``, ``last_name``). Recognizing "please update my last name to
X" does not require a language model; a handful of regex patterns cover it,
and — critically — the Policy Engine (Stage 2) already treats
``LLMInterpretation`` as untrusted, low-confidence-tolerant input regardless
of what produced it. Nothing downstream assumes real NLU happened.

What this does NOT do, on purpose:
- It does not understand phrasing outside the patterns below. Unrecognized
  phrasing is reported honestly via ``missing_data``/``ambiguities`` and a
  low ``classification_confidence`` — which routes to MANUAL_REVIEW in the
  Policy Engine, the same safe fallback a genuinely uncertain LLM response
  would produce.
- It does not treat any risky-sounding phrase as a security decision by
  itself; ``risk_flags`` only ever nudges the Policy Engine toward
  MANUAL_REVIEW, exactly like a real LLM's risk_flags would.
- It only scans fragments whose ``trust_level`` is NOT
  ``CRM_WORKFLOW_APPROVAL``/``CRM_SYSTEM_FIELD`` for risk phrases, so a
  legitimate "Approved by HR" workflow field is never itself flagged as
  suspicious — only requester/operator/historical text is.

When this stops being enough: the moment ticket types multiply beyond a
handful of fixed patterns, or requests need genuine free-text understanding,
swap in a real LLM client behind ``ports.interpreter.TicketInterpreter`` —
nothing else in the codebase needs to change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from crm_ai_agent.domain.entities import EvidenceItem, LLMInterpretation, TicketSnapshot
from crm_ai_agent.domain.enums import KnownTicketType, TrustLevel

# Fragments at these trust levels are CRM-controlled, not free text a
# requester or operator typed — never scanned for risk phrases.
_TRUSTED_FRAGMENT_LEVELS = frozenset({TrustLevel.CRM_WORKFLOW_APPROVAL, TrustLevel.CRM_SYSTEM_FIELD})

_NEW_NAME_RE = re.compile(
    r"\b(?:rename|update|change)\b.{0,60}?\bto\s+([A-Z][A-Za-z'\-]{1,40})\b"
)

# Deliberately simple substring checks, not a general prompt-injection
# detector — a safety net for obvious cases, matching the same posture as
# adapters/crm_playwright/artifacts.py's sanitizer.
_RISK_PHRASES = (
    "pre-approved",
    "pre approved",
    "without checking",
    "without verification",
    "no need to verify",
    "no need to check",
    "bypass approval",
    "ignore previous instructions",
    "ignore the above",
    "act as",
    "apply it immediately",
    "apply this immediately",
)


def _untrusted_text(snapshot: TicketSnapshot) -> str:
    return " ".join(
        fragment.text for fragment in snapshot.fragments if fragment.trust_level not in _TRUSTED_FRAGMENT_LEVELS
    )


def _detect_risk_flags(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    return tuple(phrase for phrase in _RISK_PHRASES if phrase in lowered)


def _extract_new_name(text: str) -> str | None:
    match = _NEW_NAME_RE.search(text)
    return match.group(1) if match else None


@dataclass(frozen=True)
class RuleBasedTicketInterpreter:
    schema_version: str = "1.0"

    def interpret(self, snapshot: TicketSnapshot) -> LLMInterpretation:
        untrusted_text = _untrusted_text(snapshot)
        lowered = untrusted_text.lower()

        risk_flags = _detect_risk_flags(untrusted_text)
        new_name = _extract_new_name(untrusted_text)

        requested_changes: dict[str, str] = {}
        ambiguities: list[str] = []
        missing_data: list[str] = []

        if new_name is None:
            missing_data.append("could not extract a new name value from ticket text")
            confidence = 0.3
        elif "last name" in lowered:
            requested_changes["last_name"] = new_name
            confidence = 0.85
        elif "first name" in lowered:
            requested_changes["first_name"] = new_name
            confidence = 0.85
        else:
            ambiguities.append(
                f"found candidate new name {new_name!r} but could not determine whether "
                "it is a first name or a last name"
            )
            confidence = 0.6

        subject_reference = snapshot.trusted_fields.get("requester_email") or f"ticket:{snapshot.ticket_id}"

        evidence = tuple(
            EvidenceItem(
                section=fragment.section,
                source_reference=fragment.source_reference,
                trust_level=fragment.trust_level,
                text=fragment.text,
            )
            for fragment in snapshot.fragments
        )

        return LLMInterpretation(
            schema_version=self.schema_version,
            snapshot_id=snapshot.snapshot_id,
            ticket_type=KnownTicketType.CHANGE_USER_NAME.value,
            classification_confidence=confidence,
            requested_changes=requested_changes,
            subject_reference=subject_reference,
            evidence=evidence,
            ambiguities=tuple(ambiguities),
            missing_data=tuple(missing_data),
            risk_flags=risk_flags,
        )
