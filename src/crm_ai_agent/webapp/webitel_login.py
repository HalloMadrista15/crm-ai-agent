"""Webitel login from inside the console, so every network engineer can sign
in with their own account instead of running scripts/webitel_*_login_and_
save_session.py by hand.

Same flow as those scripts (confirmed working against both real instances
on 2026-09-28): open a VISIBLE browser, fill domain / username / password
through Webitel's multi-step form, let the human type any confirmation code
themselves, detect completion by URL/password-field polling, then save the
Playwright storage_state that WebitelApiActions reads its cookies and
access-token from. Nothing is ever clicked after the login form — the
session is only saved, never used here.

Each login runs in its own thread with its own Playwright instance (the
sync API is thread-affine), separate from the long-lived CRM browser in
crm_session.py.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

# Per-city info.env keys. Almaty's login keys predate multi-city support and
# keep their original unprefixed names.
WEBITEL_CITY_KEYS: dict[str, dict[str, str]] = {
    "almaty": {
        "base_url": "WEBITEL_ALMATY_BASE_URL",
        "domain": "WEBITEL_DOMAIN",
        "login": "WEBITEL_LOGIN",
        "password": "WEBITEL_PASSWORD",
    },
    "astana": {
        "base_url": "WEBITEL_ASTANA_BASE_URL",
        "domain": "WEBITEL_ASTANA_DOMAIN",
        "login": "WEBITEL_ASTANA_LOGIN",
        "password": "WEBITEL_ASTANA_PASSWORD",
    },
}

# Name-based selectors first: Astana's PrimeVue form randomises element ids
# but keeps stable ``name`` attributes (scripts/webitel_astana_probe_login_
# form.py); the generic fallbacks are what worked for Almaty.
_DOMAIN_SELECTORS = ["input[name='domain']", "#domain", "input[placeholder*='domain' i]", "input[placeholder*='домен' i]"]
_USERNAME_SELECTORS = ["input[name='username']", "#username", "input[placeholder*='login' i]", "input[placeholder*='логин' i]", "input[autocomplete='username']", "input[type='text']"]
_PASSWORD_SELECTORS = ["input[name='password']", "input[type='password']", "input[autocomplete='current-password']"]
_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "button:has-text('Войти')", "button:has-text('Log in')", "button:has-text('Sign in')",
    "button:has-text('Login')", "button:has-text('Next')", "button:has-text('Далее')",
    "button:has-text('Continue')", "button:has-text('Продолжить')",
]

LOGIN_TIMEOUT_SECONDS = 10 * 60
_POLL_SECONDS = 2
_STABLE_SECONDS = 5


def city_config(env: dict[str, str], city: str) -> dict[str, str]:
    """This city's base_url/domain/login/password from info.env values
    (missing ones as ""). Raises ValueError for an unknown city."""
    keys = WEBITEL_CITY_KEYS.get(city)
    if keys is None:
        raise ValueError(f"Unknown city {city!r} — must be one of: {', '.join(WEBITEL_CITY_KEYS)}")
    return {field: env.get(key, "") for field, key in keys.items()}


def looks_logged_in(*, initial_url: str, current_url: str, seen_password_field: bool, password_fields_now: int) -> bool:
    """Same completion rule as the login scripts: the page left the login URL,
    or a password field was shown and is now gone (the SPA may keep the URL)."""
    left_login_url = current_url != initial_url and not any(
        keyword in current_url.lower() for keyword in ("login", "auth", "signin")
    )
    return left_login_url or (seen_password_field and password_fields_now == 0)


class WebitelLoginJob:
    """One visible-browser login for one city. ``state`` is "running", then
    "done" or "failed" (with ``error``)."""

    def __init__(self, *, city: str, config: dict[str, str], storage_state_path: Path) -> None:
        self.city = city
        self.login = config["login"]
        self._config = config
        self._storage_state_path = storage_state_path
        self.state = "running"
        self.error = ""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def snapshot(self) -> dict[str, Any]:
        return {"state": self.state, "error": self.error, "login": self.login}

    def _run(self) -> None:
        try:
            self._login()
            self.state = "done"
        except Exception as exc:  # noqa: BLE001 — reported to the console, never crashes the server
            message = str(exc)
            if "closed" in message.lower():
                message = "окно Webitel закрыто до завершения входа"
            self.error = message
            self.state = "failed"

    def _login(self) -> None:
        from playwright.sync_api import sync_playwright

        from crm_ai_agent.adapters.webitel_api.session import load_session

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            try:
                page = browser.new_context().new_page()
                page.goto(self._config["base_url"], wait_until="domcontentloaded")
                # Confirmed 2026-09-28: a fixed 1.5s wait was sometimes too
                # short for the Vue form to render — the Next button got
                # clicked on an empty form ("Field is required").
                try:
                    page.wait_for_selector(", ".join(_DOMAIN_SELECTORS[:1] + _USERNAME_SELECTORS[:1]), state="visible", timeout=20000)
                except Exception:
                    pass
                initial_url = page.url
                seen_password_field = False
                debug_dir = self._storage_state_path.parent / "webitel_login_debug" / self.city
                debug_dir.mkdir(parents=True, exist_ok=True)

                def snapshot(name: str) -> None:
                    try:
                        page.screenshot(path=str(debug_dir / f"{name}.png"))
                    except Exception:
                        pass

                def fill_first(selectors: list[str], value: str) -> bool:
                    for selector in selectors:
                        try:
                            locator = page.locator(selector).first
                            if locator.count() > 0 and locator.is_visible():
                                if not (locator.input_value(timeout=1000) or ""):
                                    locator.fill(value, timeout=3000)
                                return True
                        except Exception:
                            continue
                    return False

                def click_first(selectors: list[str]) -> bool:
                    for selector in selectors:
                        try:
                            locator = page.locator(selector).first
                            if locator.count() > 0 and locator.is_visible() and locator.is_enabled():
                                locator.click(timeout=3000)
                                return True
                        except Exception:
                            continue
                    return False

                # Multi-step form (domain, then username + password): fill
                # whatever is visible and advance. The submit button is only
                # pressed when a field was actually found on this step, and a
                # step that shows no field yet gets a few retries before
                # assuming the form is done.
                misses = 0
                for step in range(12):
                    snapshot(f"step_{step}")
                    found = False
                    if self._config["domain"] and fill_first(_DOMAIN_SELECTORS, self._config["domain"]):
                        found = True
                    if fill_first(_USERNAME_SELECTORS, self._config["login"]):
                        found = True
                    if fill_first(_PASSWORD_SELECTORS, self._config["password"]):
                        found = True
                        seen_password_field = True
                    if not found:
                        misses += 1
                        if misses >= 3:
                            break
                        page.wait_for_timeout(1000)
                        continue
                    misses = 0
                    page.wait_for_timeout(400)
                    click_first(_SUBMIT_SELECTORS)
                    page.wait_for_timeout(1500)
                snapshot("after_form")

                # The human may still need to type a confirmation code or fix
                # a wrong password in the window — wait for them.
                deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
                stable_since: float | None = None
                while True:
                    if page.is_closed():
                        raise RuntimeError("окно Webitel закрыто до завершения входа")
                    if time.monotonic() > deadline:
                        raise RuntimeError("вход не завершён за 10 минут")
                    try:
                        password_fields = page.locator("input[type='password']").count()
                    except Exception:
                        password_fields = 0
                    if password_fields > 0:
                        seen_password_field = True
                    if looks_logged_in(
                        initial_url=initial_url,
                        current_url=page.url,
                        seen_password_field=seen_password_field,
                        password_fields_now=password_fields,
                    ):
                        stable_since = stable_since or time.monotonic()
                        if time.monotonic() - stable_since >= _STABLE_SECONDS:
                            break
                    else:
                        stable_since = None
                    page.wait_for_timeout(_POLL_SECONDS * 1000)

                self._storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                page.context.storage_state(path=str(self._storage_state_path))
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
        # A saved file without Webitel's cookie/token would only fail later,
        # on the first real action — fail here instead.
        load_session(self._storage_state_path)
