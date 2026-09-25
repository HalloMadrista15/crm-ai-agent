"""Owns ONE persistent Playwright browser/page for the life of the console
server process, instead of launching a fresh browser per request (the
approach every standalone recon script in scripts/ still uses, via a saved
storage_state.json file).

Confirmed 2026-09-20: this CRM's session survives roughly 20 minutes of
real, active use in one browser tab (per the project owner's own
experience), but reusing saved cookies in a brand-new browser instance —
the old per-request approach — died almost immediately, sometimes within
a couple of minutes. Something about this app appears to bind session
validity to more than just the cookies that ``storage_state()`` captures
(the exact mechanism was not identified — a plausible cause is
tab-instance-scoped state, e.g. sessionStorage, which Playwright's
storage_state does not capture at all). Keeping one browser/page alive for
the console's whole lifetime sidesteps the problem entirely rather than
trying to explain it further.

Playwright's sync API is thread-affine (must be driven from the same
thread that created it), so all actual Playwright calls here run on one
dedicated background thread via a simple task queue; public methods submit
work to that thread and block for the result — safe to call from any of
the HTTP server's request-handling threads.

Read-only except for the login flow itself (which only ever fills the
login/password fields and presses Enter — never touches an MFA/token
field, exactly like scripts/crm_login_and_save_session.py). There is no
write method here and there must never be one added — the same rule
stated in every other real adapter in this project.
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any, Callable

# Confirmed 2026-09-21 via read-only OData recon (GET /0/odata/BnzVwApproval)
# against the real "Задачи на согласование" list: this is the StatusId
# shared by every task still awaiting a decision. Filtering on the flat
# column name ("StatusId eq ...") is rejected server-side ("Column by path
# StatusId not found in scheme BnzVwApproval") — the navigation-property
# form ("Status/Id eq ...") is what list_pending_tickets actually sends.
_APPROVAL_STATUS_PENDING = "3462594d-77a7-4b0a-874a-6d8b54b293bc"

# The TypeColumnValue shared by "Доступ к ИС" office notes specifically
# (confirmed against the real KST-HPK-9422 ticket) — as opposed to other
# TsiOfficeNotes subtypes (e.g. "Организация рабочего места нового
# сотрудника"), which use a different card page and would 404 under the
# TsiOfficeNotesISAccessPage URL this module builds.
_IS_ACCESS_NOTE_TYPE_ID = "c9870a37-444c-4655-a68c-cb7a1915ff71"


class CrmSessionError(Exception):
    pass


def _read_field_value(page: Any, column_name: str) -> str:
    """Same confirmed id-prefix pattern as
    adapters/crm_playwright/ticket_reader.py — duplicated rather than
    imported because that module launches its own throwaway browser per
    call; this one drives an already-open, persistent page instead. See
    that module's docstring for how the pattern was confirmed."""

    prefix = f"TsiOfficeNotesISAccessPage{column_name}"
    candidates = page.locator(
        f'input[id^="{prefix}"]:not([id$="-virtual"]), textarea[id^="{prefix}"]:not([id$="-virtual"])'
    )
    if candidates.count() == 0:
        return ""
    return candidates.first.input_value().strip()


