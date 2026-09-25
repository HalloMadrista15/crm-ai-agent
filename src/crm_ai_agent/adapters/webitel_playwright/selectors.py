"""Selector inventory for Webitel (Almaty instance, wtl-alm.astana-motors.kz).

Same constraint as adapters/crm_playwright/selectors.py: every entry below
was observed in a real, rendered page during read-only recon (view-only —
no user, device, or setting was created/edited/deleted), never guessed. See
scripts/webitel_dump_page.py and scripts/webitel_dump_user.py for how the
page dumps were taken.

Webitel's admin UI is a Vue.js SPA built on a component library ("wt-...").
Its buttons and inputs mark their semantic role via plain HTML attributes —
`icon="edit"`, `icon="generate"` on `<button>`, `placeholder="..."` on
`<input>` — rather than a dedicated `data-testid`-style attribute. These
attributes are still tied to the component's role rather than to visual
styling, so they sit at the same locator-priority tier as a `data-testid`
for this app specifically.

Nav-tile actionability quirk: the top-level app tiles (Admin, Directory,
etc.) never satisfy Playwright's default "visible and stable" click
precondition in headless mode, even though they render fine (probably a
continuous CSS animation on those cards). Real navigation clicks in this
adapter must dispatch a native DOM click (`locator.evaluate("el =>
el.click()")`) rather than `locator.click()` — see the recon scripts for
the working pattern.

Confirmed real product: this is Webitel's official open-source Contact
Center admin panel (page title "Admin | Webitel"), not a custom system.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SelectorEntry:
    name: str
    strategy: str  # "attribute" | "placeholder" | "text"
    value: str
    notes: str = ""


# Observed 2026-09-07 via read-only recon of Admin > Directory > Users on
# the Almaty instance (wtl-alm.astana-motors.kz), including one user's own
# "General info" edit tab (view only, nothing saved).
SELECTOR_INVENTORY: dict[str, SelectorEntry] = {
    "users_list.search_input": SelectorEntry(
        name="users_list.search_input",
        strategy="placeholder",
        value='input[placeholder="Search"]',
        notes="Filters the Directory > Users table client-side; used to find one user by login/extension.",
    ),
    "users_list.row_edit_button": SelectorEntry(
        name="users_list.row_edit_button",
        strategy="attribute",
        value='button[icon="edit"]',
        notes=(
            "One per table row. Not unique on an unfiltered list — always search/filter to a single "
            "row first (see users_list.search_input), then take .first."
        ),
    ),
    "users_list.row_delete_button": SelectorEntry(
        name="users_list.row_delete_button",
        strategy="attribute",
        value='button[icon="delete"]',
        notes="NOT exercised during recon — inferred from the same button-per-row pattern as row_edit_button. Verify before ever using.",
    ),
    "user_edit.name_input": SelectorEntry(
        name="user_edit.name_input",
        strategy="placeholder",
        value='input[placeholder="Name"]',
    ),
    "user_edit.login_input": SelectorEntry(
        name="user_edit.login_input",
        strategy="placeholder",
        value='input[placeholder="Login"]',
    ),
    "user_edit.extensions_input": SelectorEntry(
        name="user_edit.extensions_input",
        strategy="placeholder",
        value='input[placeholder="Extensions"]',
        notes="The internal number (e.g. the '14006' referenced in a real ticket comment).",
    ),
    "user_edit.password_input": SelectorEntry(
        name="user_edit.password_input",
        strategy="attribute",
        value='.user-password-input input[placeholder="Password"]',
        notes="Empty/write-only in the UI — the current password is never displayed, only replaceable.",
    ),
    "user_edit.password_generate_button": SelectorEntry(
        name="user_edit.password_generate_button",
        strategy="attribute",
        value='button[icon="generate"]',
        notes="Generates a random password into user_edit.password_input. NOT exercised during recon (would mutate state) — only observed as present.",
    ),
    "user_edit.temporary_password_switch": SelectorEntry(
        name="user_edit.temporary_password_switch",
        strategy="text",
        value="Temporary password",
        notes="A wt-switcher toggle, located by its adjacent label text — no stable attribute observed for the control itself yet.",
    ),
    "user_edit.tab_license": SelectorEntry(
        name="user_edit.tab_license",
        strategy="text",
        value="License",
        notes="One of the tab strip items on a user's edit card (General/Roles/License/Devices/Variables/Tokens/Change logs/Permissions).",
    ),
    "user_edit.tab_roles": SelectorEntry(
        name="user_edit.tab_roles",
        strategy="text",
        value="Roles",
    ),
    "user_edit.tab_variables": SelectorEntry(
        name="user_edit.tab_variables",
        strategy="text",
        value="Variables",
    ),
    "multiselect.dropdown_arrow_button": SelectorEntry(
        name="multiselect.dropdown_arrow_button",
        strategy="attribute",
        value='button[icon="arrow-down"]',
        notes=(
            "Shared by the Roles and License tabs (both use the same multiselect component). "
            "Clicking it only opens the dropdown popup to reveal every checked option — a view, "
            "not a data change. Selected values collapse to e.g. 'sysadmin +2' in the DOM with no "
            "way to read the hidden ones without opening this popup; confirmed 2026-09-07 there is "
            "no title/aria-label carrying the full list. NEVER click the adjacent "
            "'.multiselect__clear'/icon=\"close\" button next to this one — that clears the selection."
        ),
    ),
    "multiselect.selected_option": SelectorEntry(
        name="multiselect.selected_option",
        strategy="attribute",
        value='li.multiselect__element[aria-selected="true"]',
        notes="Only meaningful after multiselect.dropdown_arrow_button has been clicked to open the popup.",
    ),
    "user_edit.variables_row": SelectorEntry(
        name="user_edit.variables_row",
        strategy="attribute",
        value=".value-pair",
        notes="One per key/value row on the Variables tab.",
    ),
    "user_edit.variables_key_input": SelectorEntry(
        name="user_edit.variables_key_input",
        strategy="attribute",
        value='input[placeholder="Key"]',
        notes="Scope to one .value-pair row first — not unique on the page otherwise.",
    ),
    "user_edit.variables_value_input": SelectorEntry(
        name="user_edit.variables_value_input",
        strategy="attribute",
        value='input[placeholder="Value"]',
        notes="Scope to one .value-pair row first — not unique on the page otherwise.",
    ),
    "user_edit.end_all_sessions_button": SelectorEntry(
        name="user_edit.end_all_sessions_button",
        strategy="text",
        value="END ALL SESSIONS",
        notes="Destructive-looking (styled red/critical) — logs the user out everywhere. NOT exercised during recon. Treat as HIGH risk if ever registered as an action.",
    ),
}

# NOT yet observed / not yet in this inventory (do not fabricate):
# - The "Devices" section under Directory (distinct from a user's own
#   "Devices" tab) — the real ticket scenario ("свободный номер 14006,
#   изменить пароль") may live partly here instead of/alongside Users.
# - Any Save/Apply control's real selector — deliberately not clicked
#   during recon, so its selector is unknown. Do not fabricate one; a real
#   mutation adapter must observe it directly when that work is authorized.
