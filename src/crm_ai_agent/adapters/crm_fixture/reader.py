"""A ``CrmTicketReader`` backed by static JSON fixtures.

This never touches a network or a browser. It exists for two reasons:

1. It lets everything above the CRM port (Policy Engine, canonical hashing,
   future Plan Compiler) be exercised end-to-end in tests without a real
   CRM, using anonymized fixture data
   (``tests/fixtures/tickets/*.json``) shaped like a real ticket export.
2. ``parse_ticket_export`` is the exact conversion the real Playwright
   adapter (Stage 3, not built) will also need to perform: raw CRM page
   content in, an immutable ``TicketSnapshot`` with trust-tagged fragments
   out. Writing and testing that conversion now, against a fixed JSON shape
   instead of a real page, means the Playwright adapter's job shrinks to
   "scrape the page into this same dict shape" rather than "invent the
   snapshot-building logic under time pressure."

The JSON fixture shape (see tests/fixtures/tickets/*.json for real examples)
is a plain dict with keys: ``ticket_id``, ``status``, ``title``,
``captured_at`` (ISO-8601), optional ``trusted_fields`` (dict of str->str),
and ``fragments`` (list of ``{section, source_reference, trust_level,
text}``). ``trust_level`` must be one of ``crm_ai_agent.domain.enums.TrustLevel``'s
values — this is what stops a fixture (or a future real scrape) from
smuggling untrusted text in as if it were a trusted CRM field.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from crm_ai_agent.domain.entities import TextFragment, TicketSnapshot
from crm_ai_agent.domain.enums import TrustLevel
from crm_ai_agent.ports.crm import TicketNotFoundError

SNAPSHOT_SCHEMA_VERSION = "1.0"


def parse_ticket_export(data: dict[str, Any], *, snapshot_id: str) -> TicketSnapshot:
    fragments = tuple(
        TextFragment(
            section=fragment["section"],
            source_reference=fragment["source_reference"],
            captured_at=datetime.fromisoformat(data["captured_at"]),
            trust_level=TrustLevel(fragment["trust_level"]),
            text=fragment["text"],
        )
        for fragment in data.get("fragments", [])
    )
    return TicketSnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=snapshot_id,
        ticket_id=data["ticket_id"],
        captured_at=datetime.fromisoformat(data["captured_at"]),
        source_hash=data.get("source_hash", ""),
        status=data["status"],
        title=data["title"],
        fragments=fragments,
        trusted_fields=data.get("trusted_fields", {}),
    )


class FixtureCrmReader:
    """Implements ``ports.crm.CrmTicketReader`` from a directory of JSON
    files named ``<external_ticket_id>.json``.
    """

    def __init__(self, fixtures_dir: Path) -> None:
        self._fixtures_dir = fixtures_dir

    def read_ticket(self, external_ticket_id: str) -> TicketSnapshot:
        path = self._fixtures_dir / f"{external_ticket_id}.json"
        if not path.exists():
            raise TicketNotFoundError(f"no fixture for external_ticket_id={external_ticket_id!r} at {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        snapshot_id = f"fixture-{external_ticket_id}"
        return parse_ticket_export(data, snapshot_id=snapshot_id)
