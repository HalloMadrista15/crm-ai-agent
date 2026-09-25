"""Real Playwright-backed CRM ticket reader for the "Доступ к ИС
(Информационные системы)" service note (Creatio schema
``TsiOfficeNotesISAccessPage``) — the only ticket type this project has
confirmed, real field selectors for.

Unlike ``adapters/crm_playwright/reader.py``'s unconditional
``NotImplementedError`` stub (written when no CRM test access existed at
all), real production access does exist now, and this reader was built
against it through explicit, per-step read-only recon — see
``scripts/crm_probe_field_extraction.py`` for how the field-id pattern
below was confirmed on a real ticket, and
``docs/open_questions.md``/``docs/action_catalog.md`` for the wider
context.

Confirmed field-id pattern: Creatio renders each bound field's actual
input/textarea with an ``id`` starting with
``TsiOfficeNotesISAccessPage<SchemaColumnName>`` (e.g.
``TsiOfficeNotesISAccessPageTsiRMOrgEmployeeFullNameTextEdit-el`` for a
plain text field, ``...ComboBoxEdit-el`` for a combo, ``...MemoEdit-el``
for a textarea — the suffix varies by control type, the prefix does not).
The ``data-item-marker`` attributes in ``selectors.py`` mark the *label*,
not the value control, which is why reading ``.inner_text()`` on the
marker itself only ever returns the caption — this module locates the
value control by id-prefix instead.

Strictly read-only: only ``.input_value()`` calls here, and the only
navigation is opening the ticket URL itself — no click, no fill, no
submit. There is no write method on this class and there must never be
one added — the same rule stated in every other real adapter in this
project (``webitel_playwright/entity_resolver.py``,
``webitel_api/entity_resolver.py``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from crm_ai_agent.domain.entities import TextFragment, TicketSnapshot
from crm_ai_agent.domain.enums import TrustLevel
from crm_ai_agent.ports.crm import TicketReadError

SCHEMA_NAME = "TsiOfficeNotesISAccessPage"
SNAPSHOT_SCHEMA_VERSION = "1.0"


def _read_field_value(page, column_name: str) -> str:
    """Read one field's current value by its confirmed id-prefix pattern.
    Excludes ``-virtual`` suffixed elements (Creatio renders a hidden
    "-virtual" twin alongside the real "-el" control for some fields, e.g.
    BnzComment — both were observed empty/in-sync in recon, but only the
    real one should be trusted). Returns "" if no matching control exists,
    rather than raising — callers decide what an empty required field
    means (see ``read_ticket``'s check on ``status``).
    """

    prefix = f"{SCHEMA_NAME}{column_name}"
    candidates = page.locator(
        f'input[id^="{prefix}"]:not([id$="-virtual"]), textarea[id^="{prefix}"]:not([id$="-virtual"])'
    )
    if candidates.count() == 0:
        return ""
    return candidates.first.input_value().strip()


class CrmPlaywrightTicketReader:
    """Reads ONE confirmed ticket type. ``read_ticket``'s
    ``external_ticket_id`` argument is the full ticket URL, not a bare ID —
    Creatio's ticket search/list was never explored, only direct
    deep-links to a known ticket. This is a documented deviation from
    ``ports.crm.CrmTicketReader``'s literal parameter name, acceptable
    while the only caller (the local console, not built yet) already knows
    this and passes a URL.
    """

    def __init__(self, *, storage_state_path: Path) -> None:
        if not storage_state_path.exists():
            raise FileNotFoundError(
                f"No saved CRM session at {storage_state_path}. "
                "Run scripts/crm_login_and_save_session.py first."
            )
        self._storage_state_path = storage_state_path

    def read_ticket(self, external_ticket_id: str) -> TicketSnapshot:
        ticket_url = external_ticket_id
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(self._storage_state_path))
            page = context.new_page()
            try:
                page.goto(ticket_url, wait_until="domcontentloaded")
                page.wait_for_load_state("load", timeout=20000)
                try:
                    # Creatio appears to keep a background poll/keepalive
                    # request going, which can starve this of ever firing —
                    # best-effort only, never fatal to the read.
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(4000)

                status = _read_field_value(page, "TsiOfficeNoteState")
                if not status:
                    raise TicketReadError(
                        f"could not read status field for {ticket_url} — "
                        "page may not have loaded, or this is not a TsiOfficeNotesISAccessPage ticket"
                    )

                employee_full_name = _read_field_value(page, "TsiRMOrgEmployeeFullName")
                employee_account = _read_field_value(page, "BnzEmployeeAccount")
                information_system = _read_field_value(page, "BnzInformationSystem")
                comment = _read_field_value(page, "BnzComment")
            finally:
                browser.close()

        now = datetime.now(timezone.utc)
        fragments: list[TextFragment] = []
        if comment:
            fragments.append(
                TextFragment(
                    section="comment",
                    source_reference=f"{ticket_url}#BnzComment",
                    captured_at=now,
                    trust_level=TrustLevel.REQUESTER_TEXT,
                    text=comment,
                )
            )

        return TicketSnapshot(
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            snapshot_id=f"crm-live-{now.timestamp():.0f}",
            ticket_id=ticket_url,
            captured_at=now,
            source_hash="",
            status=status,
            title=SCHEMA_NAME,
            fragments=tuple(fragments),
            trusted_fields={
                "employee_full_name": employee_full_name,
                "employee_account": employee_account,
                "information_system": information_system,
            },
        )
