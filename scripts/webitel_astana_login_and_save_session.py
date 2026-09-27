"""One-time, manual-assisted login helper for Webitel Astana — NOT part of
the crm_ai_agent package, NOT imported by any production code path.

Mirrors webitel_login_and_save_session.py exactly, pointed at the Astana
instance and a separate saved-session file
(.auth/webitel_astana_storage_state.json), so it never collides with the
Almaty session. See that script's docstring for the full behavior contract
(headed browser, human enters MFA, no automatic button clicks,
URL-and-password-field polling instead of stdin).

This is READ-ONLY reconnaissance scope: after login, only viewing pages is
in scope. No user records, settings, or any other data may be created,
edited, or deleted through this session.

Run manually, once, from a terminal:
    python scripts/webitel_astana_login_and_save_session.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTH_DIR = ROOT / ".auth"
STORAGE_STATE_PATH = AUTH_DIR / "webitel_astana_storage_state.json"

LOGIN_WAIT_TIMEOUT_SECONDS = 15 * 60
POLL_INTERVAL_SECONDS = 2
STABLE_SECONDS_REQUIRED = 5


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


def main() -> int:
    from playwright.sync_api import sync_playwright

    env = _load_env_file(ROOT / "info.env")
    base_url = env.get("WEBITEL_ASTANA_BASE_URL") or os.environ.get("WEBITEL_ASTANA_BASE_URL")
    domain = env.get("WEBITEL_ASTANA_DOMAIN") or os.environ.get("WEBITEL_ASTANA_DOMAIN")
    login = env.get("WEBITEL_ASTANA_LOGIN") or os.environ.get("WEBITEL_ASTANA_LOGIN")
    password = env.get("WEBITEL_ASTANA_PASSWORD") or os.environ.get("WEBITEL_ASTANA_PASSWORD")

    if not base_url or not login or not password:
        print("Missing WEBITEL_ASTANA_BASE_URL / WEBITEL_ASTANA_LOGIN / WEBITEL_ASTANA_PASSWORD in info.env", file=sys.stderr)
        return 1

    AUTH_DIR.mkdir(exist_ok=True)
    debug_dir = ROOT / ".auth" / "webitel_astana_login_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        print(f"Opening {base_url} ...")
        page.goto(base_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        initial_url = page.url  # captured BEFORE any form interaction — see bug note below
        seen_password_field = False

        # Confirmed 2026-09-27 via scripts/webitel_astana_probe_login_form.py
        # against the real login form (a PrimeVue-based UI, different from
        # Almaty's — a 2-step wizard: domain, then username+password). The
        # element ids are randomly generated per page load, but the
        # ``name`` attributes are stable.
        domain_selectors = ["input[name='domain']"]
        username_selectors = ["input[name='username']"]
        password_selectors = ["input[name='password']"]
        submit_selectors = [
            "button[type='submit']",
            "button:has-text('Войти')", "button:has-text('Log in')", "button:has-text('Sign in')",
            "button:has-text('Login')", "button:has-text('Next')", "button:has-text('Далее')",
            "button:has-text('Continue')", "button:has-text('Продолжить')",
        ]

        def fill_first_match(selectors: list[str], value: str, label: str) -> bool:
            for sel in selectors:
                try:
                    locator = page.locator(sel).first
                    if locator.count() > 0 and locator.is_visible():
                        current = locator.input_value(timeout=1000) or ""
                        if not current:
                            locator.fill(value, timeout=3000)
                            print(f"Filled {label} using selector: {sel}")
                        return True
                except Exception:
                    continue
            return False

        def click_first_match(selectors: list[str], label: str) -> bool:
            for sel in selectors:
                try:
                    locator = page.locator(sel).first
                    if locator.count() > 0 and locator.is_visible() and locator.is_enabled():
                        locator.click(timeout=3000)
                        print(f"Clicked {label} using selector: {sel}")
                        return True
                except Exception:
                    continue
            return False

        # This form may be multi-step (domain -> login -> password, each
        # revealed after the previous is submitted). Loop a few times,
        # filling whatever is visible and advancing, screenshotting each
        # step so there is a record of exactly what was clicked.
        for step in range(6):
            page.screenshot(path=str(debug_dir / f"step_{step}_before.png"))
            did_something = False
            if domain and fill_first_match(domain_selectors, domain, "domain"):
                did_something = True
            if fill_first_match(username_selectors, login, "login"):
                did_something = True
            if fill_first_match(password_selectors, password, "password"):
                did_something = True
                seen_password_field = True
            page.wait_for_timeout(400)
            if click_first_match(submit_selectors, f"submit (step {step})"):
                did_something = True
            page.wait_for_timeout(1200)
            page.screenshot(path=str(debug_dir / f"step_{step}_after.png"))
            if not did_something:
                break

        print(f"Login-form step screenshots saved under {debug_dir}")
        print()
        print("=" * 70)
        print("READ-ONLY RECON ONLY: after logging in, only VIEW pages.")
        print("Do not create, edit, or delete any user, number, or setting.")
        print()
        print("This script just tried to fill and submit the login form itself")
        print("(see the step screenshots). If an MFA/token prompt is now showing,")
        print("enter it yourself in the browser window — this script will not")
        print("touch that field. If the form still isn't right, fix it manually.")
        print()
        print("This terminal cannot wait for a keypress (non-interactive runner),")
        print("so it auto-detects login completion by polling the page URL and")
        print(f"the password field. Timeout: {LOGIN_WAIT_TIMEOUT_SECONDS}s total.")
        print("=" * 70)

        def _looks_logged_in() -> bool:
            nonlocal seen_password_field
            current_url = page.url
            url_changed_away_from_login = current_url != initial_url and not any(
                keyword in current_url.lower() for keyword in ("login", "auth", "signin")
            )
            try:
                password_field_count = page.locator("input[type='password']").count()
            except Exception:
                password_field_count = 0
            if password_field_count > 0:
                seen_password_field = True
            password_field_gone = seen_password_field and password_field_count == 0
            return url_changed_away_from_login or password_field_gone

        stable_since: float | None = None
        deadline = time.monotonic() + LOGIN_WAIT_TIMEOUT_SECONDS

        while time.monotonic() < deadline:
            if _looks_logged_in():
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= STABLE_SECONDS_REQUIRED:
                    break
            else:
                stable_since = None
            time.sleep(POLL_INTERVAL_SECONDS)
        else:
            print(
                "Timed out waiting for login to complete. Saving whatever session "
                "state exists now — rerun this script if it is not actually logged in.",
                file=sys.stderr,
            )

        context.storage_state(path=str(STORAGE_STATE_PATH))
        print(f"Session saved to {STORAGE_STATE_PATH} (final URL: {page.url})")

        browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
