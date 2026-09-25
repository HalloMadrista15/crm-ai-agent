"""Read-only reconnaissance: open one ticket URL using the saved session and
dump its rendered HTML + a screenshot to a local, non-repo scratch
directory for offline inspection.

Deliberately does NOT print any page text (title, field values, names) to
stdout/the console — only file paths and byte counts — since the ticket
page contains real employee PII. Inspect the saved files directly instead
of piping this script's output anywhere.

Never fills or submits anything, and never clicks a save/action button. The
one exception: an optional 4th argument names a tab label to click first
(e.g. "Согласование") — Creatio lazy-loads tab content, so viewing a
non-default tab requires switching to it. This only changes what is
displayed in the browser, not any stored data, so it stays within the
read-only recon scope agreed for this script. Uses the session saved by
crm_login_and_save_session.py; does not log in itself.

Usage:
    python scripts/crm_dump_ticket.py <ticket_url> <output_dir> [tab_label]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORAGE_STATE_PATH = ROOT / ".auth" / "crm_storage_state.json"


def main() -> int:
    from playwright.sync_api import sync_playwright

    if len(sys.argv) not in (3, 4):
        print("Usage: python scripts/crm_dump_ticket.py <ticket_url> <output_dir> [tab_label]", file=sys.stderr)
        return 1

    ticket_url = sys.argv[1]
    output_dir = Path(sys.argv[2])
    tab_label = sys.argv[3] if len(sys.argv) == 4 else None
    output_dir.mkdir(parents=True, exist_ok=True)

    if not STORAGE_STATE_PATH.exists():
        print(f"No saved session at {STORAGE_STATE_PATH}. Run crm_login_and_save_session.py first.", file=sys.stderr)
        return 1

    html_path = output_dir / "ticket_dump.html"
    screenshot_path = output_dir / "ticket_dump.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STORAGE_STATE_PATH))
        page = context.new_page()

        page.goto(ticket_url, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(2000)  # Creatio's SPA finishes rendering slightly after networkidle

        if tab_label:
            # Read-only: switches the visible tab, does not write anything.
            # Scoped to the card's own tab strip (.ts-tabpanel-items) so it
            # cannot match an unrelated left-nav link containing the same
            # substring (e.g. "Задачи на согласование").
            page.locator(".ts-tabpanel-items li", has_text=tab_label).first.click(timeout=15000)
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(2000)

        html = page.content()
        html_path.write_text(html, encoding="utf-8")
        page.screenshot(path=str(screenshot_path), full_page=True)

        print(f"Saved HTML ({len(html)} bytes) to {html_path}")
        print(f"Saved screenshot to {screenshot_path}")

        browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
