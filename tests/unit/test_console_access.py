"""Sign-in with AD and the list of those let in: the pieces by themselves, then
the shared console with them over real local HTTP. The domain is never asked —
check_password is replaced, or only its subprocess plumbing runs."""

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from crm_ai_agent.webapp import console_access, server
from crm_ai_agent.webapp.server import Handler


class TestLogins(unittest.TestCase):
    def test_the_ways_people_write_their_login(self) -> None:
        for raw in ("m.ivanov", " M.Ivanov ", "MK\\m.ivanov", "mk.int\\M.IVANOV", "m.ivanov@mk.int", "M.Ivanov@MK.INT"):
            self.assertEqual(console_access.normalize_login(raw, "mk.int"), "m.ivanov", raw)

    def test_anything_else_is_not_a_login(self) -> None:
        for raw in ("", " ", "m.ivanov@other.kz", "OTHER\\m.ivanov", "root@", "a b", "../x", "m.ivanov\n", "-x", "a" * 65, "иванов",
                    "m.ivanov@mk.int@mk.int", "x;y", "m.ivanov\0"):
            login = console_access.normalize_login(raw, "mk.int")
            self.assertIn(login, ("", "m.ivanov") if raw.strip() == "m.ivanov" else ("",), raw)

    def test_lists(self) -> None:
        self.assertEqual(console_access.parse_logins("m.ivanov, P.Petrov;s.serov@mk.int  m.ivanov", "mk.int"), ["m.ivanov", "p.petrov", "s.serov"])
        self.assertEqual(console_access.parse_logins("", "mk.int"), [])
        with self.assertRaises(ValueError):
            console_access.parse_logins("m.ivanov, p.petrov@other.kz", "mk.int")


class TestThrottle(unittest.TestCase):
    def test_a_few_failures_and_then_a_wait(self) -> None:
        throttle = console_access.Throttle(limit=3, window=100)
        with mock.patch.object(console_access.time, "time", return_value=1000.0) as clock:
            for _ in range(3):
                self.assertEqual(throttle.wait_seconds("a"), 0)
                throttle.failed("a")
            self.assertEqual(throttle.wait_seconds("a"), 100)
            self.assertEqual(throttle.wait_seconds("b"), 0)
            clock.return_value = 1099.0
            self.assertEqual(throttle.wait_seconds("a"), 1)
            clock.return_value = 1100.0
            self.assertEqual(throttle.wait_seconds("a"), 0)

    def test_a_success_starts_over(self) -> None:
        throttle = console_access.Throttle(limit=2, window=100)
        throttle.failed("a")
        throttle.succeeded("a")
        throttle.failed("a")
        self.assertEqual(throttle.wait_seconds("a"), 0)


