"""One-off, read-only recon: dumps the real HTML attributes of every
input/button on the Webitel Astana login form, at each step, so the real
login helper script (webitel_astana_login_and_save_session.py) can use
confirmed selectors instead of generic guesses.

Never fills in the login/password itself — the human fills them; this
script only watches and records what's on screen every few seconds until
Ctrl+C or timeout. NOT part of the crm_ai_agent package.

Run manually, once, from a terminal:
    python scripts/webitel_astana_probe_login_form.py
"""

from __future__ import annotations

import time
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


DUMP_JS = """
() => {
  const describe = (el) => ({
    tag: el.tagName,
    type: el.getAttribute('type'),
    id: el.id,
    name: el.getAttribute('name'),
    placeholder: el.getAttribute('placeholder'),
    class: el.className,
    autocomplete: el.getAttribute('autocomplete'),
    visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
  });
  const inputs = Array.from(document.querySelectorAll('input')).map(describe);
  const buttons = Array.from(document.querySelectorAll('button')).map(describe);
  return {url: location.href, inputs, buttons};
}
"""


def main() -> int:
    from playwright.sync_api import sync_playwright

    env = _load_env_file(ROOT / "info.env")
    base_url = env.get("WEBITEL_ASTANA_BASE_URL")
    if not base_url:
        print("Missing WEBITEL_ASTANA_BASE_URL in info.env")
        return 1

    log_path = ROOT / ".auth" / "webitel_astana_probe_log.txt"
    log_path.parent.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_context().new_page()
        print(f"Opening {base_url} ...")
        page.goto(base_url, wait_until="domcontentloaded")

        print("Watching the form and logging every 3s for 3 minutes.")
        print("Please manually click through Step 1 -> Step 2 (domain/login/")
        print("password fields) so each step's real HTML gets recorded.")
        print(f"Log: {log_path}")

        with open(log_path, "w", encoding="utf-8") as f:
            last_signature = None
            for i in range(60):  # ~3 minutes at 3s each
                try:
                    result = page.evaluate(DUMP_JS)
                except Exception:
                    time.sleep(3)
                    continue
                # This is a single-page app — the URL doesn't change between
                # steps, so compare the visible input/button set instead.
                signature = str(result["inputs"]) + str(result["buttons"])
                if signature != last_signature:
                    f.write(f"\n=== snapshot {i} — url: {result['url']} ===\n")
                    f.write("inputs:\n")
                    for inp in result["inputs"]:
                        f.write(f"  {inp}\n")
                    f.write("buttons:\n")
                    for btn in result["buttons"]:
                        f.write(f"  {btn}\n")
                    f.flush()
                    last_signature = signature
                    print(f"[{i}] recorded new snapshot ({len(result['inputs'])} inputs)")
                time.sleep(3)

        browser.close()

    print(f"Done. See {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
