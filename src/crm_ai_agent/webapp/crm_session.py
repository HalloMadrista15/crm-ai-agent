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

Read-only except for the login flow itself: it fills login/password and,
when the CRM asks for one, the confirmation code the operator typed into
the console (changed 2026-09-28 at the operator's request — the browser now
runs headless instead of popping up a window for them to type the code
into; the code is only relayed, never stored). There is no write method
here and there must never be one added — the same rule stated in every
other real adapter in this project.
"""

from __future__ import annotations

import json
import queue
import re
import threading
from pathlib import Path
from typing import Any, Callable

_DEBUG_SCREENSHOT_PATH = Path(__file__).resolve().parents[3] / ".auth" / "debug_login_check.png"

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


def _visa_owner_filter(owner_ids: list[str]) -> str:
    """OData filter matching approvals owned by any of ``owner_ids``.

    Confirmed 2026-09-28 against the real CRM: BnzVwApproval.VisaOwner is a
    SysAdminUnit, i.e. either a user or a role — the previously hand-found
    CRM_VISA_OWNER_ID turned out to be the functional role "Сетевой
    инженер" (SysAdminUnitTypeValue 6), not the operator's own account,
    while other "Доступ к ИС" approvals were owned by individual users. So
    "my" approvals are those owned by me OR by any role I'm in, which is
    also what Creatio's own "Задачи на согласование" list shows."""
    if not owner_ids:
        raise CrmSessionError("No Visa Owner ids to filter on")
    clauses = " or ".join(f"VisaOwner/Id eq {owner_id}" for owner_id in owner_ids)
    return clauses if len(owner_ids) == 1 else f"({clauses})"


# Tables holding every approval step of a ticket, in preference order — see
# CrmSessionManager.read_approvals.
_APPROVAL_TABLES = ("SysApproval", "BnzVwAllApproval")


def _approval_steps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Approval rows (with VisaOwner/SetBy/Status/BnzVisaType expanded to
    their names) as the display-ready steps of the "Согласование" tab, in
    position order. Only the latest approval round (BnzCycle) is kept, like
    the tab's own "Текущее согласование" view."""

    def name(row: dict[str, Any], nav: str) -> str:
        return ((row.get(nav) or {}).get("Name") or "").strip()

    cycles = [row["BnzCycle"] for row in rows if isinstance(row.get("BnzCycle"), int)]
    if cycles:
        rows = [row for row in rows if row.get("BnzCycle", max(cycles)) == max(cycles)]

    steps = [
        {
            "position": row.get("BnzPosition"),
            "visa_type": name(row, "BnzVisaType"),
            "owner": name(row, "VisaOwner"),
            "set_by": name(row, "SetBy"),
            "status": name(row, "Status"),
            # CRM stores line breaks in approval comments as "[BRMACRO]".
            "comment": (row.get("Comment") or "").replace("[BRMACRO]", "\n").strip(),
            "is_current": bool(row.get("BnzIsCurrentGroup")),
        }
        for row in rows
    ]
    steps.sort(key=lambda step: (step["position"] is None, step["position"] or 0))
    return steps


