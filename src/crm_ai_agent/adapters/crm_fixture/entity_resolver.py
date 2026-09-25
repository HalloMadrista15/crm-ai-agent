"""A ``EntityResolver`` backed by a static JSON file mapping a query string
(e.g. a requester email) to zero, one, or many candidate CRM user records.

Fixture shape (see tests/fixtures/crm_users.json): a JSON object whose keys
are query strings and whose values are lists of
``{"user_id", "first_name", "last_name"}`` records. Zero matches -> key
absent or an empty list. Multiple matches -> more than one record under the
same key, exactly the ambiguous case ``ResolvedEntity``/the Policy Engine
must refuse to act on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from crm_ai_agent.domain.entities import ResolvedEntity
from crm_ai_agent.ports.entity_resolver import SubjectNotFoundError


class FixtureEntityResolver:
    def __init__(self, fixture_path: Path) -> None:
        self._records: dict[str, list[dict[str, Any]]] = json.loads(
            fixture_path.read_text(encoding="utf-8")
        )

    def resolve(self, query: str) -> ResolvedEntity:
        matches = self._records.get(query, [])
        if len(matches) == 1:
            return ResolvedEntity(query=query, match_count=1, entity_id=matches[0]["user_id"])
        return ResolvedEntity(query=query, match_count=len(matches))

    def get_current_state(self, entity_id: str) -> dict[str, str]:
        for matches in self._records.values():
            for record in matches:
                if record["user_id"] == entity_id:
                    return {"first_name": record["first_name"], "last_name": record["last_name"]}
        raise SubjectNotFoundError(f"no CRM user record with user_id={entity_id!r}")
