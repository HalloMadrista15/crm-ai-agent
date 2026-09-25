"""The entity-resolution port: a read-only lookup of the CRM subject an
interpretation refers to, plus reading that subject's current field values.

Like ``ports/crm.py`` and ``ports/interpreter.py``, this is a ``Protocol`` so
the Plan Compiler (``safety/plan_compiler.py``) does not care whether a real
CRM or a JSON fixture answers it. No implementation here talks to a real
CRM — ``adapters/crm_fixture/entity_resolver.py`` is fixture-backed, and a
real Playwright-based resolver is Stage 3+ work blocked on the same lack of
CRM test access as ``adapters/crm_playwright/reader.py``.

Two methods, not one, because the Plan Compiler needs more than "does this
resolve uniquely" (``ResolvedEntity``, Stage 2's ambiguity contract) — it
also needs the subject's CURRENT field values to build a plan action's
``expected_before_state`` and to fill in fields the interpretation didn't
ask to change (an edit-name action always needs both first and last name,
even if only one is being changed).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from crm_ai_agent.domain.entities import ResolvedEntity


class SubjectNotFoundError(Exception):
    pass


@runtime_checkable
class EntityResolver(Protocol):
    def resolve(self, query: str) -> ResolvedEntity:
        """Read-only search. Must never raise for zero or multiple matches —
        that is exactly what ``ResolvedEntity.match_count`` communicates to
        the Policy Engine. Raise only on a genuine lookup failure (e.g. the
        backing store itself is unreachable)."""
        ...

    def get_current_state(self, entity_id: str) -> dict[str, str]:
        """Return the subject's current field values (e.g. ``first_name``,
        ``last_name``). Raises ``SubjectNotFoundError`` if ``entity_id``
        does not exist — this is only ever called with an ``entity_id`` a
        prior ``resolve()`` call already confirmed exists, so its absence
        here means the record changed or vanished between the two reads."""
        ...
