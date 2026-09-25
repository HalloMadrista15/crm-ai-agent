"""Read-only reconnaissance: open the "Задачи на согласование" list page
via the left nav and dump its HTML + screenshot. Never clicks into an
individual row, never fills/submits anything.

Usage:
    python scripts/crm_dump_approval_tasks_list.py <output_dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORAGE_STATE_PATH = ROOT / ".auth" / "crm_storage_state.json"


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

    if len(sys.argv) != 2:
        print("Usage: python scripts/crm_dump_approval_tasks_list.py <output_dir>", file=sys.stderr)
        return 1
    output_dir = Path(sys.argv[1])
    output_dir.mkdir(parents=True, exist_ok=True)

    env = _load_env_file(ROOT / "info.env")
    base_url = env.get("CRM_BASE_URL")
    if not base_url:
        print("Missing CRM_BASE_URL in info.env", file=sys.stderr)
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STORAGE_STATE_PATH))
        page = context.new_page()

        page.goto(base_url, wait_until="domcontentloaded")
        page.wait_for_load_state("load", timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        page.screenshot(path=str(output_dir / "before_click.png"))

        page.get_by_text("Задачи на согласование", exact=False).first.click(timeout=15000)
        page.wait_for_load_state("load", timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        html = page.content()
        (output_dir / "approval_tasks_dump.html").write_text(html, encoding="utf-8")
        page.screenshot(path=str(output_dir / "approval_tasks.png"), full_page=True)
        print(f"Saved HTML ({len(html)} bytes) and screenshot to {output_dir}")
        print(f"Final URL: {page.url}")

        browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