class CrmSessionManager:
    def __init__(self, *, base_url: str, login: str, password: str) -> None:
        self._base_url = base_url
        self._login = login
        self._password = password
        self._playwright = None
        self._browser = None
        self._page = None
        self._queue: "queue.Queue[tuple[Callable[[CrmSessionManager], Any], queue.Queue]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            self._playwright = p
            while True:
                func, result_q = self._queue.get()
                try:
                    result = func(self)
                    result_q.put(("ok", result))
                except Exception as exc:  # noqa: BLE001 — reported to the caller, not crashed here
                    result_q.put(("error", exc))

    def _submit(self, func: Callable[["CrmSessionManager"], Any], timeout: float = 60.0) -> Any:
        result_q: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._queue.put((func, result_q))
        try:
            status, value = result_q.get(timeout=timeout)
        except queue.Empty as exc:
            raise CrmSessionError(f"Timed out after {timeout}s waiting for the CRM browser thread") from exc
        if status == "error":
            raise value
        return value

    def start_login(self) -> None:
        """Opens a VISIBLE browser, fills login/password, presses Enter,
        and returns immediately — does NOT wait for or verify completion.
        Poll ``is_logged_in()`` afterward. Never touches an MFA field; the
        human completes that themselves in the window this opens, exactly
        like scripts/crm_login_and_save_session.py."""

        def _do(mgr: "CrmSessionManager") -> None:
            if mgr._browser is not None:
                try:
                    mgr._browser.close()
                except Exception:
                    pass
            mgr._browser = mgr._playwright.chromium.launch(headless=False)
            context = mgr._browser.new_context()
            mgr._page = context.new_page()
            page = mgr._page

            page.goto(mgr._base_url, wait_until="domcontentloaded")
            page.wait_for_load_state("load", timeout=20000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(1500)

            def fill(selector: str, value: str) -> bool:
                try:
                    locator = page.locator(selector).first
                    if locator.count() > 0:
                        locator.fill(value, timeout=3000)
                        return True
                except Exception:
                    pass
                return False

            username_filled = fill("#loginEdit-el", mgr._login)
            password_filled = fill("#passwordEdit-el", mgr._password)
            if username_filled and password_filled:
                page.locator("#passwordEdit-el").first.press("Enter")

        self._submit(_do)

    def is_logged_in(self) -> bool:
        """Confirmed 2026-09-21: checking ``page.url`` for the post-login
        SPA route was unreliable — Creatio's client-side hash router can
        lag several seconds behind the session actually becoming valid
        (cookies are set as soon as auth succeeds; the SPA shell finishing
        its own bootstrap and updating the URL is a separate, slower
        thing), so operators kept seeing "запускаю..." long after they'd
        actually finished logging in. Checking real auth directly — a
        lightweight authenticated request from inside the page — tracks
        what actually matters and isn't tied to SPA navigation timing."""

        def _do(mgr: "CrmSessionManager") -> bool:
            if mgr._page is None:
                return False
            try:
                status = mgr._page.evaluate(
                    """async () => {
                        try {
                            const r = await fetch('/0/odata/$metadata', {credentials: 'include'});
                            return r.status;
                        } catch (e) {
                            return 0;
                        }
                    }"""
                )
            except Exception:
                return False
            return status == 200

        return self._submit(_do, timeout=10.0)

    def list_pending_tickets(self, visa_owner_id: str) -> list[dict[str, str]]:
        """Read-only: lists real "Доступ к ИС" office-note tickets still
        awaiting THIS operator's approval decision, via Creatio's OData API
        (GET /0/odata/BnzVwApproval — confirmed 2026-09-21 against the real
        "Задачи на согласование" list) instead of scraping that page's
        positional grid.

        The request itself is scoped server-side to this operator — it
        never pulls other people's tasks. GUID lookup columns rejected a
        flat filter ("StatusId eq ..." → "Column by path StatusId not
        found in scheme BnzVwApproval") but accept navigation-property
        syntax instead ("Status/Id eq ..."), confirmed 2026-09-21; only
        TypeColumnValue (the "Доступ к ИС" subtype marker) has no known
        navigation-property name, so that one check stays client-side —
        cheap since the server-side filters already narrow to a handful of
        rows, not company-wide volume."""

        def _do(mgr: "CrmSessionManager") -> list[dict[str, str]]:
            if mgr._page is None:
                raise CrmSessionError("Not logged in — click 'Войти в CRM' first")
            odata_url = (
                f"{mgr._base_url}/0/odata/BnzVwApproval"
                f"?$filter=VisaOwner/Id eq {visa_owner_id}"
                f" and Status/Id eq {_APPROVAL_STATUS_PENDING}"
                " and IsCanceled eq false and ReferenceSchemaName eq 'TsiOfficeNotes'"
                "&$orderby=TaskCreatedOn desc"
            )
            result = mgr._page.evaluate(
                """async (url) => {
                    const r = await fetch(url, {credentials: 'include', headers: {'Accept': 'application/json'}});
                    return {status: r.status, body: await r.text()};
                }""",
                odata_url,
            )
            if result["status"] != 200:
                raise CrmSessionError(f"CRM OData request failed: HTTP {result['status']} — {result['body'][:500]}")
            rows = json.loads(result["body"]).get("value", [])

            tickets = []
            for row in rows:
                if row.get("TypeColumnValue") != _IS_ACCESS_NOTE_TYPE_ID:
                    continue
                entity_id = row["EntityId"]
                tickets.append(
                    {
                        "name": row.get("Name", ""),
                        "url": f"{mgr._base_url}/0/Nui/ViewModule.aspx#CardModuleV2/TsiOfficeNotesISAccessPage/edit/{entity_id}",
                        "task_created_on": row.get("TaskCreatedOn", ""),
                    }
                )
            return tickets

        return self._submit(_do, timeout=20.0)

    def read_ticket(self, ticket_url: str) -> dict[str, str]:
        def _do(mgr: "CrmSessionManager") -> dict[str, str]:
            if mgr._page is None:
                raise CrmSessionError("Not logged in — click 'Войти в CRM' first")
            page = mgr._page

            page.goto(ticket_url, wait_until="domcontentloaded")
            page.wait_for_load_state("load", timeout=20000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(4000)

            status = _read_field_value(page, "TsiOfficeNoteState")
            if not status:
                raise CrmSessionError(
                    f"Could not read ticket at {ticket_url} — page may not have loaded, "
                    "session may have expired, or this is not a TsiOfficeNotesISAccessPage ticket"
                )
            return {
                "status": status,
                "employee_full_name": _read_field_value(page, "TsiRMOrgEmployeeFullName"),
                "employee_account": _read_field_value(page, "BnzEmployeeAccount"),
                "information_system": _read_field_value(page, "BnzInformationSystem"),
                "comment": _read_field_value(page, "BnzComment"),
            }

        return self._submit(_do, timeout=60.0)
