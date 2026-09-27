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
    CrmSessionManager,
    _read_field_value,
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

    def evaluate(self, script: str, arg: object = None) -> object:
        self.evaluate_calls.append(arg)
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


if __name__ == "__main__":
    unittest.main()
