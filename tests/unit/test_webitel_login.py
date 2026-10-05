"""Offline tests for the console's in-app Webitel login (webapp/webitel_login.py).
The visible-browser flow itself needs a real Webitel and a human; what's
covered here is the per-city config mapping and the "login finished" rule."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from crm_ai_agent.webapp.webitel_login import city_config, latest_screenshot, looks_logged_in, page_problem

_ENV = {
    "WEBITEL_ALMATY_BASE_URL": "https://wtl-alm.example.invalid",
    "WEBITEL_DOMAIN": "alm.example.invalid",
    "WEBITEL_LOGIN": "10078",
    "WEBITEL_PASSWORD": "pw-alm",
    "WEBITEL_ASTANA_BASE_URL": "https://wtl-ast.example.invalid",
    "WEBITEL_ASTANA_DOMAIN": "ast.example.invalid",
    "WEBITEL_ASTANA_LOGIN": "m.muratbay",
}


class TestCityConfig(unittest.TestCase):
    def test_almaty_uses_its_original_unprefixed_login_keys(self) -> None:
        self.assertEqual(
            city_config(_ENV, "almaty"),
            {"base_url": "https://wtl-alm.example.invalid", "domain": "alm.example.invalid", "login": "10078", "password": "pw-alm"},
        )

    def test_astana_uses_prefixed_keys_and_missing_ones_are_empty(self) -> None:
        config = city_config(_ENV, "astana")
        self.assertEqual(config["login"], "m.muratbay")
        self.assertEqual(config["password"], "")

    def test_unknown_city_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            city_config(_ENV, "shymkent")


class TestLooksLoggedIn(unittest.TestCase):
    def test_still_on_the_login_form(self) -> None:
        self.assertFalse(
            looks_logged_in(initial_url="https://w/", current_url="https://w/", seen_password_field=True, password_fields_now=1)
        )

    def test_password_field_gone_counts_even_on_the_same_url(self) -> None:
        self.assertTrue(
            looks_logged_in(initial_url="https://w/", current_url="https://w/", seen_password_field=True, password_fields_now=0)
        )

    def test_moving_to_an_auth_page_is_not_a_login(self) -> None:
        self.assertFalse(
            looks_logged_in(initial_url="https://w/", current_url="https://w/auth/otp", seen_password_field=False, password_fields_now=0)
        )

    def test_leaving_the_login_url_counts(self) -> None:
        self.assertTrue(
            looks_logged_in(initial_url="https://w/", current_url="https://w/admin", seen_password_field=False, password_fields_now=0)
        )


class _Page:
    def __init__(self, result: object) -> None:
        self._result = result

    def evaluate(self, script: str) -> object:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class TestPageProblem(unittest.TestCase):
    def test_webitels_own_error_line_is_reported(self) -> None:
        self.assertEqual(page_problem(_Page("Webitel: Invalid username or password")), "Webitel: Invalid username or password")

    def test_nothing_to_report_while_progressing(self) -> None:
        self.assertEqual(page_problem(_Page("")), "")
        self.assertEqual(page_problem(_Page(None)), "")

    def test_page_navigating_away_is_not_a_problem(self) -> None:
        self.assertEqual(page_problem(_Page(RuntimeError("Execution context was destroyed"))), "")


class TestLatestScreenshot(unittest.TestCase):
    def test_none_before_any_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(latest_screenshot(Path(tmp) / "s.json", "astana"))

    def test_newest_step_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shots = Path(tmp) / "webitel_login_debug" / "astana"
            shots.mkdir(parents=True)
            (shots / "step_0.png").write_bytes(b"old")
            (shots / "failed.png").write_bytes(b"new")
            now = time.time()
            os.utime(shots / "step_0.png", (now - 60, now - 60))
            self.assertEqual(latest_screenshot(Path(tmp) / "s.json", "astana"), b"new")


if __name__ == "__main__":
    unittest.main()
