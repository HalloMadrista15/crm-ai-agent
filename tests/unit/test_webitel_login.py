"""Offline tests for the console's in-app Webitel login (webapp/webitel_login.py).
The visible-browser flow itself needs a real Webitel and a human; what's
covered here is the per-city config mapping and the "login finished" rule."""

import unittest

from crm_ai_agent.webapp.webitel_login import city_config, looks_logged_in

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


if __name__ == "__main__":
    unittest.main()
