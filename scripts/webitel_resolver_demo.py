"""Manual smoke test for WebitelEntityResolver against the real system.
Read-only: only calls resolve()/get_current_state(), never a write method
(there is no write method on this class at all).

Usage:
    python scripts/webitel_resolver_demo.py <login>
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from crm_ai_agent.adapters.webitel_playwright.entity_resolver import WebitelEntityResolver  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/webitel_resolver_demo.py <login>", file=sys.stderr)
        return 1

    login = sys.argv[1]
    resolver = WebitelEntityResolver.from_info_env(
        info_env_path=ROOT / "info.env",
        storage_state_path=ROOT / ".auth" / "webitel_almaty_storage_state.json",
    )

    resolution = resolver.resolve(login)
    print(f"resolve({login!r}) -> match_count={resolution.match_count}, entity_id={resolution.entity_id!r}")

    if resolution.match_count == 1:
        state = resolver.get_current_state(resolution.entity_id)
        print(f"get_current_state({resolution.entity_id!r}) ->")
        for k, v in state.items():
            print(f"  {k}: {v}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
