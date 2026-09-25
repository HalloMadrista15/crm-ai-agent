"""One-off read-only diagnostic: confirm how to reliably extract a field's
VALUE (not just find its data-item-marker label) from a live Creatio page.
Never fills, clicks a save control, or modifies anything — only navigates
and reads text via Playwright's live DOM queries.

Usage:
    python scripts/crm_probe_field_extraction.py <ticket_url>
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORAGE_STATE_PATH = ROOT / ".auth" / "crm_storage_state.json"

MARKERS = [
    "TsiOfficeNoteState Статус записки",
    "BnzEmployeeAccount Учетная запись сотрудника",
    "BnzInformationSystem Информационная система",
    "BnzComment Комментарии",
]


def main() -> int:
    from playwright.sync_api import sync_playwright

    if len(sys.argv) != 2:
        print("Usage: python scripts/crm_probe_field_extraction.py <ticket_url>", file=sys.stderr)
        return 1
    ticket_url = sys.argv[1]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STORAGE_STATE_PATH))
        page = context.new_page()
        page.goto(ticket_url, wait_until="domcontentloaded")
        # Creatio appears to keep some background poll/keepalive request
        # going, which can starve wait_for_load_state("networkidle") of
        # ever firing even once the page is fully usable — a fixed settle
        # delay after "load" is more reliable for this app than networkidle.
        page.wait_for_load_state("load", timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(4000)
        page.screenshot(path=str(ROOT / ".auth" / "probe_debug.png"))

        for marker in MARKERS:
            column_name = marker.split(" ", 1)[0]
            prefix = f"TsiOfficeNotesISAccessPage{column_name}"
            print(f"--- {marker} (column={column_name}) ---")

            candidates = page.locator(
                f'input[id^="{prefix}"], textarea[id^="{prefix}"]'
            )
            ccount = candidates.count()
            print(f"  input/textarea with id^={prefix!r}: {ccount}")
            for i in range(ccount):
                el = candidates.nth(i)
                attrs = el.evaluate("el => ({id: el.id, tag: el.tagName})")
                try:
                    val = el.input_value()
                except Exception as e:
                    val = f"<input_value failed: {e}>"
                print(f"    [{i}] {attrs} value={val!r}")

            if ccount == 0:
                # Fall back: maybe it's not an input/textarea at all (e.g. a
                # combobox rendered as a styled div/span for Status).
                any_el = page.locator(f'[id^="{prefix}"]')
                acount = any_el.count()
                print(f"  ANY element with id^={prefix!r}: {acount}")
                for i in range(min(acount, 8)):
                    el = any_el.nth(i)
                    attrs = el.evaluate("el => ({id: el.id, tag: el.tagName})")
                    text = el.inner_text().strip()
                    print(f"    [{i}] {attrs} inner_text={text!r}")
            print()

        browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
