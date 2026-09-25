"""Selector inventory.

Per the project's standing constraint (never invent real CRM selectors),
every entry below was observed in a real, rendered page of the production
Astana Motors Creatio instance (read-only recon, no data changed) — never
guessed. See ``scripts/crm_dump_ticket.py`` for how a page dump was taken.

Creatio renders a `data-item-marker="<SchemaColumnName> <Caption>"`
attribute on most bound form fields. It behaves like a `data-testid`: stable
across sessions/renders, tied to the underlying schema column rather than
visual layout, and not something a redesign would casually change. This
puts it at the top of the locator priority order (``data-testid`` >
role/label > other stable attribute > text) for this CRM specifically.

Entries observed so far come from ONE ticket type: the "Доступ к ИС
(Информационные системы)" service note (Creatio schema
``TsiOfficeNotesISAccessPage``). Fields for other ticket/note types have not
been observed and must not be assumed to share these names.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SelectorEntry:
    name: str
    strategy: str  # "data-testid" | "role" | "attribute" | "text"
    value: str
    notes: str = ""


# Source schema: TsiOfficeNotesISAccessPage ("Доступ к ИС (Информационные
# системы)" service note). Observed 2026-09-03 via read-only page dump.
SELECTOR_INVENTORY: dict[str, SelectorEntry] = {
    "is_access_note.status": SelectorEntry(
        name="is_access_note.status",
        strategy="attribute",
        value='[data-item-marker="TsiOfficeNoteState Статус записки"]',
        notes="Ticket-level status field (e.g. 'На согласовании').",
    ),
    "is_access_note.employee_full_name": SelectorEntry(
        name="is_access_note.employee_full_name",
        strategy="attribute",
        value='[data-item-marker="TsiRMOrgEmployeeFullName ФИО сотрудника"]',
    ),
    "is_access_note.employee_account": SelectorEntry(
        name="is_access_note.employee_account",
        strategy="attribute",
        value='[data-item-marker="BnzEmployeeAccount Учетная запись сотрудника"]',
        notes="The CRM/Webitel login/username this request concerns.",
    ),
    "is_access_note.information_system": SelectorEntry(
        name="is_access_note.information_system",
        strategy="attribute",
        value='[data-item-marker="BnzInformationSystem Информационная система"]',
        notes="Target system for the request, e.g. 'Webitel с привязкой к CRM'.",
    ),
    "is_access_note.comment": SelectorEntry(
        name="is_access_note.comment",
        strategy="attribute",
        value='[data-item-marker="BnzComment Комментарии"]',
        notes="Free text — untrusted input, same as TextFragment.trust_level=REQUESTER_TEXT/OPERATOR_COMMENT.",
    ),
    "approval_tab.container": SelectorEntry(
        name="approval_tab.container",
        strategy="attribute",
        value='[data-item-marker="VisaDetailV2Container"]',
        notes=(
            "This resolves open_questions.md #3: 'Согласование' is Creatio's built-in "
            "VisaDetailV2 approval component, not a custom field. Confirmed by observing "
            "grid row DOM ids of the form 'VisaDetailV2DataGridGrid-<gridId>-item-<recordGuid>' "
            "on a real ticket. IMPORTANT: individual grid cells inside this container are NOT "
            "semantically marked — they render as positional '.grid-cols-N' divs (e.g. status "
            "text sits in a bare '<span grid-data-type=\"text\">' with no stable column "
            "identifier). Scraping those positions is fragile and NOT recommended. Because this "
            "is a standard OOTB Creatio entity, the far more robust approach for Stage 3's real "
            "implementation is reading visa records through Creatio's OData API (e.g. an "
            "'VisaDetailV2' or similarly-named entity set) rather than screen-scraping this grid. "
            "That API has not been explored yet — this entry only documents that the DOM "
            "confirms a structured approval object exists at all, which was previously unknown."
        ),
    ),
    "is_access_note.value_control_id_prefix": SelectorEntry(
        name="is_access_note.value_control_id_prefix",
        strategy="attribute",
        value='[id^="TsiOfficeNotesISAccessPage<ColumnName>"]',
        notes=(
            "Confirmed 2026-09-11 via scripts/crm_probe_field_extraction.py: the data-item-marker "
            "attributes above mark each field's LABEL, not its value control — reading .inner_text() "
            "on the marker only ever returns the caption. The actual input/textarea holding the "
            "value has an id starting with 'TsiOfficeNotesISAccessPage<SchemaColumnName>' (suffix "
            "varies by control type: TextEdit-el, LookupEdit-el, ComboBoxEdit-el, MemoEdit-el). "
            "A '-virtual' suffixed twin sometimes also exists (e.g. for BnzComment) and must be "
            "excluded. See adapters/crm_playwright/ticket_reader.py, the first real (non-stub) "
            "CRM reader in this project, built on this pattern."
        ),
    ),
}

# NOT yet observed / not yet in this inventory (do not fabricate):
# - Per-row/per-cell selectors inside approval_tab.container — see its notes
#   above for why position-based grid scraping should be avoided in favor of
#   the OData API once that is explored.
# - Any field/selector belonging to a different service-note type than
#   TsiOfficeNotesISAccessPage.