def _own_visa_owner_ids(page: Any) -> list[str]:
    """The logged-in user's own SysAdminUnit Id followed by the Ids of every
    role they are in (SysAdminUnitInRole), read inside the logged-in page."""
    result = page.evaluate(
        """async () => {
            // Right after login the Creatio shell may still be booting.
            for (let i = 0; i < 30 && !(window.Terrasoft && Terrasoft.SysValue && Terrasoft.SysValue.CURRENT_USER); i++) {
                await new Promise(r => setTimeout(r, 500));
            }
            const user = ((window.Terrasoft && Terrasoft.SysValue && Terrasoft.SysValue.CURRENT_USER) || {}).value;
            if (!user) return {user: null, status: 0, body: ''};
            const r = await fetch(`/0/odata/SysAdminUnitInRole?$filter=SysAdminUnit/Id eq ${user}&$top=1000`,
                                  {credentials: 'include', headers: {'Accept': 'application/json'}});
            return {user, status: r.status, body: await r.text()};
        }"""
    )
    if not result.get("user"):
        raise CrmSessionError("CRM did not report the logged-in user — is the CRM page fully loaded?")
    if result.get("status") != 200:
        raise CrmSessionError(f"CRM role lookup failed: HTTP {result.get('status')} — {str(result.get('body'))[:300]}")
    owner_ids = [result["user"]]
    for row in json.loads(result["body"]).get("value", []):
        role_id = row.get("SysAdminUnitRoleId")
        if role_id and role_id not in owner_ids:
            owner_ids.append(role_id)
    return owner_ids


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
    def __init__(self, *, base_url: str, login: str = "", password: str = "", show_browser: bool = False) -> None:
        self._base_url = base_url
        self._login = login
        self._password = password
        # Headless by default; CRM_SHOW_BROWSER=1 in info.env shows the
        # window again for troubleshooting.
        self._show_browser = show_browser
        self._playwright = None
        self._browser = None
        self._page = None
        # Where read_approvals found the full approvals list (schema-level,
        # so it survives re-logins); None until the first successful lookup.
        self._approval_source: dict[str, Any] | None = None
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

    def start_login(self, login: str | None = None, password: str | None = None) -> None:
        """Opens the (headless) browser, fills login/password, presses
        Enter, and returns immediately — does NOT wait for or verify
        completion. Poll ``is_logged_in()``/``login_progress()`` afterward;
        if the CRM asks for a confirmation code, the operator enters it in
        the console and it arrives via ``submit_code()``.

        ``login``/``password`` replace the credentials this manager was
        built with, so an operator can switch accounts without restarting
        the server; any previously opened CRM window is closed first."""

        def _do(mgr: "CrmSessionManager") -> None:
            if login is not None:
                mgr._login = login
            if password is not None:
                mgr._password = password
            if not mgr._login or not mgr._password:
                raise CrmSessionError("CRM login and password are not set")
            if mgr._browser is not None:
                try:
                    mgr._browser.close()
                except Exception:
                    pass
            mgr._browser = mgr._playwright.chromium.launch(headless=not mgr._show_browser)
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

    def login_progress(self) -> dict[str, Any]:
        """Read-only look at the login page: whether the CRM is asking for a
        confirmation code (seen 2026-09-28: "Введите код подтверждения из
        приложения-аутентификатора" on /Login/NuiLogin.aspx, with a code
        field and a "Подтвердить" button), and any error line it shows
        (e.g. a wrong password)."""

        def _do(mgr: "CrmSessionManager") -> dict[str, Any]:
            if mgr._page is None or mgr._page.is_closed():
                return {"code_required": False, "error_text": ""}
            try:
                return mgr._page.evaluate(
                    r"""() => {
                        if (!location.href.toLowerCase().includes('/login/')) return {code_required: false, error_text: ''};
                        const text = document.body ? document.body.innerText : '';
                        const otherInputs = [...document.querySelectorAll('input')].filter(i =>
                            i.offsetParent !== null && !['loginEdit-el', 'passwordEdit-el'].includes(i.id)
                            && !['hidden', 'checkbox', 'radio', 'submit', 'button'].includes(i.type));
                        const errorLine = text.split('\n').map(l => l.trim())
                            .find(l => l.length < 200 && /неверн|ошибк|недействит|invalid|incorrect|expired|истек/i.test(l)) || '';
                        return {
                            code_required: /код подтверждения|verification code|one-time code/i.test(text) && otherInputs.length > 0,
                            error_text: errorLine,
                        };
                    }"""
                )
            except Exception:
                return {"code_required": False, "error_text": ""}

        return self._submit(_do, timeout=10.0)

    def submit_code(self, code: str) -> None:
        """Types the confirmation code the operator entered in the console
        into the CRM's code field and confirms. The code is not stored."""

        def _do(mgr: "CrmSessionManager") -> None:
            if mgr._page is None or mgr._page.is_closed():
                raise CrmSessionError("Вход в CRM не запущен — нажмите «Войти в CRM»")
            page = mgr._page
            field = page.locator(
                "input:not(#loginEdit-el):not(#passwordEdit-el):not([type=hidden]):not([type=checkbox]):not([type=radio]):visible"
            ).first
            if field.count() == 0:
                raise CrmSessionError("CRM не показывает поле для кода — возможно, вход уже выполнен или код больше не нужен")
            field.fill(code, timeout=5000)
            confirm = page.locator("button:visible, a:visible, [role=button]:visible, input[type=submit]:visible").filter(
                has_text=re.compile("подтвердить|confirm|verify", re.IGNORECASE)
            ).first
            if confirm.count() > 0:
                confirm.click(timeout=5000)
            else:
                field.press("Enter")

        self._submit(_do, timeout=30.0)

    def screenshot_png(self) -> bytes:
        """Read-only: what the (headless) CRM browser currently shows."""

        def _do(mgr: "CrmSessionManager") -> bytes:
            if mgr._page is None or mgr._page.is_closed():
                raise CrmSessionError("Браузер CRM не запущен")
            return mgr._page.screenshot()

        return self._submit(_do, timeout=20.0)

    def has_open_page(self) -> bool:
        """Whether a CRM window from an earlier start_login() is still
        open — e.g. sitting on the MFA prompt. The console checks this
        before auto-starting a login so that reloading the page never
        replaces a window the operator is in the middle of using."""

        def _do(mgr: "CrmSessionManager") -> bool:
            if mgr._page is None:
                return False
            try:
                return not mgr._page.is_closed()
            except Exception:
                return False

        return self._submit(_do, timeout=10.0)

    def is_logged_in(self) -> bool:
        """Confirmed 2026-09-21: checking ``page.url`` for the post-login
        SPA route was unreliable — Creatio's client-side hash router can
        lag several seconds behind the session actually becoming valid
        (cookies are set as soon as auth succeeds; the SPA shell finishing
        its own bootstrap and updating the URL is a separate, slower
        thing), so operators kept seeing "запускаю..." long after they'd
        actually finished logging in. Checking real auth directly — a
        lightweight authenticated request from inside the page — tracks
        what actually matters and isn't tied to SPA navigation timing.

        Confirmed 2026-09-28: the first version of this check hit
        ``/0/odata/$metadata``, which turned out to be servable without
        auth at all — it kept reporting "logged in" while the real browser
        was still sitting on an MFA prompt. Switched to a real data query
        (``BnzVwApproval?$top=1``), but that ALSO turned out to return 200
        during the MFA prompt — this CRM apparently sets valid session
        cookies as soon as username/password are accepted, before MFA is
        confirmed, so any authenticated-but-not-MFA'd request already
        succeeds. Now requires BOTH: the URL must have actually left
        ``/Login/`` (a cheap, always-correct-while-MFA-is-pending signal —
        the original page.url lag problem this whole check was built to
        avoid only ever affected the few seconds *after* a real login,
        never the MFA-pending state) AND the data query succeeding.

        Confirmed 2026-09-28: the URL must come from ``location.href``
        inside the page, not ``page.url``. ``page.url`` is a cached value
        that Playwright's sync API only refreshes while some real call is
        in flight; this thread otherwise sits idle on its queue, so after
        the post-MFA navigation ``page.url`` stayed on /Login/NuiLogin.aspx
        forever and the old early return meant no real call ever ran to
        refresh it — the console showed "запускаю..." over a browser that
        was already on the home page."""

        def _do(mgr: "CrmSessionManager") -> bool:
            if mgr._page is None:
                return False
            try:
                result = mgr._page.evaluate(
                    """async () => {
                        const href = location.href;
                        if (href.toLowerCase().includes('/login/')) {
                            return {href, status: null};
                        }
                        try {
                            const r = await fetch('/0/odata/BnzVwApproval?$top=1', {credentials: 'include'});
                            return {href, status: r.status};
                        } catch (e) {
                            return {href, status: 0};
                        }
                    }"""
                )
            except Exception:
                return False
            if not isinstance(result, dict):
                return False
            if "/login/" in str(result.get("href", "")).lower():
                return False
            return result.get("status") == 200

        return self._submit(_do, timeout=10.0)

    def debug_login_check(self) -> dict[str, Any]:
        """Temporary diagnostic: breaks is_logged_in's two conditions
        apart so a failure can be attributed to one or the other."""

        def _do(mgr: "CrmSessionManager") -> dict[str, Any]:
            if mgr._page is None:
                return {"has_page": False}
            try:
                url = mgr._page.url
            except Exception as exc:
                return {"has_page": True, "url_error": str(exc)}
            on_login_url = "/login/" in url.lower()
            try:
                eval_result = mgr._page.evaluate(
                    """async () => {
                        try {
                            const r = await fetch('/0/odata/BnzVwApproval?$top=1', {credentials: 'include'});
                            const text = await r.text();
                            return {status: r.status, body: text.slice(0, 300)};
                        } catch (e) {
                            return {status: 0, error: String(e)};
                        }
                    }"""
                )
            except Exception as exc:
                eval_result = {"evaluate_threw": str(exc)}
            shot_path = str(_DEBUG_SCREENSHOT_PATH)
            try:
                mgr._page.screenshot(path=shot_path)
                screenshot_saved = True
            except Exception:
                screenshot_saved = False
            return {
                "has_page": True,
                "url": url,
                "on_login_url": on_login_url,
                "fetch_result": eval_result,
                "screenshot_saved": screenshot_saved,
                "screenshot_path": shot_path,
            }

        return self._submit(_do, timeout=15.0)

    def read_approvals(self, entity_id: str) -> dict[str, Any]:
        """Read-only: every approval step of one "Доступ к ИС" ticket, as in
        the ticket card's "Согласование" tab.

        BnzVwApproval can't be used for this — confirmed 2026-09-28 it only
        holds the currently active step (one row per ticket). The CRM schema
        has three tables with the full step columns (BnzPosition, VisaOwner,
        ...): SysApproval (the base table), BnzVwAllApproval (a view over all
        approvals) and BnzApprovalHistory (past rounds — it returned no rows
        for a live ticket). _APPROVAL_TABLES lists them in preference order;
        the first one present in /0/odata/$metadata with an EntityId column
        is used, found once per session."""

        def _do(mgr: "CrmSessionManager") -> dict[str, Any]:
            if mgr._page is None:
                raise CrmSessionError("Not logged in — click 'Войти в CRM' first")
            result = mgr._page.evaluate(
                r"""async (args) => {
                    let source = args.source;
                    if (!source) {
                        const xml = await (await fetch('/0/odata/$metadata', {credentials: 'include'})).text();
                        for (const name of args.tables) {
                            const m = xml.match(new RegExp(`<EntityType Name="${name}"[\\s\\S]*?</EntityType>`));
                            if (!m || !m[0].includes('<Property Name="EntityId"')) continue;
                            source = {
                                entity: name,
                                has_canceled: m[0].includes('<Property Name="IsCanceled"'),
                                has_visa_type: m[0].includes('<NavigationProperty Name="BnzVisaType"'),
                            };
                            break;
                        }
                        if (!source) return {source: null};
                    }
                    const filter = `EntityId eq ${args.entityId}` + (source.has_canceled ? ' and IsCanceled eq false' : '');
                    const expand = ['VisaOwner($select=Name)', 'SetBy($select=Name)', 'Status($select=Name)']
                        .concat(source.has_visa_type ? ['BnzVisaType($select=Name)'] : []).join(',');
                    const r = await fetch(`/0/odata/${source.entity}?$filter=${filter}&$expand=${expand}&$orderby=BnzPosition`,
                                          {credentials: 'include', headers: {'Accept': 'application/json'}});
                    return {source, status: r.status, body: await r.text()};
                }""",
                {"entityId": entity_id, "source": mgr._approval_source, "tables": list(_APPROVAL_TABLES)},
            )
            if not result.get("source"):
                raise CrmSessionError("Could not find the approvals table in the CRM schema")
            if result.get("status") != 200:
                raise CrmSessionError(f"CRM approvals request failed: HTTP {result.get('status')} — {str(result.get('body'))[:300]}")
            mgr._approval_source = result["source"]
            return {
                "source": result["source"]["entity"],
                "steps": _approval_steps(json.loads(result["body"]).get("value", [])),
            }

        return self._submit(_do, timeout=90.0)

    def list_pending_tickets(self, visa_owner_id: str | None = None) -> list[dict[str, str]]:
        """Read-only: lists real "Доступ к ИС" office-note tickets still
        awaiting THIS operator's approval decision, via Creatio's OData API
        (GET /0/odata/BnzVwApproval — confirmed 2026-09-21 against the real
        "Задачи на согласование" list) instead of scraping that page's
        positional grid.

        ``visa_owner_id`` is an explicit override (CRM_VISA_OWNER_ID); when
        it is empty, the approvals of the logged-in user and of every role
        they belong to are listed — see _visa_owner_filter for why.

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
            owner_ids = [visa_owner_id] if visa_owner_id else _own_visa_owner_ids(mgr._page)
            odata_url = (
                f"{mgr._base_url}/0/odata/BnzVwApproval"
                f"?$filter={_visa_owner_filter(owner_ids)}"
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
