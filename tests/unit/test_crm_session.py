"""Offline tests for CrmSessionManager — the persistent single-browser
session used by the console server (webapp/crm_session.py).

Never launches a real browser: CrmSessionManager's constructor does start
its real background thread (which imports playwright.sync_api — confirmed
available in this environment), but no browser is created until
start_login() runs. These tests instead inject a fake page object directly
via the (private, same-module) ``_page`` attribute and drive the real
is_logged_in()/read_ticket() methods through the real thread/queue
machinery, which is the part worth protecting with regression tests — the
session-longevity bug this module was written to fix was never about the
URL-matching logic itself, but locking that logic down is still cheap
insurance now that it's confirmed correct against the real system
(2026-09-20).
"""

import unittest

from crm_ai_agent.webapp.crm_session import (
    _APPROVAL_STATUS_PENDING,
    CrmSessionError,
    CrmSessionExpired,
    CrmSessionManager,
    desktop_user_agent,
    _own_visa_owner_ids,
    _read_field_value,
    _visa_owner_filter,
)

class _FakeLocator:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def count(self) -> int:
        return 0 if self._value is None else 1

    @property
    def first(self) -> "_FakeLocator":
        return self

    def input_value(self) -> str:
        assert self._value is not None
        return self._value


class _FakePage:
    def __init__(
        self,
        url: str,
        field_values: dict[str, str] | None = None,
        evaluate_result: object = None,
    ) -> None:
        self.url = url
        self._field_values = field_values or {}
        self.evaluate_result = evaluate_result
        self.evaluate_calls: list[object] = []

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self.url = url

    def wait_for_load_state(self, *args: object, **kwargs: object) -> None:
        pass

    def wait_for_timeout(self, ms: int) -> None:
        pass

    def locator(self, selector: str) -> _FakeLocator:
        for column_name, value in self._field_values.items():
            if f"TsiOfficeNotesISAccessPage{column_name}" in selector:
                return _FakeLocator(value)
        return _FakeLocator(None)

    def is_closed(self) -> bool:
        return False

    def evaluate(self, script: str, arg: object = None) -> object:
        if script == "location.href":  # _on_login_page's probe, not a data request
            return self.url
        self.evaluate_calls.append(arg)
        if "location.href" in script:
            # is_logged_in's probe: the page's live URL plus the OData status.
            return {"href": self.url, "status": self.evaluate_result}
        return self.evaluate_result


class TestReadFieldValue(unittest.TestCase):
    def test_returns_stripped_value_when_field_present(self) -> None:
        page = _FakePage(url="https://example.invalid", field_values={"BnzComment": "  hello  "})
        self.assertEqual(_read_field_value(page, "BnzComment"), "hello")

    def test_returns_empty_string_when_field_absent(self) -> None:
        page = _FakePage(url="https://example.invalid")
        self.assertEqual(_read_field_value(page, "BnzComment"), "")


