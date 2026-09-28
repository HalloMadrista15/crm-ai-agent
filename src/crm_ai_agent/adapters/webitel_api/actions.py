"""The FIRST write-capable module in this project — real PUT/POST requests
against the Webitel API. Everything before this (both webitel_playwright
and webitel_api's entity_resolver.py) was strictly read-only by design.

Confirmed real body shapes and endpoints from a Postman collection the
project owner shared and had already tested themselves (2026-09-11,
create-user shape confirmed 2026-09-20 from the same collection's "Массовое
создание пользователей" request) — see docs/action_catalog.md's
provision_webitel_user entry for the scenario this serves, and
safety/action_registry.py's PROVISION_WEBITEL_USER spec, which this module
now fully implements (new_user_login/new_user_name/new_user_extension were
always part of that spec — build_plan/execute_plan below were an interim
narrower "update an existing user" implementation of the same action).
Never fabricated.

Safety posture, matching everything built so far:
- ``build_plan``/``build_create_plan``/``build_delete_plan`` are pure read
  (GETs only) and never touch any user's data. ``execute_plan``,
  ``execute_create_plan``, and ``execute_delete_plan`` are the only
  functions in this project that perform a write/delete, and each is
  reachable only through the console's explicit two-step "show plan, then
  confirm" flow — never auto-triggered by a plan or ticket read.
- ``execute_delete_plan`` (added 2026-09-20, real endpoint/shape confirmed
  from the same Postman collection's "Массовое удаление пользователей"
  request: ``DELETE /api/users/{id}?permanent=true``) re-fetches the user
  by extension immediately before deleting (never trusts a stale plan-time
  id) so it can't be tricked into deleting the wrong record if the
  extension got reassigned between "show plan" and "confirm".
- ``execute_plan`` re-fetches the target user fresh immediately before
  writing (not reusing the plan-time snapshot) so a change made to the
  target between "show me the plan" and "confirm" isn't silently
  clobbered, and only ever modifies ``roles``, ``license``, and
  ``profile.group`` on the target's existing record — every other field
  (name, username, extension, devices, contact, chat_name, ...) is carried
  through unchanged, because Webitel's PUT replaces the whole record and
  omitting a field would blank it out.
- ``execute_create_plan`` generates a fresh random password at write time
  via ``secrets`` — never reuses any hardcoded password (the Postman
  collection's own script hardcodes one; this module deliberately does
  not). ``force_password_change`` is left off (2026-09-21 operator
  decision — the generated password is used as-is, not forced to change
  on first login).
- Neither build_plan nor build_create_plan ever includes a password in the
  plan shown to the approver — there's nothing secret to approve, only the
  decision of *what* to clone.
"""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from crm_ai_agent.adapters.webitel_api.session import WebitelApiSession, load_session
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


