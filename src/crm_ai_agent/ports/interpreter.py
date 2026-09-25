"""The ticket-interpretation port: the shape a real LLM client and a
free rule-based stand-in both satisfy.

Downstream code (the Policy Engine, and eventually the Plan Compiler) only
ever depends on getting an ``LLMInterpretation`` from a ``TicketSnapshot`` —
it does not know or care whether that came from calling an LLM API or from
regex rules. This is what lets the project run entirely for free right now
(``adapters/rule_based_interpreter/``) and swap in a real LLM client later
(Stage 4's original scope) as a drop-in replacement, with zero changes to
the Policy Engine or anything built on top of it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from crm_ai_agent.domain.entities import LLMInterpretation, TicketSnapshot


@runtime_checkable
class TicketInterpreter(Protocol):
    def interpret(self, snapshot: TicketSnapshot) -> LLMInterpretation:
        """Produce a structured interpretation of ``snapshot``.

        Implementations must never raise on merely low-confidence or
        ambiguous input — that is exactly what ``ambiguities``,
        ``missing_data``, and a low ``classification_confidence`` are for.
        Raise only if the snapshot itself is malformed.
        """
        ...
