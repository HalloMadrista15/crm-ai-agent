"""The real Playwright-backed ``CrmTicketReader`` — NOT implemented.

Every constructor call raises ``PlaywrightAdapterNotImplementedError``. This
is intentional and must not be "fixed" by adding a fallback or a mock login:
writing the real login flow, page-reading logic, and selectors requires
looking at an actual CRM instance, which this project does not have access
to yet (docs/open_questions.md #1 — CRM product/version and test
credentials are still open questions). Guessing at selectors now would
produce code that looks finished but silently breaks against the real UI,
which is worse than an adapter that fails loudly at construction time.

When real access exists, this class should be implemented to satisfy
``ports.crm.CrmTicketReader`` exactly as ``adapters/crm_fixture/reader.py``
does, reusing ``adapters.crm_fixture.reader.parse_ticket_export``'s target
shape: scrape the ticket page into the same dict shape that function
consumes (or refactor to share a common ``TicketSnapshot``-building
function), then let the existing ``TextFragment``/``trust_level`` handling
work unchanged. Screenshots/traces collected during a real read should be
routed through ``adapters/crm_playwright/artifacts.py``'s
``sanitize_text_for_artifact`` before being persisted, per
docs/threat_model.md's PII-in-artifacts risk.
"""

from __future__ import annotations


class PlaywrightAdapterNotImplementedError(NotImplementedError):
    pass


class PlaywrightCrmTicketReader:
    def __init__(self, *, base_url: str, storage_state_path: str | None = None) -> None:
        raise PlaywrightAdapterNotImplementedError(
            "PlaywrightCrmTicketReader is not implemented: no test CRM access exists yet "
            "(see docs/open_questions.md #1). Do not fabricate selectors or a login flow — "
            "implement this only against a real, observed CRM instance."
        )

    def read_ticket(self, external_ticket_id: str):  # pragma: no cover - unreachable
        raise PlaywrightAdapterNotImplementedError(
            "PlaywrightCrmTicketReader is not implemented"
        )