class TestCrmSessionManager(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # One manager, reused by every test in this class — constructing it
        # spins up a real background thread + a real (idle) Playwright
        # driver process, which is real overhead worth paying once, not
        # per-test. No browser is ever launched since start_login() is
        # never called here.
        cls.mgr = CrmSessionManager(base_url="https://example.invalid", login="x", password="y")

    def test_is_logged_in_false_when_no_page(self) -> None:
        self.mgr._page = None
        self.assertFalse(self.mgr.is_logged_in())

    def test_is_logged_in_false_when_data_query_rejected(self) -> None:
        # Off the login URL, but the real OData query doesn't return 200.
        self.mgr._page = _FakePage(url="https://crm.astana-motors.kz/0/Nui/ViewModule.aspx", evaluate_result=302)
        self.assertFalse(self.mgr.is_logged_in())

    def test_is_logged_in_true_when_off_login_url_and_data_query_succeeds(self) -> None:
        self.mgr._page = _FakePage(url="https://crm.astana-motors.kz/0/Nui/ViewModule.aspx#HomePage", evaluate_result=200)
        self.assertTrue(self.mgr.is_logged_in())

    def test_is_logged_in_false_while_still_on_login_url_even_if_data_query_succeeds(self) -> None:
        """Real bug confirmed 2026-09-28: this CRM sets valid session
        cookies as soon as username/password are accepted, before MFA is
        confirmed — so the OData query alone returned 200 while the human
        was still staring at the MFA code prompt on /Login/NuiLogin.aspx.
        The URL must have actually left /Login/ too."""

        self.mgr._page = _FakePage(url="https://crm.astana-motors.kz/Login/NuiLogin.aspx?ReturnUrl=%2f", evaluate_result=200)
        self.assertFalse(self.mgr.is_logged_in())

    def test_is_logged_in_uses_live_location_not_stale_page_url(self) -> None:
        """Real bug confirmed 2026-09-28: after MFA the browser was on the
        home page, but Playwright's cached ``page.url`` still said
        /Login/NuiLogin.aspx because nothing had pumped its events — the
        console sat on "запускаю..." indefinitely."""

        class _StaleUrlPage(_FakePage):
            def evaluate(self, script: str, arg: object = None) -> object:
                return {"href": "https://crm.astana-motors.kz/0/Nui/ViewModule.aspx#HomePage/Page_w2gdvpz", "status": 200}

        self.mgr._page = _StaleUrlPage(url="https://crm.astana-motors.kz/Login/NuiLogin.aspx?ReturnUrl=%2f")
        self.assertTrue(self.mgr.is_logged_in())

    def test_is_logged_in_never_checks_metadata_endpoint(self) -> None:
        """Regression guard: $metadata turned out to be servable without
        auth (confirmed 2026-09-28 — it kept saying "logged in" while the
        real browser was still stuck on an MFA prompt), so the check must
        never rely on it again."""

        captured_scripts: list[str] = []

        class _CapturingPage(_FakePage):
            def evaluate(self, script: str, arg: object = None) -> object:
                captured_scripts.append(script)
                return 200

        self.mgr._page = _CapturingPage(url="https://crm.astana-motors.kz/")
        self.mgr.is_logged_in()

        (script,) = captured_scripts
        self.assertNotIn("fetch('/0/odata/$metadata'", script)
        self.assertIn("BnzVwApproval", script)

    def test_is_logged_in_false_when_evaluate_raises(self) -> None:
        class _RaisingPage(_FakePage):
            def evaluate(self, script: str, arg: object = None) -> object:
                raise RuntimeError("Execution context was destroyed")

        self.mgr._page = _RaisingPage(url="https://crm.astana-motors.kz/")
        self.assertFalse(self.mgr.is_logged_in())

    def test_start_login_without_credentials_raises_before_opening_a_browser(self) -> None:
        self.mgr._page = None
        with self.assertRaises(CrmSessionError):
            self.mgr.start_login(login="", password="")
        self.assertIsNone(self.mgr._page)

    def test_has_open_page_false_when_no_page(self) -> None:
        self.mgr._page = None
        self.assertFalse(self.mgr.has_open_page())

    def test_has_open_page_tracks_whether_the_window_was_closed(self) -> None:
        class _ClosablePage(_FakePage):
            closed = False

            def is_closed(self) -> bool:
                return self.closed

        page = _ClosablePage(url="https://crm.astana-motors.kz/")
        self.mgr._page = page
        self.assertTrue(self.mgr.has_open_page())
        page.closed = True
        self.assertFalse(self.mgr.has_open_page())

    def test_read_ticket_raises_without_page(self) -> None:
        self.mgr._page = None
        with self.assertRaises(CrmSessionError):
            self.mgr.read_ticket("https://crm.astana-motors.kz/0/Nui/ViewModule.aspx#CardModuleV2/x")

    def test_read_ticket_raises_when_status_field_missing(self) -> None:
        self.mgr._page = _FakePage(url="https://crm.astana-motors.kz/", field_values={})
        with self.assertRaises(CrmSessionError):
            self.mgr.read_ticket("https://crm.astana-motors.kz/0/Nui/ViewModule.aspx#CardModuleV2/x")

    def test_read_ticket_returns_all_fields(self) -> None:
        self.mgr._page = _FakePage(
            url="https://crm.astana-motors.kz/",
            field_values={
                "TsiOfficeNoteState": "На согласовании",
                "TsiRMOrgEmployeeFullName": "Иванов И.И.",
                "BnzEmployeeAccount": "i.ivanov",
                "BnzInformationSystem": "Webitel",
                "BnzComment": "some comment",
            },
        )
        result = self.mgr.read_ticket("https://crm.astana-motors.kz/0/Nui/ViewModule.aspx#CardModuleV2/x")
        self.assertEqual(
            result,
            {
                "status": "На согласовании",
                "employee_full_name": "Иванов И.И.",
                "employee_account": "i.ivanov",
                "information_system": "Webitel",
                "comment": "some comment",
            },
        )


class TestListPendingTickets(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mgr = CrmSessionManager(base_url="https://crm.astana-motors.kz", login="x", password="y")

    def test_raises_without_page(self) -> None:
        self.mgr._page = None
        with self.assertRaises(CrmSessionError):
            self.mgr.list_pending_tickets("owner-1")

    def test_raises_on_non_200_odata_response(self) -> None:
        self.mgr._page = _FakePage(
            url="https://crm.astana-motors.kz/",
            evaluate_result={"status": 500, "body": "{}"},
        )
        with self.assertRaises(CrmSessionError):
            self.mgr.list_pending_tickets("owner-1")

    def test_filters_out_non_access_note_subtypes(self) -> None:
        """The server-side filter already scopes results to this owner's
        pending, non-canceled TsiOfficeNotes tasks — the one thing still
        checked client-side is the "Доступ к ИС" subtype marker, since no
        navigation-property name is known for it (see module docstring)."""

        import json as _json

        rows = [
            {
                "Name": "KST-HPK-9422 Доступ к ИС",
                "EntityId": "8ffb6dbc-1b24-46cf-ba1d-3fa6b95765ea",
                "TypeColumnValue": "c9870a37-444c-4655-a68c-cb7a1915ff71",
                "TaskCreatedOn": "2026-09-17T17:26:16Z",
            },
            {  # wrong subtype (not an ISAccessPage note): excluded
                "Name": "AST-HNPA-6 Организация рабочего места",
                "EntityId": "id-4",
                "TypeColumnValue": "some-other-subtype",
                "TaskCreatedOn": "2026-09-17T17:26:16Z",
            },
        ]
        self.mgr._page = _FakePage(
            url="https://crm.astana-motors.kz/",
            evaluate_result={"status": 200, "body": _json.dumps({"value": rows})},
        )

        tickets = self.mgr.list_pending_tickets("owner-1")

        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["name"], "KST-HPK-9422 Доступ к ИС")
        self.assertEqual(
            tickets[0]["url"],
            "https://crm.astana-motors.kz/0/Nui/ViewModule.aspx#CardModuleV2/TsiOfficeNotesISAccessPage/edit/8ffb6dbc-1b24-46cf-ba1d-3fa6b95765ea",
        )

    def test_odata_query_is_scoped_server_side_to_this_owner(self) -> None:
        """Regression guard: the request must be scoped to this specific
        operator server-side (via 'VisaOwner/Id eq ...' — the flat
        'VisaOwnerId eq ...' form is rejected by Creatio, confirmed
        2026-09-21) — never a broad, unscoped company-wide pull filtered
        down after the fact."""

        import json as _json

        page = _FakePage(
            url="https://crm.astana-motors.kz/",
            evaluate_result={"status": 200, "body": _json.dumps({"value": []})},
        )
        self.mgr._page = page

        self.mgr.list_pending_tickets("owner-123")

        (requested_url,) = page.evaluate_calls
        self.assertIn("VisaOwner/Id eq owner-123", requested_url)
        self.assertIn(f"Status/Id eq {_APPROVAL_STATUS_PENDING}", requested_url)
        self.assertIn("IsCanceled", requested_url)
        self.assertIn("ReferenceSchemaName", requested_url)
        self.assertNotIn("VisaOwnerId eq", requested_url)
        self.assertNotIn("StatusId eq", requested_url)

    def test_without_explicit_owner_lists_my_approvals_and_my_roles(self) -> None:
        """Real finding 2026-09-28: the hand-found Visa Owner Id was the
        role "Сетевой инженер", not the operator's own account — approvals
        go to users and to roles, so the default must cover both."""

        import json as _json

        class _RolesPage(_FakePage):
            def evaluate(self, script: str, arg: object = None) -> object:
                if script == "location.href":
                    return self.url
                if "SysAdminUnitInRole" in script:
                    roles = [{"SysAdminUnitRoleId": "role-network"}, {"SysAdminUnitRoleId": "role-it"}]
                    return {"user": "user-me", "status": 200, "body": _json.dumps({"value": roles})}
                self.evaluate_calls.append(arg)
                return {"status": 200, "body": _json.dumps({"value": []})}

        page = _RolesPage(url="https://crm.astana-motors.kz/")
        self.mgr._page = page

        self.mgr.list_pending_tickets()

        (requested_url,) = page.evaluate_calls
        self.assertIn(
            "(VisaOwner/Id eq user-me or VisaOwner/Id eq role-network or VisaOwner/Id eq role-it)", requested_url
        )
        self.assertIn(f"Status/Id eq {_APPROVAL_STATUS_PENDING}", requested_url)


class _CodeField:
    """Records what submit_code does to the CRM's code field and button."""

    def __init__(self, log: list, present: bool = True, name: str = "field") -> None:
        self._log, self._present, self._name = log, present, name

    @property
    def first(self) -> "_CodeField":
        return self

    def count(self) -> int:
        return 1 if self._present else 0

    def fill(self, value: str, timeout: int = 0) -> None:
        self._log.append(("fill", self._name, value))

    def press(self, key: str) -> None:
        self._log.append(("press", self._name, key))

    def click(self, timeout: int = 0) -> None:
        self._log.append(("click", self._name))

    def filter(self, has_text: object = None) -> "_CodeField":
        return self


class _CodePage(_FakePage):
    def __init__(self, *, has_field: bool = True, has_button: bool = True) -> None:
        super().__init__(url="https://crm.astana-motors.kz/Login/NuiLogin.aspx")
        self.log: list = []
        self._has_field, self._has_button = has_field, has_button

    def locator(self, selector: str) -> _CodeField:  # type: ignore[override]
        if selector.startswith("input:not(#loginEdit-el)"):
            return _CodeField(self.log, self._has_field, "field")
        return _CodeField(self.log, self._has_button, "button")


class TestConfirmationCode(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mgr = CrmSessionManager(base_url="https://crm.astana-motors.kz")

    def test_code_is_typed_into_the_crm_field_and_confirmed(self) -> None:
        page = _CodePage()
        self.mgr._page = page
        self.mgr.submit_code("123456")
        self.assertEqual(page.log, [("fill", "field", "123456"), ("click", "button")])

    def test_enter_is_used_when_there_is_no_confirm_button(self) -> None:
        page = _CodePage(has_button=False)
        self.mgr._page = page
        self.mgr.submit_code("123456")
        self.assertEqual(page.log[-1], ("press", "field", "Enter"))

    def test_no_code_field_is_an_error_not_a_blind_submit(self) -> None:
        page = _CodePage(has_field=False)
        self.mgr._page = page
        with self.assertRaises(CrmSessionError):
            self.mgr.submit_code("123456")
        self.assertEqual(page.log, [])

    def test_submit_without_a_login_in_progress_is_an_error(self) -> None:
        self.mgr._page = None
        with self.assertRaises(CrmSessionError):
            self.mgr.submit_code("123456")

    def test_progress_reports_the_code_prompt(self) -> None:
        class _PromptPage(_FakePage):
            def evaluate(self, script: str, arg: object = None) -> object:
                return {"code_required": True, "error_text": ""}

        self.mgr._page = _PromptPage(url="https://crm.astana-motors.kz/Login/NuiLogin.aspx")
        self.assertEqual(self.mgr.login_progress(), {"code_required": True, "error_text": ""})

    def test_progress_without_a_page_asks_for_nothing(self) -> None:
        self.mgr._page = None
        self.assertEqual(self.mgr.login_progress(), {"code_required": False, "error_text": "", "logged_out": False})


class TestApprovals(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mgr = CrmSessionManager(base_url="https://crm.astana-motors.kz")

    def setUp(self) -> None:
        self.mgr._approval_source = None

    def _rows_page(self, rows: list[dict], source: dict | None, status: int = 200) -> _FakePage:
        import json as _json

        return _FakePage(
            url="https://crm.astana-motors.kz/",
            evaluate_result={"source": source, "status": status, "body": _json.dumps({"value": rows})},
        )

    def test_steps_are_ordered_and_use_expanded_names(self) -> None:
        rows = [
            {"BnzPosition": 7, "VisaOwner": {"Name": "Сетевой инженер"}, "Status": {"Name": "Не начато"},
             "SetBy": None, "BnzVisaType": {"Name": "К исполнению"}, "Comment": "", "BnzIsCurrentGroup": True},
            {"BnzPosition": 6, "VisaOwner": {"Name": "Системный администратор Mycar Almaty"},
             "Status": {"Name": "Выполнено с примечаниями"}, "SetBy": {"Name": "Danial Butanbayev"},
             "Comment": "  УЗ reception.hyundai номера 19400 и 19403.  ", "BnzIsCurrentGroup": False},
        ]
        source = {"entity": "SysApproval", "has_canceled": True, "has_visa_type": True}
        self.mgr._page = self._rows_page(rows, source)

        result = self.mgr.read_approvals("f1c2d8c0-87c8-4e80-832e-cb87e75e0e22")

        self.assertEqual(result["source"], "SysApproval")
        first, second = result["steps"]
        self.assertEqual((first["position"], first["owner"], first["set_by"]), (6, "Системный администратор Mycar Almaty", "Danial Butanbayev"))
        self.assertEqual(first["comment"], "УЗ reception.hyundai номера 19400 и 19403.")
        self.assertEqual((second["visa_type"], second["set_by"], second["is_current"]), ("К исполнению", "", True))

    def test_brmacro_in_comments_becomes_a_line_break(self) -> None:
        rows = [{"BnzPosition": 6, "Comment": "УЗ reception.hyundai номера 19400 и 19403.[BRMACRO]Прошу 19403 исключить"}]
        self.mgr._page = self._rows_page(rows, {"entity": "SysApproval", "has_canceled": True, "has_visa_type": True})
        (step,) = self.mgr.read_approvals("id-1")["steps"]
        self.assertEqual(step["comment"], "УЗ reception.hyundai номера 19400 и 19403.\nПрошу 19403 исключить")

    def test_only_the_latest_approval_round_is_shown(self) -> None:
        rows = [
            {"BnzPosition": 1, "BnzCycle": 0, "VisaOwner": {"Name": "old round"}},
            {"BnzPosition": 1, "BnzCycle": 1, "VisaOwner": {"Name": "new round"}},
            {"BnzPosition": 2, "BnzCycle": 1, "VisaOwner": {"Name": "new round 2"}},
        ]
        source = {"entity": "SysApproval", "has_canceled": True, "has_visa_type": True}
        self.mgr._page = self._rows_page(rows, source)
        owners = [step["owner"] for step in self.mgr.read_approvals("id-1")["steps"]]
        self.assertEqual(owners, ["new round", "new round 2"])

    def test_prefers_the_base_approvals_table(self) -> None:
        page = self._rows_page([], {"entity": "SysApproval", "has_canceled": True, "has_visa_type": True})
        self.mgr._page = page
        self.mgr.read_approvals("id-1")
        self.assertEqual(page.evaluate_calls[0]["tables"], ["SysApproval", "BnzVwAllApproval"])

    def test_found_table_is_remembered_for_the_next_ticket(self) -> None:
        source = {"entity": "SysApproval", "has_canceled": True, "has_visa_type": True}
        page = self._rows_page([], source)
        self.mgr._page = page
        self.mgr.read_approvals("id-1")
        self.mgr.read_approvals("id-2")
        self.assertIsNone(page.evaluate_calls[0]["source"])
        self.assertEqual(page.evaluate_calls[1]["source"], source)

    def test_missing_table_is_an_error(self) -> None:
        self.mgr._page = self._rows_page([], None)
        with self.assertRaises(CrmSessionError):
            self.mgr.read_approvals("id-1")

    def test_failed_request_is_an_error_and_not_remembered(self) -> None:
        source = {"entity": "X", "has_canceled": False, "has_visa_type": False}
        self.mgr._page = self._rows_page([], source, status=400)
        with self.assertRaises(CrmSessionError):
            self.mgr.read_approvals("id-1")
        self.assertIsNone(self.mgr._approval_source)


class TestExpiredSession(unittest.TestCase):
    """Seen 2026-09-28: the CRM logged the (headless) browser out and the
    console only reported a cryptic "Could not read ticket" error."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mgr = CrmSessionManager(base_url="https://crm.astana-motors.kz")

    def test_ticket_on_the_login_page_means_the_session_expired(self) -> None:
        class _RedirectsToLogin(_FakePage):
            def goto(self, url: str, wait_until: str | None = None) -> None:
                pass  # the real CRM bounces a logged-out browser back to /Login/

        self.mgr._page = _RedirectsToLogin(url="https://crm.astana-motors.kz/Login/NuiLogin.aspx?ReturnUrl=%2f")
        with self.assertRaises(CrmSessionExpired):
            self.mgr.read_ticket("https://crm.astana-motors.kz/0/Nui/ViewModule.aspx#x/edit/1")

    def test_pending_list_on_the_login_page_means_the_session_expired(self) -> None:
        self.mgr._page = _FakePage(url="https://crm.astana-motors.kz/Login/NuiLogin.aspx")
        with self.assertRaises(CrmSessionExpired):
            self.mgr.list_pending_tickets("owner-1")

    def test_odata_answering_with_the_login_html_means_the_session_expired(self) -> None:
        self.mgr._page = _FakePage(
            url="https://crm.astana-motors.kz/0/Nui/ViewModule.aspx",
            evaluate_result={"status": 200, "body": "<!DOCTYPE html><html>login</html>"},
        )
        with self.assertRaises(CrmSessionExpired):
            self.mgr.list_pending_tickets("owner-1")

    def test_expired_is_still_a_crm_session_error(self) -> None:
        self.assertTrue(issubclass(CrmSessionExpired, CrmSessionError))

    def test_headless_browser_presents_a_normal_chrome_user_agent(self) -> None:
        ua = desktop_user_agent("131.0.6778.33")
        self.assertIn("Chrome/131.0.6778.33", ua)
        self.assertNotIn("Headless", ua)


class TestVisaOwnerHelpers(unittest.TestCase):
    def test_single_owner_filter_has_no_parentheses(self) -> None:
        self.assertEqual(_visa_owner_filter(["a"]), "VisaOwner/Id eq a")

    def test_several_owners_are_ored_in_parentheses(self) -> None:
        # Parenthesised so the "and Status/Id eq ..." that follows applies to all of them.
        self.assertEqual(_visa_owner_filter(["a", "b"]), "(VisaOwner/Id eq a or VisaOwner/Id eq b)")

    def test_empty_owner_list_is_an_error_not_an_unscoped_query(self) -> None:
        with self.assertRaises(CrmSessionError):
            _visa_owner_filter([])

    def test_own_ids_are_user_first_then_roles_without_duplicates(self) -> None:
        import json as _json

        roles = [{"SysAdminUnitRoleId": "role-a"}, {"SysAdminUnitRoleId": "user-me"}, {"SysAdminUnitRoleId": "role-a"}]
        page = _FakePage(url="https://crm.astana-motors.kz/", evaluate_result={
            "user": "user-me", "status": 200, "body": _json.dumps({"value": roles}),
        })
        self.assertEqual(_own_visa_owner_ids(page), ["user-me", "role-a"])

    def test_own_ids_fail_loudly_when_shell_has_no_user(self) -> None:
        page = _FakePage(url="https://crm.astana-motors.kz/", evaluate_result={"user": None, "status": 0, "body": ""})
        with self.assertRaises(CrmSessionError):
            _own_visa_owner_ids(page)

    def test_own_ids_fail_loudly_on_role_lookup_error(self) -> None:
        page = _FakePage(url="https://crm.astana-motors.kz/", evaluate_result={"user": "u", "status": 403, "body": "no"})
        with self.assertRaises(CrmSessionError):
            _own_visa_owner_ids(page)


if __name__ == "__main__":
    unittest.main()
