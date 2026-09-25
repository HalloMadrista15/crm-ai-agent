"""Read-only reconnaissance: open a user's tab, click the dropdown arrow
on the first multiselect field (to reveal its full checked-item list — a
view-only popup, not a data change), dump HTML + screenshot. Never clicks
the clear ("x") button, never checks/unchecks an item, never saves.

Usage:
    python scripts/webitel_dump_multiselect.py <login> <tab_label> <output_dir>
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
        print("Usage: python scripts/webitel_dump_multiselect.py <login> <tab_label> <output_dir>", file=sys.stderr)
        return 1

    login, tab_label, output_dir_arg = sys.argv[1], sys.argv[2], sys.argv[3]
    output_dir = Path(output_dir_arg)
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

        page.get_by_text(tab_label, exact=True).first.evaluate("el => el.click()")
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(1500)

        # View-only: opens the multiselect's own dropdown popup to reveal
        # every checked item. Never touches the "clear" (x) control.
        page.locator('button[icon="arrow-down"]').first.evaluate("el => el.click()")
        page.wait_for_timeout(800)

        html = page.content()
        (output_dir / f"{tab_label.lower()}_expanded_dump.html").write_text(html, encoding="utf-8")
        page.screenshot(path=str(output_dir / f"{tab_label.lower()}_expanded.png"), full_page=True)

        print(f"Saved expanded-dropdown dump for tab '{tab_label}' to {output_dir}")
        print("STOPPING HERE — no further clicks. Nothing was saved or modified.")

        browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
