"""Read-only reconnaissance: open ONE Webitel user's edit view (found by
searching for their login), click ONE additional tab within that same
card (e.g. "License", "Variables"), and dump HTML + screenshot. Never
clicks Save/Apply/Delete/Generate — only Search, the row's own Edit icon,
then one named tab label.

Usage:
    python scripts/webitel_dump_user_tab.py <login> <tab_label> <output_dir>
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

    if len(sys.argv) != 4:
        print("Usage: python scripts/webitel_dump_user_tab.py <login> <tab_label> <output_dir>", file=sys.stderr)
        return 1

    login = sys.argv[1]
    tab_label = sys.argv[2]
    output_dir = Path(sys.argv[3])
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

        for label in ("Admin", "Directory", "Users"):
            page.get_by_text(label, exact=False).first.evaluate("el => el.click()")
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(1200)

        page.get_by_placeholder("Search").fill(login)
        page.wait_for_timeout(1200)

        page.locator('button[icon="edit"]').first.evaluate("el => el.click()")
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(1500)

        # The one additional action beyond the previous recon: click a tab
        # label within this same user's card. Still just a navigation
        # click within a view — no field on any tab is touched.
        page.get_by_text(tab_label, exact=True).first.evaluate("el => el.click()")
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(1500)

        html = page.content()
        (output_dir / f"user_{tab_label.lower()}_dump.html").write_text(html, encoding="utf-8")
        page.screenshot(path=str(output_dir / f"user_{tab_label.lower()}.png"), full_page=True)

        print(f"Saved HTML and screenshot for tab '{tab_label}' to {output_dir}")
        print("STOPPING HERE — no further clicks. Nothing was saved or modified.")

        browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
