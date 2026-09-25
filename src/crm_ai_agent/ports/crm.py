"""The CRM read port: the only shape application code may depend on to read
a ticket, independent of what actually implements it.

This is deliberately a read-only ``Protocol`` — no ``write``/``update``
method exists here at all. Mutation is out of scope for the port entirely;
it belongs to the Action Registry (``safety/action_registry.py``) and its
executors (Stage 7, not built), which take a validated ``PlanAction`` rather
than an open-ended CRM write call. Keeping reads and writes on structurally
different interfaces means an orchestration bug can't accidentally call a
write method that was never meant to be reachable from the read path.

Two things can satisfy this port today:

- ``adapters/crm_fixture/reader.py`` — reads static JSON fixtures, used by
  tests and local development. Never talks to a network.
- ``adapters/crm_playwright/reader.py`` — the real adapter. It raises
  ``NotImplementedError`` unconditionally right now (see that module): there
  is no test CRM access yet (docs/open_questions.md #1), so writing real
  selectors and a login flow now would mean guessing at a UI nobody on this
  project has seen, which is exactly the "не придумывай реальные CRM
  selectors" constraint this project has operated under from Stage 1.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from crm_ai_agent.domain.entities import TicketSnapshot


@runtime_checkable
class CrmTicketReader(Protocol):
    def read_ticket(self, external_ticket_id: str) -> TicketSnapshot:
        """Return an immutable snapshot of the current state of one ticket.

        Implementations must raise (never return ``None`` or a
        partially-populated snapshot) if the ticket cannot be found or fully
        read — a caller proceeding on partial data is exactly the failure
        mode ``TicketSnapshot``'s trust-level-tagged fragments exist to
        prevent one layer up.
        """
        ...


class TicketNotFoundError(Exception):
    pass


class TicketReadError(Exception):
    """Raised when a ticket exists but could not be fully/safely read
    (e.g. an unexpected page layout, a partially loaded panel). Distinct
    from ``TicketNotFoundError`` so callers can route "doesn't exist" and
    "couldn't read it" to different handling (the latter should probably
    retry or go to manual review; the former should not).
    """
