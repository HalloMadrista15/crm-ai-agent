"""The first REAL (non-fixture) Playwright adapter in this project — reads
a Webitel user's Roles, License, and "group" Variable. Strictly read-only:
every method here only navigates and reads text. None of them ever call
``.fill()``, click Save/Apply, click "generate" (password), click the
multiselect "clear" (x) control, or click Delete/"END ALL SESSIONS". See
adapters/webitel_playwright/selectors.py for exactly which real, observed
selectors this relies on and why.

Unlike ``adapters/crm_playwright/reader.py`` (still an unconditional
NotImplementedError stub — no CRM test access exists), real Webitel access
does exist: production credentials in a local, gitignored ``info.env``, and
a saved Playwright session (``scripts/webitel_login_and_save_session.py``).
This class is what that access was used to build, under an explicit,
per-step read-only agreement with the project owner (see
docs/open_questions.md's Webitel recon notes).

This satisfies ``ports.entity_resolver.EntityResolver``, so the rest of the
codebase (Policy Engine, Plan Compiler) can use it exactly like the
fixture-backed CRM resolver — it does not know or care that this one talks
to a real system.

Scope: only ``resolve()`` (by login) and ``get_current_state()`` (roles,
license, group) exist. There is no write method on this class, and there
must never be one added here — a mutation adapter, when authorized, belongs
in a separate module so that "can this class read" and "can this class
write" are never the same import.
"""

from __future__ import annotations

from pathlib import Path

from crm_ai_agent.domain.entities import ResolvedEntity
from crm_ai_agent.ports.entity_resolver import SubjectNotFoundError


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


class WebitelEntityResolver:
    def __init__(self, *, base_url: str, storage_state_path: Path) -> None:
        if not storage_state_path.exists():
            raise FileNotFoundError(
                f"No saved Webitel session at {storage_state_path}. "
                "Run scripts/webitel_login_and_save_session.py first."
            )
        self._base_url = base_url
        self._storage_state_path = storage_state_path

    @classmethod
    def from_info_env(cls, info_env_path: Path, storage_state_path: Path) -> "WebitelEntityResolver":
        env = _load_env_file(info_env_path)
        base_url = env.get("WEBITEL_ALMATY_BASE_URL")
        if not base_url:
            raise ValueError(f"Missing WEBITEL_ALMATY_BASE_URL in {info_env_path}")
        return cls(base_url=base_url, storage_state_path=storage_state_path)

    def _navigate_to_user_edit(self, page, login: str) -> None:
        page.goto(self._base_url, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(1500)

        for label in ("Admin", "Directory", "Users"):
            page.get_by_text(label, exact=False).first.evaluate("el => el.click()")
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(1200)

        page.get_by_placeholder("Search").fill(login)
        page.wait_for_timeout(1200)

    def resolve(self, query: str) -> ResolvedEntity:
        """Look up a user by login. ``query`` is expected to be a login
        (e.g. "10078") — this resolver's search box filters by login (and
        incidentally name/extension), and ``entity_id`` on a unique match
        is the login itself, since that is what the rest of this codebase
        (``provision_webitel_user``'s ``template_user_login``) actually
        needs.
        """

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(self._storage_state_path))
            page = context.new_page()
            try:
                page.goto(self._base_url, wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle", timeout=20000)
                page.wait_for_timeout(1500)
                for label in ("Admin", "Directory", "Users"):
                    page.get_by_text(label, exact=False).first.evaluate("el => el.click()")
                    page.wait_for_load_state("networkidle", timeout=20000)
                    page.wait_for_timeout(1200)

                page.get_by_placeholder("Search").fill(query)
                page.wait_for_timeout(1200)

                match_count = page.locator('button[icon="edit"]').count()
            finally:
                browser.close()

        if match_count == 1:
            return ResolvedEntity(query=query, match_count=1, entity_id=query, source="webitel")
        return ResolvedEntity(query=query, match_count=match_count, source="webitel")

    def get_current_state(self, entity_id: str) -> dict[str, str]:
        """Read ``roles``, ``license``, and ``group`` for ``entity_id``
        (a login). Raises ``SubjectNotFoundError`` if no such user exists.
        Every value read here comes from actually opening the multiselect
        dropdowns (Roles/License) to reveal their full checked-option list
        — the collapsed view only shows the first item plus a '+N' badge
        with no other way to read the rest (confirmed 2026-09-07).
        """

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(self._storage_state_path))
            page = context.new_page()
            try:
                self._navigate_to_user_edit(page, entity_id)

                if page.locator('button[icon="edit"]').count() == 0:
                    raise SubjectNotFoundError(f"no Webitel user found for login={entity_id!r}")

                page.locator('button[icon="edit"]').first.evaluate("el => el.click()")
                page.wait_for_load_state("networkidle", timeout=20000)
                page.wait_for_timeout(1500)

                roles = self._read_multiselect(page, "Roles")
                license_ = self._read_multiselect(page, "License")
                group = self._read_group_variable(page)
            finally:
                browser.close()

        return {
            "roles": ",".join(roles),
            "license": ",".join(license_),
            "group": group,
        }

    def _read_multiselect(self, page, tab_label: str) -> list[str]:
        page.get_by_text(tab_label, exact=True).first.evaluate("el => el.click()")
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(1200)

        # View-only: opens the dropdown popup, never touches the adjacent
        # "clear" (x) control.
        page.locator('button[icon="arrow-down"]').first.evaluate("el => el.click()")
        page.wait_for_timeout(800)

        selected = page.locator('li.multiselect__element[aria-selected="true"]')
        values = [selected.nth(i).inner_text().strip() for i in range(selected.count())]

        # Close the dropdown the same way a viewer would (click the arrow
        # again) rather than leaving it open when we navigate to the next
        # tab — purely cosmetic/state hygiene, not a data change.
        page.locator('button[icon="arrow-down"]').first.evaluate("el => el.click()")
        page.wait_for_timeout(300)

        return values

    def _read_group_variable(self, page) -> str:
        page.get_by_text("Variables", exact=True).first.evaluate("el => el.click()")
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(1200)

        rows = page.locator(".value-pair")
        for i in range(rows.count()):
            row = rows.nth(i)
            key = row.locator('input[placeholder="Key"]').input_value()
            if key == "group":
                return row.locator('input[placeholder="Value"]').input_value()
        return ""
