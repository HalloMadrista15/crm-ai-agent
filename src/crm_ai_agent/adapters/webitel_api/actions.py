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
- ``build_plan``/``build_create_plan``/``build_delete_plan``/
  ``build_group_plan`` are pure read (GETs only) and never touch any data.
  ``execute_plan``, ``execute_create_plan``, ``execute_delete_plan`` and
  ``execute_group_plan`` (a group number's Flow schema and Dialplan route,
  2026-11-02) are the
  only functions in this project that perform a write/delete, and each is
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
import re
import secrets
import string
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from crm_ai_agent.adapters.webitel_api.session import WebitelApiSession, load_session
from crm_ai_agent.ports.entity_resolver import SubjectNotFoundError

_REQUEST_TIMEOUT_SECONDS = 15

# Letters and digits only, without 0, o and O (2026-10-16 operator request):
# a password read off the screen or dictated to a colleague mixes them up.
PASSWORD_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "0oO")
PASSWORD_LENGTH = 16


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    """The one place this project makes up a password (a reset in
    execute_plan, a new user and its device in execute_create_plan): random
    from PASSWORD_ALPHABET, always with an upper-case letter, a lower-case
    letter and a digit. Rejects and redraws rather than patching characters
    in, so every allowed password stays equally likely."""
    while True:
        password = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))
        if (
            any(c.isupper() for c in password)
            and any(c.islower() for c in password)
            and any(c.isdigit() for c in password)
        ):
            return password


# How long a group's members ring, per city whose group number template this
# module has (the operator's own, pasted 2026-11-02/03). A city missing here
# has no template yet, and its group plan is refused.
GROUP_RING_SECONDS = {"almaty": "30", "astana": "15"}
# Only Astana's template may end by handing an unanswered call to one more
# number — and only some of its groups do.
GROUP_FALLBACK_CITIES = ("astana",)


def _group_start(name: str) -> list[dict[str, Any]]:
    return [
        {"ringReady": ""},
        {"preAnswer": ""},
        {"recordSession": {
            "action": "start", "bridged": True, "followTransfer": True,
            "minSec": 2, "stereo": True, "type": "mp3",
        }},
        {"log": f">> {name} <<<"},
    ]


def _ring_members(extensions: list[str], seconds: str) -> dict[str, Any]:
    return {"bridge": {
        "endpoints": [
            {"extension": extension, "parameters": {"leg_timeout": seconds}, "type": "user"}
            for extension in extensions
        ],
        "strategy": "fork",
    }}


def group_schema(city: str, name: str, extensions: list[str], fallback: str = "") -> list[dict[str, Any]]:
    """A group number's Flow schema: that city's template with only the log
    line (the group's name) and the members filled in — all ringing at once
    ("fork"). Almaty adds ringback tones (``transfer`` 488 is an id in
    Almaty's Webitel only) and ``busy`` once answered; Astana rings for 15 s
    instead of 30 and, given a ``fallback``, then calls that number if its
    user is idle (an int there, as in the operator's template)."""
    seconds = GROUP_RING_SECONDS.get(city)
    if seconds is None:
        raise WebitelApiError("для этого города шаблона группового номера пока нет")
    if fallback and city not in GROUP_FALLBACK_CITIES:
        raise WebitelApiError("в шаблоне этого города нет номера на случай, если никто не ответил")
    set_step = {"set": {"continue_on_fail": "true", "hangup_after_bridge": "true"}}
    if city == "almaty":
        tone = {"name": "%(1000,6000,425);loops=-1", "type": "tone"}
        return [
            set_step,
            {"ringback": {"all": True, "call": dict(tone), "hold": dict(tone), "transfer": {"id": 488}}},
            *_group_start(name),
            _ring_members(extensions, seconds),
            {"bridge": {"bridged": [{"log": "Ok call is bridged"}, {"set": {"busy": "2"}}, {"log": "END call is bridged"}]}},
            {"hangup": ""},
        ]
    fallback_step = [{"bridge": {"endpoints": [{"extension": int(fallback), "idle": True, "type": "user"}]}}] if fallback else []
    return [
        set_step,
        *_group_start(name),
        _ring_members(extensions, seconds),
        *fallback_step,
        {"hangup": ""},
    ]