class TestSignIns(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / ".auth" / "console_sign_ins.json"

    def test_survives_a_restart_and_keeps_only_hashes(self) -> None:
        token = console_access.SignIns(self.path).start("m.ivanov")
        self.assertNotIn(token, self.path.read_text(encoding="utf-8"))
        again = console_access.SignIns(self.path)
        self.assertEqual(again.login_of(token), "m.ivanov")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_lasts_twelve_hours_and_not_longer(self) -> None:
        sign_ins = console_access.SignIns(self.path)
        with mock.patch.object(console_access.time, "time", return_value=1000.0) as clock:
            token = sign_ins.start("m.ivanov")
            clock.return_value = 1000.0 + 12 * 3600 - 1
            self.assertEqual(sign_ins.login_of(token), "m.ivanov")
            clock.return_value += 1
            self.assertEqual(sign_ins.login_of(token), "")
            sign_ins.start("p.petrov")
            self.assertEqual(self.path.read_text(encoding="utf-8").count("until"), 1)  # the old one is gone from disk

    def test_unknown_and_malformed_cookies(self) -> None:
        sign_ins = console_access.SignIns(self.path)
        for token in ("", "x", "0" * 64, "0" * 63, "../../etc", "G" * 64):
            self.assertEqual(sign_ins.login_of(token), "")

    def test_ending(self) -> None:
        sign_ins = console_access.SignIns(self.path)
        one, other, colleague = sign_ins.start("m.ivanov"), sign_ins.start("m.ivanov"), sign_ins.start("p.petrov")
        sign_ins.end(one)
        self.assertEqual([sign_ins.login_of(t) for t in (one, other, colleague)], ["", "m.ivanov", "p.petrov"])
        sign_ins.end_all("m.ivanov")
        self.assertEqual([console_access.SignIns(self.path).login_of(t) for t in (one, other, colleague)], ["", "", "p.petrov"])

    def test_a_damaged_file_means_everyone_signs_in_again(self) -> None:
        self.path.parent.mkdir()
        for text in ("", "{", "[]", '{"a": 1}', '{"a": {"login": "m.ivanov"}}', "null"):
            self.path.write_text(text, encoding="utf-8")
            sign_ins = console_access.SignIns(self.path)
            self.assertEqual(sign_ins.login_of("0" * 64), "", text)
            self.assertEqual(sign_ins.login_of(sign_ins.start("m.ivanov")), "m.ivanov")


class TestEnvLine(unittest.TestCase):
    def test_only_that_line_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "info.env"
            path.write_text("# Консоль\nA=1\n\nCONSOLE_AD_USERS=old\n# CONSOLE_AD_USERS=commented\nB=x=y\nCONSOLE_AD_USERS=twice\n", encoding="utf-8")
            console_access.set_env_line(path, "CONSOLE_AD_USERS", "a, b")
            self.assertEqual(path.read_text(encoding="utf-8"),
                             "# Консоль\nA=1\n\nCONSOLE_AD_USERS=a, b\n# CONSOLE_AD_USERS=commented\nB=x=y\n")
            console_access.set_env_line(path, "NEW", "1")
            self.assertTrue(path.read_text(encoding="utf-8").endswith("B=x=y\nNEW=1\n"))
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["info.env"])


