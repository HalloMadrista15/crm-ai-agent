"""Launcher for the local operator console (src/crm_ai_agent/webapp/server.py).

Usage:
    python scripts/run_console.py [port]

Opens no browser tab automatically — go to http://127.0.0.1:<port> yourself.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from crm_ai_agent.webapp.server import run  # noqa: E402

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    run(port=port)