def _check_group_numbers(number: str, extensions: list[str], fallback: str) -> None:
    """The group number goes into the route's pattern, so only digits
    (anything else could make a pattern that catches other numbers too); the
    fallback goes into the schema as an int, so no leading 0 either. Neither
    may lead back to the group number: the call would just come round
    again."""
    if not re.fullmatch(r"[0-9]+", number):
        raise WebitelApiError(f"групповой номер «{number}» — только цифры")
    if number in extensions:
        raise WebitelApiError(f"групповой номер {number} не может быть среди участников")
    if fallback and not re.fullmatch(r"[1-9][0-9]*", fallback):
        raise WebitelApiError(f"номер «если никто не ответил» «{fallback}» — только цифры")
    if fallback == number:
        raise WebitelApiError("если никто не ответил, звонок не может уйти обратно на групповой номер")


# The fields Webitel's Devices form sends on «Save», with what it sends for
# one left empty — a device's GET omits empty fields.
_DEVICE_FORM_FIELDS: dict[str, Any] = {
    "name": "", "account": "", "user": {}, "phone": {}, "ip": "", "brand": "", "model": "", "mac": "",
    "hotdesks": [], "hotdesk": False,
}


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

    def _patch(self, path: str, body: Any) -> Any:
        url = f"{self._base_url}{path}"
        payload = json.dumps(body).encode("utf-8")
        headers = {**self._session.headers(), "Content-Type": "application/json"}
        request = urllib.request.Request(url, data=payload, headers=headers, method="PATCH")
        try:
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WebitelApiError(f"PATCH {path} failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise WebitelApiError(f"PATCH {path} failed: {exc.reason}") from exc

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

    def get_permissions(self, user_id: str) -> list[dict[str, Any]]:
        """Read-only: the user's "Permissions" tab — who may read/edit/
        delete this user record. Confirmed 2026-10-16 against the real
        system: ``GET /api/users/{id}/acl?page=N&size=M`` returns
        ``{"page", "next" (only while more pages follow), "items": [{"grantee":
        {"id", "name", "user": true for a person, absent for a role},
        "granted": "r" | "rw" | "xrwdxrwd" ...}]}``."""

        items: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._get(f"/api/users/{user_id}/acl?page={page}&size=100")
            items.extend(data.get("items") or [])
            if not data.get("next"):
                return items
            page += 1

    def _permissions_to_copy(self, template_id: str, target_id: str | None = None) -> list[dict[str, Any]]:
        """The template's role grants the target doesn't already have as-is
        (missing, or set differently); all of them for a user not created
        yet (``target_id`` None). Grants to individual people
        (``grantee.user``) — the record's creator, an admin — are left out:
        copying them would hand whoever created the template full rights
        over every user cloned from it."""

        current = {} if target_id is None else {
            p["grantee"]["id"]: p.get("granted", "") for p in self.get_permissions(target_id)
        }
        return [
            {
                "grantee": p["grantee"]["id"],
                "name": p["grantee"].get("name", ""),
                "granted": p.get("granted", ""),
                "current": current.get(p["grantee"]["id"], ""),
            }
            for p in self.get_permissions(template_id)
            if not p["grantee"].get("user") and current.get(p["grantee"]["id"]) != p.get("granted", "")
        ]

    def build_plan(self, *, template_extension: str, target_extension: str, copy_permissions: bool = False) -> dict[str, Any]:
        """Read-only: GETs only, no data touched. Returns a plan dict meant
        to be shown to a human before ``execute_plan`` is ever called. With
        ``copy_permissions`` (the "Permissions" checkbox, 2026-10-16) it
        also lists the template's role grants to add to the target — on top
        of what the target has, never removing any (operator decision)."""

        template = self.get_raw_user(template_extension)
        target = self.get_raw_user(target_extension)

        plan = {
            "template_extension": template_extension,
            "template_name": template.get("name", ""),
            "target_extension": target_extension,
            "target_id": target["id"],
            "target_name": target.get("name", ""),
            "roles": template.get("roles", []),
            "license": template.get("license", []),
            "group": (template.get("profile") or {}).get("group", ""),
        }
        if copy_permissions:
            plan["copy_permissions"] = True
            plan["permissions"] = self._permissions_to_copy(template["id"], target["id"])
        return plan

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
        result so it can be shown to the approver once.

        With ``plan["copy_permissions"]`` the plan's ``permissions`` are then
        granted on the target — see _grant_permissions."""

        grants = self._planned_grants(plan)

        target = self._get(f"/api/users/{plan['target_id']}")
        target["roles"] = plan["roles"]
        target["license"] = plan["license"]
        target.setdefault("profile", {})
        target["profile"]["group"] = plan["group"]

        new_password = None
        if plan.get("reset_password"):
            new_password = generate_password()
            target["password"] = new_password

        result = self._put(f"/api/users/{plan['target_id']}", target)
        if new_password is not None:
            result = self._set_device_passwords(target, new_password, {**result, "new_password": new_password})
        return self._grant_permissions(plan["target_id"], grants, result)

    def _set_device_passwords(self, user: dict[str, Any], password: str, result: dict[str, Any]) -> dict[str, Any]:
        """Gives each of the user's devices the user's new password: the two
        must match (operator rule, 2026-11-03), and a phone registers with
        the device's. Runs after the user's own password was already
        changed, so a failure comes back as ``devices_error`` — with the new
        password still in the result — instead of an exception."""
        updated: list[str] = []
        errors: list[str] = []
        for device in user.get("devices") or []:
            label = device.get("name") or str(device.get("id", ""))
            try:
                self._set_device_password(str(device["id"]), password)
            except (WebitelApiError, KeyError) as exc:
                errors.append(f"{label}: {exc}")
                continue
            updated.append(label)
        result = {**result, "devices_updated": updated}
        return {**result, "devices_error": "; ".join(errors)} if errors else result

    def _set_device_password(self, device_id: str, password: str) -> None:
        """``PUT /api/devices/{id}`` with the body Webitel's own Devices form
        sends on «Save» (confirmed 2026-11-03): every field of the form, each
        carried over from the device's current record, only the password
        new. The PUT replaces the whole device, so a field left out would be
        blanked — a desk phone's MAC or model, say."""
        device = self._get(f"/api/devices/{device_id}")
        body = {key: device.get(key, default) for key, default in _DEVICE_FORM_FIELDS.items()}
        body["password"] = password
        self._put(f"/api/devices/{device_id}", body)

    @staticmethod
    def _planned_grants(plan: dict[str, Any]) -> list[dict[str, Any]]:
        """The plan's permissions as ``{"grantee": <int id>, "grants", "name"}``
        — built before any write, so a malformed plan fails before anything
        changes."""
        if not plan.get("copy_permissions"):
            return []
        return [
            {"grantee": int(p["grantee"]), "grants": p["granted"], "name": p.get("name", "")}
            for p in plan.get("permissions") or []
        ]

    def _grant_permissions(self, user_id: str, grants: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
        """Makes each planned grantee's permissions on ``user_id`` exactly the
        planned ones, via ``PATCH /api/users/{id}/acl`` (shape confirmed
        2026-10-16 from Webitel's own Permissions tab:
        ``[{"grantee": <id>, "grants": "r"}]``).

        That PATCH does not SET permissions, it FLIPS the letters it names
        (confirmed 2026-10-18 on test user 30120): "r" + "w" -> "rw", "rw" +
        "w" -> "r", "rw" + "rw" -> the grantee disappears. So the target's
        permissions are read first: a grantee that already has the planned
        ones is left alone, one it lacks gets them, and one with other
        permissions is first cleared (by sending what it has) and then given
        the planned ones. This matters for a brand-new user too: Webitel gives
        it some roles by default (semiadmin, practice seen), and sending those
        again would take them away — the first version did exactly that.
        Afterwards the permissions are read back and compared with the plan.

        This runs after the user was already written, so a failure comes
        back as ``permissions_error`` in the result instead of an exception —
        raising would lose a password that write already set."""
        if not grants:
            return result
        path = f"/api/users/{user_id}/acl"
        try:
            current = {str(p["grantee"]["id"]): p.get("granted", "") for p in self.get_permissions(user_id)}
        except WebitelApiError as exc:
            return {**result, "permissions_error": f"не удалось прочитать права перед записью: {exc}"}
        errors: list[str] = []
        for grant in grants:
            have = current.get(str(grant["grantee"]), "")
            if have == grant["grants"]:
                continue
            try:
                if have:
                    self._patch(path, [{"grantee": grant["grantee"], "grants": have}])
                self._patch(path, [{"grantee": grant["grantee"], "grants": grant["grants"]}])
            except WebitelApiError as exc:
                errors.append(f"{grant['name'] or grant['grantee']}: {exc}")
        try:
            actual = {str(p["grantee"]["id"]): p.get("granted", "") for p in self.get_permissions(user_id)}
        except WebitelApiError as exc:
            return {**result, "permissions_error": f"не удалось проверить права после записи: {exc}"}
        missing = [g for g in grants if actual.get(str(g["grantee"])) != g["grants"]]
        if missing:
            names = ", ".join(f"{g['name'] or g['grantee']} ({g['grants']})" for g in missing)
            detail = f" — {'; '.join(errors)}" if errors else ""
            return {**result, "permissions_error": f"не встали: {names}{detail}",
                    "permissions_copied": len(grants) - len(missing)}
        return {**result, "permissions_copied": len(grants)}

    def build_create_plan(
        self,
        *,
        template_extension: str,
        new_username: str,
        new_name: str,
        new_extension: str,
        copy_permissions: bool = False,
    ) -> dict[str, Any]:
        """Read-only: one GET for the template, plus a check that
        ``new_extension`` isn't already taken (creating a duplicate would
        silently shadow an existing user — refuse instead). No password is
        included; ``execute_create_plan`` generates one fresh at write
        time. With ``copy_permissions`` (the "Permissions" checkbox,
        2026-10-16) it also lists the template's role grants for the new
        user, as in build_plan."""

        template = self.get_raw_user(template_extension)

        try:
            self.get_raw_user(new_extension)
        except SubjectNotFoundError:
            pass
        else:
            raise WebitelApiError(f"extension={new_extension!r} is already in use — refusing to create a duplicate")

        plan = {
            "template_extension": template_extension,
            "template_name": template.get("name", ""),
            "new_username": new_username,
            "new_name": new_name,
            "new_extension": new_extension,
            "roles": template.get("roles", []),
            "license": template.get("license", []),
            "group": (template.get("profile") or {}).get("group", ""),
        }
        if copy_permissions:
            plan["copy_permissions"] = True
            plan["permissions"] = self._permissions_to_copy(template["id"])
        return plan

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
        response's own shape (unconfirmed) to find the device id.

        With ``plan["copy_permissions"]`` the template's role grants go on
        the new user last — see _grant_permissions."""

        grants = self._planned_grants(plan)
        temporary_password = generate_password()
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

        return self._grant_permissions(result["id"], grants, {**result, "temporary_password": temporary_password})

    def find_schema(self, name: str) -> dict[str, Any] | None:
        """Read-only: the Flow schema named exactly ``name``, or None. The
        search (``q``) also matches parts of names, so names are compared
        here. List shape assumed to be Webitel's usual ``{"items": [...]}``
        like /api/users — not yet seen from the real Flow page."""
        query = urllib.parse.quote(name)
        data = self._get(f"/api/routing/schema?page=1&size=100&q={query}")
        items = data if isinstance(data, list) else data.get("items") or []
        return next((item for item in items if item.get("name") == name), None)

    def find_route(self, number: str) -> dict[str, Any] | None:
        """Read-only: the Dialplan route whose pattern is exactly
        ``^<number>$`` (what execute_group_plan creates), or None. Reads
        every route page by page (``next`` while more follow, as in
        get_permissions) and compares patterns here, since what the search
        matches is unknown. List shape assumed, not yet seen from the real
        Dialplan page."""
        pattern = f"^{number}$"
        for page in range(1, 101):
            data = self._get(f"/api/routing/outbound/calls?page={page}&size=100")
            items = data if isinstance(data, list) else data.get("items") or []
            found = next((item for item in items if item.get("pattern") == pattern), None)
            if found is not None or isinstance(data, list) or not data.get("next"):
                return found
        return None

    def _check_group_is_new(self, name: str, number: str) -> None:
        if self.find_schema(name) is not None:
            raise WebitelApiError(f"схема «{name}» уже есть в Webitel — дубликат не создаю")
        route = self.find_route(number)
        if route is not None:
            raise WebitelApiError(f"на номер {number} уже есть маршрут Dialplan «{route.get('name', '')}»")

    def build_group_plan(
        self, *, city: str, name: str, number: str, extensions: list[str], fallback: str = "",
    ) -> dict[str, Any]:
        """Read-only. ``number`` is the group number callers dial, and
        ``fallback`` (Astana only, optional) who gets the call when no member
        answered — see _check_group_numbers. Refuses a name some Flow schema
        already has, a number some route already leads, and members or a
        fallback that aren't Webitel users (a bridge to a missing extension
        just never rings) — every missing one named at once. Repeated
        extensions are kept once. A number that is also a user's own
        extension is only flagged (``number_user``): the route would send
        that user's calls to the group. The plan carries the schema
        execute_group_plan will post, for the approver to see."""
        extensions = list(dict.fromkeys(extensions))
        _check_group_numbers(number, extensions, fallback)
        schema = group_schema(city, name, extensions, fallback)
        self._check_group_is_new(name, number)
        names: dict[str, str] = {}
        missing: list[str] = []
        for extension in dict.fromkeys([*extensions, *([fallback] if fallback else [])]):
            try:
                names[extension] = self.get_raw_user(extension).get("name", "")
            except SubjectNotFoundError:
                missing.append(extension)
        if missing:
            raise WebitelApiError(f"нет пользователя Webitel с номером: {', '.join(missing)}")
        try:
            number_user = self.get_raw_user(number).get("name") or number
        except SubjectNotFoundError:
            number_user = ""
        return {
            "name": name,
            "number": number,
            "number_user": number_user,
            "members": [{"extension": extension, "name": names[extension]} for extension in extensions],
            "ring_seconds": GROUP_RING_SECONDS[city],
            "fallback": fallback,
            "fallback_name": names.get(fallback, ""),
            "schema": schema,
        }

    def execute_group_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        """POSTs the group's Flow schema, then the Dialplan route sending the
        group number to it — both request shapes confirmed 2026-11-02 from
        Webitel's own forms (Flow → «Код», ``editor: false``; Dialplan →
        «Добавить»). Re-checks name and number first: either may have been
        taken since the plan was shown. The schema is rebuilt from the
        plan's city, name, members and fallback rather than taken from the
        plan.

        A failed route comes back as ``route_error`` instead of an
        exception: the schema already exists by then and the operator must
        know its id."""
        name, number, fallback = plan["name"], plan["number"], plan.get("fallback") or ""
        extensions = [m["extension"] for m in plan["members"]]
        _check_group_numbers(number, extensions, fallback)
        steps = group_schema(plan.get("city") or "almaty", name, extensions, fallback)
        self._check_group_is_new(name, number)
        schema = self._post("/api/routing/schema", {
            "name": name,
            "type": "voice",
            "editor": False,
            "schema": steps,
            "payload": {},
            "tags": [],
        })
        result = {"id": schema.get("id"), "name": name, "number": number, "members": extensions}
        try:
            route = self._post("/api/routing/outbound/calls", {
                "name": name,
                "schema": {"type": "voice", "editor": False, "id": str(schema["id"]), "name": name},
                "pattern": f"^{number}$",
                "description": "",
            })
        except (WebitelApiError, KeyError) as exc:
            return {**result, "route_error": str(exc)}
        return {**result, "route_id": route.get("id")}

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

    def build_password_plan(self, extension: str) -> dict[str, Any]:
        """Read-only: whose password a reset changes, which devices get the
        same one, and whether the user is online now — a phone or softphone
        registered with the old password drops off until it's given the new
        one."""
        user = self.get_raw_user(extension)
        presence = self.presence_labels(self.get_presence_status(extension))
        return {
            "extension": extension,
            "id": user["id"],
            "name": user.get("name", ""),
            "username": user.get("username", ""),
            "devices": [
                {"id": d["id"], "name": d.get("name", "")} for d in user.get("devices") or [] if d.get("id")
            ],
            "presence": presence,
        }

    def execute_password_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        """A fresh random password (generate_password) for the user, then the
        same one for each of their devices — see _set_device_passwords.
        Re-fetches by extension first and refuses if it now belongs to
        someone else, as execute_delete_plan does. The user's record is put
        back whole with only the password set, the way execute_plan resets
        one (Webitel's PUT replaces the whole record)."""
        user = self.get_raw_user(plan["extension"])
        if user["id"] != plan["id"]:
            raise WebitelApiError(
                f"extension={plan['extension']!r} now belongs to a different user "
                f"(id {user['id']!r}, was {plan['id']!r} when the plan was built) — refusing to reset"
            )
        password = generate_password()
        user["password"] = password
        self._put(f"/api/users/{user['id']}", user)
        return self._set_device_passwords(user, password, {
            "extension": plan["extension"],
            "name": user.get("name", ""),
            "new_password": password,
        })
