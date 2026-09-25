"""Read-only Webitel entity resolver over the real REST API, instead of
Playwright UI clicks (see ``adapters/webitel_playwright/entity_resolver.py``
for that earlier version). The API shape here is confirmed from a real
Postman collection the project owner shared for inspection on 2026-09-11
(their own working requests, built while testing create/delete/role-assign
themselves) — never fabricated or guessed.

Every request this module makes is a GET. There is no PUT/POST/DELETE
anywhere in this file, and there must never be one added here — a mutation
adapter, when that work is authorized, belongs in its own separate module,
so "can this class read" and "can this class write" are never the same
import. This mirrors the same rule already stated in the Playwright
version's module docstring.

Confirmed real endpoints (from the shared collection):
    GET    /api/users?page=&size=&q=<query>   search (substring match)
    GET    /api/users/{id}                    full user record
    GET    /api/roles?q=<name>&fields=...      role lookup by name
    PUT    /api/users/{id}                    update (NOT used here)
    POST   /api/users                         create (NOT used here)
    DELETE /api/users/{id}?permanent=true     hard delete (NOT used here)
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from crm_ai_agent.adapters.webitel_api.session import WebitelApiSession, load_session
from crm_ai_agent.domain.entities import ResolvedEntity
from crm_ai_agent.ports.entity_resolver import SubjectNotFoundError

_REQUEST_TIMEOUT_SECONDS = 15


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


class WebitelApiError(Exception):
    pass


class WebitelApiEntityResolver:
    def __init__(self, *, base_url: str, session: WebitelApiSession) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = session

    @classmethod
    def from_info_env(cls, info_env_path: Path, storage_state_path: Path) -> "WebitelApiEntityResolver":
        env = _load_env_file(info_env_path)
        base_url = env.get("WEBITEL_ALMATY_BASE_URL")
        if not base_url:
            raise ValueError(f"Missing WEBITEL_ALMATY_BASE_URL in {info_env_path}")
        return cls(base_url=base_url, session=load_session(storage_state_path))

    def _get(self, path: str) -> Any:
        url = f"{self._base_url}{path}"
        request = urllib.request.Request(url, headers=self._session.headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise WebitelApiError(f"GET {path} failed: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise WebitelApiError(f"GET {path} failed: {exc.reason}") from exc
        return json.loads(body)

    def _search_users(self, query: str) -> list[dict[str, Any]]:
        query_param = urllib.parse.quote(query)
        data = self._get(f"/api/users?page=1&size=10&q={query_param}")
        return data if isinstance(data, list) else data.get("items", [])

    def resolve(self, query: str) -> ResolvedEntity:
        """``query`` is expected to be an extension/login (e.g. "10078").

        Webitel's search endpoint does substring matching (searching "1007"
        would also return "10078"), so this filters results down to an
        EXACT ``extension`` match itself rather than trusting the API's raw
        result count — the same ``users.find(u => u.extension === ext)``
        filter the project owner's own Postman scripts apply for the same
        reason.
        """

        matches = [u for u in self._search_users(query) if u.get("extension") == query]

        if len(matches) == 1:
            return ResolvedEntity(query=query, match_count=1, entity_id=query, source="webitel_api")
        return ResolvedEntity(query=query, match_count=len(matches), source="webitel_api")

    def get_current_state(self, entity_id: str) -> dict[str, str]:
        """Read ``roles``, ``license``, and ``group`` for the user whose
        extension is ``entity_id``.

        Role values are human-readable names (e.g. "sysadmin"), read
        directly from the API's ``roles: [{id, name}]``. License values are
        the API's own opaque IDs (GUIDs): unlike the Playwright/UI adapter
        — which reads the human-readable license type names shown in that
        dropdown (e.g. "CALL_CENTER") — the API's ``license: [{id}]`` for a
        user carries no name field at all in the shape confirmed so far.
        This is a real, confirmed difference between the two adapters, not
        an oversight.
        """

        matches = [u for u in self._search_users(entity_id) if u.get("extension") == entity_id]
        if not matches:
            raise SubjectNotFoundError(f"no Webitel user found for extension={entity_id!r}")

        user = self._get(f"/api/users/{matches[0]['id']}")

        roles = [r.get("name", "") for r in user.get("roles", []) if r.get("name")]
        license_ids = [lic.get("id", "") for lic in user.get("license", []) if lic.get("id")]
        group = (user.get("profile") or {}).get("group", "")

        return {
            "roles": ",".join(roles),
            "license": ",".join(license_ids),
            "group": group,
        }
