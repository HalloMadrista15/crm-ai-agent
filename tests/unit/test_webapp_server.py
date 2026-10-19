"""Tests the local console server's own routing/validation logic by
actually starting it on 127.0.0.1 with an OS-assigned ephemeral port and
making real local HTTP requests to it. This never touches the network
beyond localhost, never touches CRM/Webitel (endpoints that would need
those are only exercised with inputs that fail validation before reaching
that code, e.g. a missing "url"), and never launches Playwright.
"""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from crm_ai_agent.webapp import server
from crm_ai_agent.webapp.server import Handler, _extract_webitel_logins, _resolve_crm_login, _save_env_updates


class TestConsoleServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _post(self, path: str, body: dict | None = None):
        data = json.dumps(body or {}).encode("utf-8")
        request = urllib.request.Request(
            self._url(path), data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_index_serves_html(self) -> None:
        with urllib.request.urlopen(self._url("/"), timeout=10) as response:
            self.assertEqual(response.status, 200)
            html = response.read().decode("utf-8")
            self.assertIn("Консоль оператора", html)

    def test_unknown_get_path_is_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self._url("/nope"), timeout=10)
        self.assertEqual(ctx.exception.code, 404)

    def test_unknown_post_path_is_404(self) -> None:
        status, payload = self._post("/api/nope", {})
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_ticket_endpoint_requires_url(self) -> None:
        status, payload = self._post("/api/crm/ticket", {})
        self.assertEqual(status, 400)
        self.assertIn("url", payload["error"])

    def test_webitel_plan_endpoint_requires_both_extensions(self) -> None:
        status, payload = self._post("/api/webitel/plan", {"template_extension": "10078"})
        self.assertEqual(status, 400)

    def test_webitel_execute_endpoint_requires_plan(self) -> None:
        status, payload = self._post("/api/webitel/execute", {})
        self.assertEqual(status, 400)

    def test_login_status_endpoint_returns_boolean_ready_field(self) -> None:
        status, payload = self._post("/api/crm/login/status", {})
        self.assertEqual(status, 200)
        self.assertIn("ready", payload)
        self.assertIsInstance(payload["ready"], bool)
        self.assertIsInstance(payload["browser_open"], bool)

    def test_login_endpoint_rejects_half_filled_credentials_before_opening_a_browser(self) -> None:
        status, payload = self._post("/api/crm/login", {"login": "i.ivanov"})
        self.assertEqual(status, 400)
        self.assertIn("пароль", payload["error"])

    def test_credentials_endpoint_never_returns_the_password(self) -> None:
        status, payload = self._post("/api/crm/credentials", {})
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"login", "has_password", "visa_owner_id"})
        self.assertIsInstance(payload["has_password"], bool)


    def test_settings_save_with_emptied_visa_owner_id_switches_to_my_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "info.env"
            path.write_text("CRM_LOGIN=m.muratbay\nCRM_VISA_OWNER_ID=old-guid\n", encoding="utf-8")
            with mock.patch.object(server, "INFO_ENV_PATH", path):
                status, _ = self._post("/api/settings/save", {"crm_visa_owner_id": ""})
            content = path.read_text(encoding="utf-8")
        self.assertEqual(status, 200)
        self.assertNotIn("CRM_VISA_OWNER_ID", content)
        self.assertIn("CRM_LOGIN=m.muratbay", content)

    def test_forget_removes_only_the_remembered_crm_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "info.env"
            path.write_text(
                "CRM_BASE_URL=https://crm.example.invalid\nCRM_LOGIN=m.muratbay\nCRM_PASSWORD=pw\n"
                "CRM_VISA_OWNER_ID=role-guid\nWEBITEL_LOGIN=10078\n",
                encoding="utf-8",
            )
            with mock.patch.object(server, "INFO_ENV_PATH", path):
                status, payload = self._post("/api/crm/credentials/forget", {})
            content = path.read_text(encoding="utf-8")
        self.assertEqual((status, payload), (200, {"forgotten": True}))
        for gone in ("CRM_LOGIN", "CRM_PASSWORD", "CRM_VISA_OWNER_ID"):
            self.assertNotIn(gone, content)
        self.assertIn("CRM_BASE_URL=https://crm.example.invalid", content)
        self.assertIn("WEBITEL_LOGIN=10078", content)

    def test_expired_crm_session_is_a_401_the_page_can_act_on(self) -> None:
        from crm_ai_agent.webapp.crm_session import CrmSessionExpired

        class _Session:
            def read_ticket(self, url: str) -> dict:
                raise CrmSessionExpired()

        with mock.patch.object(server, "_get_crm_session", return_value=_Session()):
            status, payload = self._post("/api/crm/ticket", {"url": "https://crm.example.invalid/0/x/edit/1"})
        self.assertEqual(status, 401)
        self.assertIs(payload["crm_session_expired"], True)

    def test_copy_plan_carries_the_reset_password_checkbox(self) -> None:
        """Bug found 2026-09-28: the "Также сбросить пароль" checkbox was
        never sent, so execute_plan never reset anything."""

        class _Actions:
            def build_plan(self, **_: str) -> dict:
                return {"target_id": "1", "roles": [], "license": [], "group": ""}

        body = {"template_extension": "10078", "target_extension": "10087", "city": "almaty"}
        with mock.patch.object(server, "_get_webitel_actions", return_value=_Actions()):
            _, ticked = self._post("/api/webitel/plan", {**body, "reset_password": True})
            _, unticked = self._post("/api/webitel/plan", body)
            _, stringy = self._post("/api/webitel/plan", {**body, "reset_password": "true"})
        self.assertIs(ticked["reset_password"], True)
        self.assertIs(unticked["reset_password"], False)
        self.assertIs(stringy["reset_password"], False)

    def test_copy_plan_carries_the_permissions_checkbox(self) -> None:
        seen: list[bool] = []

        class _Actions:
            def build_plan(self, *, copy_permissions: bool = False, **_: str) -> dict:
                seen.append(copy_permissions)
                return {"target_id": "1", "roles": [], "license": [], "group": ""}

        body = {"template_extension": "10078", "target_extension": "10087", "city": "almaty"}
        with mock.patch.object(server, "_get_webitel_actions", return_value=_Actions()):
            self._post("/api/webitel/plan", {**body, "copy_permissions": True})
            self._post("/api/webitel/plan", body)
            self._post("/api/webitel/plan", {**body, "copy_permissions": "true"})
        self.assertEqual(seen, [True, False, False])

    def test_create_plan_carries_the_permissions_checkbox(self) -> None:
        seen: list[bool] = []

        class _Actions:
            def build_create_plan(self, *, copy_permissions: bool = False, **_: str) -> dict:
                seen.append(copy_permissions)
                return {"new_username": "10087", "roles": [], "license": [], "group": ""}

        body = {"template_extension": "10078", "new_username": "10087", "new_name": "N", "new_extension": "10087", "city": "almaty"}
        with mock.patch.object(server, "_get_webitel_actions", return_value=_Actions()):
            self._post("/api/webitel/create_plan", {**body, "copy_permissions": True})
            self._post("/api/webitel/create_plan", body)
        self.assertEqual(seen, [True, False])

    def test_confirmation_code_endpoint_rejects_non_digits_before_touching_the_crm(self) -> None:
        status, payload = self._post("/api/crm/login/code", {"code": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("6 цифр", payload["error"])

    def test_webitel_credentials_save_uses_each_citys_own_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "info.env"
            env.write_text("WEBITEL_DOMAIN=mycar-almaty.example.invalid\n", encoding="utf-8")
            with mock.patch.object(server, "INFO_ENV_PATH", env):
                self._post("/api/webitel/credentials/save", {"city": "almaty", "login": "10078", "password": "pw-a"})
                self._post("/api/webitel/credentials/save", {"city": "astana", "login": "m.muratbay", "password": "pw-b"})
                status, payload = self._post("/api/webitel/credentials/save", {"city": "astana", "login": "x"})
            content = env.read_text(encoding="utf-8")
        for line in ("WEBITEL_LOGIN=10078", "WEBITEL_PASSWORD=pw-a", "WEBITEL_ASTANA_LOGIN=m.muratbay",
                     "WEBITEL_ASTANA_PASSWORD=pw-b", "WEBITEL_DOMAIN=mycar-almaty.example.invalid"):
            self.assertIn(line, content)
        self.assertEqual(status, 400)
        self.assertIn("пароль", payload["error"])

    def test_webitel_credentials_forget_removes_login_password_and_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "info.env"
            env.write_text("WEBITEL_LOGIN=10078\nWEBITEL_PASSWORD=pw\nWEBITEL_ASTANA_LOGIN=keep\nWEBITEL_ALMATY_BASE_URL=https://a\n", encoding="utf-8")
            paths = {"almaty": Path(tmp) / "a.json", "astana": Path(tmp) / "b.json"}
            paths["almaty"].write_text("{}", encoding="utf-8")
            paths["astana"].write_text("{}", encoding="utf-8")
            with mock.patch.object(server, "INFO_ENV_PATH", env), mock.patch.dict(server.WEBITEL_STORAGE_STATE_PATHS, paths):
                status, _ = self._post("/api/webitel/credentials/forget", {"city": "almaty"})
            content = env.read_text(encoding="utf-8")
            almaty_session_left, astana_session_left = paths["almaty"].exists(), paths["astana"].exists()
        self.assertEqual(status, 200)
        self.assertNotIn("WEBITEL_LOGIN=", content)
        self.assertNotIn("WEBITEL_PASSWORD=", content)
        self.assertIn("WEBITEL_ASTANA_LOGIN=keep", content)
        self.assertIn("WEBITEL_ALMATY_BASE_URL=https://a", content)
        self.assertFalse(almaty_session_left)
        self.assertTrue(astana_session_left)

    def test_webitel_session_status_rejects_unknown_city(self) -> None:
        status, _ = self._post("/api/webitel/session_status", {"city": "shymkent"})
        self.assertEqual(status, 400)

    def test_webitel_session_status_without_a_saved_session_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "info.env"
            env.write_text(
                "WEBITEL_ASTANA_BASE_URL=https://wtl-ast.example.invalid\nWEBITEL_ASTANA_LOGIN=m.muratbay\nWEBITEL_ASTANA_PASSWORD=secret-pw\n",
                encoding="utf-8",
            )
            paths = {"almaty": Path(tmp) / "a.json", "astana": Path(tmp) / "b.json"}
            with mock.patch.object(server, "INFO_ENV_PATH", env), mock.patch.dict(server.WEBITEL_STORAGE_STATE_PATHS, paths):
                status, payload = self._post("/api/webitel/session_status", {"city": "astana"})
        self.assertEqual(status, 200)
        self.assertEqual((payload["session"], payload["login"], payload["has_password"]), ("missing", "m.muratbay", True))
        self.assertNotIn("secret-pw", json.dumps(payload))  # only whether one is saved, never the password

    def test_webitel_login_without_saved_credentials_explains_where_to_enter_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "info.env"
            env.write_text("WEBITEL_ASTANA_BASE_URL=https://wtl-ast.example.invalid\nWEBITEL_ASTANA_DOMAIN=d\n", encoding="utf-8")
            with mock.patch.object(server, "INFO_ENV_PATH", env):
                status, payload = self._post("/api/webitel/login", {"city": "astana"})
        self.assertEqual(status, 400)
        self.assertIn("Настройках", payload["error"])

    def test_settings_save_without_visa_owner_field_keeps_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "info.env"
            path.write_text("CRM_VISA_OWNER_ID=old-guid\n", encoding="utf-8")
            with mock.patch.object(server, "INFO_ENV_PATH", path):
                self._post("/api/settings/save", {"webitel_almaty_login": "10078"})
            content = path.read_text(encoding="utf-8")
        self.assertIn("CRM_VISA_OWNER_ID=old-guid", content)
        self.assertIn("WEBITEL_LOGIN=10078", content)


class _FakeCrm:
    """Stands in for CrmSessionManager on the shared console: no browser,
    the test decides when the CRM asks for a code or accepts the login."""

    instances: list["_FakeCrm"] = []

    def __init__(self, **_: object) -> None:
        self.logins: list[tuple[str | None, str | None]] = []
        self.codes: list[str] = []
        self.logged_in = False
        self.code_required = False
        self.closed = False
        _FakeCrm.instances.append(self)

    @property
    def login(self) -> str:
        return (self.logins[-1][0] or "") if self.logins else ""

    def start_login(self, login: str | None = None, password: str | None = None) -> None:
        self.logins.append((login, password))

    def is_logged_in(self) -> bool:
        return self.logged_in

    def has_open_page(self) -> bool:
        return not self.closed

    def login_progress(self) -> dict:
        return {"code_required": self.code_required, "error_text": "", "logged_out": False}

    def submit_code(self, code: str) -> None:
        self.codes.append(code)
        self.logged_in = True

    def close(self) -> None:
        self.closed = True


class TestSharedConsole(unittest.TestCase):
    """CONSOLE_SHARED=1: the same console for everyone, but each browser has
    its own workspace (cookie) with its own logins, sessions and CRM browser.
    Real local HTTP, fake CRM browsers, temp files only."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env_path = self.root / "info.env"
        # Logins left in the shared info.env from its single-operator days
        # must never reach any browser.
        self.env_path.write_text(
            "CRM_BASE_URL=https://crm.example.invalid\nWEBITEL_ALMATY_BASE_URL=https://wtl-alm.example.invalid\n"
            "CRM_LOGIN=old.owner\nCRM_PASSWORD=old-pw\nWEBITEL_LOGIN=10078\nWEBITEL_PASSWORD=shared-secret\n",
            encoding="utf-8",
        )
        _FakeCrm.instances = []
        for patcher in (
            mock.patch.object(server, "INFO_ENV_PATH", self.env_path),
            mock.patch.object(server, "OPERATORS_DIR", self.root / "operators"),
            mock.patch.object(server, "AUDIT_LOG_PATH", self.root / "audit.jsonl"),
            mock.patch.object(server, "_SHARED", True),
            mock.patch.object(server, "CrmSessionManager", _FakeCrm),
            mock.patch.dict(server._crm_sessions, clear=True),
            mock.patch.dict(server._webitel_login_jobs, clear=True),
            mock.patch.dict(server._workspace_seen, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _open_page(self, cookie: str = ""):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/", headers={"Cookie": cookie} if cookie else {})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read().decode("utf-8"), response.headers.get("Set-Cookie", "")

    def _new_browser(self) -> str:
        return self._open_page()[1].split(";", 1)[0]

    def _post(self, path: str, body: dict | None = None, cookie: str = "", headers: dict | None = None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body or {}).encode("utf-8"),
            headers={"Content-Type": "application/json", **({"Cookie": cookie} if cookie else {}), **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _audit_lines(self) -> list[dict]:
        path = self.root / "audit.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_opening_the_page_gives_the_browser_its_own_workspace(self) -> None:
        html, set_cookie = self._open_page()
        self.assertIn("Консоль на сервере", html)
        for attribute in ("console_workspace=", "HttpOnly", "SameSite=Lax", "Max-Age="):
            self.assertIn(attribute, set_cookie)
        cookie = set_cookie.split(";", 1)[0]
        token = cookie.split("=", 1)[1]
        self.assertEqual(self._open_page(cookie)[1], "")  # a known browser keeps its workspace
        workspaces = [d.name for d in (self.root / "operators").iterdir()]
        self.assertEqual(workspaces, [server._workspace_id(token)])
        self.assertNotIn(token, workspaces[0])  # the cookie itself is never on disk

    def test_api_without_a_workspace_starts_nothing(self) -> None:
        for cookie in ("", "console_workspace=../../etc"):
            status, payload = self._post("/api/crm/login/status", cookie=cookie)
            self.assertEqual(status, 400)
            self.assertIn("Обновите страницу", payload["error"])
        self.assertEqual(_FakeCrm.instances, [])

    def test_browsers_never_see_each_others_logins(self) -> None:
        first, second = self._new_browser(), self._new_browser()
        self._post("/api/webitel/credentials/save", {"city": "almaty", "login": "20001", "password": "a-pw"}, cookie=first)
        self._post("/api/crm/login", {"login": "m.muratbay", "password": "pw", "remember": True}, cookie=first)
        _, mine = self._post("/api/webitel/session_status", {"city": "almaty"}, cookie=first)
        _, theirs = self._post("/api/webitel/session_status", {"city": "almaty"}, cookie=second)
        _, my_crm = self._post("/api/crm/credentials", cookie=first)
        _, their_crm = self._post("/api/crm/credentials", cookie=second)
        self.assertEqual((mine["login"], mine["has_password"]), ("20001", True))
        self.assertEqual((theirs["login"], theirs["has_password"]), ("", False))
        self.assertEqual((my_crm["login"], my_crm["has_password"]), ("m.muratbay", True))
        self.assertEqual((their_crm["login"], their_crm["has_password"]), ("", False))

    def test_each_browser_has_its_own_crm_browser(self) -> None:
        first, second = self._new_browser(), self._new_browser()
        self._post("/api/crm/login", {"login": "m.muratbay", "password": "pw"}, cookie=first)
        self._post("/api/crm/login", {"login": "i.ivanov", "password": "pw2"}, cookie=second)
        self.assertEqual([crm.logins for crm in _FakeCrm.instances], [[("m.muratbay", "pw")], [("i.ivanov", "pw2")]])
        _FakeCrm.instances[0].logged_in = True
        self.assertIs(self._post("/api/crm/login/status", cookie=first)[1]["ready"], True)
        self.assertIs(self._post("/api/crm/login/status", cookie=second)[1]["ready"], False)

    def test_cross_site_requests_are_refused(self) -> None:
        cookie = self._new_browser()
        status, _ = self._post("/api/crm/credentials", cookie=cookie, headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self._post("/api/crm/credentials", cookie=cookie, headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 403)
        status, _ = self._post("/api/crm/credentials", cookie=cookie, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_an_idle_workspace_gets_its_crm_browser_closed(self) -> None:
        idle, active = self._new_browser(), self._new_browser()
        self._post("/api/crm/login", {"login": "m.muratbay", "password": "pw"}, cookie=idle)
        crm = _FakeCrm.instances[-1]
        workspace = server._workspace_id(idle.split("=", 1)[1])
        server._workspace_seen[workspace] = time.time() - server._WORKSPACE_IDLE_SECONDS - 1
        self._post("/api/crm/credentials", cookie=active)
        for _ in range(50):
            if crm.closed:
                break
            time.sleep(0.02)
        self.assertTrue(crm.closed)
        self.assertNotIn(workspace, server._crm_sessions)

    def test_webitel_changes_are_audited_without_secrets(self) -> None:
        cookie = self._new_browser()
        self._post("/api/webitel/credentials/save", {"city": "almaty", "login": "20001", "password": "a-pw"}, cookie=cookie)
        self._post("/api/crm/login", {"login": "m.muratbay", "password": "pw"}, cookie=cookie)

        class _Actions:
            def execute_create_plan(self, plan: dict) -> dict:
                return {"created": True, "temporary_password": "S3cret-pw"}

        plan = {"city": "almaty", "new_username": "10087", "new_name": "Иван Иванов", "new_extension": "10087",
                "template_extension": "10078", "roles": [{"name": "agent"}]}
        with mock.patch.object(server, "_get_webitel_actions", return_value=_Actions()):
            status, result = self._post("/api/webitel/create_execute", {"plan": plan}, cookie=cookie)
        self.assertEqual((status, result["temporary_password"]), (200, "S3cret-pw"))
        entry = self._audit_lines()[-1]
        self.assertEqual(
            (entry["crm_login"], entry["webitel_login"], entry["action"], entry["new_extension"], entry["ok"]),
            ("m.muratbay", "20001", "/api/webitel/create_execute", "10087", True),
        )
        self.assertEqual(len(entry["workspace"]), 8)
        self.assertNotIn("S3cret-pw", (self.root / "audit.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn("roles", entry)


class TestSharedConsoleConfig(unittest.TestCase):
    def test_never_listens_on_the_network_unless_shared_and_https(self) -> None:
        self.assertNotEqual(server._exposure_problem("0.0.0.0", False, True), "")
        self.assertNotEqual(server._exposure_problem("0.0.0.0", True, False), "")
        self.assertEqual(server._exposure_problem("0.0.0.0", True, True), "")
        self.assertEqual(server._exposure_problem("127.0.0.1", False, False), "")


class TestResolveCrmLogin(unittest.TestCase):
    """Runs against a temp info.env, never the real project one."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.env_path = Path(self._tmp.name) / "info.env"
        patcher = mock.patch.object(server, "INFO_ENV_PATH", self.env_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_typed_credentials_with_remember_are_saved(self) -> None:
        self.assertEqual(
            _resolve_crm_login({"login": "i.ivanov", "password": "hunter2", "remember": True}), ("i.ivanov", "hunter2")
        )
        content = self.env_path.read_text(encoding="utf-8")
        self.assertIn("CRM_LOGIN=i.ivanov", content)
        self.assertIn("CRM_PASSWORD=hunter2", content)

    def test_typed_credentials_without_remember_are_not_saved(self) -> None:
        self.env_path.write_text("CRM_LOGIN=m.muratbay\nCRM_PASSWORD=old\n", encoding="utf-8")
        self.assertEqual(_resolve_crm_login({"login": "i.ivanov", "password": "hunter2"}), ("i.ivanov", "hunter2"))
        content = self.env_path.read_text(encoding="utf-8")
        self.assertIn("CRM_LOGIN=m.muratbay", content)
        self.assertNotIn("hunter2", content)

    def test_empty_request_uses_remembered_credentials(self) -> None:
        self.env_path.write_text("CRM_LOGIN=m.muratbay\nCRM_PASSWORD=secret\n", encoding="utf-8")
        self.assertEqual(_resolve_crm_login({}), ("m.muratbay", "secret"))

    def test_empty_request_without_remembered_credentials_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _resolve_crm_login({})

    def test_login_without_password_is_rejected_and_not_saved(self) -> None:
        with self.assertRaises(ValueError):
            _resolve_crm_login({"login": "i.ivanov", "remember": True})
        self.assertFalse(self.env_path.exists())

    def test_switching_remembered_account_drops_the_old_visa_owner_id(self) -> None:
        self.env_path.write_text(
            "CRM_BASE_URL=https://crm.example.invalid\nCRM_LOGIN=m.muratbay\nCRM_PASSWORD=old\nCRM_VISA_OWNER_ID=old-guid\n",
            encoding="utf-8",
        )
        _resolve_crm_login({"login": "i.ivanov", "password": "hunter2", "remember": True})
        content = self.env_path.read_text(encoding="utf-8")
        self.assertNotIn("CRM_VISA_OWNER_ID", content)
        self.assertIn("CRM_BASE_URL=https://crm.example.invalid", content)
        self.assertIn("CRM_LOGIN=i.ivanov", content)

    def test_first_remembered_login_keeps_an_existing_visa_owner_id(self) -> None:
        self.env_path.write_text("CRM_VISA_OWNER_ID=my-guid\n", encoding="utf-8")
        _resolve_crm_login({"login": "m.muratbay", "password": "pw", "remember": True})
        self.assertIn("CRM_VISA_OWNER_ID=my-guid", self.env_path.read_text(encoding="utf-8"))

    def test_same_account_keeps_its_visa_owner_id(self) -> None:
        self.env_path.write_text("CRM_LOGIN=m.muratbay\nCRM_PASSWORD=old\nCRM_VISA_OWNER_ID=my-guid\n", encoding="utf-8")
        _resolve_crm_login({"login": "m.muratbay", "password": "new", "remember": True})
        content = self.env_path.read_text(encoding="utf-8")
        self.assertIn("CRM_VISA_OWNER_ID=my-guid", content)
        self.assertIn("CRM_PASSWORD=new", content)


class TestSaveEnvUpdates(unittest.TestCase):
    """Never exercised through the HTTP server in these tests — that would
    write to the real project info.env (the server hardcodes that path).
    Tests the pure function directly against a temp file instead."""

    def test_writes_whitelisted_keys_to_a_fresh_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "info.env"
            _save_env_updates(path, {"CRM_LOGIN": "i.ivanov", "CRM_PASSWORD": "hunter2"})
            content = path.read_text(encoding="utf-8")
            self.assertIn("CRM_LOGIN=i.ivanov", content)
            self.assertIn("CRM_PASSWORD=hunter2", content)

    def test_empty_value_does_not_overwrite_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "info.env"
            path.write_text("CRM_LOGIN=original\n", encoding="utf-8")
            _save_env_updates(path, {"CRM_LOGIN": "", "CRM_PASSWORD": "new-pass"})
            content = path.read_text(encoding="utf-8")
            self.assertIn("CRM_LOGIN=original", content)
            self.assertIn("CRM_PASSWORD=new-pass", content)

    def test_non_whitelisted_keys_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "info.env"
            path.write_text("WEBITEL_ALMATY_BASE_URL=https://wtl-alm.example.invalid\n", encoding="utf-8")
            _save_env_updates(path, {"WEBITEL_ALMATY_BASE_URL": "https://evil.invalid", "CRM_LOGIN": "i.ivanov"})
            content = path.read_text(encoding="utf-8")
            # Shared infra config is untouched by this endpoint even if a
            # caller tries to pass it — only _SETTINGS_KEYS are writable.
            self.assertIn("WEBITEL_ALMATY_BASE_URL=https://wtl-alm.example.invalid", content)
            self.assertIn("CRM_LOGIN=i.ivanov", content)

    def test_preserves_unrelated_existing_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "info.env"
            path.write_text("CRM_BASE_URL=https://crm.example.invalid\nWEBITEL_DOMAIN=example.invalid\n", encoding="utf-8")
            _save_env_updates(path, {"CRM_LOGIN": "i.ivanov"})
            content = path.read_text(encoding="utf-8")
            self.assertIn("CRM_BASE_URL=https://crm.example.invalid", content)
            self.assertIn("WEBITEL_DOMAIN=example.invalid", content)
            self.assertIn("CRM_LOGIN=i.ivanov", content)


class TestConfirmationCodeFormat(unittest.TestCase):
    def test_digits_with_pasted_spaces_or_dashes_are_accepted(self) -> None:
        self.assertEqual(server._normalize_confirmation_code("123 456"), "123456")
        self.assertEqual(server._normalize_confirmation_code("12-34-56"), "123456")

    def test_anything_else_is_rejected(self) -> None:
        for bad in ("", None, "abc123", "12", "12345", "1234567", "12345678901"):
            with self.assertRaises(ValueError):
                server._normalize_confirmation_code(bad)


class TestOperatorErrorMessage(unittest.TestCase):
    def test_expired_webitel_session_says_where_to_log_in(self) -> None:
        from crm_ai_agent.adapters.webitel_api.actions import WebitelApiError
        from crm_ai_agent.adapters.webitel_api.session import WebitelSessionError

        for exc in (WebitelApiError("GET /api/users failed: HTTP 401"), WebitelSessionError("no session")):
            self.assertIn("Вход в Webitel", server._operator_error_message(exc))

    def test_other_errors_are_left_alone(self) -> None:
        from crm_ai_agent.webapp.crm_session import CrmSessionError

        exc = CrmSessionError("CRM OData request failed: HTTP 401")
        self.assertEqual(server._operator_error_message(exc), "CRM OData request failed: HTTP 401")


class TestTicketEntityId(unittest.TestCase):
    def test_reads_the_guid_from_a_real_card_link(self) -> None:
        url = "https://crm.astana-motors.kz//0/Nui/ViewModule.aspx#CardModuleV2/TsiOfficeNotesISAccessPage/edit/F1C2D8C0-87c8-4e80-832e-cb87e75e0e22"
        self.assertEqual(server._ticket_entity_id(url), "f1c2d8c0-87c8-4e80-832e-cb87e75e0e22")

    def test_no_guid_means_none(self) -> None:
        self.assertIsNone(server._ticket_entity_id("https://crm.astana-motors.kz/0/Nui/ViewModule.aspx#HomePage"))


class TestExtractWebitelLogins(unittest.TestCase):
    def test_numbers_from_an_approval_comment(self) -> None:
        comment = "УЗ reception.hyundai номера 19400 и 19403. Прошу 19403 исключить из группового номера 19401"
        self.assertEqual(_extract_webitel_logins(comment), ["19400", "19403", "19401"])

    def test_real_ticket_comment_yields_the_number_not_the_ad_account(self) -> None:
        comment = (
            "В связи с рабочей необходимостью просим вас подключить и настроить номер - 22104 "
            "на нового абонента Webitel, и так же назначить лицензию CallCentr что бы данный "
            "сотрудник мог звонить напрямую из CRM. Логин - 22104@mycar.astana-motors.kz"
        )
        self.assertEqual(_extract_webitel_logins(comment), ["22104"])

    def test_login_at_domain_without_label(self) -> None:
        self.assertEqual(_extract_webitel_logins("Прошу доступ для 10087@mycar.astana-motors.kz"), ["10087"])

    def test_labelled_list_of_several_numbers(self) -> None:
        self.assertEqual(_extract_webitel_logins("Номера: 10087, 10088 и 10089"), ["10087", "10088", "10089"])

    def test_ignores_unlabelled_numbers_and_phone_numbers(self) -> None:
        self.assertEqual(_extract_webitel_logins("Кабинет 305, тел. +7 701 123 45 67, приказ от 25.09.2026"), [])

    def test_long_phone_number_after_label_is_not_truncated_into_a_login(self) -> None:
        self.assertEqual(_extract_webitel_logins("Номер телефона: 87011234567"), [])
        self.assertEqual(_extract_webitel_logins("номер 87011234567"), [])

    def test_empty_or_missing_comment(self) -> None:
        self.assertEqual(_extract_webitel_logins(""), [])
        self.assertEqual(_extract_webitel_logins(None), [])


if __name__ == "__main__":
    unittest.main()
