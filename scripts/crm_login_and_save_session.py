"""One-time, manual-assisted login helper — NOT part of the crm_ai_agent
package, NOT imported by any production code path.

What this does and does not do:
- Opens a VISIBLE (headed) Chromium window pointed at CRM_BASE_URL.
- Fills the login field with CRM_LOGIN and the password field with
  CRM_PASSWORD from info.env, using generic, best-effort selectors.
- Then STOPS and waits for a human to manually enter the MFA/token code in
  that same visible window and submit it — this script never touches that
  field, because the code is only ever known to the human who receives it.
- Once the page navigates past the login flow (heuristically detected, see
  below), saves the authenticated session (cookies + localStorage) to
  .auth/crm_storage_state.json so read-only scripts can reuse it without
  repeating the MFA step every time.
- Never clicks anything beyond the login form itself. No ticket data is
  read or written here.

Run manually, once, from a terminal (not in headless CI):
    python scripts/crm_login_and_save_session.py

If the generic selectors below don't match this CRM's actual login page,
the script will pause and tell you exactly which field it also could not
find; it will not guess further or blindly submit.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTH_DIR = ROOT / ".auth"
STORAGE_STATE_PATH = AUTH_DIR / "crm_storage_state.json"

LOGIN_WAIT_TIMEOUT_SECONDS = 15 * 60
POLL_INTERVAL_SECONDS = 1
# Widened from 5s after observing (2026-09-18) a transient navigation to the
# authenticated app shell that bounced back to the login page a few seconds
# later — 5s of "stability" wasn't enough to rule that out.
STABLE_SECONDS_REQUIRED = 12


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
    base_url = env.get("CRM_BASE_URL") or os.environ.get("CRM_BASE_URL")
    login = env.get("CRM_LOGIN") or os.environ.get("CRM_LOGIN")
    password = env.get("CRM_PASSWORD") or os.environ.get("CRM_PASSWORD")

    if not base_url or not login or not password:
        print("Missing CRM_BASE_URL / CRM_LOGIN / CRM_PASSWORD in info.env", file=sys.stderr)
        return 1

    AUTH_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        print(f"Opening {base_url} ...")
        page.goto(base_url, wait_until="domcontentloaded")
        # The login form's inputs are rendered by client-side JS, not
        # present in the initial HTML — domcontentloaded fires before they
        # exist, so every previous fill attempt found 0 matches. Wait for
        # the network to go quiet (JS bundle + any config fetch finished),
        # plus a fixed settle delay, before looking for form fields.
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(1500)

        # Real selectors, confirmed 2026-09-11 via scripts/crm_probe_login_form.py
        # against the actual empty login page (no credentials involved):
        # this is Creatio's classic ASP.NET "NuiLogin.aspx" page. Its two
        # extra hidden input[type=password] fields (t-comp14/15-el) are a
        # known anti-autofill decoy pattern, not real fields — #passwordEdit-el
        # is the one actually visible and used.
        username_selectors = ["#loginEdit-el"]
        password_selectors = ["#passwordEdit-el"]

        def fill_first_match(selectors: list[str], value: str, label: str) -> bool:
            for sel in selectors:
                try:
                    locator = page.locator(sel).first
                    if locator.count() > 0:
                        locator.fill(value, timeout=3000)
                        print(f"Filled {label} using selector: {sel}")
                        return True
                except Exception:
                    continue
            return False

        username_filled = fill_first_match(username_selectors, login, "login")
        if not username_filled:
            print(
                "Could not find #loginEdit-el. The browser window is open — you can "
                "fill it in manually, then continue with the password and MFA step yourself.",
                file=sys.stderr,
            )
        password_filled = fill_first_match(password_selectors, password, "password")
        if not password_filled:
            print(
                "Could not find #passwordEdit-el. Fill it in manually in the open browser window.",
                file=sys.stderr,
            )

        if username_filled and password_filled:
            # This login page has no <button> element at all (confirmed via
            # scripts/crm_probe_login_form.py) — pressing Enter in the
            # password field is the standard way to submit a form like this
            # without needing to locate a non-existent submit control.
            page.locator(password_selectors[0]).first.press("Enter")
            print("Submitted the login form (pressed Enter in the password field).")

        print()
        print("=" * 70)
        print("Now check the browser window. If it did not submit on its own, click")
        print("the submit control yourself (this script does not click one).")
        print("If/when an MFA or token prompt appears, ENTER IT YOURSELF in the")
        print("browser window. This script will NOT touch that field.")
        print()
        print("This terminal cannot wait for a keypress (non-interactive runner),")
        print("so it will instead poll the page URL and auto-detect once you have")
        print("navigated away from the login page and it has stayed stable for a")
        print(f"few seconds. Timeout: {LOGIN_WAIT_TIMEOUT_SECONDS}s total.")
        print("=" * 70)

        def _looks_logged_in() -> bool:
            # Confirmed 2026-09-18 across several real logins: the
            # authenticated app shell URL always looks like
            # ".../0/Nui/ViewModule.aspx#HomePage/..." (or another
            # ViewModule.aspx route), while the login page is always under
            # "/Login/". A bare "did the URL change" check was tried first
            # and falsely fired on a transient bounce through the app shell
            # that redirected back to login; a get_by_text() search for a
            # real nav element was tried next but silently never matched
            # (root cause not found — possibly a stale execution context
            # during navigation) despite the element being visibly present
            # on screen. Matching the URL *pattern* itself is what actually
            # worked reliably, so that is what this checks.
            current_url = page.url.lower()
            if "/login/" in current_url:
                return False
            return "viewmodule.aspx" in current_url

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
