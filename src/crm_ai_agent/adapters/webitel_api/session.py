"""Loads Webitel API auth (cookies + access-token) from the
Playwright-produced ``storage_state.json``, for use by the direct-API
adapters in this package.

No login logic lives here — see
``scripts/webitel_login_and_save_session.py`` for how that session file
gets produced and refreshed (it can go stale after some minutes; rerun that
script if calls here start failing with an auth error). This module only
reads the file.

Deliberately NOT reading auth from a captured Postman export: a token
copied out of a Postman collection is a snapshot that goes stale the moment
the session it came from expires, and re-baking a fresh one into source
files each time is exactly the kind of secret-in-a-committed-file risk this
project has avoided everywhere else (see info.env's own gitignore entry).
Reading the same live session file the rest of the project already
refreshes keeps there being exactly one place auth comes from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class WebitelSessionError(Exception):
    pass


@dataclass(frozen=True)
class WebitelApiSession:
    cookie_header: str
    access_token: str

    def headers(self) -> dict[str, str]:
        return {
            "X-Webitel-Access": self.access_token,
            "Cookie": self.cookie_header,
            "Accept": "application/json",
        }


def load_session(storage_state_path: Path) -> WebitelApiSession:
    if not storage_state_path.exists():
        raise WebitelSessionError(
            f"No saved Webitel session at {storage_state_path}. "
            "Log in via the console («Вход и настройки» → «Вход в Webitel») first."
        )
    data = json.loads(storage_state_path.read_text(encoding="utf-8"))

    cookies = {c["name"]: c["value"] for c in data.get("cookies", [])}
    if "WBTLAUTH" not in cookies:
        raise WebitelSessionError(
            f"Saved session at {storage_state_path} has no WBTLAUTH cookie — log in again."
        )
    cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items())

    access_token: str | None = None
    for origin in data.get("origins", []):
        for item in origin.get("localStorage", []):
            if item.get("name") == "access-token":
                access_token = item.get("value")
    if not access_token:
        raise WebitelSessionError(
            f"Saved session at {storage_state_path} has no access-token in localStorage — log in again."
        )

    return WebitelApiSession(cookie_header=cookie_header, access_token=access_token)
