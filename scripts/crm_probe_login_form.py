"""One-off diagnostic: dump the real <input> elements on the CRM's empty
login page (no credentials entered, nothing filled or submitted) so the
login script's selectors can be based on what's actually there instead of
generic guesses.

Usage:
    python scripts/crm_probe_login_form.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    if not base_url:
        print("Missing CRM_BASE_URL in info.env", file=sys.stderr)
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(base_url, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(2000)

        inputs = page.locator("input")
        count = inputs.count()
        print(f"Found {count} <input> elements after networkidle+settle:")
        for i in range(count):
            el = inputs.nth(i)
            attrs = el.evaluate(
                "el => ({id: el.id, name: el.name, type: el.type, "
                "placeholder: el.placeholder, autocomplete: el.autocomplete, "
                "visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)})"
            )
            print(f"  [{i}] {attrs}")

        buttons = page.locator("button")
        bcount = buttons.count()
        print(f"\nFound {bcount} <button> elements:")
        for i in range(bcount):
            el = buttons.nth(i)
            text = el.inner_text().strip()
            attrs = el.evaluate("el => ({id: el.id, type: el.type})")
            print(f"  [{i}] text={text!r} {attrs}")

        # Also check for iframes, which would explain 0 matches entirely.
        frames = page.frames
        print(f"\n{len(frames)} frame(s) on the page:")
        for f in frames:
            print(f"  - {f.url}")

        browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
