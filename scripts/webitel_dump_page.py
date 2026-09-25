"""Read-only reconnaissance for Webitel: navigate via a sequence of text
clicks (menu items only — never a data row, never a save/submit control)
and dump the resulting page's HTML + a screenshot to a local scratch
directory for offline inspection.

Mirrors crm_dump_ticket.py's contract exactly: never prints page text to
the console (only file paths), never fills or submits a form, never clicks
anything but navigation labels explicitly passed in on the command line.
Uses the session saved by webitel_login_and_save_session.py.

Usage:
    python scripts/webitel_dump_page.py <output_dir> [nav_label ...]

Example:
    python scripts/webitel_dump_page.py out_dir Admin Directory Users
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORAGE_STATE_PATH = ROOT / ".auth" / "webitel_almaty_storage_state.json"


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

    if len(sys.argv) < 2:
        print("Usage: python scripts/webitel_dump_page.py <output_dir> [nav_label ...]", file=sys.stderr)
        return 1

    output_dir = Path(sys.argv[1])
    nav_labels = sys.argv[2:]
    output_dir.mkdir(parents=True, exist_ok=True)

    if not STORAGE_STATE_PATH.exists():
        print(f"No saved session at {STORAGE_STATE_PATH}. Run webitel_login_and_save_session.py first.", file=sys.stderr)
        return 1

    env = _load_env_file(ROOT / "info.env")
    base_url = env.get("WEBITEL_ALMATY_BASE_URL")
    if not base_url:
        print("Missing WEBITEL_ALMATY_BASE_URL in info.env", file=sys.stderr)
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STORAGE_STATE_PATH))
        page = context.new_page()

        page.goto(base_url, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(output_dir / "nav_start.png"))

        for i, label in enumerate(nav_labels):
            # Playwright's actionability check (even with force=True) still
            # refuses these nav tiles as "not visible" despite them clearly
            # rendering on screen (see nav_start.png) — likely a headless
            # rendering quirk with their CSS. Dispatching a native DOM click
            # directly bypasses that check. Still just a click on a
            # navigation label passed in explicitly — never a data row or
            # form control.
            page.get_by_text(label, exact=False).first.evaluate("el => el.click()")
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(1200)
            page.screenshot(path=str(output_dir / f"nav_{i}_{label}.png"))

        html = page.content()
        (output_dir / "page_dump.html").write_text(html, encoding="utf-8")
        page.screenshot(path=str(output_dir / "final.png"), full_page=True)

        print(f"Saved HTML ({len(html)} bytes) and screenshots to {output_dir}")

        browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