class WebitelApiActions:
    def __init__(self, *, base_url: str, session: WebitelApiSession) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = session

    @classmethod
    def from_info_env(cls, info_env_path: Path, storage_state_path: Path, *, city: str = "almaty") -> "WebitelApiActions":
        """``city`` selects which ``WEBITEL_<CITY>_BASE_URL`` key to read
        (e.g. "almaty" -> WEBITEL_ALMATY_BASE_URL, "astana" ->
        WEBITEL_ASTANA_BASE_URL — confirmed 2026-09-27 for a second real
        instance). Defaults to "almaty" to match every caller that existed
        before multi-city support."""

        env = _load_env_file(info_env_path)
        key = f"WEBITEL_{city.upper()}_BASE_URL"
        base_url = env.get(key)
        if not base_url:
            raise ValueError(f"Missing {key} in {info_env_path}")
        return cls(base_url=base_url, session=load_session(storage_state_path))

    def _get(self, path: str) -> Any:
        url = f"{self._base_url}{path}"
        request = urllib.request.Request(url, headers=self._session.headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise WebitelApiError(f"GET {path} failed: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise WebitelApiError(f"GET {path} failed: {exc.reason}") from exc

    def _put(self, path: str, body: dict[str, Any]) -> Any:
        url = f"{self._base_url}{path}"
        payload = json.dumps(body).encode("utf-8")
        headers = {**self._session.headers(), "Content-Type": "application/json"}
        request = urllib.request.Request(url, data=payload, headers=headers, method="PUT")
        try:
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WebitelApiError(f"PUT {path} failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise WebitelApiError(f"PUT {path} failed: {exc.reason}") from exc

    def _delete(self, path: str) -> None:
        url = f"{self._base_url}{path}"
        request = urllib.request.Request(url, headers=self._session.headers(), method="DELETE")
        try:
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WebitelApiError(f"DELETE {path} failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise WebitelApiError(f"DELETE {path} failed: {exc.reason}") from exc

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        url = f"{self._base_url}{path}"
        payload = json.dumps(body).encode("utf-8")
        headers = {**self._session.headers(), "Content-Type": "application/json"}
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WebitelApiError(f"POST {path} failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise WebitelApiError(f"POST {path} failed: {exc.reason}") from exc

    def check_session(self) -> None:
        """Read-only: the cheapest authenticated request, so the console can
        tell an active saved session from an expired one. Raises
        WebitelApiError (HTTP 401 when expired)."""
        self._get("/api/users?page=1&size=1&fields=id")

    def get_raw_user(self, extension: str) -> dict[str, Any]:
        """The full, unmodified user record — everything a PUT body needs
        to carry through unchanged. Raises SubjectNotFoundError if no user
        has this extension."""

        query = urllib.parse.quote(extension)
        data = self._get(f"/api/users?page=1&size=10&q={query}")
        users = data if isinstance(data, list) else data.get("items", [])
        matches = [u for u in users if u.get("extension") == extension]
        if not matches:
            raise SubjectNotFoundError(f"no Webitel user found for extension={extension!r}")
        return self._get(f"/api/users/{matches[0]['id']}")

    def find_neighbors(self, extension: str, *, radius: int = 2) -> list[dict[str, Any]]:
        """Read-only: looks up the ``radius`` extensions on either side of
        ``extension`` (e.g. 10085-10086 and 10088-10089 for "10087" with
        radius=2) and returns whichever of them exist. Meant to help an
        operator find a plausible template user by desk/extension
        proximity without already knowing a name — but proximity is only a
        suggestion, not a guarantee: extensions can belong to meeting
        rooms or other non-people (confirmed 2026-09-20 with 10086 —
        "Meeting Room Yellow" — right next to a real colleague at 10088),
        so the operator still picks which suggestion, if any, is right."""

        try:
            base = int(extension)
        except ValueError as exc:
            raise WebitelApiError(f"extension={extension!r} is not numeric — cannot compute neighbors") from exc

        results: list[dict[str, Any]] = []
        for offset in list(range(-radius, 0)) + list(range(1, radius + 1)):
            candidate = str(base + offset)
            try:
                user = self.get_raw_user(candidate)
            except SubjectNotFoundError:
                continue
            results.append(
                {
                    "extension": candidate,
                    "name": user.get("name", ""),
                    "roles": [r.get("name", "") for r in user.get("roles", [])],
                    "group": (user.get("profile") or {}).get("group", ""),
                }
            )
        results.sort(key=lambda r: int(r["extension"]))
        return results

    def build_plan(self, *, template_extension: str, target_extension: str) -> dict[str, Any]:
        """Read-only: two GETs, no data touched. Returns a plan dict meant
        to be shown to a human before ``execute_plan`` is ever called."""

        template = self.get_raw_user(template_extension)
        target = self.get_raw_user(target_extension)

        return {
            "template_extension": template_extension,
            "template_name": template.get("name", ""),
            "target_extension": target_extension,
            "target_id": target["id"],
            "target_name": target.get("name", ""),
            "roles": template.get("roles", []),
            "license": template.get("license", []),
            "group": (template.get("profile") or {}).get("group", ""),
        }

    def execute_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Update an EXISTING user's roles/license/group. Re-fetches the
        target fresh (not the plan-time snapshot) and only overwrites
        roles/license/group — every other field on the record is
        preserved as-is.

        Never touches the password UNLESS ``plan["reset_password"]`` is
        explicitly ``True`` (2026-09-21 operator request) — off by
        default, since the whole point of this action historically was to
        leave everything but roles/license/group alone. When set, a fresh
        random password is generated (never reused/hardcoded, matching
        execute_create_plan) and returned as ``new_password`` in the
        result so it can be shown to the approver once."""

        target = self._get(f"/api/users/{plan['target_id']}")
        target["roles"] = plan["roles"]
        target["license"] = plan["license"]
        target.setdefault("profile", {})
        target["profile"]["group"] = plan["group"]

        new_password = None
        if plan.get("reset_password"):
            new_password = secrets.token_urlsafe(12)
            target["password"] = new_password

        result = self._put(f"/api/users/{plan['target_id']}", target)
        if new_password is not None:
            result = {**result, "new_password": new_password}
        return result

    def build_create_plan(
        self, *, template_extension: str, new_username: str, new_name: str, new_extension: str
    ) -> dict[str, Any]:
        """Read-only: one GET for the template, plus a check that
        ``new_extension`` isn't already taken (creating a duplicate would
        silently shadow an existing user — refuse instead). No password is
        included; ``execute_create_plan`` generates one fresh at write
        time."""

        template = self.get_raw_user(template_extension)

        try:
            self.get_raw_user(new_extension)
        except SubjectNotFoundError:
            pass
        else:
            raise WebitelApiError(f"extension={new_extension!r} is already in use — refusing to create a duplicate")

        return {
            "template_extension": template_extension,
            "template_name": template.get("name", ""),
            "new_username": new_username,
            "new_name": new_name,
            "new_extension": new_extension,
            "roles": template.get("roles", []),
            "license": template.get("license", []),
            "group": (template.get("profile") or {}).get("group", ""),
        }

    def execute_create_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        """POST a brand-new user, then a matching device (real request
        shape confirmed 2026-09-21 from Webitel's own "New device" form —
        without this, the account has no SIP endpoint and can't actually
        register a phone/softphone). Generates a fresh random temporary
        password here (see module docstring); the device is created with
        that same password, matching what the real UI form does.

        Also sets that device as the user's *default* device via a
        follow-up PUT: creating the device links it to the user (it shows
        up in the user's ``devices`` list automatically), but leaves the
        user's own ``device`` field (singular — the "Default device"
        dropdown in Webitel's own UI, marked required there) unset. Real
        gap confirmed 2026-09-21 by creating a user through this exact
        code path and finding "Default device" still empty in the UI.
        Re-fetches the user rather than trusting the device POST
        response's own shape (unconfirmed) to find the device id."""

        temporary_password = secrets.token_urlsafe(12)
        user_body = {
            "name": plan["new_name"],
            "email": "",
            "username": plan["new_username"],
            "password": temporary_password,
            "extension": plan["new_extension"],
            "roles": plan["roles"],
            "license": plan["license"],
            "devices": [],
            "contact": {},
            "chat_name": "",
            "force_password_change": False,
            "profile": {"group": plan["group"]} if plan.get("group") else {},
        }
        result = self._post("/api/users", user_body)

        device_body = {
            "name": plan["new_extension"],
            "account": plan["new_extension"],
            "password": temporary_password,
            "user": {
                "name": plan["new_name"],
                "status": "",
                "state": True,
                "dnd": False,
                "id": result["id"],
                "force_password_change": False,
            },
            "phone": {},
            "ip": "",
            "brand": "",
            "model": "",
            "mac": "",
        }
        self._post("/api/devices", device_body)

        fresh_user = self._get(f"/api/users/{result['id']}")
        devices = fresh_user.get("devices") or []
        if devices:
            fresh_user["device"] = devices[0]
            self._put(f"/api/users/{result['id']}", fresh_user)

        return {**result, "temporary_password": temporary_password}

    def get_presence_status(self, extension: str) -> str | None:
        """Read-only: the "{sip}"/"{dlg}"/... marker behind Webitel's own
        Web/SIP/Dlg/DnD status chips in the user list (confirmed
        2026-09-21 from the real search response's ``presence.status``
        field — absent entirely when idle, e.g. no live phone
        registration/call). Only present on the search/list response, not
        on the ``/api/users/{id}`` detail GET."""

        query = urllib.parse.quote(extension)
        data = self._get(f"/api/users?page=1&size=10&q={query}")
        users = data if isinstance(data, list) else data.get("items", [])
        matches = [u for u in users if u.get("extension") == extension]
        if not matches:
            raise SubjectNotFoundError(f"no Webitel user found for extension={extension!r}")
        presence = matches[0].get("presence")
        return presence.get("status") if presence else None

    @staticmethod
    def presence_labels(status: str | None) -> list[str]:
        """get_presence_status's marker ("{sip}", "{web,sip}", ...) as the
        chip names Webitel's own user list shows (Web/SIP/Dlg/DnD); empty
        when the user is idle. Unknown markers are passed through as-is
        rather than dropped, so nothing active is ever hidden."""
        if not status:
            return []
        known = {"web": "Web", "sip": "SIP", "dlg": "Dlg", "dnd": "DnD"}
        parts = [part.strip() for part in status.strip().strip("{}").split(",") if part.strip()]
        return [known.get(part.lower(), part) for part in parts]

    def build_delete_plan(self, extension: str) -> dict[str, Any]:
        """Read-only: one lookup of the user to be deleted, so the
        approver sees exactly who/what they're about to permanently
        remove before confirming — including a live-session warning
        (confirmed 2026-09-21: deleting a user with an active SIP
        registration or ongoing call/dialog tends to fail in Webitel) and
        the ids of any devices that will also be removed (confirmed the
        same day: deleting a user does NOT cascade-delete their device —
        it's left orphaned, still consuming a VOIP_DEVICE license slot,
        unless removed explicitly)."""

        user = self.get_raw_user(extension)
        presence_status = self.get_presence_status(extension)
        return {
            "extension": extension,
            "id": user["id"],
            "name": user.get("name", ""),
            "username": user.get("username", ""),
            "group": (user.get("profile") or {}).get("group", ""),
            "presence_status": presence_status,
            "presence": self.presence_labels(presence_status),
            # Never delete someone mid-call: execute_delete_plan re-checks
            # this at confirm time too, since the plan can be stale.
            "in_call": "Dlg" in self.presence_labels(presence_status),
            "device_ids": [d["id"] for d in user.get("devices", []) if d.get("id")],
        }

    def execute_delete_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Permanently deletes the user and every device listed in the
        plan. Re-fetches by extension right before deleting (never trusts
        the plan-time id) so a since-changed or since-reassigned extension
        can't cause the wrong record to be removed — see module docstring.

        Always logs the user out of every device first (real endpoint
        ``POST /api/users/{id}/logout``, confirmed 2026-09-21 from
        Webitel's own "End all sessions" button — same request shape the
        UI itself sends). Confirmed the same day: deleting a user with an
        active SIP registration or ongoing call tends to fail in Webitel,
        and this is what that button does to clear it — logging out first
        is a no-op for an already-idle user, so it's always safe to do
        unconditionally rather than only when build_delete_plan's
        presence_status warning was non-empty (which could be stale by
        confirm time anyway).

        Devices are deleted using the same
        ``DELETE /api/devices/{id}?permanent=true`` shape as users
        (confirmed 2026-09-21 by testing it against a real orphaned
        device) — using plan['device_ids'] rather than re-fetching, since
        the user is about to be deleted anyway and a device re-fetch would
        need the user to still exist to look it up by extension."""

        fresh = self.get_raw_user(plan["extension"])
        if fresh["id"] != plan["id"]:
            raise WebitelApiError(
                f"extension={plan['extension']!r} now belongs to a different user "
                f"(id {fresh['id']!r}, was {plan['id']!r} when the plan was built) — refusing to delete"
            )
        # Re-checked here, not trusted from the plan: a call may have
        # started since "Показать план". Logging out would drop it.
        if "Dlg" in self.presence_labels(self.get_presence_status(plan["extension"])):
            raise WebitelApiError(
                f"{plan['extension']} сейчас в разговоре (Dlg) — удаление отменено, повторите после окончания звонка"
            )
        self._post(f"/api/users/{plan['id']}/logout", {})
        for device_id in plan.get("device_ids", []):
            self._delete(f"/api/devices/{device_id}?permanent=true")
        self._delete(f"/api/users/{plan['id']}?permanent=true")
        return {
            "deleted": True,
            "id": plan["id"],
            "extension": plan["extension"],
            "name": plan.get("name", ""),
            "deleted_device_ids": plan.get("device_ids", []),
        }