class TestCheckPassword(unittest.TestCase):
    """Only the plumbing around the subprocess; PAM itself is the server's."""

    def _with_child(self, code: str, password: str = "Pa55 word", timeout: float = 20) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            child = Path(tmp) / "child.py"
            child.write_text("import json, sys, time\nasked = json.loads(sys.stdin.buffer.read())\n" + code, encoding="utf-8")
            with mock.patch.object(console_access, "__file__", str(child)), \
                    mock.patch.object(console_access, "_CHECK_TIMEOUT_SECONDS", timeout):
                return console_access.check_password("m.ivanov", "mk.int", password)

    def test_answers(self) -> None:
        same = "ok = asked == {'user': 'm.ivanov@mk.int', 'password': 'Пароль \\'\"$'}\nsys.exit(0 if ok else 11)"
        self.assertEqual(self._with_child(same, "Пароль '\"$"), "")
        self.assertEqual(self._with_child("sys.exit(11)"), "wrong")
        self.assertEqual(self._with_child("sys.exit(12)"), "expired")

    def test_whatever_goes_wrong_is_not_a_sign_in(self) -> None:
        for code in ("sys.exit(13)", "sys.exit(1)", "raise RuntimeError", "import os; os._exit(139)"):
            self.assertEqual(self._with_child(code), "unavailable", code)
        self.assertEqual(self._with_child("time.sleep(30)", timeout=1), "unavailable")

    def test_impossible_passwords_are_not_even_asked_about(self) -> None:
        with mock.patch.object(console_access.subprocess, "run") as run:
            for password in ("", "a\0b", "x" * 257):
                self.assertEqual(console_access.check_password("m.ivanov", "mk.int", password), "wrong")
            run.assert_not_called()

    def test_the_password_is_not_on_the_command_line(self) -> None:
        with mock.patch.object(console_access.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            console_access.check_password("m.ivanov", "mk.int", "S3cret")
        self.assertNotIn("S3cret", repr(run.call_args.args))
        self.assertEqual(run.call_args.args[0][0], sys.executable)


class TestSignInSettings(unittest.TestCase):
    ENV = {"CONSOLE_AD_DOMAIN": "mk.int", "CONSOLE_AD_ADMINS": "m.ivanov", "CONSOLE_AD_USERS": "p.petrov"}

    def _problem(self, shared: bool = True, **changes: str) -> str:
        with mock.patch.object(console_access, "check_problem", return_value=""):
            return server._ad_problem({**self.ENV, **changes}, shared)

    def test_what_starts_and_what_does_not(self) -> None:
        self.assertEqual(self._problem(), "")
        self.assertEqual(self._problem(CONSOLE_AD_USERS=""), "")
        self.assertEqual(server._ad_problem({}, True), "")  # no sign-in, as before
        self.assertEqual(server._ad_problem({}, False), "")
        self.assertIn("только с CONSOLE_SHARED", self._problem(shared=False))
        self.assertIn("некому управлять", self._problem(CONSOLE_AD_ADMINS=""))
        self.assertIn("CONSOLE_AD_USERS", self._problem(CONSOLE_AD_USERS="p.petrov@other.kz"))
        self.assertIn("CONSOLE_AD_ADMINS", self._problem(CONSOLE_AD_ADMINS="m ivanov@x"))
        # The list is there but the switch is lost: not open to everyone.
        self.assertIn("открылась бы всем", self._problem(CONSOLE_AD_DOMAIN=""))

    def test_does_not_start_where_passwords_cannot_be_checked(self) -> None:
        with mock.patch.object(console_access, "check_problem", return_value="нет libpam"):
            self.assertEqual(server._ad_problem(self.ENV, True), "нет libpam")
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "info.env"
            env_path.write_text("CONSOLE_SHARED=1\n" + "".join(f"{k}={v}\n" for k, v in self.ENV.items()), encoding="utf-8")
            with mock.patch.object(server, "INFO_ENV_PATH", env_path), mock.patch.object(server, "_ConsoleServer") as listener, \
                    mock.patch.object(console_access, "check_problem", return_value="нет libpam"):
                with self.assertRaises(SystemExit):
                    server.run(port=0)
            listener.assert_not_called()
        self.assertEqual(server._AD_DOMAIN, "")


class TestSignInOverHttp(unittest.TestCase):
    PASSWORDS = {"m.ivanov": "Ivan-Pa55", "p.petrov": "Petr-Pa55", "s.serov": "Serov-Pa55", "x.stranger": "Str-Pa55"}
    NAMES = {"m.ivanov": "Иван Иванов", "p.petrov": "Пётр Петров", "s.serov": "Серж <b>Серов</b>", "x.stranger": "Чужой"}

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
        self._write_env(users="p.petrov")
        self.asked: list[tuple[str, str]] = []
        self.check_takes = 0.0
        for patcher in (
            mock.patch.object(server, "INFO_ENV_PATH", self.env_path),
            mock.patch.object(server, "OPERATORS_DIR", self.root / "operators"),
            mock.patch.object(server, "AUDIT_LOG_PATH", self.root / "audit.jsonl"),
            mock.patch.object(server, "AD_LOG_PATH", self.root / "ad_logins.jsonl"),
            mock.patch.object(server, "_SHARED", True),
            mock.patch.object(server, "_AD_DOMAIN", "mk.int"),
            mock.patch.object(server, "_sign_ins", console_access.SignIns(self.root / "sign_ins.json")),
            mock.patch.object(server, "_ad_throttle", console_access.Throttle()),
            mock.patch.object(server, "_ad_address_throttle", console_access.Throttle(limit=10)),
            mock.patch.object(server, "CrmSessionManager", mock.Mock()),
            mock.patch.object(console_access, "check_password", self._check_password),
            mock.patch.object(console_access, "account_name", lambda login, domain: self.NAMES.get(login)),
            mock.patch.dict(server._crm_sessions, clear=True),
            mock.patch.dict(server._workspace_seen, clear=True),
            mock.patch.dict(server._journal_access, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write_env(self, admins: str = "m.ivanov", users: str = "") -> None:
        self.env_path.write_text(
            "# общий файл\nCRM_BASE_URL=https://crm.example.invalid\nCONSOLE_AD_DOMAIN=mk.int\n"
            f"CONSOLE_AD_ADMINS={admins}\nCONSOLE_AD_USERS={users}\n", encoding="utf-8")

    def _check_password(self, login: str, domain: str, password: str) -> str:
        self.asked.append((login, password))
        time.sleep(self.check_takes)
        if password in ("expired", "unavailable"):
            return password
        return "" if self.PASSWORDS.get(login) == password else "wrong"

    def _open(self, cookies: dict[str, str] | None = None) -> str:
        """Loads the page like a browser: what it is told to store goes into ``cookies``."""
        cookies = {} if cookies is None else cookies
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/", headers=self._cookie_header(cookies))
        with urllib.request.urlopen(request, timeout=10) as response:
            self._store(cookies, response.headers)
            return response.read().decode("utf-8")

    def _post(self, path: str, body: dict | None = None, cookies: dict[str, str] | None = None, **headers: str):
        cookies = {} if cookies is None else cookies
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body or {}).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "X-Console-Version": server._PAGE_VERSION, **self._cookie_header(cookies),
                     **{name.replace("_", "-"): value for name, value in headers.items()}},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                self._store(cookies, response.headers)
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    @staticmethod
    def _cookie_header(cookies: dict[str, str]) -> dict[str, str]:
        return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())} if cookies else {}

    @staticmethod
    def _store(cookies: dict[str, str], headers) -> None:
        for line in headers.get_all("Set-Cookie") or []:
            name, _, value = line.split(";", 1)[0].partition("=")
            if "Max-Age=0" in line:
                cookies.pop(name, None)
            else:
                cookies[name] = value

    def _signed_in(self, login: str) -> dict[str, str]:
        cookies: dict[str, str] = {}
        status, _ = self._post("/api/ad/login", {"login": login, "password": self.PASSWORDS[login]}, cookies)
        self.assertEqual(status, 200)
        self._open(cookies)
        return cookies

    def _lines(self, name: str) -> list[dict]:
        path = self.root / name
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_without_a_sign_in_there_is_only_the_front_door(self) -> None:
        cookies: dict[str, str] = {}
        html = self._open(cookies)
        self.assertIn("Логин AD", html)
        self.assertNotIn("data-section-panel", html)
        self.assertEqual(cookies, {})
        self.assertFalse((self.root / "operators").exists())  # no workspace for a passer-by
        for path in ("/api/journal", "/api/access", "/api/access/grant", "/api/crm/credentials", "/api/webitel/delete_execute",
                     "/api/ad/logout", "/api/settings/save", "/api/nothing"):
            status, payload = self._post(path, {"plan": {"id": "1"}})
            self.assertEqual((status, payload.get("ad_login")), (401, True), path)

    def test_a_workspace_cookie_alone_opens_nothing(self) -> None:
        cookies = self._signed_in("p.petrov")
        del cookies["console_ad"]
        self.assertIn("Логин AD", self._open(cookies))
        self.assertEqual(self._post("/api/crm/credentials", {}, cookies)[0], 401)

    def test_signing_in(self) -> None:
        cookies: dict[str, str] = {}
        status, payload = self._post("/api/ad/login", {"login": "MK\\P.Petrov", "password": "Petr-Pa55"}, cookies)
        self.assertEqual((status, payload), (200, {"signed_in": "p.petrov"}))
        self.assertIn("data-section-panel", self._open(cookies))
        self.assertEqual(self._post("/api/access", {}, cookies), (200, {"me": "p.petrov"}))  # not an admin: no list
        self.assertEqual(self._post("/api/crm/credentials", {}, cookies)[0], 200)
        self.assertNotIn("Petr-Pa55", "".join(p.read_text(encoding="utf-8") for p in self.root.rglob("*") if p.is_file()))

    def test_the_cookie_is_out_of_reach_of_scripts_and_https_only(self) -> None:
        with mock.patch.object(server, "_TLS_ENABLED", True):
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/ad/login", method="POST", headers={"Content-Type": "application/json"},
                data=json.dumps({"login": "p.petrov", "password": "Petr-Pa55"}).encode("utf-8"))
            with urllib.request.urlopen(request, timeout=10) as response:
                cookie = response.headers["Set-Cookie"]
        for part in ("console_ad=", "HttpOnly", "Secure", "SameSite=Lax", "Max-Age=43200"):
            self.assertIn(part, cookie)

    def test_wrong_passwords_and_strangers_get_the_same_answer(self) -> None:
        wrong = self._post("/api/ad/login", {"login": "p.petrov", "password": "nope"})
        stranger = self._post("/api/ad/login", {"login": "x.stranger", "password": "Str-Pa55"})
        self.assertEqual(wrong, stranger)
        self.assertEqual(wrong[0], 403)
        self.assertEqual([login for login, _ in self.asked], ["p.petrov"])  # the domain isn't asked about strangers
        for body in ({}, {"login": "p.petrov"}, {"login": 5, "password": ["x"]}, {"login": "root", "password": "x"}):
            self.assertEqual(self._post("/api/ad/login", body)[0], 403, body)

    def test_a_few_attempts_and_then_the_domain_is_not_asked(self) -> None:
        for _ in range(4):
            self.assertEqual(self._post("/api/ad/login", {"login": "p.petrov", "password": "nope"})[0], 403)
        status, payload = self._post("/api/ad/login", {"login": "p.petrov", "password": "Petr-Pa55"})
        self.assertEqual(status, 429)
        self.assertIn("через 30 мин", payload["error"])
        self.assertEqual(len(self.asked), 4)
        self.assertEqual(self._post("/api/ad/login", {"login": "m.ivanov", "password": "Ivan-Pa55"})[0], 200)  # a colleague still can

    def test_attempts_sent_all_at_once_do_not_get_past_the_limit(self) -> None:
        self.check_takes = 0.1  # as with a real domain, they overlap
        threads = [threading.Thread(target=self._post, args=("/api/ad/login", {"login": "p.petrov", "password": f"guess{n}"})) for n in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.asked), 4)

    def test_one_address_cannot_go_through_the_list(self) -> None:
        for n in range(10):
            self._post("/api/ad/login", {"login": f"x.stranger{n}", "password": "x"})
        self.assertEqual(self._post("/api/ad/login", {"login": "m.ivanov", "password": "Ivan-Pa55"})[0], 429)

    def test_an_expired_password_and_a_silent_domain_do_not_use_up_attempts(self) -> None:
        for _ in range(6):
            self.assertIn("истёк", self._post("/api/ad/login", {"login": "p.petrov", "password": "expired"})[1]["error"])
            self.assertEqual(self._post("/api/ad/login", {"login": "p.petrov", "password": "unavailable"})[0], 503)
        self.assertEqual(self._post("/api/ad/login", {"login": "p.petrov", "password": "Petr-Pa55"})[0], 200)

    def test_the_sign_in_log_never_has_what_a_stranger_typed(self) -> None:
        self._post("/api/ad/login", {"login": "MyPa55word", "password": "x"})
        self._post("/api/ad/login", {"login": "p.petrov", "password": "nope"})
        self._signed_in("p.petrov")
        self.assertEqual([(e["login"], e["result"]) for e in self._lines("ad_logins.jsonl")],
                         [("не из списка", "wrong"), ("p.petrov", "wrong"), ("p.petrov", "ok")])
        text = (self.root / "ad_logins.jsonl").read_text(encoding="utf-8")
        for secret in ("MyPa55word", "mypa55word", "nope", "Petr-Pa55"):
            self.assertNotIn(secret, text)

    def test_another_site_cannot_sign_in_or_out(self) -> None:
        status, _ = self._post("/api/ad/login", {"login": "p.petrov", "password": "Petr-Pa55"}, Origin="https://evil.example.invalid")
        self.assertEqual((status, self.asked), (403, []))
        cookies = self._signed_in("p.petrov")
        self.assertEqual(self._post("/api/ad/logout", {}, cookies, Origin="https://evil.example.invalid")[0], 403)
        self.assertEqual(self._post("/api/access", {}, cookies)[0], 200)

    def test_allowed_networks_still_come_first(self) -> None:
        with mock.patch.object(server, "_ALLOWED_NETS", server._parse_allowed_nets("10.80.0.96/27")), \
                mock.patch.object(Handler, "_client_ip", return_value="10.1.1.1"):
            self.assertEqual(self._post("/api/ad/login", {"login": "p.petrov", "password": "Petr-Pa55"})[0], 403)
        self.assertEqual(self.asked, [])

    def test_signing_out_works_on_the_server_too(self) -> None:
        cookies = self._signed_in("p.petrov")
        copy = dict(cookies)
        self.assertEqual(self._post("/api/ad/logout", {}, cookies)[0], 200)
        self.assertNotIn("console_ad", cookies)
        self.assertEqual(self._post("/api/access", {}, copy)[0], 401)  # a copied cookie is no good either

    def test_the_admin_lets_people_in_and_out_at_once(self) -> None:
        admin = self._signed_in("m.ivanov")
        self.assertEqual(self._post("/api/ad/login", {"login": "s.serov", "password": "Serov-Pa55"})[0], 403)
        status, payload = self._post("/api/access/grant", {"login": " S.Serov@mk.int "}, admin)
        self.assertEqual(status, 200)
        self.assertEqual([(u["login"], u["admin"]) for u in payload["users"]], [("m.ivanov", True), ("p.petrov", False), ("s.serov", False)])
        self.assertEqual(payload["changed"], "s.serov — Серж <b>Серов</b>")  # escaped by the page
        self.assertIn("CONSOLE_AD_USERS=p.petrov, s.serov\n", self.env_path.read_text(encoding="utf-8"))
        self.assertIn("# общий файл\n", self.env_path.read_text(encoding="utf-8"))

        newcomer = self._signed_in("s.serov")
        self.assertEqual(self._post("/api/access/revoke", {"login": "s.serov"}, admin)[0], 200)
        self.assertEqual(self._post("/api/crm/credentials", {}, newcomer)[0], 401)  # mid-session, without a restart
        self.assertIn("Логин AD", self._open(newcomer))
        # Let in again: signs in anew, the ended session doesn't come back to life.
        self._post("/api/access/grant", {"login": "s.serov"}, admin)
        self.assertEqual(self._post("/api/crm/credentials", {}, newcomer)[0], 401)

    def test_only_the_admin_does(self) -> None:
        cookies = self._signed_in("p.petrov")
        before = self.env_path.read_text(encoding="utf-8")
        for path in ("/api/access/grant", "/api/access/revoke", "/api/access/promote"):
            for login in ("s.serov", "p.petrov", "m.ivanov"):
                self.assertEqual(self._post(path, {"login": login}, cookies)[0], 403)
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self._lines("audit.jsonl"), [])

    def test_what_the_admin_cannot_do(self) -> None:
        admin = self._signed_in("m.ivanov")
        before = self.env_path.read_text(encoding="utf-8")
        for path, login, error in (
            ("grant", "", "Укажите логин"), ("grant", "a, b", "Укажите логин"), ("grant", "x\nCONSOLE_AD_ADMINS=x.stranger", "Укажите логин"),
            ("grant", "no.such", "В AD нет"), ("grant", "p.petrov", "уже есть"), ("grant", "m.ivanov", "уже есть"),
            ("revoke", "m.ivanov", "Себя убрать нельзя"), ("revoke", "s.serov", "нет в списке"),
            ("promote", "s.serov", "нет в списке"), ("promote", "m.ivanov", "уже админ"), ("promote", "", "Укажите логин"),
        ):
            status, payload = self._post(f"/api/access/{path}", {"login": login}, admin)
            self.assertEqual(status, 400, login)
            self.assertIn(error, payload["error"])
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), before)

    def test_an_admin_appoints_another_who_can_then_remove_the_first(self) -> None:
        first, second = self._signed_in("m.ivanov"), self._signed_in("p.petrov")
        self.assertNotIn("users", self._post("/api/access", {}, second)[1])
        status, payload = self._post("/api/access/promote", {"login": "p.petrov"}, first)
        self.assertEqual((status, [(u["login"], u["admin"]) for u in payload["users"]]), (200, [("m.ivanov", True), ("p.petrov", True)]))
        text = self.env_path.read_text(encoding="utf-8")
        self.assertIn("CONSOLE_AD_ADMINS=m.ivanov, p.petrov\nCONSOLE_AD_USERS=\n", text)
        # The first one leaves the company; nobody needs the server for that.
        self.assertEqual(self._post("/api/access/revoke", {"login": "m.ivanov"}, second)[0], 200)
        self.assertEqual(self._post("/api/access", {}, first)[0], 401)
        self.assertEqual(self._post("/api/access/revoke", {"login": "p.petrov"}, second)[0], 400)  # the last one stays
        self.assertIn("CONSOLE_AD_ADMINS=p.petrov\n", self.env_path.read_text(encoding="utf-8"))

    def test_two_admins_removing_each_other_at_once_leave_one(self) -> None:
        self._write_env(admins="m.ivanov, p.petrov")
        first, second = self._signed_in("m.ivanov"), self._signed_in("p.petrov")
        both = threading.Barrier(2)

        def meet(login, domain):  # both are past every check made before the lock
            if threading.current_thread() is not threading.main_thread():
                try:
                    both.wait(timeout=2)
                except threading.BrokenBarrierError:
                    pass
            return self.NAMES.get(login)

        with mock.patch.object(console_access, "account_name", meet):
            answers = []
            threads = [threading.Thread(target=lambda c=c, x=x: answers.append(self._post("/api/access/revoke", {"login": x}, c)[0]))
                       for c, x in ((first, "p.petrov"), (second, "m.ivanov"))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(sorted(answers), [200, 403])
        self.assertEqual(len(server._ad_people()[0]), 1)

    def test_someone_in_both_lists_is_removed_from_both(self) -> None:
        self._write_env(admins="m.ivanov, p.petrov", users="p.petrov")
        admin = self._signed_in("m.ivanov")
        self.assertEqual(self._post("/api/access/revoke", {"login": "p.petrov"}, admin)[0], 200)
        self.assertEqual(server._ad_people(), (["m.ivanov"], []))

    def test_changes_of_access_are_in_the_journal(self) -> None:
        admin = self._signed_in("m.ivanov")
        self._post("/api/access/grant", {"login": "s.serov"}, admin)
        self._post("/api/access/revoke", {"login": "s.serov"}, admin)
        with mock.patch.object(server, "_webitel_session_state", return_value={"session": "active"}):
            entries = self._post("/api/journal", {}, admin)[1]["entries"]
        self.assertEqual([(e["kind"], e["number"], e["who"], e["ok"]) for e in entries],
                         [("Доступ закрыт", "s.serov", "m.ivanov", True), ("Доступ открыт", "s.serov", "m.ivanov", True)])

    def test_the_journal_says_who_by_ad_account(self) -> None:
        cookies = self._signed_in("p.petrov")
        actions = mock.Mock()
        actions.execute_delete_plan.return_value = {"deleted": True}
        with mock.patch.object(server, "_get_webitel_actions", return_value=actions):
            self._post("/api/webitel/delete_execute", {"plan": {"extension": "30121", "name": "TEST", "city": "almaty"}}, cookies)
        self.assertEqual(self._lines("audit.jsonl")[0]["ad_login"], "p.petrov")
        self.assertEqual(server._journal_entry(self._lines("audit.jsonl")[0])["who"], "p.petrov")

    def test_an_edit_of_the_file_by_hand_works_at_once(self) -> None:
        cookies = self._signed_in("p.petrov")
        self._write_env(users="s.serov")
        self.assertEqual(self._post("/api/crm/credentials", {}, cookies)[0], 401)
        self.assertEqual(self._post("/api/ad/login", {"login": "s.serov", "password": "Serov-Pa55"})[0], 200)

    def test_a_typo_in_the_file_lets_nobody_in_and_is_not_overwritten(self) -> None:
        admin, user = self._signed_in("m.ivanov"), self._signed_in("p.petrov")
        self._write_env(users="p.petrov, s.serov@wrong")
        self.assertEqual(self._post("/api/access", {}, user)[0], 401)
        before = self.env_path.read_text(encoding="utf-8")
        status, payload = self._post("/api/access/grant", {"login": "s.serov"}, admin)
        self.assertEqual(status, 400)
        self.assertIn("CONSOLE_AD_USERS в info.env", payload["error"])
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), before)
        self.env_path.unlink()  # the whole file lost while the console runs
        self.assertEqual(self._post("/api/access", {}, admin)[0], 401)
        self.assertEqual(self._post("/api/ad/login", {"login": "m.ivanov", "password": "Ivan-Pa55"})[0], 403)

    def test_a_second_person_on_the_same_pc_does_not_get_the_first_ones_logins(self) -> None:
        cookies = self._signed_in("m.ivanov")
        self._post("/api/webitel/credentials/save", {"city": "almaty", "login": "10078", "password": "Alm-Pa55"}, cookies)
        first = cookies["console_workspace"]
        self._post("/api/ad/login", {"login": "p.petrov", "password": "Petr-Pa55"}, cookies)
        # An old tab still open with the first one's cookie gets nothing.
        self.assertEqual(self._post("/api/webitel/session_status", {"city": "almaty"}, cookies)[0], 400)
        self._open(cookies)
        self.assertNotEqual(cookies["console_workspace"], first)
        self.assertEqual(self._post("/api/webitel/session_status", {"city": "almaty"}, cookies)[1]["login"], "")

    def test_a_workspace_from_before_goes_to_whoever_signs_in_first(self) -> None:
        with mock.patch.object(server, "_AD_DOMAIN", ""):
            cookies: dict[str, str] = {}
            self._open(cookies)
            self._post("/api/webitel/credentials/save", {"city": "almaty", "login": "10078", "password": "Alm-Pa55"}, cookies)
        workspace = cookies["console_workspace"]
        self._post("/api/ad/login", {"login": "m.ivanov", "password": "Ivan-Pa55"}, cookies)
        self._open(cookies)
        self.assertEqual(cookies["console_workspace"], workspace)
        self.assertEqual(self._post("/api/webitel/session_status", {"city": "almaty"}, cookies)[1]["login"], "10078")

    def test_a_stolen_workspace_cookie_is_useless_under_another_account(self) -> None:
        owner, thief = self._signed_in("m.ivanov"), self._signed_in("p.petrov")
        thief["console_workspace"] = owner["console_workspace"]
        self.assertEqual(self._post("/api/crm/credentials", {}, thief)[0], 400)

    def test_without_ad_the_console_is_as_before(self) -> None:
        with mock.patch.object(server, "_AD_DOMAIN", ""):
            cookies: dict[str, str] = {}
            self.assertIn("data-section-panel", self._open(cookies))
            self.assertEqual(self._post("/api/access", {}, cookies), (200, {"me": ""}))
            for path in ("/api/ad/login", "/api/ad/logout", "/api/access/grant"):
                self.assertEqual(self._post(path, {"login": "s.serov"}, cookies)[0], 404, path)
            self.assertEqual([p.name for p in (self.root / "operators").rglob("ad_login")], [])

    def test_the_front_door_runs_only_its_own_script(self) -> None:
        self.assertEqual(server._SIGN_IN_HTML.count("<script"), 1)
        self.assertNotRegex(server._SIGN_IN_HTML, r"<[a-z][^>]* on[a-z]+=")
        self.assertIn("'sha256-", server._content_security_policy(server._SIGN_IN_HTML))


if __name__ == "__main__":
    unittest.main()
