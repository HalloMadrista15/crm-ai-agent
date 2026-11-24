"""Local, single-user console: a stdlib-only HTTP server (no Flask/FastAPI —
consistent with this project's no-unnecessary-dependencies rule) that wires
together everything already built:

- CrmSessionManager (webapp/crm_session.py) — ONE persistent CRM browser
  session kept alive for the server's whole lifetime (see that module's
  docstring for why: reusing a saved-cookie session in a fresh browser per
  request was found, 2026-09-20, to die far faster than a real user's ~20
  minute session in one continuously-open tab).
- WebitelApiActions.build_plan (real, read-only) to show what a
  provisioning action WOULD do.
- WebitelApiActions.execute_plan (real, WRITE) — the only endpoint in this
  entire project that changes anything in Webitel, and only reachable
  through the UI's explicit two-step "show plan, then confirm" flow.

By default it runs only on localhost, for the one operator running it on
their own machine (see docs/open_questions.md's "Обновление после реальной
разведки" — Telegram/multi-user approval was explicitly dropped in favor
of this). With CONSOLE_SHARED=1 in info.env it is a shared console on a
server instead (2026-10-07): it looks and works the same, but every
browser gets its own workspace (a cookie) with its own CRM browser, saved
logins and Webitel sessions under operators/<id>/, so colleagues never act
under each other's accounts. run() refuses to listen beyond loopback
unless the console is shared and served over HTTPS. CONSOLE_AD_DOMAIN
makes it ask for an AD sign-in first (webapp/console_access.py).
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape as html_escape
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from crm_ai_agent.ports.entity_resolver import SubjectNotFoundError
from crm_ai_agent.webapp import console_access
from crm_ai_agent.webapp.crm_session import CrmSessionExpired, CrmSessionManager
from crm_ai_agent.webapp.webitel_login import WEBITEL_CITY_KEYS, WebitelLoginJob, city_config, latest_screenshot

ROOT = Path(__file__).resolve().parents[3]
INFO_ENV_PATH = ROOT / "info.env"
WEBITEL_STORAGE_STATE_PATHS = {
    "almaty": ROOT / ".auth" / "webitel_almaty_storage_state.json",
    "astana": ROOT / ".auth" / "webitel_astana_storage_state.json",
}
# Shared console only: each operator's own logins and Webitel sessions.
OPERATORS_DIR = ROOT / "operators"
AUDIT_LOG_PATH = ROOT / "audit" / "console_actions.jsonl"
# Who signed in to the console, and who tried to.
AD_LOG_PATH = ROOT / "audit" / "ad_logins.jsonl"


_SETTINGS_KEYS = (
    "CRM_LOGIN",
    "CRM_PASSWORD",
    "CRM_VISA_OWNER_ID",
    "WEBITEL_LOGIN",
    "WEBITEL_PASSWORD",
    "WEBITEL_ASTANA_LOGIN",
    "WEBITEL_ASTANA_PASSWORD",
)
# Kept encrypted on a shared console — see saved_secrets.py.
_SECRET_KEYS = ("CRM_PASSWORD", "WEBITEL_PASSWORD", "WEBITEL_ASTANA_PASSWORD")

# A page sends several requests at once: none may read an env file another
# one is halfway through rewriting, or write back what it read before that.
_env_lock = threading.RLock()


def _save_env_updates(path: Path, updates: dict[str, str]) -> None:
    """Read-modify-write: only overwrites keys with a non-empty new value,
    so leaving a field blank in the settings form keeps whatever is
    already saved (never blanks out a working credential by accident).
    Shared infra config (base URLs, domains) isn't touched here — those
    stay in info.env, edited by whoever first sets this up, same for
    everyone; only the per-operator keys in _SETTINGS_KEYS are writable
    from this endpoint."""

    with _env_lock:
        existing = _load_env_file(path)
        for key, value in updates.items():
            if key not in _SETTINGS_KEYS:
                continue
            if value:
                existing[key] = value
        _write_env(path, existing)


def _remove_env_keys(path: Path, keys: tuple[str, ...]) -> None:
    with _env_lock:
        existing = _load_env_file(path)
        if not any(key in existing for key in keys):
            return
        _write_env(path, {k: v for k, v in existing.items() if k not in keys})


def _write_env(path: Path, values: dict[str, str]) -> None:
    """Saved logins are readable by their owner only (a no-op on Windows)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


@dataclass
class _Operator:
    """Whose saved logins, Webitel sessions and CRM browser a request uses.
    ``name`` is the browser's workspace id on a shared console and "" for the
    one operator of a local console, whose files stay info.env and .auth/."""

    name: str
    env_path: Path
    storage_paths: dict[str, Path]
    # What this browser's saved passwords are encrypted with. None on a local
    # console: its info.env also serves scripts/ and is filled in by hand.
    secret_key: bytes | None = field(default=None, repr=False)


def _local_operator() -> _Operator:
    return _Operator("", INFO_ENV_PATH, dict(WEBITEL_STORAGE_STATE_PATHS))


def _named_operator(name: str, secret_key: bytes | None = None) -> _Operator:
    home = OPERATORS_DIR / name
    return _Operator(
        name,
        home / "info.env",
        {city: home / ".auth" / f"webitel_{city}_storage_state.json" for city in WEBITEL_STORAGE_STATE_PATHS},
        secret_key,
    )


def _own_env(op: _Operator) -> dict[str, str]:
    """This operator's own saved settings, with the passwords opened. One
    that doesn't open (a damaged file, another browser's) counts as not
    saved: the console then asks for it again."""
    values = _load_env_file(op.env_path)
    if op.secret_key is None:
        return values
    from crm_ai_agent.webapp.saved_secrets import SecretError, is_sealed, unseal

    for key in _SECRET_KEYS:
        if is_sealed(values.get(key, "")):
            try:
                values[key] = unseal(op.secret_key, op.name, key, values[key])
            except SecretError:
                del values[key]
    return values


def _save_own_env(op: _Operator, updates: dict[str, str]) -> None:
    """_save_env_updates into this operator's own settings, the passwords
    encrypted first."""
    if op.secret_key is not None:
        from crm_ai_agent.webapp.saved_secrets import seal

        updates = {
            key: seal(op.secret_key, op.name, key, value) if key in _SECRET_KEYS and value else value
            for key, value in updates.items()
        }
    _save_env_updates(op.env_path, updates)


def _encrypt_old_passwords(op: _Operator) -> None:
    """Passwords saved as plain text before 2026-11-23 become encrypted the
    first time their own browser comes back (only it has the key)."""
    from crm_ai_agent.webapp.saved_secrets import is_sealed

    with _env_lock:
        values = _load_env_file(op.env_path)
        plain = {key: values[key] for key in _SECRET_KEYS if values.get(key) and not is_sealed(values[key])}
        if plain:
            _save_own_env(op, plain)


def _operator_env(op: _Operator) -> dict[str, str]:
    """The shared settings (addresses, domains) from info.env plus this
    operator's own logins — never another operator's, nor logins left in the
    shared info.env from before it became a shared console."""
    shared = {k: v for k, v in _load_env_file(INFO_ENV_PATH).items() if k not in _SETTINGS_KEYS}
    own = {k: v for k, v in _own_env(op).items() if k in _SETTINGS_KEYS}
    return {**shared, **own}


_CITY_NAMES = {"almaty": "Алматы", "astana": "Астана"}

# At most one Webitel login per operator and city at a time.
_webitel_login_jobs: dict[tuple[str, str], WebitelLoginJob] = {}


def _webitel_session_state(op: _Operator, city: str) -> dict[str, Any]:
    """Who the console logs into this city's Webitel as, and whether the
    saved session still works (one read-only request)."""
    from crm_ai_agent.adapters.webitel_api.actions import WebitelApiError
    from crm_ai_agent.adapters.webitel_api.session import WebitelSessionError

    config = city_config(_operator_env(op), city)
    job = _webitel_login_jobs.get((op.name, city))
    state: dict[str, Any] = {
        "city": city,
        "login": config["login"],
        "configured": all(config.values()),
        "has_password": bool(config["password"]),
        "job": job.snapshot() if job else None,
    }
    if not config["base_url"]:
        state["session"] = "missing"
        return state
    try:
        _get_webitel_actions(op, city).check_session()
        state["session"] = "active"
    except WebitelSessionError:
        state["session"] = "missing"
    except WebitelApiError as exc:
        state["session"] = "expired" if ("HTTP 401" in str(exc) or "HTTP 403" in str(exc)) else "error"
        state["error"] = str(exc)
    return state


def _save_webitel_credentials(op: _Operator, city: str, login: str, password: str) -> None:
    """This operator's own Webitel account for one city, into their info.env.
    Raises ValueError with an operator-facing message."""
    if not login or not password:
        raise ValueError("Введите и логин, и пароль Webitel")
    keys = WEBITEL_CITY_KEYS[city]
    _save_own_env(op, {keys["login"]: login, keys["password"]: password})


def _forget_webitel_credentials(op: _Operator, city: str) -> None:
    """Removes this city's remembered Webitel login/password AND its saved
    session — otherwise the console would keep acting as that account."""
    keys = WEBITEL_CITY_KEYS[city]
    _remove_env_keys(op.env_path, (keys["login"], keys["password"]))
    op.storage_paths[city].unlink(missing_ok=True)


def _start_webitel_login(op: _Operator, city: str) -> dict[str, Any]:
    """Raises ValueError with an operator-facing message when this city's
    login can't start (missing credentials or address)."""
    env = _operator_env(op)
    config = city_config(env, city)
    name = _CITY_NAMES.get(city, city)
    if not config["login"] or not config["password"]:
        raise ValueError(f"Впишите логин и пароль Webitel {name} в «Настройках» и нажмите «Сохранить»")
    if not config["base_url"] or not config["domain"]:
        raise ValueError(f"В info.env нет адреса или домена Webitel {name} — см. info.env.example")
    job = _webitel_login_jobs.get((op.name, city))
    if job and job.state == "running":
        return {"started": False, "running": True}
    _webitel_login_jobs[(op.name, city)] = WebitelLoginJob(
        city=city,
        config=config,
        storage_state_path=op.storage_paths[city],
        show_browser=env.get("WEBITEL_SHOW_BROWSER") == "1",
    )
    return {"started": True, "login": config["login"]}


def _operator_error_message(exc: Exception) -> str:
    """The error text shown in the console; an expired or missing Webitel
    session also says where to log in again."""
    from crm_ai_agent.adapters.webitel_api.actions import WebitelApiError
    from crm_ai_agent.adapters.webitel_api.session import WebitelSessionError

    message = str(exc)
    if isinstance(exc, WebitelSessionError) or (isinstance(exc, WebitelApiError) and "HTTP 401" in message):
        message += " — войдите в Webitel заново: «Вход и настройки» → «Вход в Webitel»"
    return message


def _get_webitel_actions(op: _Operator, city: str) -> Any:
    from crm_ai_agent.adapters.webitel_api.actions import WebitelApiActions
    from crm_ai_agent.adapters.webitel_api.session import load_session

    storage_state_path = op.storage_paths.get(city)
    if storage_state_path is None:
        raise ValueError(f"Unknown city {city!r} — must be one of: {', '.join(WEBITEL_STORAGE_STATE_PATHS)}")
    key = WEBITEL_CITY_KEYS[city]["base_url"]
    base_url = _operator_env(op).get(key)
    if not base_url:
        raise ValueError(f"Missing {key} in {INFO_ENV_PATH}")
    return WebitelApiActions(base_url=base_url, session=load_session(storage_state_path))


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    with _env_lock:
        if not path.exists():
            return values
        text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


# A ticket's "Учётная запись" (BnzEmployeeAccount) is the employee's AD
# login (e.g. "in.turukpayeva"), never a Webitel login — Webitel logins are
# numeric extensions, and requesters only ever write them in the free-text
# comment, e.g. "настроить номер - 22104 ... Логин - 22104@mycar.astana-motors.kz".
_WEBITEL_LOGIN_AT_DOMAIN_RE = re.compile(r"(?<![\w.])(\d{3,6})@[\w-]+(?:\.[\w-]+)+")
_WEBITEL_LABELLED_LOGINS_RE = re.compile(
    r"(?:логин|номер|extension)\w*\s*[-–—:]?\s*"
    r"(\d{3,6}(?!\d)(?:@[\w.-]+)?(?:\s*(?:,|;|\bи\b)\s*\d{3,6}(?!\d)(?:@[\w.-]+)?)*)",
    re.IGNORECASE,
)
_DIGITS_RE = re.compile(r"(?<![\w.])\d{3,6}(?![\d])")


_TICKET_ENTITY_ID_RE = re.compile(r"/edit/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


def _ticket_entity_id(url: str) -> str | None:
    """The ticket's record Id from a CRM card link (…/edit/<guid>)."""
    match = _TICKET_ENTITY_ID_RE.search(url)
    return match.group(1).lower() if match else None


def _extract_webitel_logins(comment: str | None) -> list[str]:
    """Numeric Webitel logins mentioned in a ticket comment, in order of
    first appearance, deduplicated. Empty if the comment names none — the
    operator then fills the field by hand rather than getting a guess."""
    if not comment:
        return []
    found: list[tuple[int, str]] = [(m.start(), m.group(1)) for m in _WEBITEL_LOGIN_AT_DOMAIN_RE.finditer(comment)]
    for labelled in _WEBITEL_LABELLED_LOGINS_RE.finditer(comment):
        offset = labelled.start(1)
        found.extend((offset + m.start(), m.group(0)) for m in _DIGITS_RE.finditer(labelled.group(1)))
    logins: list[str] = []
    for _, login in sorted(found):
        if login not in logins:
            logins.append(login)
    return logins


# One persistent CRM browser per operator ("" = the local console's one).
_crm_sessions: dict[str, CrmSessionManager] = {}
_crm_sessions_lock = threading.Lock()


def _new_crm_session() -> CrmSessionManager:
    # Credentials are not baked in here: every login passes them (typed in
    # the console or the remembered ones), so an operator with nothing saved
    # yet can still log in.
    env = _load_env_file(INFO_ENV_PATH)
    base_url = env.get("CRM_BASE_URL")
    if not base_url:
        raise RuntimeError("Missing CRM_BASE_URL in info.env")
    return CrmSessionManager(base_url=base_url, show_browser=env.get("CRM_SHOW_BROWSER") == "1")


def _get_crm_session(op: _Operator | None = None) -> CrmSessionManager:
    name = op.name if op else ""
    with _crm_sessions_lock:
        if name not in _crm_sessions:
            _crm_sessions[name] = _new_crm_session()
        return _crm_sessions[name]


def _crm_login_of(op: _Operator) -> str:
    """The CRM account this operator's CRM browser last logged in as."""
    with _crm_sessions_lock:
        manager = _crm_sessions.get(op.name)
    return (manager.login if manager else "") or _load_env_file(op.env_path).get("CRM_LOGIN", "")


def _normalize_confirmation_code(raw: Any) -> str:
    """The authenticator code typed into the console: always exactly 6
    digits for this CRM (spaces and dashes people paste in are dropped)."""
    code = re.sub(r"[\s-]", "", str(raw or ""))
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("Код подтверждения — 6 цифр из приложения-аутентификатора")
    return code


def _resolve_crm_login(body: dict[str, Any], op: _Operator | None = None) -> tuple[str, str]:
    """Credentials for /api/crm/login: the ones typed into the console if
    any (saved to the operator's info.env only when "remember" is ticked),
    otherwise the remembered ones. Raises ValueError with an operator-facing
    message."""
    op = op or _local_operator()
    login = str(body.get("login") or "").strip()
    password = str(body.get("password") or "")
    if login or password:
        if not login or not password:
            raise ValueError("Введите и логин, и пароль CRM")
        if body.get("remember"):
            saved_login = _load_env_file(op.env_path).get("CRM_LOGIN", "")
            if saved_login and saved_login != login:
                # The saved Visa Owner ID belonged to the previous account;
                # dropping it makes the console detect the new one after login.
                _remove_env_keys(op.env_path, ("CRM_VISA_OWNER_ID",))
            _save_own_env(op, {"CRM_LOGIN": login, "CRM_PASSWORD": password})
        return login, password
    env = _own_env(op)
    login, password = env.get("CRM_LOGIN", ""), env.get("CRM_PASSWORD", "")
    if not login or not password:
        raise ValueError("Логин и пароль CRM не сохранены — введите их")
    return login, password


# --- Shared console: a workspace per browser, and who changed what ---
#
# Every browser gets its own workspace (a cookie), and inside it logs in to
# CRM and Webitel exactly as on a local console. Workspaces never see each
# other's logins, sessions or CRM browser. With CONSOLE_AD_DOMAIN the console
# asks who it is first, and a workspace belongs to one AD account.

_WORKSPACE_COOKIE = "console_workspace"
_WORKSPACE_TOKEN_RE = re.compile(r"[0-9a-f]{32}")
_WORKSPACE_COOKIE_MAX_AGE = 365 * 24 * 3600
_WORKSPACE_IDLE_SECONDS = 12 * 3600

# Set by run() from CONSOLE_SHARED; tests flip these directly.
_SHARED = False
_TLS_ENABLED = False
# Set by run() from CONSOLE_ALLOWED_NETS: the only addresses the console
# answers (empty = anyone who can reach the port). Loopback is always let in.
_ALLOWED_NETS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []


def _parse_allowed_nets(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """CONSOLE_ALLOWED_NETS: addresses and networks separated by commas or
    spaces, e.g. "10.80.0.0/24, 10.70.70.5". A typo raises — the console
    then refuses to start instead of quietly letting everyone in."""
    nets = []
    for item in re.split(r"[\s,;]+", raw.strip()):
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise ValueError(f"CONSOLE_ALLOWED_NETS: не адрес и не подсеть: {item!r}") from exc
    return nets


def _ip_allowed(ip: str, nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    if not nets:
        return True
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return address.is_loopback or any(address in net for net in nets)


# --- Sign-in with AD: only the accounts in CONSOLE_AD_ADMINS and
# CONSOLE_AD_USERS; the admins change both lists in the console.

_AD_COOKIE = "console_ad"
# Set by run() from CONSOLE_AD_DOMAIN ("" = no sign-in). Not read again later:
# info.env damaged while the console runs lets nobody in, not everybody.
_AD_DOMAIN = ""
_sign_ins = console_access.SignIns(ROOT / ".auth" / "console_sign_ins.json")
_ad_throttle = console_access.Throttle()
_ad_address_throttle = console_access.Throttle(limit=10)
# One attempt at a time: ten sent at once don't get past the limit of four.
_ad_lock = threading.Lock()


def _ad_people(strict: bool = False) -> tuple[list[str], list[str]]:
    """(admins, those they let in), read anew every time, so an edit of
    info.env works at once. A list with a typo in it lets nobody in —
    with ``strict`` it raises instead."""
    env = _load_env_file(INFO_ENV_PATH)
    lists = []
    for key in ("CONSOLE_AD_ADMINS", "CONSOLE_AD_USERS"):
        try:
            lists.append(console_access.parse_logins(env.get(key, ""), _AD_DOMAIN))
        except ValueError as exc:
            if strict:
                raise ValueError(f"{key} в info.env: {exc}") from exc
            lists.append([])
    return lists[0], lists[1]


def _ad_problem(env: dict[str, str], shared: bool) -> str:
    """Why the console can't start with these CONSOLE_AD_* settings, or ""."""
    domain = env.get("CONSOLE_AD_DOMAIN", "").strip()
    if not domain:
        if env.get("CONSOLE_AD_ADMINS") or env.get("CONSOLE_AD_USERS"):
            return "список CONSOLE_AD_... есть, а CONSOLE_AD_DOMAIN пуст — консоль открылась бы всем"
        return ""
    if not shared:
        return "CONSOLE_AD_DOMAIN работает только с CONSOLE_SHARED=1"
    for key in ("CONSOLE_AD_ADMINS", "CONSOLE_AD_USERS"):
        try:
            console_access.parse_logins(env.get(key, ""), domain)
        except ValueError as exc:
            return f"{key}: {exc}"
    if not console_access.parse_logins(env.get("CONSOLE_AD_ADMINS", ""), domain):
        return "CONSOLE_AD_ADMINS пуст — некому управлять доступом"
    return console_access.check_problem()


def _access_info(login: str) -> dict[str, Any]:
    """What «Доступ в консоль» shows: who is signed in and, to an admin,
    everyone who may."""
    if not _AD_DOMAIN:
        return {"me": ""}
    admins, users = _ad_people()
    if login not in admins:
        return {"me": login}
    everyone = [*admins, *(user for user in users if user not in admins)]
    return {"me": login, "users": [
        {"login": user, "name": console_access.account_name(user, _AD_DOMAIN) or "", "admin": user in admins}
        for user in everyone
    ]}


def _change_access(by: str, action: str, raw: str) -> tuple[str, str]:
    """An admin lets an account in ("grant"), no longer ("revoke") or makes it
    an admin ("promote"); (login, the person's name). Takes effect at once.
    ValueError says what is wrong. Nobody removes themselves, so there is
    always an admin left."""
    login = console_access.normalize_login(raw, _AD_DOMAIN)
    if not login:
        raise ValueError("Укажите логин AD, например i.ivanov")
    name = console_access.account_name(login, _AD_DOMAIN)
    with _env_lock:
        admins, users = _ad_people(strict=True)
        if by not in admins:  # asked here, not before: two admins may be removing each other
            raise PermissionError("Доступом управляют админы консоли")
        if action == "grant":
            if login in admins or login in users:
                raise ValueError(f"У {login} доступ уже есть")
            if name is None:
                raise ValueError(f"В AD нет логина {login}")
            users.append(login)
        else:
            if login not in admins and login not in users:
                raise ValueError(f"{login} нет в списке")
            if action == "promote" and login in admins:
                raise ValueError(f"{login} уже админ")
            if action == "revoke" and login == by:
                raise ValueError("Себя убрать нельзя")
            admins, users = ([x for x in people if x != login] for people in (admins, users))
            if action == "promote":
                admins.append(login)
        console_access.set_env_line(INFO_ENV_PATH, "CONSOLE_AD_ADMINS", ", ".join(admins))
        console_access.set_env_line(INFO_ENV_PATH, "CONSOLE_AD_USERS", ", ".join(users))
    if action == "revoke":
        _sign_ins.end_all(login)
    return login, name or ""


def _owns_workspace(workspace: str, login: str) -> bool:
    """A workspace belongs to the first AD account that uses it: on a PC two
    people share, the second one doesn't get the first one's saved logins."""
    owner_path = OPERATORS_DIR / workspace / "ad_login"
    with _workspaces_lock:
        if owner_path.exists():
            return owner_path.read_text(encoding="utf-8").strip() == login
        console_access.replace_file(owner_path, login + "\n")
        return True


def _log_sign_in(client_ip: str, login: str, result: str) -> None:
    entry = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ip": client_ip, "login": login, "result": result,
    }
    with _audit_lock:
        AD_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AD_LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")


# When each workspace last made a request: the CRM browser of one idle for
# _WORKSPACE_IDLE_SECONDS is closed (its saved logins stay on disk).
_workspace_seen: dict[str, float] = {}
_workspaces_lock = threading.Lock()


def _workspace_id(token: str) -> str:
    """The cookie is the key to a browser's saved passwords, so it never
    appears on disk: the workspace directory is named after its hash."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()[:32]


def _close_later(manager: CrmSessionManager | None) -> None:
    """Closing a CRM browser can take seconds; never inside a request or a lock."""
    if manager is not None:
        threading.Thread(target=manager.close, daemon=True).start()


def _sweep_idle_workspaces(now: float) -> None:
    """Called with _workspaces_lock held."""
    for workspace, seen in list(_workspace_seen.items()):
        if now - seen > _WORKSPACE_IDLE_SECONDS:
            del _workspace_seen[workspace]
            with _crm_sessions_lock:
                manager = _crm_sessions.pop(workspace, None)
            _close_later(manager)


def _exposure_problem(host: str, shared: bool, tls: bool) -> str:
    """Why the console must not listen this way, or "" when it may."""
    if host in ("127.0.0.1", "localhost", "::1"):
        return ""
    if not shared:
        return "CONSOLE_HOST открывает консоль в сеть без CONSOLE_SHARED=1 — все в сети работали бы под одними учётками"
    if not tls:
        return "CONSOLE_HOST открывает консоль в сеть без HTTPS — задайте CONSOLE_TLS_CERT и CONSOLE_TLS_KEY"
    return ""


# Only these plan fields go into the audit log — never a plan's or a
# result's secrets (e.g. the temporary password a reset or create returns).
_AUDIT_PLAN_FIELDS = (
    "city", "template_extension", "target_extension", "target_name", "copy_permissions", "permissions",
    "new_username", "new_name", "new_extension", "extension", "name", "reset_password", "number", "members", "fallback", "devices", "objects",
)
_audit_lock = threading.Lock()


def _audit(op: _Operator, client_ip: str, action: str, details: dict[str, Any]) -> None:
    """Append-only record of who changed what in Webitel through the console."""
    entry = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "workspace": op.name[:8] or "local",
        "ip": client_ip,
        "action": action,
        **details,
    }
    with _audit_lock:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")


# What the journal calls a change, by the endpoint that made it.
_JOURNAL_KINDS = {
    "execute": "Копирование",
    "create_execute": "Создание",
    "delete_execute": "Удаление",
    "password_execute": "Сброс пароля",
    "group_execute": "Группа",
    "role_execute": "Роль",
    "grant": "Доступ открыт",
    "revoke": "Доступ закрыт",
    "promote": "Назначен админ",
}
_JOURNAL_PROBLEMS = {"permissions_error": "права", "route_error": "маршрут", "devices_error": "устройство", "objects_error": "Objects"}
_JOURNAL_LIMITS = (10, 25, 50, 100)
# How long a browser found to be logged in to Webitel may go on reading the
# journal before Webitel is asked again.
_JOURNAL_ACCESS_SECONDS = 300
_journal_access: dict[str, float] = {}


def _journal_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """One line of the audit log as the journal shows it. ``who`` is only
    what was recorded: the AD account signed in to the console, the CRM
    and Webitel logins the browser worked under, or its address when
    there was none of them."""

    def first(*keys: str) -> str:
        return next((str(entry[key]) for key in keys if entry.get(key)), "")

    kind = str(entry.get("action", "")).rsplit("/", 1)[-1]
    details = []
    if entry.get("template_extension"):
        details.append(f"образец {entry['template_extension']}")
    if entry.get("reset_password"):
        details.append("сброс пароля")
    if entry.get("copy_permissions"):
        details.append("Permissions")
    if entry.get("members"):
        details.append("участники " + ", ".join(str(m.get("extension", "")) for m in entry["members"]))
    if entry.get("fallback"):
        details.append(f"если не ответили — {entry['fallback']}")
    problems = [f"{label}: {entry[key]}" for key, label in _JOURNAL_PROBLEMS.items() if entry.get(key)]
    if entry.get("error"):
        problems.append(str(entry["error"]))
    logins = [first(key).split("@")[0] for key in ("ad_login", "crm_login", "webitel_login")]
    return {
        "time": first("time"),
        "city": _CITY_NAMES.get(first("city"), first("city")),
        "kind": _JOURNAL_KINDS.get(kind, kind),
        "number": first("target_extension", "new_extension", "extension", "number"),
        "name": first("target_name", "new_name", "name"),
        "details": " · ".join(details),
        "who": " · ".join(dict.fromkeys(login for login in logins if login)) or first("ip"),
        "ok": entry.get("ok") is True,
        "problem": "; ".join(problems),
    }


def _read_journal(query: str, offset: int, limit: int) -> dict[str, Any]:
    """The audit log, newest first; every word of ``query`` has to be
    somewhere in what an entry shows."""
    with _audit_lock:
        lines = AUDIT_LOG_PATH.read_text(encoding="utf-8-sig").splitlines() if AUDIT_LOG_PATH.exists() else []
    words = query.lower().split()
    found = []
    for line in reversed(lines):
        try:
            shown = _journal_entry(json.loads(line))
        except (ValueError, AttributeError, TypeError):
            continue  # a line cut short by a crash must not hide the rest
        text = " ".join(shown[key] for key in ("kind", "city", "number", "name", "details", "who")).lower()
        if all(word in text for word in words):
            found.append(shown)
    return {"entries": found[offset:offset + limit], "total": len(found)}


def _may_read_journal(op: _Operator) -> bool:
    """Colleagues' names and numbers: on a shared console only for a browser
    that is logged in to Webitel through it."""
    if not _SHARED or time.time() - _journal_access.get(op.name, 0) < _JOURNAL_ACCESS_SECONDS:
        return True
    logged_in = any(_webitel_session_state(op, city)["session"] == "active" for city in WEBITEL_STORAGE_STATE_PATHS)
    if logged_in:
        _journal_access[op.name] = time.time()
    return logged_in


_INDEX_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Консоль оператора</title>
<style>
  :root{
    --bg:#0c0c0b; --rail:#111110; --shell:#151514;
    --surface:#1b1b19; --surface-2:#232321; --surface-3:#2d2d2a;
    --border:rgba(255,255,255,.07); --border-strong:rgba(255,255,255,.15);
    --text:#f3f3ef; --text-soft:#a9a9a2; --text-faint:#73736c;
    --lime:#b8f26c; --lime-ink:#11200a; --lime-soft:rgba(184,242,108,.11);
    --amber:#f5aa3d; --amber-soft:rgba(245,170,61,.11); --amber-bg:#2c2416;
    --accent:#f3f3ef; --accent-dark:#d8d8d2; --accent-soft:rgba(255,255,255,.07); --accent-ink:#121211;
    --good:var(--lime); --good-soft:var(--lime-soft); --warn:var(--amber); --warn-soft:var(--amber-soft);
    --danger:#f05a4b; --danger-dark:#ff7b6d; --danger-soft:rgba(240,90,75,.12);
    --shadow:0 1px 0 rgba(255,255,255,.035) inset, 0 18px 40px -26px rgba(0,0,0,.85);
    --radius:20px; --radius-sm:12px; --rail-w:88px;
    --font:"Inter","Geist","Segoe UI Variable Text","Segoe UI",-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
    --font-display:"Inter Display","Inter","Geist","Segoe UI Variable Display","Segoe UI",-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
    --mono:ui-monospace,"Geist Mono","Cascadia Mono",SFMono-Regular,Menlo,Consolas,monospace;
    --page-max:1180px; --gutter:clamp(14px,3vw,36px);
  }
  *{box-sizing:border-box;}
  html{-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;background:var(--bg);color-scheme:dark;}
  body{font-family:var(--font);background:var(--bg);color:var(--text);margin:0;font-size:14px;line-height:1.55;min-height:100vh;}
  ::selection{background:rgba(184,242,108,.25);}

  /* Left rail: the sections, as icons with a short label. */
  .rail{position:fixed;inset:0 auto 0 0;width:var(--rail-w);z-index:20;display:flex;flex-direction:column;align-items:center;gap:6px;padding:18px 0 16px;background:var(--rail);border-right:1px solid var(--border);}
  .rail{overflow-y:auto;scrollbar-width:none;}
  .rail::-webkit-scrollbar{display:none;}
  .rail-logo{width:40px;height:40px;flex-shrink:0;display:grid;place-items:center;color:var(--amber);margin-bottom:14px;filter:drop-shadow(0 0 10px rgba(245,170,61,.35));}
  .rail-links{display:flex;flex-direction:column;align-items:center;gap:4px;}
  .nav-link,.nav-account-btn{display:flex;flex-direction:column;align-items:center;gap:4px;width:76px;padding:6px 0;background:transparent;border:none;border-radius:14px;color:var(--text-faint);font-size:10.5px;font-weight:500;letter-spacing:.01em;}
  .rail-icon{position:relative;display:grid;place-items:center;width:42px;height:42px;border-radius:50%;background:var(--surface);border:1px solid var(--border);color:var(--text-soft);transition:background-color .15s,color .15s,border-color .15s,box-shadow .15s;}
  .rail-label{line-height:1.2;}
  .nav-link:hover,.nav-account-btn:hover{background:transparent;color:var(--text);}
  .nav-link:hover .rail-icon,.nav-account-btn:hover .rail-icon{background:var(--surface-3);color:var(--text);border-color:var(--border-strong);}
  .nav-link.active,.nav-account-btn.active{color:var(--text);background:transparent;}
  .nav-link.active .rail-icon,.nav-account-btn.active .rail-icon{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);box-shadow:0 0 0 4px rgba(255,255,255,.06),0 6px 18px -6px rgba(255,255,255,.35);}
  .nav-account-btn{margin-top:auto;}
  .nav-account-btn .status-dot{position:absolute;top:1px;right:1px;margin:0;border:2px solid var(--rail);width:11px;height:11px;}

  .shell{margin-left:var(--rail-w);min-height:100vh;background:radial-gradient(900px 420px at 15% -120px,rgba(184,242,108,.045),transparent 70%) no-repeat,var(--shell);padding-bottom:72px;}
  .topbar{position:sticky;top:0;z-index:10;display:flex;align-items:center;justify-content:space-between;gap:16px;max-width:calc(var(--page-max) + 2 * var(--gutter));margin:0 auto;padding:14px var(--gutter);background:rgba(21,21,20,.82);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);}
  .status-chips{display:flex;gap:8px;flex-wrap:wrap;}
  .chip{display:inline-flex;align-items:center;gap:7px;padding:6px 12px;border-radius:999px;background:var(--surface);border:1px solid var(--border);color:var(--text-soft);font-size:12.5px;font-weight:500;}
  .chip b{color:var(--text);font-weight:600;}
  .chip .status-dot{margin:0;}
  .chip:hover{background:var(--surface-2);color:var(--text);border-color:var(--border-strong);}
  .topbar-brand{font-family:var(--font-display);font-size:13.5px;font-weight:600;color:var(--text-soft);letter-spacing:-.01em;white-space:nowrap;}
  .page{max-width:var(--page-max);margin:0 auto;padding:10px var(--gutter) 0;box-sizing:content-box;}
  .page-head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px 24px;flex-wrap:wrap;margin-bottom:22px;}
  .page-head h1{font-family:var(--font-display);font-size:32px;font-weight:650;letter-spacing:-.035em;line-height:1.15;margin:0 0 6px;color:var(--text);}
  .page-sub{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline;font-size:12.5px;color:var(--text-faint);}
  #pageDate{color:var(--text-soft);}
  #pageDate::after{content:"·";margin-left:10px;color:var(--text-faint);}
  .top-sub{max-width:72ch;}

  .status-dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:8px;vertical-align:1px;background:var(--text-faint);}
  .status-dot.good{background:var(--lime);box-shadow:0 0 8px rgba(184,242,108,.65);}
  .status-dot.warn{background:var(--amber);box-shadow:0 0 8px rgba(245,170,61,.55);}
  .status-dot.critical{background:var(--danger);box-shadow:0 0 8px rgba(240,90,75,.55);}
  .saved-login{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;padding:10px 14px;margin-bottom:12px;background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:13px;color:var(--text-soft);}
  .saved-login b{color:var(--text);font-weight:600;}
  .saved-login-actions{display:flex;align-items:center;gap:6px;}
  button.icon-btn{display:inline-flex;align-items:center;justify-content:center;padding:6px 8px;}
  button.icon-btn:hover{color:var(--danger-dark);border-color:rgba(255,123,109,.4);background:var(--danger-soft);}
  .card-head .pill{margin-left:auto;}
  .steps{display:flex;flex-direction:column;gap:16px;}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:22px 24px;box-shadow:var(--shadow);}
  .card-head{display:flex;align-items:center;gap:10px;margin-bottom:18px;}
  .card-head h2{font-family:var(--font-display);font-size:16.5px;font-weight:600;margin:0;color:var(--text);letter-spacing:-.015em;text-transform:none;}
  .card-body{padding-left:0;}
  label.field-label{display:block;font-size:12px;font-weight:500;color:var(--text-soft);margin:0 0 6px;}
  .row{display:flex;gap:10px;align-items:flex-end;margin-bottom:14px;flex-wrap:wrap;}
  .row:last-child{margin-bottom:0;}
  .field-group{display:flex;flex-direction:column;flex:1;min-width:200px;}
  .field-group.narrow{flex:none;min-width:64px;width:64px;}
  input[type=text],input[type=password],textarea{
    padding:10px 13px;border:1px solid var(--border);border-radius:var(--radius-sm);font:inherit;
    background:var(--surface-2);color:var(--text);transition:border-color .15s,box-shadow .15s,background-color .15s;
  }
  textarea{resize:vertical;}
  input[type=text]::placeholder,input[type=password]::placeholder,textarea::placeholder{color:var(--text-faint);}
  input[type=text]:hover,input[type=password]:hover,textarea:hover{border-color:var(--border-strong);}
  input[type=text]:focus,input[type=password]:focus,textarea:focus{outline:none;border-color:rgba(184,242,108,.55);background:#262624;box-shadow:0 0 0 4px rgba(184,242,108,.1);}
  .subsection{padding:14px 16px;background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius-sm);margin-bottom:14px;}
  .subsection:last-of-type{margin-bottom:0;}
  .subsection-title{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--text-faint);margin-bottom:12px;}
  .city-picker{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
  .city-picker-label{font-size:12.5px;font-weight:500;color:var(--text-faint);}
  .city-tabs{margin-bottom:0;}
  .tabs{display:flex;gap:2px;background:var(--surface);border:1px solid var(--border);border-radius:999px;padding:4px;margin-bottom:20px;width:fit-content;}
  .tab-btn,.city-btn{background:transparent;color:var(--text-soft);font-weight:500;font-size:13px;padding:6px 15px;border-radius:999px;}
  .tab-btn:hover,.city-btn:hover{background:var(--surface-2);color:var(--text);}
  .tab-btn.active{background:var(--surface-3);color:var(--text);box-shadow:inset 0 0 0 1px var(--border-strong);}
  .tab-btn.active:hover{background:var(--surface-3);}
  .city-btn.active{background:var(--lime);color:var(--lime-ink);font-weight:600;box-shadow:0 0 18px rgba(184,242,108,.25);}
  .city-btn.active:hover{background:var(--lime);color:var(--lime-ink);}
  .hint{font-size:12px;color:var(--text-faint);margin-top:7px;}
  .schema-json{margin:2px 0 8px;}
  .schema-json summary{cursor:pointer;font-size:12px;color:var(--text-soft);}
  .schema-json summary:hover{color:var(--text);}
  .schema-json pre{margin-top:6px;max-height:320px;overflow:auto;}
  code{background:var(--surface-3);color:var(--text);border-radius:6px;padding:1px 6px;font-size:11.5px;font-family:var(--mono);}
  .checkbox-row{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text-soft);margin-bottom:14px;cursor:pointer;user-select:none;}
  .checkbox-row:hover{color:var(--text);}
  .checkbox-row input[type=checkbox]{width:16px;height:16px;accent-color:var(--lime);cursor:pointer;}
  .checkbox-line{display:flex;flex-wrap:wrap;gap:0 24px;}
  .list .field.list-error{background:var(--danger-soft);border-color:rgba(240,90,75,.3);}
  .list .field.list-error .k{color:var(--danger-dark);}
  .list .field.list-ok .status{color:var(--lime);font-weight:600;}
  .secret{display:inline-block;padding:2px 10px;border-radius:8px;background:var(--lime-soft);border:1px solid rgba(184,242,108,.35);font-family:var(--mono);font-size:14px;font-weight:600;color:var(--lime);user-select:all;letter-spacing:.02em;cursor:pointer;transition:background-color .15s,border-color .15s;}
  .secret:hover{background:rgba(184,242,108,.2);border-color:rgba(184,242,108,.6);}
  .secret.copied::after{content:"";display:inline-block;width:17px;height:17px;margin-left:8px;vertical-align:-3px;background:currentColor;-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m12 15 2 2 4-4'/%3E%3Crect x='8' y='8' width='14' height='14' rx='2'/%3E%3Cpath d='M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2'/%3E%3C/svg%3E") center/contain no-repeat;mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m12 15 2 2 4-4'/%3E%3Crect x='8' y='8' width='14' height='14' rx='2'/%3E%3Cpath d='M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2'/%3E%3C/svg%3E") center/contain no-repeat;}
  .list .field.list-warn{background:var(--amber-bg);border-color:rgba(245,170,61,.35);}
  .list .field.list-warn .status{color:var(--text);font-weight:600;}
  .presence{display:block;margin-top:3px;font-size:12px;}
  .presence.idle{color:var(--text-soft);font-weight:500;}
  .presence.active{color:var(--amber);}
  .list .field.list-error .presence{color:var(--danger-dark);font-weight:500;}
  button{
    padding:9px 18px;border:none;border-radius:999px;background:var(--accent);color:var(--accent-ink);
    font:inherit;font-weight:600;cursor:pointer;white-space:nowrap;
    transition:background-color .15s,border-color .15s,color .15s,box-shadow .15s,transform .06s;
  }
  button:hover{background:var(--accent-dark);}
  button:active{transform:scale(.98);}
  button:focus-visible{outline:2px solid rgba(184,242,108,.7);outline-offset:2px;}
  button:disabled{background:var(--surface-3);color:var(--text-faint);cursor:not-allowed;transform:none;}
  button.danger{background:var(--danger);color:#fff;box-shadow:0 8px 24px -10px rgba(240,90,75,.7);}
  button.danger:hover{background:var(--danger-dark);}
  button.danger:disabled{background:var(--surface-3);color:var(--text-faint);box-shadow:none;}
  button.ghost{background:var(--surface-2);color:var(--text);border:1px solid var(--border);padding:6px 13px;font-size:12.5px;font-weight:500;}
  button.ghost:hover{background:var(--surface-3);border-color:var(--border-strong);}
  button.ghost:disabled{opacity:.45;cursor:not-allowed;background:transparent;border-color:var(--border);}
  .pill{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;font-weight:600;padding:3px 10px;border-radius:999px;border:1px solid transparent;}
  .pill.good{background:var(--lime-soft);color:var(--lime);border-color:rgba(184,242,108,.25);}
  .pill.warn{background:var(--amber-soft);color:var(--amber);border-color:rgba(245,170,61,.28);}
  .pill.critical{background:var(--danger-soft);color:var(--danger-dark);border-color:rgba(240,90,75,.3);}
  .pill.neutral{background:var(--surface-2);color:var(--text-soft);border-color:var(--border);}
  .result-card{background:var(--surface-2);border:1px solid var(--border);border-radius:16px;padding:4px 16px;margin-top:12px;}
  .field{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--border);font-size:13px;}
  .field:last-child{border-bottom:none;}
  .field .k{color:var(--text-soft);flex-shrink:0;}
  .field .v{font-weight:500;text-align:right;color:var(--text);min-width:0;overflow-wrap:anywhere;}
  .form-step-title{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--text-faint);margin:4px 0 12px;}
  .form-step-title ~ .form-step-title{margin-top:20px;padding-top:16px;border-top:1px solid var(--border);}
  .neighbors-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;}
  .radius-label{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--text-soft);}
  .radius-label input{width:52px;padding:6px 8px;}
  .neighbor-meta{display:block;font-weight:400;font-size:12px;color:var(--text-faint);}
  .list .field.neighbor.picked{border-color:rgba(184,242,108,.45);background:var(--lime-soft);}
  .split-main .list:not(:empty) + .checkbox-line{margin-top:14px;}
  .form-submit{margin-top:18px;}
  .approvals{padding:12px 0 10px;}
  .approvals-title{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--text-faint);margin-bottom:8px;}
  .approvals-note{font-size:12.5px;color:var(--danger-dark);overflow-wrap:anywhere;}
  .approval{padding:9px 12px;border:1px solid var(--border);border-radius:var(--radius-sm);margin-bottom:6px;font-size:12.5px;background:var(--surface-2);}
  .approval.current{border-color:rgba(245,170,61,.4);background:var(--amber-bg);}
  .approval.quiet{display:none;}
  .approvals.show-all .approval.quiet{display:block;}
  .approval-head{display:flex;align-items:baseline;gap:8px;}
  .approval-pos{color:var(--text-faint);font-variant-numeric:tabular-nums;min-width:18px;}
  .approval-owner{font-weight:600;color:var(--text);flex:1;min-width:0;overflow-wrap:anywhere;}
  .approval-status{color:var(--text-soft);text-align:right;}
  .approval-meta{color:var(--text-faint);margin:2px 0 0 26px;}
  .approval-comment{margin:6px 0 0 26px;padding:7px 11px;border-left:2px solid var(--amber);background:rgba(255,255,255,.025);border-radius:0 8px 8px 0;color:var(--text);white-space:pre-wrap;overflow-wrap:anywhere;}
  .approvals-toggle{margin-top:2px;}
  .list{display:flex;flex-direction:column;gap:6px;margin-top:10px;}
  .list .field{background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px 14px;align-items:center;}
  .list .field .k{font-weight:500;color:var(--text);flex-shrink:1;min-width:0;overflow-wrap:anywhere;}
  pre{background:#121211;border:1px solid var(--border);border-radius:var(--radius-sm);padding:12px 14px;font-size:12px;font-family:var(--mono);overflow-x:auto;white-space:pre-wrap;word-break:break-word;color:var(--text-soft);}
  .msg{font-size:12.5px;margin-top:10px;color:var(--text-soft);overflow-wrap:anywhere;}
  .msg:empty{margin-top:0;}
  .results-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:14px;}
  .msg.error{color:var(--danger-dark);}
  .msg.info{color:var(--text-soft);}

  /* Two-column layouts: stacked by default, side by side once there is room.
     Left (.split-main) is what the operator fills in, right (.split-side) is
     what comes back — ticket details, a plan, execution results. */
  .split{display:grid;grid-template-columns:minmax(0,1fr);gap:0 28px;align-items:start;}
  .split-side .result-card{margin-top:12px;}
  .split-side > .row{margin-top:14px;}
  .split-placeholder{display:none;}
  .login-column,.side-column{display:flex;flex-direction:column;gap:16px;}
  .code-banner{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:18px;padding:11px 16px;border:1px solid rgba(245,170,61,.35);background:var(--amber-bg);border-radius:14px;color:var(--text);font-weight:500;}
  .code-banner::before{content:"⚠";color:var(--amber);font-size:15px;margin-right:-4px;}
  .code-banner button{margin-left:auto;}
  .code-box{margin:12px 0 0;padding:14px;border:1px solid rgba(245,170,61,.35);background:var(--amber-bg);border-radius:14px;}
  .code-box input{font-size:18px;letter-spacing:.25em;font-variant-numeric:tabular-nums;font-family:var(--mono);}
  .screenshot-btn{margin-top:10px;}
  .crm-screenshot{display:block;width:100%;margin-top:10px;border:1px solid var(--border);border-radius:var(--radius-sm);}
  .webitel-city{padding:14px 0;border-bottom:1px solid var(--border);}
  .webitel-city:first-child{padding-top:0;}
  .webitel-city:last-child{border-bottom:none;padding-bottom:0;}
  .webitel-city [data-role="form"]{margin-bottom:12px;}
  .webitel-city-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:6px;}
  .webitel-city-head b{font-weight:600;}
  .webitel-city-login{font-size:12.5px;color:var(--text-soft);margin-bottom:8px;}
  .webitel-city-login b{color:var(--text);}
  .steps.cols-2 > .login-column{order:-1;}
  @media (min-width:1000px){
    .steps.cols-2 > .login-column{order:0;}
    .split{grid-template-columns:minmax(0,1fr) minmax(0,1fr);}
    .split.form-split{grid-template-columns:minmax(0,5fr) minmax(0,7fr);}
    .split-side .msg{margin-top:0;}
    .split-side .msg:first-child:empty + *{margin-top:0;}
    .split-side .msg:not(:empty) + .result-card{margin-top:10px;}
    .split-side .result-card{margin-top:0;}
    /* Shown only while the right column has nothing real in it yet. */
    .split-side:not(:has(.msg:not(:empty), .list:not(:empty), .result-card:not([hidden]), .row:not([hidden]))) > .split-placeholder{
      display:flex;align-items:center;justify-content:center;text-align:center;min-height:210px;padding:24px 32px;
      border:1px solid var(--border);border-radius:16px;color:var(--text-faint);font-size:13px;
      background:radial-gradient(rgba(255,255,255,.07) 1px,transparent 1.2px) 0 0/18px 18px,#171716;
    }
    .steps.cols-2{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);align-items:start;}
  }

  .list .field.journal{display:grid;grid-template-columns:104px minmax(0,1fr) minmax(0,auto);gap:4px 16px;align-items:baseline;}
  .journal-when{color:var(--text-soft);font-variant-numeric:tabular-nums;white-space:nowrap;}
  .journal-what{min-width:0;overflow-wrap:anywhere;}
  .journal-what b{font-weight:600;margin-right:4px;}
  .journal-note{display:block;font-size:12px;color:var(--text-soft);}
  .journal-who{color:var(--text-soft);text-align:right;overflow-wrap:anywhere;}
  .journal-more{margin-top:14px;}
  .journal-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;}
  .journal-bar .msg{margin:0;}
  .journal-limits{width:auto;margin:0 0 0 auto;}
  .limit-btn{background:transparent;color:var(--text-soft);font-weight:500;font-size:13px;padding:5px 12px;border-radius:999px;}
  .limit-btn:hover{background:var(--surface-2);color:var(--text);}
  .limit-btn.active,.limit-btn.active:hover{background:var(--surface-3);color:var(--text);box-shadow:inset 0 0 0 1px var(--border-strong);}

  /* Low windows: everything in the rail has to fit. */
  @media (max-height:800px) and (min-width:761px){
    .rail{padding:10px 0;gap:2px;}
    .rail-logo{width:32px;height:32px;margin-bottom:4px;}
    .rail-links{gap:0;}
    .nav-link,.nav-account-btn{padding:4px 0;gap:3px;}
    .rail-icon{width:36px;height:36px;}
  }

  /* Phones and narrow windows: the rail becomes a scrolling bar on top. */
  @media (max-width:760px){
    .rail{position:sticky;top:0;inset:auto;width:auto;height:auto;flex-direction:row;padding:8px 10px;gap:4px;overflow-x:auto;scrollbar-width:none;border-right:none;border-bottom:1px solid var(--border);}
    .rail::-webkit-scrollbar{display:none;}
    .rail-logo{display:none;}
    .rail-links{flex-direction:row;gap:2px;}
    .nav-link,.nav-account-btn{width:66px;flex-shrink:0;}
    .nav-account-btn{margin-top:0;margin-left:auto;}
    .rail-icon{width:36px;height:36px;}
    .shell{margin-left:0;}
    .topbar{position:static;flex-wrap:wrap;padding-top:12px;padding-bottom:4px;}
    .topbar-brand{display:none;}
    .page-head h1{font-size:25px;}
    .list .field.journal{grid-template-columns:minmax(0,1fr) auto;}
    .journal-what{grid-column:1 / -1;grid-row:2;}
    .card{padding:18px 16px;border-radius:16px;}
    .field-group:not(.narrow){flex:1 1 100%;min-width:0;}
    .row > button{flex:1 1 auto;}
    input[type=text],input[type=password],textarea{font-size:16px;}
    .tabs{width:100%;}
    .tab-btn{flex:1;}
    .field{flex-direction:column;gap:2px;}
    .field .v{text-align:left;}
    .list .field{flex-direction:row;flex-wrap:wrap;}
    .list .field .k{flex:1 1 140px;}
    .list .field .v{flex:1 1 180px;}
  }
  [hidden]{display:none!important;}
</style>
</head>
<body>
<aside class="rail">
  <div class="rail-logo" aria-hidden="true"><svg width="22" height="22" viewBox="0 0 24 24"><path d="M9 2.5h6v6.5h6.5v6H15v6.5H9V15H2.5V9H9z" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg></div>
  <nav class="rail-links">
    <button type="button" class="nav-link active" data-section="ticket" data-title="Заявка" title="Заявка"><span class="rail-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg></span><span class="rail-label">Заявка</span></button>
    <button type="button" class="nav-link" data-section="copy" data-title="Копирование по образцу" title="Копировать"><span class="rail-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="8" y="8" width="14" height="14" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg></span><span class="rail-label">Копировать</span></button>
    <button type="button" class="nav-link" data-section="create" data-title="Новый пользователь" title="Создать"><span class="rail-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6"/><path d="M22 11h-6"/></svg></span><span class="rail-label">Создать</span></button>
    <button type="button" class="nav-link" data-section="delete" data-title="Удаление пользователей" title="Удалить"><span class="rail-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 11h-6"/></svg></span><span class="rail-label">Удалить</span></button>
    <button type="button" class="nav-link" data-section="groups" data-title="Групповой номер" title="Группы"><span class="rail-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg></span><span class="rail-label">Группы</span></button>
    <button type="button" class="nav-link" data-section="password" data-title="Сброс пароля" title="Пароль"><span class="rail-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6"/><path d="m15.5 7.5 3 3L22 7l-3-3"/></svg></span><span class="rail-label">Пароль</span></button>
    <button type="button" class="nav-link" data-section="roles" data-title="Роль Webitel" title="Роли"><span class="rail-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/></svg></span><span class="rail-label">Роли</span></button>
    <button type="button" class="nav-link" data-section="journal" data-title="Журнал" title="Журнал"><span class="rail-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></svg></span><span class="rail-label">Журнал</span></button>
  </nav>
  <button type="button" class="nav-account-btn" data-section="account" data-title="Вход и настройки" title="Вход и настройки"><span class="rail-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 7h-9"/><path d="M14 17H5"/><circle cx="17" cy="17" r="3"/><circle cx="7" cy="7" r="3"/></svg><span class="status-dot" id="navLoginDot" title="CRM: не выполнен вход"></span></span><span class="rail-label">Вход</span></button>
</aside>
<main class="shell">
<header class="topbar">
  <div class="status-chips">
    <button type="button" class="chip" data-chip="crm"><span class="status-dot"></span>CRM <b>не выполнен</b></button>
    <button type="button" class="chip" data-chip="almaty"><span class="status-dot"></span>Алматы <b>проверяю...</b></button>
    <button type="button" class="chip" data-chip="astana"><span class="status-dot"></span>Астана <b>проверяю...</b></button>
  </div>
  <span class="topbar-brand">Консоль оператора</span>
</header>
<div class="page">
  <div id="outdatedBanner" class="code-banner" hidden>Консоль обновилась. <button type="button" id="btnReload">Обновить страницу</button></div>
  <div id="adBanner" class="code-banner" hidden>Вход в консоль закончился. <button type="button" id="btnAdLogin">Войти снова</button></div>
  <div id="crmCodeBanner" class="code-banner" hidden>CRM просит код подтверждения. <button type="button" id="btnGoToCode">Ввести код</button></div>
  <div class="page-head">
    <div>
      <h1 id="pageTitle">Заявка</h1>
      <div class="page-sub"><span id="pageDate"></span><span class="sub top-sub">Локальный инструмент. Только на этом компьютере. Каждое реальное изменение требует отдельного подтверждения.</span></div>
    </div>
    <div class="city-picker" id="cityPicker">
      <span class="city-picker-label">Город:</span>
      <div class="tabs city-tabs">
        <button type="button" class="city-btn active" data-city="almaty">Алматы</button>
        <button type="button" class="city-btn" data-city="astana">Астана</button>
      </div>
    </div>
  </div>

  <section class="app-section" data-section-panel="account" hidden>
  <div class="steps cols-2">

  <div class="side-column">
  <div class="card">
    <div class="card-head"><h2>Настройки — свои учётные данные</h2></div>
    <div class="card-body">
      <div class="row">
        <div class="field-group">
          <label class="field-label" for="settingsCrmVisaOwnerId">CRM Visa Owner ID (необязательно)</label>
          <input type="text" id="settingsCrmVisaOwnerId" placeholder="пусто — ваши задачи и задачи всех ваших ролей" autocomplete="off">
        </div>
      </div>
      <div class="row">
        <button id="btnSettingsSave">Сохранить</button>
      </div>
      <div id="settingsMsg" class="msg info"></div>
    </div>
  </div>

  <div class="card" id="accessCard" hidden>
    <div class="card-head"><h2>Доступ в консоль</h2></div>
    <div class="card-body">
      <div class="saved-login">
        <span class="saved-login-text">Вы вошли как <b id="accessMe"></b></span>
        <span class="saved-login-actions"><button type="button" class="ghost" id="btnAdLogout">Выйти</button></span>
      </div>
      <div id="accessAdmin" hidden>
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="accessLogin">Логин AD</label>
            <input type="text" id="accessLogin" placeholder="напр. i.ivanov" autocomplete="off" spellcheck="false">
          </div>
          <button type="button" id="btnAccessGrant">Добавить</button>
        </div>
        <div id="accessMsg" class="msg info"></div>
        <div id="accessList" class="list"></div>
      </div>
    </div>
  </div>
  </div>

  <div class="login-column">
  <div class="card login-card">
    <div class="card-head"><h2>Вход в CRM</h2><span id="loginStatus" class="pill neutral">не проверено</span></div>
    <div class="card-body">
      <div class="saved-login" id="savedLogin" hidden>
        <span class="saved-login-text">Сохранённый вход: <b id="savedLoginName"></b></span>
        <span class="saved-login-actions">
          <button type="button" id="btnChangeLogin" class="ghost">Сменить</button>
          <button type="button" id="btnForgetLogin" class="ghost icon-btn" title="Удалить сохранённый вход" aria-label="Удалить сохранённый вход">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>
          </button>
        </span>
      </div>
      <div id="loginForm">
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="crmLogin">Логин CRM</label>
            <input type="text" id="crmLogin" placeholder="Логин" autocomplete="username">
          </div>
        </div>
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="crmPassword">Пароль CRM</label>
            <input type="password" id="crmPassword" placeholder="••••••••" autocomplete="current-password">
          </div>
        </div>
        <label class="checkbox-row">
          <input type="checkbox" id="crmRemember" checked>
          Запомнить и входить автоматически
        </label>
      </div>
      <div class="row">
        <button id="btnLogin">Войти в CRM</button>
      </div>
      <div id="crmCodeBox" class="code-box" hidden>
        <label class="field-label" for="crmCode">Код подтверждения из приложения-аутентификатора</label>
        <div class="row">
          <div class="field-group">
            <input type="text" id="crmCode" inputmode="numeric" autocomplete="one-time-code" placeholder="000000">
          </div>
          <button type="button" id="btnCrmCode">Подтвердить</button>
        </div>
      </div>
      <div id="loginMsg" class="msg info">Логин и пароль подставятся сами. Если CRM спросит код подтверждения — поле для него появится здесь.</div>
      <button type="button" id="btnCrmScreenshot" class="ghost screenshot-btn" hidden>Что на экране CRM</button>
      <img id="crmScreenshot" class="crm-screenshot" alt="Экран CRM" hidden>
    </div>
  </div>

  <div class="card">
    <div class="card-head"><h2>Вход в Webitel</h2></div>
    <div class="card-body">
      <div class="webitel-city" data-city="almaty">
        <div class="webitel-city-head"><b>Алматы</b><span class="pill neutral" data-role="status">проверяю...</span></div>
        <div class="saved-login" data-role="saved" hidden>
          <span class="saved-login-text">Сохранённый вход: <b data-role="saved-name"></b></span>
          <span class="saved-login-actions">
            <button type="button" class="ghost" data-role="change">Сменить</button>
            <button type="button" class="ghost icon-btn" data-role="forget" title="Удалить сохранённый вход" aria-label="Удалить сохранённый вход">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>
            </button>
          </span>
        </div>
        <div data-role="form" hidden>
          <div class="row">
            <div class="field-group">
              <label class="field-label">Логин Webitel Алматы</label>
              <input type="text" data-role="login-input" placeholder="Логин" autocomplete="off">
            </div>
          </div>
          <div class="row">
            <div class="field-group">
              <label class="field-label">Пароль</label>
              <input type="password" data-role="password-input" placeholder="••••••••" autocomplete="off">
            </div>
          </div>
        </div>
        <button type="button" class="ghost" data-role="login-btn">Войти</button>
        <div class="msg info" data-role="msg"></div>
        <button type="button" class="ghost screenshot-btn" data-role="shot-btn" hidden>Что было на экране Webitel</button>
        <img class="crm-screenshot" data-role="shot" alt="Экран Webitel" hidden>
      </div>
      <div class="webitel-city" data-city="astana">
        <div class="webitel-city-head"><b>Астана</b><span class="pill neutral" data-role="status">проверяю...</span></div>
        <div class="saved-login" data-role="saved" hidden>
          <span class="saved-login-text">Сохранённый вход: <b data-role="saved-name"></b></span>
          <span class="saved-login-actions">
            <button type="button" class="ghost" data-role="change">Сменить</button>
            <button type="button" class="ghost icon-btn" data-role="forget" title="Удалить сохранённый вход" aria-label="Удалить сохранённый вход">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>
            </button>
          </span>
        </div>
        <div data-role="form" hidden>
          <div class="row">
            <div class="field-group">
              <label class="field-label">Логин Webitel Астана</label>
              <input type="text" data-role="login-input" placeholder="Логин" autocomplete="off">
            </div>
          </div>
          <div class="row">
            <div class="field-group">
              <label class="field-label">Пароль</label>
              <input type="password" data-role="password-input" placeholder="••••••••" autocomplete="off">
            </div>
          </div>
        </div>
        <button type="button" class="ghost" data-role="login-btn">Войти</button>
        <div class="msg info" data-role="msg"></div>
        <button type="button" class="ghost screenshot-btn" data-role="shot-btn" hidden>Что было на экране Webitel</button>
        <img class="crm-screenshot" data-role="shot" alt="Экран Webitel" hidden>
      </div>
    </div>
  </div>
  </div>

  </div>
  </section>

  <section class="app-section" data-section-panel="ticket">
  <div class="steps">

  <div class="card">
    <div class="card-head"><h2>Заявка</h2></div>
    <div class="card-body split">
      <div class="split-main">
        <div class="row">
          <button id="btnPendingTickets" class="ghost">Обновить список ожидающих</button>
        </div>
        <div id="pendingMsg" class="msg info"></div>
        <div id="pendingList" class="list"></div>
        <div class="row" style="margin-top:12px;">
          <div class="field-group">
            <label class="field-label" for="ticketUrl">Ссылка на заявку (CRM)</label>
            <input type="text" id="ticketUrl" placeholder="https://crm.astana-motors.kz/...">
          </div>
          <button id="btnReadTicket">Открыть</button>
        </div>
      </div>
      <div class="split-side">
        <div id="ticketMsg" class="msg info"></div>
        <div id="ticketCard" class="result-card" hidden></div>
        <div class="split-placeholder">Откройте заявку из списка или по ссылке — здесь появятся её данные</div>
      </div>
    </div>
  </div>

  </div>
  </section>

  <section class="app-section" data-section-panel="copy" hidden>
  <div class="steps">

  <div class="card">
    <div class="card-head"><h2>Webitel: копирование ролей/лицензии/группы по образцу</h2></div>
    <div class="card-body split form-split">
      <div class="split-main">
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="targetExt">Кому скопировать (через запятую, можно диапазон 15210-15220)</label>
            <input type="text" id="targetExt" placeholder="напр. 10079, 10085-10090">
          </div>
        </div>
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="templateExt">Логин пользователя-образца</label>
            <input type="text" id="templateExt" placeholder="напр. 10078">
          </div>
        </div>
        <div class="neighbors-row">
          <button type="button" id="btnCopyNeighbors" class="ghost">Подобрать по соседним номерам</button>
          <label class="radius-label" title="Сколько номеров смотреть в каждую сторону">±
            <input type="text" id="copyNeighborsRadius" value="2" inputmode="numeric">
          </label>
        </div>
        <div id="copyNeighborsMsg" class="msg info"></div>
        <div id="copyNeighborsList" class="list"></div>
        <div class="checkbox-line">
          <label class="checkbox-row">
            <input type="checkbox" id="resetPasswordCheck">
            Сбросить пароль
          </label>
          <label class="checkbox-row">
            <input type="checkbox" id="copyPermissionsCheck">
            Копировать Permissions
          </label>
        </div>
        <div class="row">
          <button id="btnPlan">Показать план</button>
        </div>
      </div>
      <div class="split-side">
        <div id="planMsg" class="msg info"></div>
        <div id="planList" class="list"></div>
        <div class="row" id="confirmRow" hidden>
          <button id="btnExecute" class="danger">Подтвердить и применить все</button>
        </div>
        <div id="executeMsg" class="msg info"></div>
        <div class="split-placeholder">Здесь появится план. В Webitel ничего не меняется, пока вы его не подтвердите.</div>
      </div>
    </div>
  </div>

  </div>
  </section>

  <section class="app-section" data-section-panel="create" hidden>
  <div class="steps">

  <div class="card">
    <div class="card-head"><h2>Webitel: создание нового пользователя</h2></div>
    <div class="card-body">

      <div class="tabs">
        <button type="button" class="tab-btn active" data-tab="single">Один пользователь</button>
        <button type="button" class="tab-btn" data-tab="bulk">Массово</button>
      </div>

      <div class="tab-panel split form-split" data-panel="single">
        <div class="split-main">
        <div class="form-step-title">1. Новый пользователь</div>
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="singleNewLogin">Логин (номер)</label>
            <input type="text" id="singleNewLogin" placeholder="напр. 10087">
          </div>
        </div>
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="singleNewName">ФИО</label>
            <input type="text" id="singleNewName" placeholder="Имя Фамилия">
          </div>
        </div>

        <div class="form-step-title">2. Образец — чьи роли, лицензию и группу скопировать</div>
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="singleTemplateExt">Логин образца</label>
            <input type="text" id="singleTemplateExt" placeholder="напр. 10078">
          </div>
        </div>
        <div class="neighbors-row">
          <button type="button" id="btnNeighbors" class="ghost">Подобрать по соседним номерам</button>
          <label class="radius-label" title="Сколько номеров смотреть в каждую сторону">±
            <input type="text" id="neighborsRadius" value="2" inputmode="numeric">
          </label>
        </div>
        <div id="neighborsMsg" class="msg info"></div>
        <div id="neighborsList" class="list"></div>

        <label class="checkbox-row form-submit">
          <input type="checkbox" id="singleCopyPermissionsCheck">
          Копировать Permissions
        </label>
        <div class="row">
          <button id="btnCreatePlanSingle">Показать план</button>
        </div>
        </div>
        <div class="split-side">
        <div id="createPlanMsgSingle" class="msg info"></div>
        <div id="createPlanCardSingle" class="result-card" hidden></div>
        <div class="row" id="createConfirmRowSingle" hidden>
          <button id="btnCreateExecuteSingle" class="danger">Подтвердить и создать</button>
        </div>
        <div id="createExecuteMsgSingle" class="msg info"></div>
        <div class="split-placeholder">Здесь появится план создания. Пользователь не создаётся, пока вы не подтвердите.</div>
        </div>
      </div>

      <div class="tab-panel split form-split" data-panel="bulk" hidden>
        <div class="split-main">
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="createTemplateExt">Логин пользователя-образца (общий для всех ниже)</label>
            <input type="text" id="createTemplateExt" placeholder="напр. 10078">
          </div>
        </div>
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="createBulkList">Новые пользователи (по одному на строку)</label>
            <textarea id="createBulkList" rows="4" placeholder="10087,Иван Иванов&#10;10097,Мария Петрова"></textarea>
            <div class="hint">Формат строки: <code>логин,ФИО</code></div>
          </div>
        </div>
        <label class="checkbox-row">
          <input type="checkbox" id="bulkCopyPermissionsCheck">
          Копировать Permissions
        </label>
        <div class="row">
          <button id="btnCreatePlan">Показать план для всех</button>
        </div>
        </div>
        <div class="split-side">
        <div id="createPlanMsg" class="msg info"></div>
        <div id="createPlanList" class="list"></div>
        <div class="row" id="createConfirmRow" hidden>
          <button id="btnCreateExecute" class="danger">Подтвердить и создать все</button>
        </div>
        <div id="createExecuteMsg" class="msg info"></div>
        <div class="split-placeholder">Здесь появится план для всех строк. Ничего не создаётся, пока вы не подтвердите.</div>
        </div>
      </div>

    </div>
  </div>

  </div>
  </section>

  <section class="app-section" data-section-panel="delete" hidden>
  <div class="steps">

  <div class="card">
    <div class="card-head"><h2>Webitel: удаление пользователей</h2></div>
    <div class="card-body split form-split">
      <div class="split-main">
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="deleteExt">Логин/добавочный (через запятую, можно диапазон 15210-15220)</label>
            <input type="text" id="deleteExt" placeholder="напр. 10087, 10097">
          </div>
        </div>
        <div class="row">
          <button id="btnDeletePlan">Показать план</button>
        </div>
      </div>
      <div class="split-side">
        <div id="deletePlanMsg" class="msg info"></div>
        <div id="deletePlanList" class="list"></div>
        <div class="row" id="deleteCountRow" hidden>
          <div class="field-group">
            <label class="field-label" for="deleteCount" id="deleteCountLabel"></label>
            <input type="text" id="deleteCount" inputmode="numeric" autocomplete="off">
          </div>
        </div>
        <div class="row" id="deleteConfirmRow" hidden>
          <button id="btnDeleteExecute" class="danger">Подтвердить и удалить все</button>
        </div>
        <div id="deleteExecuteMsg" class="msg info"></div>
        <div class="split-placeholder">Здесь появится список на удаление. Никто не удаляется, пока вы не подтвердите.</div>
      </div>
    </div>
  </div>

  </div>
  </section>

  <section class="app-section" data-section-panel="groups" hidden>
  <div class="steps">

  <div class="card">
    <div class="card-head"><h2>Webitel: групповой номер</h2></div>
    <div class="card-body">

      <div class="tabs">
        <button type="button" class="tab-btn active" data-tab="group-single">Одна группа</button>
        <button type="button" class="tab-btn" data-tab="group-bulk">Несколько</button>
      </div>

      <div class="tab-panel split form-split" data-panel="group-single">
        <div class="split-main">
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="groupNumber">Групповой номер (на него звонят)</label>
            <input type="text" id="groupNumber" inputmode="numeric" placeholder="напр. 30151">
          </div>
        </div>
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="groupName">Название</label>
            <input type="text" id="groupName" placeholder="напр. ALM-OP-30151">
          </div>
        </div>
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="groupMembers">Участники — номера, которые звонят</label>
            <textarea id="groupMembers" rows="4" placeholder="напр. 30152, 30153"></textarea>
            <div class="hint">Через запятую, пробел или с новой строки. Звонят все сразу.</div>
          </div>
        </div>
        <div class="row" data-city-only="astana" hidden>
          <div class="field-group">
            <label class="field-label" for="groupFallback">Если никто не ответил — на номер (необязательно)</label>
            <input type="text" id="groupFallback" inputmode="numeric" placeholder="напр. 21700">
          </div>
        </div>
        <div class="row">
          <button id="btnGroupPlanSingle">Показать план</button>
        </div>
        </div>
        <div class="split-side">
        <div id="groupPlanMsgSingle" class="msg info"></div>
        <div id="groupPlanListSingle" class="list"></div>
        <div class="row" id="groupConfirmRowSingle" hidden>
          <button id="btnGroupExecuteSingle" class="danger">Подтвердить и создать</button>
        </div>
        <div id="groupExecuteMsgSingle" class="msg info"></div>
        <div class="split-placeholder">Здесь появится план. Схема в Webitel не создаётся, пока вы не подтвердите.</div>
        </div>
      </div>

      <div class="tab-panel split form-split" data-panel="group-bulk" hidden>
        <div class="split-main">
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="groupBulkList">Группы (по одной на строку)</label>
            <textarea id="groupBulkList" rows="6" placeholder="30151, ALM-OP-30151: 30152, 30153&#10;30161, ALM-OP-30161: 30162, 30163"></textarea>
            <div class="hint">Формат строки: <code>номер, название: участники</code><span data-city-only="astana" hidden>. Если никто не ответил — на номер: в конце <code>&gt; номер</code></span></div>
          </div>
        </div>
        <div class="row">
          <button id="btnGroupPlanBulk">Показать план для всех</button>
        </div>
        </div>
        <div class="split-side">
        <div id="groupPlanMsgBulk" class="msg info"></div>
        <div id="groupPlanListBulk" class="list"></div>
        <div class="row" id="groupConfirmRowBulk" hidden>
          <button id="btnGroupExecuteBulk" class="danger">Подтвердить и создать все</button>
        </div>
        <div id="groupExecuteMsgBulk" class="msg info"></div>
        <div class="split-placeholder">Здесь появится план для всех строк. Ничего не создаётся, пока вы не подтвердите.</div>
        </div>
      </div>

    </div>
  </div>

  </div>
  </section>

  <section class="app-section" data-section-panel="password" hidden>
  <div class="steps">

  <div class="card">
    <div class="card-head"><h2>Webitel: сброс пароля (пользователь и его устройство)</h2></div>
    <div class="card-body split form-split">
      <div class="split-main">
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="passwordExt">Логин/добавочный (можно несколько через запятую)</label>
            <input type="text" id="passwordExt" placeholder="напр. 10087, 10097">
            <div class="hint">Новый пароль будет один и тот же у пользователя и его устройства.</div>
          </div>
        </div>
        <div class="row">
          <button id="btnPasswordPlan">Показать план</button>
        </div>
      </div>
      <div class="split-side">
        <div id="passwordPlanMsg" class="msg info"></div>
        <div id="passwordPlanList" class="list"></div>
        <div class="row" id="passwordConfirmRow" hidden>
          <button id="btnPasswordExecute" class="danger">Подтвердить и сбросить</button>
        </div>
        <div id="passwordExecuteMsg" class="msg info"></div>
        <div class="split-placeholder">Здесь появится список. Пароли не меняются, пока вы не подтвердите.</div>
      </div>
    </div>
  </div>

  </div>
  </section>

  <section class="app-section" data-section-panel="roles" hidden>
  <div class="steps">

  <div class="card">
    <div class="card-head"><h2>Webitel: роль</h2></div>
    <div class="card-body split form-split">
      <div class="split-main">
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="roleName">Название</label>
            <input type="text" id="roleName" placeholder="напр. user-KRG-CAK-30500">
          </div>
        </div>
        <div class="row">
          <button id="btnRolePlan">Показать план</button>
        </div>
      </div>
      <div class="split-side">
        <div id="rolePlanMsg" class="msg info"></div>
        <div id="rolePlanCard" class="result-card" hidden></div>
        <div class="row" id="roleConfirmRow" hidden>
          <button id="btnRoleExecute" class="danger">Подтвердить и создать</button>
        </div>
        <div id="roleExecuteMsg" class="msg info"></div>
        <div class="split-placeholder">Здесь появится план. Роль не создаётся, пока вы не подтвердите.</div>
      </div>
    </div>
  </div>

  </div>
  </section>

  <section class="app-section" data-section-panel="journal" hidden>
  <div class="steps">

  <div class="card">
    <div class="card-head"><h2>Что сделано в Webitel через консоль</h2></div>
    <div class="card-body">
      <div class="row">
        <div class="field-group">
          <input type="text" id="journalQuery" placeholder="номер, имя или кто" aria-label="Поиск по журналу">
        </div>
        <button id="btnJournal">Найти</button>
      </div>
      <div class="journal-bar">
        <div id="journalMsg" class="msg info"></div>
        <div class="tabs journal-limits" role="group" aria-label="Сколько записей показывать">
          <button type="button" class="limit-btn" data-limit="10">10</button>
          <button type="button" class="limit-btn active" data-limit="25">25</button>
          <button type="button" class="limit-btn" data-limit="50">50</button>
          <button type="button" class="limit-btn" data-limit="100">100</button>
        </div>
      </div>
      <div id="journalList" class="list"></div>
      <div class="row journal-more" id="journalMoreRow" hidden>
        <button type="button" id="btnJournalMore" class="ghost">Показать ещё</button>
      </div>
    </div>
  </div>

  </div>
  </section>

</div>
</main>

<script>
const PAGE_VERSION = '{{PAGE_VERSION}}';

async function api(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Console-Version': PAGE_VERSION},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // Never reloads by itself: passwords may still be on the screen.
    if (data.outdated) document.getElementById('outdatedBanner').hidden = false;
    if (data.ad_login) document.getElementById('adBanner').hidden = false;
    const err = new Error(data.error || ('HTTP ' + res.status));
    err.data = data;
    throw err;
  }
  return data;
}

document.getElementById('btnReload').addEventListener('click', () => location.reload());
document.getElementById('btnAdLogin').addEventListener('click', () => location.reload());

const navButtons = document.querySelectorAll('.nav-link, .nav-account-btn');
const CITY_PICKER_SECTIONS = ['copy', 'create', 'delete', 'groups', 'password', 'roles'];

navButtons.forEach(btn => {
  btn.addEventListener('click', () => {
    navButtons.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const target = btn.dataset.section;
    document.querySelectorAll('.app-section').forEach(panel => {
      panel.hidden = panel.dataset.sectionPanel !== target;
    });
    document.getElementById('cityPicker').hidden = !CITY_PICKER_SECTIONS.includes(target);
    document.getElementById('pageTitle').textContent = btn.dataset.title;
    if (target === 'journal') loadJournal(0);
  });
});

document.getElementById('cityPicker').hidden = !CITY_PICKER_SECTIONS.includes('ticket');

const today = new Date().toLocaleDateString('ru-RU', {weekday: 'long', day: 'numeric', month: 'long'});
document.getElementById('pageDate').textContent = today.charAt(0).toUpperCase() + today.slice(1);

// Top bar chips: the CRM and Webitel login statuses from «Вход и настройки»,
// always in sight; a click opens that page.
function setChip(name, text, tone) {
  const chip = document.querySelector(`.chip[data-chip="${name}"]`);
  chip.querySelector('b').textContent = text;
  chip.querySelector('.status-dot').className = 'status-dot ' + (tone || '');
}
document.querySelectorAll('.chip').forEach(chip => {
  chip.addEventListener('click', () => document.querySelector('.nav-account-btn').click());
});

let selectedCity = 'almaty';

// Example numbers in the Webitel forms follow the selected city, so an
// Astana operator isn't shown Almaty's 100xx extensions as the pattern.
const CITY_PLACEHOLDERS = {
  almaty: {
    templateExt: 'напр. 10078', targetExt: 'напр. 10079, 10085-10090',
    singleNewLogin: 'напр. 10087', singleTemplateExt: 'напр. 10078',
    createTemplateExt: 'напр. 10078', createBulkList: '10087,Иван Иванов\\n10097,Мария Петрова',
    deleteExt: 'напр. 10087, 10090-10095', passwordExt: 'напр. 10087, 10097',
    groupNumber: 'напр. 30151', groupName: 'напр. ALM-OP-30151', groupMembers: 'напр. 30152, 30153',
    groupBulkList: '30151, ALM-OP-30151: 30152, 30153\\n30161, ALM-OP-30161: 30162, 30163',
  },
  astana: {
    templateExt: 'напр. 28070', targetExt: 'напр. 28071, 28075-28080',
    singleNewLogin: 'напр. 28071', singleTemplateExt: 'напр. 28070',
    createTemplateExt: 'напр. 28070', createBulkList: '28071,Иван Иванов\\n28075,Мария Петрова',
    deleteExt: 'напр. 28071, 28075-28080', passwordExt: 'напр. 28071, 28075',
    groupNumber: 'напр. 21701', groupName: 'напр. BMW Astana - 21701', groupMembers: 'напр. 21703, 21704',
    groupBulkList: '21701, BMW Astana - 21701: 21703, 21704 > 21700\\n28020, AST-AMP - 28020: 21322, 21323',
  },
};

function applyCityFields(city) {
  for (const [id, text] of Object.entries(CITY_PLACEHOLDERS[city] || {})) {
    const el = document.getElementById(id);
    if (el) el.placeholder = text;
  }
  // Parts only one city's template has (Astana's «если никто не ответил»).
  document.querySelectorAll('[data-city-only]').forEach(el => {
    el.hidden = el.dataset.cityOnly !== city;
  });
}

document.querySelectorAll('.city-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.city-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    selectedCity = btn.dataset.city;
    applyCityFields(selectedCity);
  });
});
applyCityFields(selectedCity);

document.getElementById('btnSettingsSave').addEventListener('click', async () => {
  const msgEl = document.getElementById('settingsMsg');
  const btn = document.getElementById('btnSettingsSave');
  const payload = {
    crm_visa_owner_id: document.getElementById('settingsCrmVisaOwnerId').value.trim(),
  };
  btn.disabled = true;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Сохраняю...';
  try {
    await api('/api/settings/save', payload);
    msgEl.className = 'msg info';
    msgEl.textContent = 'Сохранено';
  } catch (e) {
    msgEl.textContent = 'Ошибка: ' + e.message;
    msgEl.className = 'msg error';
  } finally {
    btn.disabled = false;
  }
});

// --- Webitel login, per city, with each operator's own saved account ---
const WEBITEL_CITIES = ['almaty', 'astana'];
const WEBITEL_SESSION_PILLS = {
  active: ['сессия активна', 'pill good'],
  expired: ['сессия истекла', 'pill warn'],
  missing: ['не входили', 'pill neutral'],
  error: ['ошибка проверки', 'pill critical'],
};
const webitelPolling = {};
const webitelEditing = {};
const webitelLast = {};
const WEBITEL_CITY_NAMES = {almaty: 'Алматы', astana: 'Астана'};

function renderWebitelCity(city, data) {
  const box = document.querySelector(`.webitel-city[data-city="${city}"]`);
  const pill = box.querySelector('[data-role="status"]');
  const msg = box.querySelector('[data-role="msg"]');
  const btn = box.querySelector('[data-role="login-btn"]');
  const running = data.job && data.job.state === 'running';
  const [text, cls] = running ? ['вход...', 'pill warn'] : (WEBITEL_SESSION_PILLS[data.session] || WEBITEL_SESSION_PILLS.error);
  pill.textContent = text;
  pill.className = cls;
  setChip(city, text, cls.replace('pill ', ''));
  webitelLast[city] = data;
  // Same pattern as the CRM card: a remembered account shows as a row with
  // "Сменить" and a trash button; otherwise (or while changing) the fields.
  const saved = !!(data.login && data.has_password);
  const editing = !saved || !!webitelEditing[city];
  box.querySelector('[data-role="saved"]').hidden = !saved;
  box.querySelector('[data-role="saved-name"]').textContent = data.login || '';
  const form = box.querySelector('[data-role="form"]');
  if (editing && form.hidden && !saved) box.querySelector('[data-role="login-input"]').value = data.login || '';
  form.hidden = !editing;
  box.querySelector('[data-role="change"]').textContent = saved && editing ? 'Отмена' : 'Сменить';
  btn.disabled = running;
  btn.textContent = editing ? 'Сохранить и войти' : (data.session === 'active' ? 'Войти заново' : 'Войти');
  msg.className = 'msg info';
  box.querySelector('[data-role="shot-btn"]').hidden = true;
  if (running) {
    msg.textContent = 'Вхожу в Webitel...';
  } else if (data.job && data.job.state === 'failed') {
    msg.className = 'msg error';
    msg.textContent = 'Вход не удался: ' + data.job.error;
    box.querySelector('[data-role="shot-btn"]').hidden = false;
  } else if (data.session === 'expired') {
    msg.textContent = 'Сохранённая сессия истекла — нажмите «Войти».';
  } else if (data.session === 'error') {
    msg.className = 'msg error';
    msg.textContent = data.error || '';
  } else {
    msg.textContent = '';
  }
}

async function refreshWebitelCity(city) {
  try {
    const data = await api('/api/webitel/session_status', {city});
    renderWebitelCity(city, data);
    return data;
  } catch (e) {
    renderWebitelCity(city, {session: 'error', error: e.message, login: '', configured: false});
    return null;
  }
}

async function pollWebitelLogin(city) {
  if (webitelPolling[city]) return;
  webitelPolling[city] = true;
  try {
    for (let i = 0; i < 330; i++) {
      await new Promise(r => setTimeout(r, 2000));
      const data = await refreshWebitelCity(city);
      if (!data || !data.job || data.job.state !== 'running') return;
    }
  } finally {
    webitelPolling[city] = false;
  }
}

WEBITEL_CITIES.forEach(city => {
  const box = document.querySelector(`.webitel-city[data-city="${city}"]`);
  const msg = box.querySelector('[data-role="msg"]');
  const loginInput = box.querySelector('[data-role="login-input"]');
  const passwordInput = box.querySelector('[data-role="password-input"]');

  box.querySelector('[data-role="login-btn"]').addEventListener('click', async () => {
    try {
      if (!box.querySelector('[data-role="form"]').hidden) {
        // Typed credentials are remembered for this city, then used right away.
        await api('/api/webitel/credentials/save', {city, login: loginInput.value.trim(), password: passwordInput.value});
        passwordInput.value = '';
        webitelEditing[city] = false;
      }
      await api('/api/webitel/login', {city});
      await refreshWebitelCity(city);
      pollWebitelLogin(city);
    } catch (e) {
      msg.className = 'msg error';
      msg.textContent = e.message;
    }
  });

  box.querySelector('[data-role="shot-btn"]').addEventListener('click', async () => {
    const img = box.querySelector('[data-role="shot"]');
    if (!img.hidden) { img.hidden = true; return; }
    try {
      const data = await api('/api/webitel/login/screenshot', {city});
      img.src = 'data:image/png;base64,' + data.png;
      img.hidden = false;
    } catch (e) {
      msg.className = 'msg error';
      msg.textContent = e.message;
    }
  });

  box.querySelector('[data-role="change"]').addEventListener('click', () => {
    webitelEditing[city] = !webitelEditing[city];
    loginInput.value = '';
    passwordInput.value = '';
    if (webitelLast[city]) renderWebitelCity(city, webitelLast[city]);
    if (webitelEditing[city]) loginInput.focus();
  });

  box.querySelector('[data-role="forget"]').addEventListener('click', async () => {
    const login = (webitelLast[city] || {}).login || '';
    if (!confirm(`Удалить сохранённый вход ${login} для Webitel ${WEBITEL_CITY_NAMES[city]}? Логин, пароль и сохранённая сессия этого города сотрутся из консоли.`)) return;
    try {
      await api('/api/webitel/credentials/forget', {city});
      webitelEditing[city] = false;
      await refreshWebitelCity(city);
    } catch (e) {
      msg.className = 'msg error';
      msg.textContent = e.message;
    }
  });
  refreshWebitelCity(city).then(data => {
    if (data && data.job && data.job.state === 'running') pollWebitelLogin(city);
  });
});

const loginStatusEl = document.getElementById('loginStatus');
const loginMsgEl = document.getElementById('loginMsg');
const navLoginDot = document.getElementById('navLoginDot');
const savedLoginEl = document.getElementById('savedLogin');
const loginFormEl = document.getElementById('loginForm');
let savedCrmLogin = '';
let polling = false;

let crmLoginState = 'idle';
let lastAutoLogin = 0;

// Re-login with the remembered account, but never more than once a minute —
// a wrong saved password must not turn into a login loop.
function autoRelogin() {
  if (!savedCrmLogin || Date.now() - lastAutoLogin < 60000) return false;
  lastAutoLogin = Date.now();
  setTimeout(() => startCrmLogin({}), 0);
  return true;
}

// A CRM action came back with crm_session_expired: reflect it and, with a
// remembered login, log in again straight away.
function crmSessionExpired(e) {
  if (!(e && e.data && e.data.crm_session_expired)) return false;
  if (crmLoginState === 'ready' || crmLoginState === 'idle') {
    setLoginState('idle');
    loginMsgEl.className = 'msg error';
    loginMsgEl.textContent = 'Сессия CRM истекла.';
    autoRelogin();
  }
  return true;
}

function setLoginState(state) {
  const states = {
    idle: ['не выполнен', 'pill neutral', '', 'CRM: не выполнен вход'],
    starting: ['запускаю...', 'pill warn', 'warn', 'CRM: выполняется вход'],
    waiting: ['жду вход...', 'pill warn', 'warn', 'CRM: выполняется вход'],
    ready: ['вход выполнен', 'pill good', 'good', 'CRM: вход выполнен'],
    unconfirmed: ['не подтверждено', 'pill warn', 'warn', 'CRM: вход не подтверждён'],
    code: ['нужен код', 'pill warn', 'warn', 'CRM: нужен код подтверждения'],
  };
  crmLoginState = state;
  document.getElementById('crmCodeBanner').hidden = state !== 'code';
  const [text, pillClass, dotClass, title] = states[state];
  loginStatusEl.textContent = text;
  loginStatusEl.className = pillClass;
  navLoginDot.className = 'status-dot ' + dotClass;
  navLoginDot.title = title;
  setChip('crm', text, dotClass);
}

const visaOwnerInput = document.getElementById('settingsCrmVisaOwnerId');

// After a login the saved Visa Owner ID may have changed (an account
// switch clears the previous person's one), so refresh the settings field.
async function onLoggedIn() {
  try {
    const creds = await api('/api/crm/credentials', {});
    visaOwnerInput.value = creds.visa_owner_id || '';
  } catch (e) { /* purely cosmetic */ }
}

function showSavedLogin(login) {
  savedCrmLogin = login;
  document.getElementById('savedLoginName').textContent = login;
  savedLoginEl.hidden = !login;
  loginFormEl.hidden = !!login;
}

document.getElementById('btnForgetLogin').addEventListener('click', async () => {
  if (!confirm(`Удалить сохранённый вход ${savedCrmLogin}? Логин и пароль CRM (и Visa Owner ID, если был) сотрутся из консоли. Текущий вход продолжит работать, пока открыто окно CRM.`)) return;
  try {
    await api('/api/crm/credentials/forget', {});
    showSavedLogin('');
    visaOwnerInput.value = '';
    loginMsgEl.className = 'msg info';
    loginMsgEl.textContent = 'Сохранённый вход удалён. В следующий раз консоль попросит логин и пароль.';
  } catch (e) {
    loginMsgEl.className = 'msg error';
    loginMsgEl.textContent = 'Ошибка: ' + e.message;
  }
});

document.getElementById('btnChangeLogin').addEventListener('click', () => {
  loginFormEl.hidden = false;
  document.getElementById('crmLogin').value = '';
  document.getElementById('crmPassword').value = '';
  document.getElementById('crmLogin').focus();
});

async function startCrmLogin(payload) {
  setLoginState('starting');
  loginMsgEl.className = 'msg info';
  loginMsgEl.textContent = 'Вхожу в CRM...';
  try {
    const data = await api('/api/crm/login', payload);
    loginMsgEl.textContent = `Входим как ${data.login}.`;
    if (payload.remember) showSavedLogin(data.login);
    document.getElementById('crmPassword').value = '';
    pollLoginStatus();
  } catch (e) {
    setLoginState('idle');
    loginMsgEl.textContent = 'Ошибка: ' + e.message;
    loginMsgEl.className = 'msg error';
  }
}

document.getElementById('btnLogin').addEventListener('click', () => {
  const login = document.getElementById('crmLogin').value.trim();
  const password = document.getElementById('crmPassword').value;
  if (!loginFormEl.hidden && (login || password || !savedCrmLogin)) {
    // Typed credentials win over the remembered ones.
    startCrmLogin({login, password, remember: document.getElementById('crmRemember').checked});
  } else {
    startCrmLogin({});
  }
});

// Confirmation code: the CRM browser runs hidden, so when the CRM asks for
// the authenticator code the operator types it here and it's relayed.
const crmCodeBox = document.getElementById('crmCodeBox');
const crmCodeInput = document.getElementById('crmCode');

function showCrmCodeBox(show) {
  const wasHidden = crmCodeBox.hidden;
  crmCodeBox.hidden = !show;
  if (show && wasHidden) crmCodeInput.focus();
  if (!show) {
    document.getElementById('btnCrmScreenshot').hidden = true;
    document.getElementById('crmScreenshot').hidden = true;
  }
}

// After a code is sent the CRM keeps its code page up for a moment while it
// checks the code; the poll must not read that as "code needed again".
const CODE_GRACE_MS = 20000;
let codeSubmittedAt = 0;

async function submitCrmCode() {
  const code = crmCodeInput.value.trim();
  if (!code) return;
  const btn = document.getElementById('btnCrmCode');
  btn.disabled = true;
  try {
    await api('/api/crm/login/code', {code});
    codeSubmittedAt = Date.now();
    crmCodeInput.value = '';
    crmCodeBox.hidden = true;
    setLoginState('waiting');
    loginMsgEl.className = 'msg info';
    loginMsgEl.textContent = 'Код отправлен, проверяю...';
    pollLoginStatus();
  } catch (e) {
    loginMsgEl.className = 'msg error';
    loginMsgEl.textContent = 'Ошибка: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('btnCrmCode').addEventListener('click', submitCrmCode);
crmCodeInput.addEventListener('keydown', e => { if (e.key === 'Enter') submitCrmCode(); });
// The code is always 6 digits: keep only digits (so a pasted "123 456" works)
// and confirm by itself once all six are in.
crmCodeInput.addEventListener('input', () => {
  const digits = crmCodeInput.value.replace(/[^0-9]/g, '').slice(0, 6);
  if (crmCodeInput.value !== digits) crmCodeInput.value = digits;
  if (digits.length === 6) submitCrmCode();
});

document.getElementById('btnCrmScreenshot').addEventListener('click', async () => {
  const img = document.getElementById('crmScreenshot');
  if (!img.hidden) { img.hidden = true; return; }
  try {
    const data = await api('/api/crm/login/screenshot', {});
    img.src = 'data:image/png;base64,' + data.png;
    img.hidden = false;
  } catch (e) {
    loginMsgEl.className = 'msg error';
    loginMsgEl.textContent = 'Ошибка: ' + e.message;
  }
});

document.getElementById('btnGoToCode').addEventListener('click', () => {
  document.querySelector('.nav-account-btn').click();
  crmCodeInput.focus();
});

// While the console shows "вход выполнен", check once a minute that the CRM
// still agrees, so an expired session isn't discovered only on the next click.
setInterval(async () => {
  if (crmLoginState !== 'ready') return;
  try {
    const data = await api('/api/crm/login/status', {});
    if (!data.ready) crmSessionExpired({data: {crm_session_expired: true}});
  } catch (e) { /* next tick */ }
}, 60000);

async function pollLoginStatus() {
  if (polling) return;
  polling = true;
  try {
    for (let i = 0; i < 300; i++) {
      await new Promise(r => setTimeout(r, 2000));
      try {
        const data = await api('/api/crm/login/status', {});
        if (data.ready) {
          codeSubmittedAt = 0;
          setLoginState('ready');
          loginMsgEl.className = 'msg info';
          loginMsgEl.textContent = '';
          showCrmCodeBox(false);
          onLoggedIn();
          return;
        }
        if (!data.browser_open) {
          setLoginState('idle');
          showCrmCodeBox(false);
          loginMsgEl.className = 'msg info';
          loginMsgEl.textContent = 'Вход прервался — нажмите «Войти в CRM».';
          return;
        }
        const checkingCode = Date.now() - codeSubmittedAt < CODE_GRACE_MS;
        if (checkingCode && !data.error_text) {
          continue;  // still verifying the code just sent
        }
        if (data.logged_out) {
          showCrmCodeBox(false);
          setLoginState('idle');
          loginMsgEl.className = 'msg error';
          loginMsgEl.textContent = autoRelogin() ? 'Сессия CRM истекла — вхожу заново...' : 'Нужно войти в CRM — нажмите «Войти в CRM».';
          return;
        }
        document.getElementById('btnCrmScreenshot').hidden = false;
        showCrmCodeBox(data.code_required);
        if (data.code_required) {
          setLoginState('code');
          if (codeSubmittedAt && !data.error_text) {
            loginMsgEl.className = 'msg error';
            loginMsgEl.textContent = 'Код не подошёл или устарел — введите новый из приложения.';
          }
        }
        if (data.error_text) {
          loginMsgEl.className = 'msg error';
          loginMsgEl.textContent = data.error_text;
        }
      } catch (e) { /* keep polling */ }
    }
    setLoginState('unconfirmed');
  } finally {
    polling = false;
  }
}

// On open: show the remembered login and, if there is one, log in by
// itself — unless a CRM window is already open (logged in, or waiting on
// the MFA code), which a page reload must not replace.
(async () => {
  try {
    const creds = await api('/api/crm/credentials', {});
    showSavedLogin(creds.login && creds.has_password ? creds.login : '');
    visaOwnerInput.value = creds.visa_owner_id || '';
    const status = await api('/api/crm/login/status', {});
    if (status.ready) {
      setLoginState('ready');
      loginMsgEl.textContent = '';
      onLoggedIn();
    } else if (status.browser_open) {
      setLoginState('waiting');
      pollLoginStatus();
    } else if (savedCrmLogin) {
      startCrmLogin({});
    } else {
      setLoginState('idle');
      loginMsgEl.textContent = 'Введите логин и пароль CRM. С галочкой «Запомнить» консоль будет входить сама.';
    }
  } catch (e) {
    setLoginState('idle');
  }
})();

document.getElementById('btnPendingTickets').addEventListener('click', async () => {
  const msgEl = document.getElementById('pendingMsg');
  const listEl = document.getElementById('pendingList');
  listEl.innerHTML = '';
  msgEl.className = 'msg info';
  msgEl.textContent = 'Загружаю список (только чтение)...';
  try {
    const data = await api('/api/crm/pending_tickets', {});
    if (data.tickets.length === 0) {
      msgEl.textContent = 'Ожидающих заявок "Доступ к ИС" нет.';
      return;
    }
    msgEl.textContent = 'Найдено: ' + data.tickets.length + ' — нажмите, чтобы открыть:';
    listEl.innerHTML = data.tickets.map(t =>
      `<div class="field"><span class="k">${esc(t.name)}</span>` +
      `<button type="button" class="open-pending ghost" data-url="${esc(t.url)}">Открыть</button></div>`
    ).join('');
    listEl.querySelectorAll('.open-pending').forEach(btn => {
      btn.addEventListener('click', () => {
        document.getElementById('ticketUrl').value = btn.dataset.url;
        document.getElementById('btnReadTicket').click();
      });
    });
  } catch (e) {
    msgEl.className = 'msg error';
    msgEl.textContent = crmSessionExpired(e) ? 'Сессия CRM истекла — вхожу заново. Если CRM спросит код, введите его в «Вход и настройки», затем повторите.' : 'Ошибка: ' + e.message;
  }
});

// A Webitel grant string ("r", "rw", "xrwdxrwd") as the Permissions tab names it.
function grantsLabel(granted) {
  if (!granted) return 'нет';
  const parts = [['r', 'Read'], ['w', 'Edit'], ['d', 'Delete']].filter(([letter]) => granted.includes(letter)).map(([, name]) => name);
  return (parts.join('+') || granted) + (granted.includes('x') ? ' с делегированием' : '');
}

// A plan's "permissions" (role grants to copy from the template) for display.
function permissionsText(perms, emptyText) {
  if (!perms || perms.length === 0) return emptyText;
  return perms.map(p => `${esc(p.name)} — ${grantsLabel(p.granted)}${p.current ? ' (было ' + grantsLabel(p.current) + ')' : ''}`).join(', ');
}

// What became of copying permissions, for a result line ('' if not asked).
function permissionsResultNote(result) {
  if (result.permissions_error) return ` · права НЕ скопированы: ${esc(result.permissions_error)}`;
  return result.permissions_copied ? ` · права: скопировано ${result.permissions_copied}` : '';
}

// A reset password as a result line: «общий» unless the device didn't get it.
function newPasswordNote(result) {
  if (result.devices_error) {
    return `пароль пользователя: <b class="secret" title="Нажмите, чтобы скопировать">${esc(result.new_password)}</b> · устройство НЕ обновлено: ${esc(result.devices_error)}`;
  }
  if (result.devices_updated && result.devices_updated.length === 0) {
    return `пароль: <b class="secret" title="Нажмите, чтобы скопировать">${esc(result.new_password)}</b> · устройств нет`;
  }
  return `общий пароль: <b class="secret" title="Нажмите, чтобы скопировать">${esc(result.new_password)}</b>`;
}

// The same for a user just created: «общий» unless the device wasn't made.
function createdPasswordNote(result) {
  const secret = `<b class="secret" title="Нажмите, чтобы скопировать">${esc(result.temporary_password)}</b>`;
  return result.devices_error
    ? `Пароль пользователя: ${secret} · устройство НЕ создано: ${esc(result.devices_error)}`
    : `Общий пароль: ${secret}`;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    // The older way, where the browser won't let the page near the clipboard.
    const area = document.createElement('textarea');
    area.value = text;
    document.body.append(area);
    area.select();
    const copied = document.execCommand('copy');
    area.remove();
    return copied;
  }
}

// A click on a new password copies it.
document.addEventListener('click', async event => {
  const secret = event.target.closest('.secret');
  if (!secret || !await copyText(secret.textContent)) return;
  secret.classList.add('copied');
  setTimeout(() => secret.classList.remove('copied'), 1500);
});

// Result rows, with «Скопировать список» when there are new passwords: a
// number and a password per line, two columns when pasted into Excel.
function showResults(msgEl, rows, passwords) {
  msgEl.className = 'msg info';
  msgEl.innerHTML = '<div class="results-head">Готово.</div><div class="list">' + rows.join('') + '</div>';
  if (passwords.length === 0) return;
  const btn = document.createElement('button');
  const label = `Скопировать список (${passwords.length})`;
  btn.type = 'button';
  btn.className = 'ghost';
  btn.textContent = label;
  btn.addEventListener('click', async () => {
    const copied = await copyText(passwords.map(p => `${p.number}\\t${p.password}`).join('\\n'));
    btn.textContent = copied ? '✓ Скопировано' : 'Не удалось скопировать';
    setTimeout(() => { btn.textContent = label; }, 2000);
  });
  msgEl.querySelector('.results-head').append(btn);
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
}

// The ticket's "Согласование" tab: steps that carry a comment or are the
// current one are always shown; the rest only after "Показать все шаги".
function renderApprovals(data) {
  if (data.approvals_error) {
    return `<div class="approvals"><div class="approvals-title">Согласование</div><div class="approvals-note">Не удалось загрузить: ${esc(data.approvals_error)}</div></div>`;
  }
  const steps = data.approvals || [];
  if (steps.length === 0) return '';
  const hidden = steps.filter(s => !s.comment && !s.is_current).length;
  const items = steps.map(s => `
    <div class="approval${s.is_current ? ' current' : ''}${!s.comment && !s.is_current ? ' quiet' : ''}">
      <div class="approval-head">
        <span class="approval-pos">${esc(s.position)}</span>
        <span class="approval-owner">${esc(s.owner) || '—'}</span>
        <span class="approval-status">${esc(s.status)}</span>
      </div>
      ${s.visa_type || s.set_by ? `<div class="approval-meta">${esc(s.visa_type)}${s.visa_type && s.set_by ? ' · ' : ''}${s.set_by ? 'установил: ' + esc(s.set_by) : ''}</div>` : ''}
      ${s.comment ? `<div class="approval-comment">${esc(s.comment)}</div>` : ''}
    </div>`).join('');
  const label = `Показать все шаги (${steps.length})`;
  return `<div class="approvals"><div class="approvals-title">Согласование</div>${items}` +
    (hidden ? `<button type="button" class="ghost approvals-toggle" data-label="${label}">${label}</button>` : '') + '</div>';
}

document.getElementById('btnReadTicket').addEventListener('click', async () => {
  const url = document.getElementById('ticketUrl').value.trim();
  const msgEl = document.getElementById('ticketMsg');
  const cardEl = document.getElementById('ticketCard');
  cardEl.hidden = true;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Читаю заявку...';
  try {
    const data = await api('/api/crm/ticket', {url});
    msgEl.textContent = '';
    cardEl.innerHTML = `
      <div class="field"><span class="k">Статус</span><span class="v">${esc(data.status)}</span></div>
      <div class="field"><span class="k">ФИО сотрудника</span><span class="v">${esc(data.employee_full_name)}</span></div>
      <div class="field"><span class="k">Учётная запись</span><span class="v">${esc(data.employee_account)}</span></div>
      <div class="field"><span class="k">Информационная система</span><span class="v">${esc(data.information_system)}</span></div>
      <div class="field"><span class="k">Комментарии</span><span class="v">${esc(data.comment || '—')}</span></div>
      <div class="field"><span class="k">Логин(ы) Webitel из комментариев</span><span class="v">${esc(data.webitel_logins.join(', ') || 'не найдены — укажите вручную')}</span></div>
      ${renderApprovals(data)}
    `;
    cardEl.hidden = false;
    const toggle = cardEl.querySelector('.approvals-toggle');
    if (toggle) {
      toggle.addEventListener('click', () => {
        const box = cardEl.querySelector('.approvals');
        box.classList.toggle('show-all');
        toggle.textContent = box.classList.contains('show-all') ? 'Скрыть шаги без комментариев' : toggle.dataset.label;
      });
    }
    // Only the comment's numeric Webitel logins — never "Учётная запись",
    // which is the employee's AD login and not a Webitel user.
    const logins = data.webitel_logins.join(', ');
    document.getElementById('targetExt').value = logins;
    document.getElementById('singleNewLogin').value = data.webitel_logins.length === 1 ? logins : '';
    document.getElementById('singleNewName').value = data.webitel_logins.length === 1 ? (data.employee_full_name || '') : '';
  } catch (e) {
    msgEl.textContent = crmSessionExpired(e) ? 'Сессия CRM истекла — вхожу заново. Если CRM спросит код, введите его в «Вход и настройки», затем повторите.' : 'Ошибка: ' + e.message;
    msgEl.className = 'msg error';
  }
});

let currentPlans = [];  // [{ok: true, plan} | {ok: false, target, error}]

// Numbers typed into a field: separated by commas, spaces or new lines,
// with ranges like 15210-15220 (both ends included). A range of more than
// RANGE_LIMIT numbers is taken for a typo.
const RANGE_LIMIT = 100;

function expandNumbers(text) {
  const numbers = [];
  for (const token of text.replace(/\\s*-\\s*/g, '-').split(/[\\s,;]+/).filter(Boolean)) {
    const range = token.match(/^(\\d+)-(\\d+)$/);
    if (!range) {
      numbers.push(token);
      continue;
    }
    const [from, to] = [Number(range[1]), Number(range[2])].sort((a, b) => a - b);
    if (to - from + 1 > RANGE_LIMIT) return {numbers: [], error: `Диапазон ${token} больше ${RANGE_LIMIT} номеров`};
    for (let n = from; n <= to; n++) numbers.push(String(n));
  }
  return {numbers: [...new Set(numbers)]};
}

document.getElementById('btnPlan').addEventListener('click', async () => {
  const template_extension = document.getElementById('templateExt').value.trim();
  const typed = expandNumbers(document.getElementById('targetExt').value);
  // The template among the targets (say, inside a range) is left out:
  // copying it onto itself does nothing, and «Сбросить пароль» would reset it.
  const targets = typed.numbers.filter(t => t !== template_extension);
  const templateSkipped = targets.length < typed.numbers.length;
  const msgEl = document.getElementById('planMsg');
  const listEl = document.getElementById('planList');
  const confirmRow = document.getElementById('confirmRow');
  listEl.innerHTML = '';
  confirmRow.hidden = true;
  currentPlans = [];

  if (!template_extension) {
    msgEl.className = 'msg error';
    msgEl.textContent = 'Укажите логин пользователя-образца';
    return;
  }
  if (typed.error || targets.length === 0) {
    msgEl.className = 'msg error';
    msgEl.textContent = typed.error || 'Укажите, кому скопировать';
    return;
  }

  msgEl.className = 'msg info';
  msgEl.textContent = `Строю план для ${targets.length} — только чтение...`;

  const missing = [];
  const city = selectedCity;  // for the whole plan, even if it is switched meanwhile
  for (const target_extension of targets) {
    try {
      const plan = await api('/api/webitel/plan', {
        template_extension, target_extension, city,
        reset_password: document.getElementById('resetPasswordCheck').checked,
        copy_permissions: document.getElementById('copyPermissionsCheck').checked,
      });
      currentPlans.push({ok: true, plan});
    } catch (e) {
      const notFound = e.data && e.data.missing;
      if (notFound && notFound === template_extension) {
        currentPlans = [];
        msgEl.className = 'msg error';
        msgEl.textContent = `Образца ${template_extension} нет в Webitel`;
        return;
      }
      if (notFound && notFound === target_extension) {
        missing.push(target_extension);
        continue;
      }
      currentPlans.push({ok: false, target: target_extension, error: e.message});
    }
  }

  const okCount = currentPlans.filter(r => r.ok).length;
  msgEl.textContent = `Готово: ${okCount} из ${targets.length} — план построен.` +
    (templateSkipped ? ` Образец ${template_extension} пропущен.` : '') + ' Ошибки ниже, если есть.';
  const missingRow = missing.length
    ? `<div class="field list-error"><span class="k">нет в Webitel</span><span class="v">${missing.map(esc).join(', ')}</span></div>`
    : '';
  listEl.innerHTML = currentPlans.map(r => {
    if (r.ok) {
      const roleNames = r.plan.roles.map(x => x.name).join(', ') || '—';
      const licenseNames = r.plan.license.map(x => x.prod || x.id).join(', ') || '—';
      const passwordNote = r.plan.reset_password ? ' · пароль: будет сброшен (общий)' : ' · пароль: не меняется';
      const permissionsNote = ' · права: ' + (r.plan.copy_permissions ? permissionsText(r.plan.permissions, 'уже как у образца') : 'не копируются');
      return `<div class="field list-ok"><span class="k">${esc(r.plan.target_name)} (${esc(r.plan.target_extension)})</span>` +
        `<span class="v status">✓ роли: ${esc(roleNames)} · лицензии: ${esc(licenseNames)} · группа: ${esc(r.plan.group || '—')}${passwordNote}${permissionsNote}</span></div>`;
    }
    return `<div class="field list-error"><span class="k">${esc(r.target)}</span><span class="v">${esc(r.error)}</span></div>`;
  }).join('') + missingRow;

  document.getElementById('btnExecute').textContent = 'Подтвердить и применить все';
  if (okCount > 0) confirmRow.hidden = false;
});

document.getElementById('btnExecute').addEventListener('click', async () => {
  const toApply = currentPlans.filter(r => r.ok);
  if (toApply.length === 0) return;
  const msgEl = document.getElementById('executeMsg');
  const btn = document.getElementById('btnExecute');
  const names = toApply.map(r => r.plan.target_name + ' (' + r.plan.target_extension + ')').join(', ');
  const resetNote = toApply.some(r => r.plan.reset_password) ? '\\n\\nПароли будут СБРОШЕНЫ на новые.' : '';
  const permissionsNote = toApply.some(r => (r.plan.permissions || []).length) ? '\\n\\nПрава (Permissions) образца будут добавлены.' : '';
  if (!confirm(`Точно применить план к ${toApply.length} пользователям? ${names}${resetNote}${permissionsNote}`)) return;
  btn.disabled = true;
  msgEl.className = 'msg info';
  const results = [];
  const passwords = [];
  const failed = [];
  for (const [done, r] of toApply.entries()) {
    msgEl.textContent = `Применяю ${done + 1} из ${toApply.length}...`;
    const who = `<span class="k">${esc(r.plan.target_name)} (${esc(r.plan.target_extension)})</span>`;
    try {
      const result = await api('/api/webitel/execute', {plan: r.plan});
      const passwordNote = result.new_password ? ' · ' + newPasswordNote(result) : '';
      if (result.new_password) passwords.push({number: r.plan.target_extension, password: result.new_password});
      results.push(`<div class="field ${result.permissions_error || result.devices_error ? 'list-warn' : 'list-ok'}">${who}` +
        `<span class="v status">✓ обновлён${passwordNote}${permissionsResultNote(result)}</span></div>`);
    } catch (e) {
      failed.push(r);
      results.push(`<div class="field list-error">${who}<span class="v">${esc(e.message)}</span></div>`);
    }
  }
  // Only what didn't work out stays for another try: pressing the button
  // again used to reset everyone's password once more.
  currentPlans = failed;
  btn.textContent = `Повторить (${failed.length})`;
  document.getElementById('confirmRow').hidden = failed.length === 0;
  showResults(msgEl, results, passwords);
  btn.disabled = false;
});

let currentDeletePlans = [];  // [{ok: true, plan} | {ok: false, extension, error}]
let deletePlanRequest = 0;
const DELETE_WITHOUT_COUNT = 5;

document.getElementById('btnDeletePlan').addEventListener('click', async () => {
  const {numbers: extensions, error} = expandNumbers(document.getElementById('deleteExt').value);
  const msgEl = document.getElementById('deletePlanMsg');
  const listEl = document.getElementById('deletePlanList');
  const request = ++deletePlanRequest;
  const city = selectedCity;
  listEl.innerHTML = '';
  currentDeletePlans = [];
  askToDelete('', 0);

  if (error || extensions.length === 0) {
    msgEl.className = 'msg error';
    msgEl.textContent = error || 'Укажите хотя бы один логин/добавочный';
    return;
  }

  const plans = [];
  const missing = [];
  msgEl.className = 'msg info';
  for (const [done, extension] of extensions.entries()) {
    msgEl.textContent = `Ищу ${done + 1} из ${extensions.length} — только чтение...`;
    try {
      const plan = await api('/api/webitel/delete_plan', {extension, city});
      plans.push({ok: true, plan});
    } catch (e) {
      if (e.data && e.data.missing === extension) missing.push(extension);
      else plans.push({ok: false, extension, error: e.message});
    }
    if (request !== deletePlanRequest) return;  // the numbers were changed meanwhile
  }
  currentDeletePlans = plans;

  const deletable = currentDeletePlans.filter(r => r.ok && !r.plan.in_call);
  const inCall = currentDeletePlans.filter(r => r.ok && r.plan.in_call);
  const notFound = currentDeletePlans.filter(r => !r.ok);
  msgEl.textContent = `Будет удалено: ${deletable.length}` +
    (inCall.length ? ` · пропущено (идёт разговор): ${inCall.length}` : '') +
    (missing.length ? ` · нет в Webitel: ${missing.length}` : '') +
    (notFound.length ? ` · ошибка: ${notFound.length}` : '');
  const missingRow = missing.length
    ? `<div class="field list-error"><span class="k">нет в Webitel</span><span class="v">${missing.map(esc).join(', ')}</span></div>`
    : '';
  listEl.innerHTML = currentDeletePlans.map(r => {
    if (!r.ok) {
      return `<div class="field list-error"><span class="k">${esc(r.extension)}</span><span class="v">${esc(r.error)}</span></div>`;
    }
    const deviceIds = r.plan.device_ids || [];
    const deviceNote = deviceIds.length > 0 ? `, устройств: ${deviceIds.length}` : '';
    const presence = r.plan.presence || [];
    const who = `<span class="k">${esc(r.plan.name)} (${esc(r.plan.username || r.plan.extension)})</span>`;
    // The status is always stated, like Webitel's own Web/SIP/Dlg/DnD chips.
    if (r.plan.in_call) {
      return `<div class="field list-error">${who}<span class="v">✕ не будет удалён` +
        `<span class="presence">Идёт разговор (${presence.map(esc).join(', ')}) — удалять нельзя. Нажмите «Показать план» ещё раз после звонка.</span></span></div>`;
    }
    const statusLine = presence.length
      ? `<span class="presence active">Статус: в сети — ${presence.map(esc).join(', ')}. Сессии будут завершены перед удалением.</span>`
      : `<span class="presence idle">Статус: не в сети (Web, SIP, Dlg, DnD неактивны)</span>`;
    return `<div class="field ${presence.length ? 'list-warn' : 'list-ok'}">${who}` +
      `<span class="v status">✓ группа: ${esc(r.plan.group || '—')}${deviceNote}${statusLine}</span></div>`;
  }).join('') + missingRow;

  askToDelete('Подтвердить и удалить', deletable.length);
});

// The red button for ``count`` people. For more than a few of them their
// number has to be typed first: a slip in a range can't delete dozens.
function askToDelete(text, count) {
  const typed = document.getElementById('deleteCount');
  const btn = document.getElementById('btnDeleteExecute');
  typed.value = '';
  typed.dataset.expected = count;
  document.getElementById('deleteCountLabel').textContent = `Будет удалено ${count}. Введите ${count}, чтобы подтвердить`;
  document.getElementById('deleteCountRow').hidden = count <= DELETE_WITHOUT_COUNT;
  btn.textContent = `${text} (${count})`;
  btn.disabled = count > DELETE_WITHOUT_COUNT;
  document.getElementById('deleteConfirmRow').hidden = count === 0;
}

document.getElementById('deleteCount').addEventListener('input', event => {
  document.getElementById('btnDeleteExecute').disabled = event.target.value.trim() !== event.target.dataset.expected;
});

document.getElementById('btnDeleteExecute').addEventListener('click', async () => {
  const toDelete = currentDeletePlans.filter(r => r.ok && !r.plan.in_call);
  if (toDelete.length === 0) return;
  const msgEl = document.getElementById('deleteExecuteMsg');
  const btn = document.getElementById('btnDeleteExecute');
  const label = r => `${r.plan.name} (${r.plan.extension})`;
  const online = toDelete.filter(r => (r.plan.presence || []).length > 0);
  const skipped = currentDeletePlans.filter(r => r.ok && r.plan.in_call);
  const city = WEBITEL_CITY_NAMES[toDelete[0].plan.city] || toDelete[0].plan.city;
  const text = `УДАЛИТЬ НАВСЕГДА ${toDelete.length} (${city}):\\n${toDelete.map(label).join('\\n')}` +
    (online.length ? `\\n\\nСейчас в сети — их сессии будут завершены:\\n${online.map(r => label(r) + ' — ' + r.plan.presence.join(', ')).join('\\n')}` : '') +
    (skipped.length ? `\\n\\nНЕ будут удалены (идёт разговор):\\n${skipped.map(label).join('\\n')}` : '');
  if (!confirm(text)) return;
  btn.disabled = true;
  msgEl.className = 'msg info';
  const results = [];
  const failed = [];
  for (const [done, r] of toDelete.entries()) {
    msgEl.textContent = `Удаляю ${done + 1} из ${toDelete.length}...`;
    const who = `<span class="k">${esc(label(r))}</span>`;
    try {
      const result = await api('/api/webitel/delete_execute', {plan: r.plan});
      const deviceNote = result.deleted_device_ids.length > 0 ? ` (устройств удалено: ${result.deleted_device_ids.length})` : '';
      const text = `✓ ${result.already_deleted ? 'уже был удалён' : 'удалён'}${deviceNote}`;
      if (result.devices_error) failed.push(r);
      results.push(result.devices_error
        ? `<div class="field list-warn">${who}<span class="v status">${text} · устройство НЕ удалено: ${esc(result.devices_error)}</span></div>`
        : `<div class="field list-ok">${who}<span class="v status">${text}</span></div>`);
    } catch (e) {
      failed.push(r);
      results.push(`<div class="field list-error">${who}<span class="v">${esc(e.message)}</span></div>`);
    }
  }
  // Only what didn't work out stays for another try: pressing the button
  // again used to ask Webitel to delete everyone a second time.
  currentDeletePlans = failed;
  msgEl.className = 'msg info';
  msgEl.innerHTML = 'Готово.<div class="list">' + results.join('') + '</div>';
  btn.disabled = false;
  askToDelete('Повторить', failed.length);
});

let currentPasswordPlans = [];  // [{ok: true, plan} | {ok: false, extension, error}]

document.getElementById('btnPasswordPlan').addEventListener('click', async () => {
  const extensions = splitNumbers(document.getElementById('passwordExt').value);
  const msgEl = document.getElementById('passwordPlanMsg');
  const listEl = document.getElementById('passwordPlanList');
  const confirmRow = document.getElementById('passwordConfirmRow');
  listEl.innerHTML = '';
  confirmRow.hidden = true;
  document.getElementById('passwordExecuteMsg').textContent = '';
  currentPasswordPlans = [];

  if (extensions.length === 0) {
    msgEl.className = 'msg error';
    msgEl.textContent = 'Укажите хотя бы один логин/добавочный';
    return;
  }

  msgEl.className = 'msg info';
  msgEl.textContent = `Ищу ${extensions.length} — только чтение...`;

  const city = selectedCity;
  for (const extension of extensions) {
    try {
      const plan = await api('/api/webitel/password_plan', {extension, city});
      currentPasswordPlans.push({ok: true, plan});
    } catch (e) {
      currentPasswordPlans.push({ok: false, extension, error: e.message});
    }
  }

  const okCount = currentPasswordPlans.filter(r => r.ok).length;
  msgEl.textContent = `Найдено: ${okCount} из ${extensions.length}. Ошибки ниже, если есть.`;
  listEl.innerHTML = currentPasswordPlans.map(r => {
    if (!r.ok) {
      return `<div class="field list-error"><span class="k">${esc(r.extension)}</span><span class="v">${esc(r.error)}</span></div>`;
    }
    const devices = r.plan.devices.map(d => esc(d.name || d.id)).join(', ');
    const presence = r.plan.presence || [];
    const statusLine = presence.length
      ? `<span class="presence active">Сейчас в сети — ${presence.map(esc).join(', ')}. Телефон отключится, пока на нём не введут новый пароль.</span>`
      : `<span class="presence idle">Статус: не в сети</span>`;
    return `<div class="field ${presence.length ? 'list-warn' : 'list-ok'}"><span class="k">${esc(r.plan.name)} (${esc(r.plan.username || r.plan.extension)})</span>` +
      `<span class="v status">✓ ${devices ? 'устройство: ' + devices : 'устройств нет — сменится только пароль пользователя'}${statusLine}</span></div>`;
  }).join('');

  document.getElementById('btnPasswordExecute').textContent = `Подтвердить и сбросить (${okCount})`;
  confirmRow.hidden = okCount === 0;
});

document.getElementById('btnPasswordExecute').addEventListener('click', async () => {
  const toReset = currentPasswordPlans.filter(r => r.ok);
  if (toReset.length === 0) return;
  const msgEl = document.getElementById('passwordExecuteMsg');
  const btn = document.getElementById('btnPasswordExecute');
  const label = r => `${r.plan.name} (${r.plan.username || r.plan.extension})`;
  if (!confirm(`Сбросить пароль ${toReset.length}:\\n${toReset.map(label).join('\\n')}\\n\\nНовый пароль будет у пользователя и его устройства.`)) return;
  btn.disabled = true;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Сбрасываю...';
  const results = [];
  const passwords = [];
  for (const r of toReset) {
    try {
      const result = await api('/api/webitel/password_execute', {plan: r.plan});
      passwords.push({number: r.plan.extension, password: result.new_password});
      results.push(`<div class="field ${result.devices_error ? 'list-warn' : 'list-ok'}"><span class="k">${esc(label(r))}</span>` +
        `<span class="v status">✓ ${newPasswordNote(result)}</span></div>`);
    } catch (e) {
      results.push(`<div class="field list-error"><span class="k">${esc(label(r))}</span><span class="v">${esc(e.message)}</span></div>`);
    }
  }
  // Done with this plan: confirming it again would reset everyone once more.
  currentPasswordPlans = [];
  document.getElementById('passwordConfirmRow').hidden = true;
  showResults(msgEl, results, passwords);
  btn.disabled = false;
});

// «Журнал»: the console's own log of what was changed, newest first.
let journalShown = 0;
let journalLimit = 25;
let journalRequest = 0;

function journalRow(e) {
  const when = new Date(e.time).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit'});
  const tone = e.ok ? (e.problem ? ' list-warn' : '') : ' list-error';
  const what = [e.number, e.name].filter(Boolean).join(' — ');
  const note = [e.city, e.details, e.problem].filter(Boolean).join(' · ');
  return `<div class="field journal${tone}"><span class="journal-when">${esc(when)}</span>` +
    `<span class="journal-what"><b>${esc(e.kind)}</b> ${esc(what)}<span class="journal-note">${esc(note)}</span></span>` +
    `<span class="journal-who">${esc(e.who)}</span></div>`;
}

async function loadJournal(offset) {
  const msgEl = document.getElementById('journalMsg');
  const listEl = document.getElementById('journalList');
  const moreRow = document.getElementById('journalMoreRow');
  const request = ++journalRequest;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Читаю журнал...';
  try {
    const data = await api('/api/journal', {query: document.getElementById('journalQuery').value, offset, limit: journalLimit});
    if (request !== journalRequest) return;  // a newer search is already on its way
    if (offset === 0) listEl.innerHTML = '';
    listEl.insertAdjacentHTML('beforeend', data.entries.map(journalRow).join(''));
    journalShown = offset + data.entries.length;
    msgEl.textContent = data.total ? `Показано ${journalShown} из ${data.total}` : 'Ничего не найдено';
    moreRow.hidden = journalShown >= data.total;
  } catch (e) {
    if (request !== journalRequest) return;
    if (offset === 0) listEl.innerHTML = '';
    moreRow.hidden = true;
    msgEl.className = 'msg error';
    msgEl.textContent = e.message;
  }
}

document.getElementById('btnJournal').addEventListener('click', () => loadJournal(0));
document.querySelectorAll('.limit-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.limit-btn').forEach(b => b.classList.toggle('active', b === btn));
    journalLimit = Number(btn.dataset.limit);
    loadJournal(0);
  });
});
document.getElementById('btnJournalMore').addEventListener('click', () => loadJournal(journalShown));
document.getElementById('journalQuery').addEventListener('keydown', event => {
  if (event.key === 'Enter') loadJournal(0);
});

// «Доступ в консоль»: who is signed in and, for an admin, who may sign in.
function accessRow(user, me) {
  const role = user.admin
    ? '<span class="pill neutral">админ</span>'
    : `<button type="button" class="ghost" data-access="promote" data-login="${esc(user.login)}">Сделать админом</button>`;
  const remove = user.login === me
    ? ''
    : `<button type="button" class="ghost" data-access="revoke" data-login="${esc(user.login)}">Убрать</button>`;
  const label = [user.login, user.name].filter(Boolean).join(' — ');
  return `<div class="field"><span class="k">${esc(label)}</span><span class="saved-login-actions">${role}${remove}</span></div>`;
}

function showAccess(data) {
  document.getElementById('accessCard').hidden = !data.me;
  document.getElementById('accessMe').textContent = data.me;
  document.getElementById('accessAdmin').hidden = !data.users;
  document.getElementById('accessList').innerHTML = (data.users || []).map(user => accessRow(user, data.me)).join('');
}

async function changeAccess(action, login, btn) {
  const msgEl = document.getElementById('accessMsg');
  btn.disabled = true;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Проверяю...';
  try {
    const data = await api('/api/access/' + action, {login});
    showAccess(data);
    msgEl.textContent = {grant: 'Доступ открыт: ', revoke: 'Доступ закрыт: ', promote: 'Теперь админ: '}[action] + data.changed;
    if (action === 'grant') document.getElementById('accessLogin').value = '';
  } catch (e) {
    msgEl.className = 'msg error';
    msgEl.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

const btnAccessGrant = document.getElementById('btnAccessGrant');
btnAccessGrant.addEventListener('click', () => changeAccess('grant', document.getElementById('accessLogin').value, btnAccessGrant));
document.getElementById('accessLogin').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !btnAccessGrant.disabled) btnAccessGrant.click();
});
document.getElementById('accessList').addEventListener('click', event => {
  const btn = event.target.closest('[data-access]');
  if (!btn) return;
  const {access, login} = btn.dataset;
  const question = access === 'promote' ? `Сделать админом: ${login}?` : `Закрыть доступ в консоль: ${login}?`;
  if (confirm(question)) changeAccess(access, login, btn);
});
document.getElementById('btnAdLogout').addEventListener('click', async () => {
  await api('/api/ad/logout').catch(() => {});
  location.reload();
});
api('/api/access').then(showAccess).catch(() => {});

let currentRolePlan = null;

// Letters on Permissions → Objects, as its columns name them.
function objectGrantsLabel(grants) {
  return [['x', 'Create'], ['r', 'Read'], ['w', 'Edit'], ['d', 'Delete']]
    .filter(([letter]) => grants.includes(letter)).map(([, name]) => name).join(', ') || '—';
}

document.getElementById('btnRolePlan').addEventListener('click', async () => {
  const name = document.getElementById('roleName').value.trim();
  const msgEl = document.getElementById('rolePlanMsg');
  const cardEl = document.getElementById('rolePlanCard');
  const confirmRow = document.getElementById('roleConfirmRow');
  cardEl.hidden = true;
  confirmRow.hidden = true;
  document.getElementById('roleExecuteMsg').textContent = '';
  currentRolePlan = null;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Строю план (только чтение)...';
  try {
    const plan = await api('/api/webitel/role_plan', {name, city: selectedCity});
    currentRolePlan = plan;
    msgEl.textContent = '';
    cardEl.innerHTML = `
      <div class="field"><span class="k">Роль</span><span class="v">${esc(plan.name)}</span></div>
      <div class="field"><span class="k">Role permissions</span><span class="v">${plan.permissions.map(esc).join(', ')}</span></div>
      <div class="field"><span class="k">Applications</span><span class="v">${plan.applications.map(esc).join(', ')}</span></div>
      ${plan.objects.map(o => `<div class="field"><span class="k">Objects: ${esc(o.object)}</span><span class="v">${objectGrantsLabel(o.grants)}</span></div>`).join('')}
    `;
    cardEl.hidden = false;
    confirmRow.hidden = false;
  } catch (e) {
    msgEl.textContent = 'Ошибка: ' + e.message;
    msgEl.className = 'msg error';
  }
});

document.getElementById('btnRoleExecute').addEventListener('click', async () => {
  if (!currentRolePlan) return;
  const msgEl = document.getElementById('roleExecuteMsg');
  const btn = document.getElementById('btnRoleExecute');
  const city = WEBITEL_CITY_NAMES[currentRolePlan.city] || currentRolePlan.city;
  if (!confirm(`Создать роль ${currentRolePlan.name} в Webitel ${city}?`)) return;
  btn.disabled = true;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Создаю...';
  try {
    const result = await api('/api/webitel/role_execute', {plan: currentRolePlan});
    const created = `Роль ${esc(result.name)} создана${result.id ? ' (id ' + esc(result.id) + ')' : ''}`;
    msgEl.className = result.objects_error ? 'msg error' : 'msg info';
    msgEl.innerHTML = result.objects_error ? `${created}, но права в Objects НЕ выданы: ${esc(result.objects_error)}` : `Готово. ${created}.`;
    // Done with this plan: confirming it again would only meet «уже есть».
    currentRolePlan = null;
    document.getElementById('roleConfirmRow').hidden = true;
  } catch (e) {
    msgEl.textContent = 'Ошибка: ' + e.message;
    msgEl.className = 'msg error';
  } finally {
    btn.disabled = false;
  }
});

document.querySelectorAll('.tab-btn').forEach(tabBtn => {
  tabBtn.addEventListener('click', () => {
    // Only this card's tabs: «Создать» and «Группы» each have their own.
    const card = tabBtn.closest('.card-body');
    card.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    tabBtn.classList.add('active');
    const target = tabBtn.dataset.tab;
    card.querySelectorAll('.tab-panel').forEach(panel => {
      panel.hidden = panel.dataset.panel !== target;
    });
  });
});

let currentCreatePlanSingle = null;

document.getElementById('btnCreatePlanSingle').addEventListener('click', async () => {
  const template_extension = document.getElementById('singleTemplateExt').value.trim();
  const new_username = document.getElementById('singleNewLogin').value.trim();
  const new_name = document.getElementById('singleNewName').value.trim();
  const new_extension = new_username;
  const msgEl = document.getElementById('createPlanMsgSingle');
  const cardEl = document.getElementById('createPlanCardSingle');
  const confirmRow = document.getElementById('createConfirmRowSingle');
  cardEl.hidden = true;
  confirmRow.hidden = true;
  currentCreatePlanSingle = null;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Строю план (только чтение)...';
  try {
    const plan = await api('/api/webitel/create_plan', {
      template_extension, new_username, new_name, new_extension, city: selectedCity,
      copy_permissions: document.getElementById('singleCopyPermissionsCheck').checked,
    });
    currentCreatePlanSingle = plan;
    msgEl.textContent = '';
    const roleNames = plan.roles.map(r => r.name).join(', ') || '—';
    const licenseIds = plan.license.map(l => l.id).join(', ') || '—';
    cardEl.innerHTML = `
      <div class="field"><span class="k">Образец</span><span class="v">${esc(plan.template_name)} (${esc(plan.template_extension)})</span></div>
      <div class="field"><span class="k">Новый пользователь</span><span class="v">${esc(plan.new_name)} (${esc(plan.new_username)})</span></div>
      <div class="field"><span class="k">Roles → будет</span><span class="v">${esc(roleNames)}</span></div>
      <div class="field"><span class="k">License → будет</span><span class="v">${esc(licenseIds)}</span></div>
      <div class="field"><span class="k">Group → будет</span><span class="v">${esc(plan.group || '—')}</span></div>
      <div class="field"><span class="k">Permissions → будет</span><span class="v">${plan.copy_permissions ? permissionsText(plan.permissions, 'у образца нет прав для ролей') : 'не копируются (галочка не отмечена)'}</span></div>
      <div class="field"><span class="k">Устройство (SIP)</span><span class="v">${esc(plan.new_extension)} — создастся вместе с пользователем</span></div>
      <div class="field"><span class="k">Пароль</span><span class="v">общий, сгенерируется при создании, постоянный (без принудительной смены)</span></div>
    `;
    cardEl.hidden = false;
    confirmRow.hidden = false;
  } catch (e) {
    msgEl.textContent = 'Ошибка: ' + e.message;
    msgEl.className = 'msg error';
  }
});

document.getElementById('btnCreateExecuteSingle').addEventListener('click', async () => {
  if (!currentCreatePlanSingle) return;
  const msgEl = document.getElementById('createExecuteMsgSingle');
  const btn = document.getElementById('btnCreateExecuteSingle');
  if (!confirm('Точно создать нового пользователя ' + currentCreatePlanSingle.new_name + ' (' + currentCreatePlanSingle.new_username + ')?')) return;
  btn.disabled = true;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Создаю...';
  try {
    const result = await api('/api/webitel/create_execute', {plan: currentCreatePlanSingle});
    msgEl.className = result.devices_error ? 'msg error' : 'msg info';
    msgEl.innerHTML = `Готово. ${esc(result.name)} (${esc(result.extension)}) создан. ${createdPasswordNote(result)}${permissionsResultNote(result)}`;
    // Done with this plan: confirming it again would only meet «уже занят».
    currentCreatePlanSingle = null;
    document.getElementById('createConfirmRowSingle').hidden = true;
  } catch (e) {
    msgEl.textContent = 'Ошибка: ' + e.message;
    msgEl.className = 'msg error';
  } finally {
    btn.disabled = false;
  }
});

// «Подобрать по соседним номерам»: template suggestions among the numbers
// around the one being set up; «Взять» fills the template in. Only a help —
// the template can always be typed instead.
function setupNeighbors({button, radiusInput, msg, list, templateInput, center, skip, noCenterText}) {
  document.getElementById(button).addEventListener('click', async () => {
    const extension = center();
    const msgEl = document.getElementById(msg);
    const listEl = document.getElementById(list);
    const templateEl = document.getElementById(templateInput);
    listEl.innerHTML = '';
    if (!extension) {
      msgEl.className = 'msg error';
      msgEl.textContent = noCenterText;
      return;
    }
    const radius = parseInt(document.getElementById(radiusInput).value, 10) || 2;
    msgEl.className = 'msg info';
    msgEl.textContent = 'Ищу соседние номера (только чтение)...';
    try {
      const data = await api('/api/webitel/neighbors', {extension, radius, city: selectedCity});
      const skipped = skip();
      const neighbors = data.neighbors.filter(n => !skipped.includes(n.extension));
      if (neighbors.length === 0) {
        msgEl.textContent = 'Соседних номеров не найдено — укажите образец вручную.';
        return;
      }
      msgEl.textContent = `Соседи ${extension} — выберите образец (переговорки и «Vacant» лучше пропустить):`;
      listEl.innerHTML = neighbors.map(n =>
        `<div class="field neighbor" data-ext="${esc(n.extension)}"><span class="k">${esc(n.extension)} — ${esc(n.name)}` +
        `<span class="neighbor-meta">${esc(n.group || '')}${n.roles && n.roles.length ? ' · ' + esc(n.roles.join(', ')) : ''}</span></span>` +
        `<button type="button" class="pick-neighbor ghost" data-ext="${esc(n.extension)}">Взять</button></div>`
      ).join('');
      const markPicked = () => {
        const picked = templateEl.value.trim();
        listEl.querySelectorAll('.neighbor').forEach(row => {
          const isPicked = row.dataset.ext === picked;
          row.classList.toggle('picked', isPicked);
          row.querySelector('.pick-neighbor').textContent = isPicked ? '✓ Образец' : 'Взять';
        });
      };
      listEl.querySelectorAll('.pick-neighbor').forEach(btn => {
        btn.addEventListener('click', () => {
          templateEl.value = btn.dataset.ext;
          templateEl.dispatchEvent(new Event('input'));
          markPicked();
        });
      });
      markPicked();
    } catch (e) {
      msgEl.className = 'msg error';
      msgEl.textContent = 'Ошибка: ' + e.message;
    }
  });
}

setupNeighbors({
  button: 'btnNeighbors', radiusInput: 'neighborsRadius', msg: 'neighborsMsg', list: 'neighborsList',
  templateInput: 'singleTemplateExt',
  // Around the new user's own login — no second number to type.
  center: () => document.getElementById('singleNewLogin').value.trim(),
  skip: () => [],
  noCenterText: 'Сначала укажите логин нового пользователя (шаг 1) — соседей ищу вокруг него',
});
setupNeighbors({
  button: 'btnCopyNeighbors', radiusInput: 'copyNeighborsRadius', msg: 'copyNeighborsMsg', list: 'copyNeighborsList',
  templateInput: 'templateExt',
  // Around the first number to copy to; the others being set up aren't offered.
  center: () => expandNumbers(document.getElementById('targetExt').value).numbers[0] || '',
  skip: () => expandNumbers(document.getElementById('targetExt').value).numbers,
  noCenterText: 'Сначала укажите, кому скопировать — соседей ищу вокруг первого номера',
});

let currentCreatePlans = [];  // [{ok: true, plan} | {ok: false, login, error}]

function parseBulkList() {
  const raw = document.getElementById('createBulkList').value;
  return raw.split('\\n')
    .map(line => line.trim())
    .filter(line => line.length > 0)
    .map(line => {
      const [login, ...rest] = line.split(',');
      return {login: (login || '').trim(), name: rest.join(',').trim()};
    });
}

document.getElementById('btnCreatePlan').addEventListener('click', async () => {
  const template_extension = document.getElementById('createTemplateExt').value.trim();
  const entries = parseBulkList();
  const msgEl = document.getElementById('createPlanMsg');
  const listEl = document.getElementById('createPlanList');
  const confirmRow = document.getElementById('createConfirmRow');
  listEl.innerHTML = '';
  confirmRow.hidden = true;
  currentCreatePlans = [];

  if (!template_extension) {
    msgEl.className = 'msg error';
    msgEl.textContent = 'Укажите логин пользователя-образца';
    return;
  }
  if (entries.length === 0) {
    msgEl.className = 'msg error';
    msgEl.textContent = 'Добавьте хотя бы одного пользователя (логин,ФИО на строку)';
    return;
  }

  msgEl.className = 'msg info';
  msgEl.textContent = `Строю план для ${entries.length} — только чтение...`;

  const city = selectedCity;
  for (const entry of entries) {
    try {
      const plan = await api('/api/webitel/create_plan', {
        template_extension, new_username: entry.login, new_name: entry.name, new_extension: entry.login, city,
        copy_permissions: document.getElementById('bulkCopyPermissionsCheck').checked,
      });
      currentCreatePlans.push({ok: true, plan});
    } catch (e) {
      currentCreatePlans.push({ok: false, login: entry.login, error: e.message});
    }
  }

  const okCount = currentCreatePlans.filter(r => r.ok).length;
  msgEl.textContent = `Готово: ${okCount} из ${entries.length} — план построен. Ошибки ниже, если есть.`;
  listEl.innerHTML = currentCreatePlans.map(r => {
    if (r.ok) {
      const roleNames = r.plan.roles.map(x => x.name).join(', ') || '—';
      const licenseNames = r.plan.license.map(x => x.prod || x.id).join(', ') || '—';
      return `<div class="field list-ok">` +
        `<span class="k">${esc(r.plan.new_name)} (${esc(r.plan.new_username)})</span>` +
        `<span class="v status">✓ роли: ${esc(roleNames)} · лицензии: ${esc(licenseNames)} · группа: ${esc(r.plan.group || '—')}` +
        ` · права: ${r.plan.copy_permissions ? permissionsText(r.plan.permissions, 'у образца нет прав для ролей') : 'не копируются'}</span></div>`;
    }
    return `<div class="field list-error"><span class="k">${esc(r.login)}</span><span class="v">${esc(r.error)}</span></div>`;
  }).join('');

  document.getElementById('btnCreateExecute').textContent = 'Подтвердить и создать все';
  if (okCount > 0) confirmRow.hidden = false;
});

document.getElementById('btnCreateExecute').addEventListener('click', async () => {
  const toCreate = currentCreatePlans.filter(r => r.ok);
  if (toCreate.length === 0) return;
  const msgEl = document.getElementById('createExecuteMsg');
  const btn = document.getElementById('btnCreateExecute');
  const names = toCreate.map(r => r.plan.new_name + ' (' + r.plan.new_username + ')').join(', ');
  if (!confirm(`Точно создать ${toCreate.length} пользователей? ${names}`)) return;
  btn.disabled = true;
  msgEl.className = 'msg info';
  const results = [];
  const passwords = [];
  const failed = [];
  for (const [done, r] of toCreate.entries()) {
    msgEl.textContent = `Создаю ${done + 1} из ${toCreate.length}...`;
    const who = `<span class="k">${esc(r.plan.new_name)} (${esc(r.plan.new_username)})</span>`;
    try {
      const result = await api('/api/webitel/create_execute', {plan: r.plan});
      passwords.push({number: r.plan.new_extension, password: result.temporary_password});
      results.push(`<div class="field ${result.permissions_error || result.devices_error ? 'list-warn' : 'list-ok'}">${who}` +
        `<span class="v status">✓ ${createdPasswordNote(result)}${permissionsResultNote(result)}</span></div>`);
    } catch (e) {
      failed.push(r);
      results.push(`<div class="field list-error">${who}<span class="v">${esc(e.message)}</span></div>`);
    }
  }
  // Only what didn't work out stays for another try.
  currentCreatePlans = failed;
  btn.textContent = `Повторить (${failed.length})`;
  document.getElementById('createConfirmRow').hidden = failed.length === 0;
  showResults(msgEl, results, passwords);
  btn.disabled = false;
});

// «Группы»: a group number is a Flow schema from the operator's template
// plus a Dialplan route sending that number to it — only the number, a name
// and the member numbers are typed. Both tabs share this flow.
const groupPlans = {Single: [], Bulk: []};  // [{ok: true, plan} | {ok: false, name, error}]
const GROUP_LINE_FORMAT = 'формат строки «номер, название: участники»';

function splitNumbers(text) {
  return text.split(/[\\s,;]+/).filter(x => x.length > 0);
}

function readGroupEntries(tab) {
  if (tab === 'Single') {
    const number = document.getElementById('groupNumber').value.trim();
    const name = document.getElementById('groupName').value.trim();
    const extensions = splitNumbers(document.getElementById('groupMembers').value);
    // Hidden outside Astana, so whatever was typed there doesn't count.
    const fallbackInput = document.getElementById('groupFallback');
    const fallback = fallbackInput.closest('[data-city-only]').hidden ? '' : fallbackInput.value.trim();
    return number || name || extensions.length ? [{number, name, extensions, fallback}] : [];
  }
  return document.getElementById('groupBulkList').value.split('\\n')
    .map(line => line.trim())
    .filter(line => line.length > 0)
    .map(line => {
      const colon = line.indexOf(':');
      const comma = line.indexOf(',');
      if (colon < 0 || comma < 0 || comma > colon) return {name: line, error: 'не понял строку — ' + GROUP_LINE_FORMAT};
      const [members, fallback = ''] = line.slice(colon + 1).split('>');
      return {
        number: line.slice(0, comma).trim(),
        name: line.slice(comma + 1, colon).trim(),
        extensions: splitNumbers(members),
        fallback: fallback.trim(),
      };
    });
}

async function buildGroupPlans(tab) {
  const entries = readGroupEntries(tab);
  const msgEl = document.getElementById('groupPlanMsg' + tab);
  const listEl = document.getElementById('groupPlanList' + tab);
  const confirmRow = document.getElementById('groupConfirmRow' + tab);
  listEl.innerHTML = '';
  confirmRow.hidden = true;
  document.getElementById('groupExecuteMsg' + tab).textContent = '';
  groupPlans[tab] = [];

  if (entries.length === 0) {
    msgEl.className = 'msg error';
    msgEl.textContent = tab === 'Single' ? 'Укажите групповой номер, название и участников' : 'Добавьте хотя бы одну группу — ' + GROUP_LINE_FORMAT;
    return;
  }

  msgEl.className = 'msg info';
  msgEl.textContent = `Строю план для ${entries.length} — только чтение...`;

  const city = selectedCity;
  for (const entry of entries) {
    if (entry.error) {
      groupPlans[tab].push({ok: false, name: entry.name, error: entry.error});
      continue;
    }
    try {
      const plan = await api('/api/webitel/group_plan', {
        number: entry.number, name: entry.name, extensions: entry.extensions, fallback: entry.fallback, city,
      });
      groupPlans[tab].push({ok: true, plan});
    } catch (e) {
      groupPlans[tab].push({ok: false, name: entry.name, error: e.message});
    }
  }

  const okCount = groupPlans[tab].filter(r => r.ok).length;
  msgEl.textContent = tab === 'Single' ? '' : `Готово: ${okCount} из ${entries.length} — план построен. Ошибки ниже, если есть.`;
  listEl.innerHTML = groupPlans[tab].map(r => {
    if (!r.ok) {
      return `<div class="field list-error"><span class="k">${esc(r.name || '—')}</span><span class="v">${esc(r.error)}</span></div>`;
    }
    const members = r.plan.members.map(m => esc(m.extension) + (m.name ? ' — ' + esc(m.name) : '')).join(', ');
    const numberWarning = r.plan.number_user
      ? `<span class="presence active">Номер ${esc(r.plan.number)} — это пользователь ${esc(r.plan.number_user)}: его звонки пойдут в группу.</span>`
      : '';
    return `<div class="field ${r.plan.number_user ? 'list-warn' : 'list-ok'}"><span class="k">${esc(r.plan.number)} → ${esc(r.plan.name)}</span>` +
      `<span class="v status">✓ маршрут ^${esc(r.plan.number)}$ на новую схему · звонят сразу все ${r.plan.members.length}, ${esc(r.plan.ring_seconds)} с: ${members}` +
      (r.plan.fallback ? ` · никто не ответил — на ${esc(r.plan.fallback)}${r.plan.fallback_name ? ' — ' + esc(r.plan.fallback_name) : ''}, если свободен` : '') +
      `${numberWarning}</span></div>` +
      `<details class="schema-json"><summary>JSON схемы</summary><pre>${esc(JSON.stringify(r.plan.schema, null, 2))}</pre></details>`;
  }).join('');

  confirmRow.hidden = okCount === 0;
}

async function executeGroupPlans(tab) {
  const toCreate = groupPlans[tab].filter(r => r.ok);
  if (toCreate.length === 0) return;
  const msgEl = document.getElementById('groupExecuteMsg' + tab);
  const btn = document.getElementById('btnGroupExecute' + tab);
  const city = WEBITEL_CITY_NAMES[toCreate[0].plan.city] || toCreate[0].plan.city;
  const lines = toCreate.map(r => `${r.plan.number} → ${r.plan.name}`).join('\\n');
  if (!confirm(`Создать в Webitel ${city} схему Flow и маршрут Dialplan для ${toCreate.length}?\\n${lines}`)) return;
  btn.disabled = true;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Создаю...';
  const results = [];
  for (const r of toCreate) {
    const label = `<span class="k">${esc(r.plan.number)} → ${esc(r.plan.name)}</span>`;
    try {
      const result = await api('/api/webitel/group_execute', {plan: r.plan});
      const schemaNote = `схема создана${result.id ? ' (id ' + esc(result.id) + ')' : ''}`;
      results.push(result.route_error
        ? `<div class="field list-warn">${label}<span class="v status">✓ ${schemaNote} · маршрут НЕ создан: ${esc(result.route_error)}</span></div>`
        : `<div class="field list-ok">${label}<span class="v status">✓ ${schemaNote} · маршрут ^${esc(r.plan.number)}$ создан</span></div>`);
    } catch (e) {
      results.push(`<div class="field list-error">${label}<span class="v">${esc(e.message)}</span></div>`);
    }
  }
  // Done with this plan: confirming it again would only meet «уже есть».
  groupPlans[tab] = [];
  document.getElementById('groupConfirmRow' + tab).hidden = true;
  msgEl.className = 'msg info';
  msgEl.innerHTML = 'Готово.<div class="list">' + results.join('') + '</div>';
  btn.disabled = false;
}

['Single', 'Bulk'].forEach(tab => {
  document.getElementById('btnGroupPlan' + tab).addEventListener('click', () => buildGroupPlans(tab));
  document.getElementById('btnGroupExecute' + tab).addEventListener('click', () => executeGroupPlans(tab));
});

// A plan is for the fields and checkboxes as they were at «Показать план»:
// changing any of them afterwards drops it, so «Подтвердить» never sends a
// plan the screen no longer matches (2026-10-19: «Копировать Permissions»
// ticked after the plan was silently left out of the user created).
const STALE_PLAN_TEXT = 'Параметры изменились — нажмите «Показать план» заново.';

function dropPlanOnChange(ids, drop) {
  ids.forEach(id => {
    const el = document.getElementById(id);
    el.addEventListener(el.type === 'checkbox' ? 'change' : 'input', drop);
  });
}

function showStalePlan(msgId, listId, confirmRowId) {
  const list = document.getElementById(listId);
  if (list.tagName === 'DIV' && list.classList.contains('result-card')) list.hidden = true; else list.innerHTML = '';
  document.getElementById(confirmRowId).hidden = true;
  const msg = document.getElementById(msgId);
  msg.className = 'msg info';
  msg.textContent = STALE_PLAN_TEXT;
}

dropPlanOnChange(['templateExt', 'targetExt', 'resetPasswordCheck', 'copyPermissionsCheck'], () => {
  if (currentPlans.length === 0) return;
  currentPlans = [];
  showStalePlan('planMsg', 'planList', 'confirmRow');
});
dropPlanOnChange(['singleNewLogin', 'singleNewName', 'singleTemplateExt', 'singleCopyPermissionsCheck'], () => {
  if (!currentCreatePlanSingle) return;
  currentCreatePlanSingle = null;
  showStalePlan('createPlanMsgSingle', 'createPlanCardSingle', 'createConfirmRowSingle');
});
dropPlanOnChange(['createTemplateExt', 'createBulkList', 'bulkCopyPermissionsCheck'], () => {
  if (currentCreatePlans.length === 0) return;
  currentCreatePlans = [];
  showStalePlan('createPlanMsg', 'createPlanList', 'createConfirmRow');
});
dropPlanOnChange(['groupNumber', 'groupName', 'groupMembers', 'groupFallback'], () => {
  if (groupPlans.Single.length === 0) return;
  groupPlans.Single = [];
  showStalePlan('groupPlanMsgSingle', 'groupPlanListSingle', 'groupConfirmRowSingle');
});
dropPlanOnChange(['deleteExt'], () => {
  const building = document.getElementById('deletePlanMsg').textContent.startsWith('Ищу');
  deletePlanRequest++;  // a plan still being built is for the numbers as they were
  if (currentDeletePlans.length === 0 && !building) return;
  currentDeletePlans = [];
  askToDelete('', 0);
  showStalePlan('deletePlanMsg', 'deletePlanList', 'deleteConfirmRow');
});
dropPlanOnChange(['roleName'], () => {
  if (!currentRolePlan) return;
  currentRolePlan = null;
  showStalePlan('rolePlanMsg', 'rolePlanCard', 'roleConfirmRow');
});
dropPlanOnChange(['passwordExt'], () => {
  if (currentPasswordPlans.length === 0) return;
  currentPasswordPlans = [];
  showStalePlan('passwordPlanMsg', 'passwordPlanList', 'passwordConfirmRow');
});
dropPlanOnChange(['groupBulkList'], () => {
  if (groupPlans.Bulk.length === 0) return;
  groupPlans.Bulk = [];
  showStalePlan('groupPlanMsgBulk', 'groupPlanListBulk', 'groupConfirmRowBulk');
});
</script>
</body>
</html>
"""


# Changes with every change of the page. A tab opened before an update would
# go on running the old page (until 2026-11-23, one a ticket could take over)
# against the new console, so its requests are refused until it's reloaded.
_PAGE_VERSION = hashlib.sha256(_INDEX_HTML.encode("utf-8")).hexdigest()[:16]
_INDEX_HTML = _INDEX_HTML.replace("{{PAGE_VERSION}}", _PAGE_VERSION)

_LOCAL_NOTE = "Локальный инструмент. Только на этом компьютере. Каждое реальное изменение требует отдельного подтверждения."
_SHARED_INDEX_HTML = _INDEX_HTML.replace(
    _LOCAL_NOTE,
    "Консоль на сервере. Ваши входы в CRM и Webitel видны только в этом браузере. "
    "Каждое реальное изменение требует отдельного подтверждения.",
)


def _content_security_policy(html: str) -> str:
    """Lets the browser run the page's own script (by its hash) and nothing
    else. The page shows text anyone can write — a ticket's comment, a
    name in Webitel — and acts with the operator's Webitel rights, so
    markup in that text must not run even where escaping it was
    forgotten (a ticket's comment wasn't escaped until 2026-11-23)."""
    scripts = [
        "'sha256-" + base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii") + "'"
        for script in re.findall(r"<script>(.*?)</script>", html, re.S)
    ]
    script_src = " ".join(scripts) or "'none'"
    return (
        f"default-src 'none'; script-src {script_src}; "
        "style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


# What an address outside CONSOLE_ALLOWED_NETS sees instead of the console.
_FORBIDDEN_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Нет доступа</title>
<style>
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:#151514;color:#f3f3ef;
    font-family:"Inter","Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;}
  main{text-align:center;padding:24px;}
  h1{font-size:26px;font-weight:650;letter-spacing:-.03em;margin:0 0 8px;}
  p{color:#a9a9a2;margin:0;}
</style>
</head>
<body><main><h1>Нет доступа к консоли</h1><p>С адреса {ip} консоль не открывается.</p></main></body>
</html>
"""


# The console's front door when CONSOLE_AD_DOMAIN is set.
_SIGN_IN_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Консоль оператора — вход</title>
<style>
  :root{color-scheme:dark;}
  *{box-sizing:border-box;}
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0c0c0b;color:#f3f3ef;font-size:14px;line-height:1.5;
    font-family:"Inter","Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;}
  form{width:min(360px,calc(100vw - 32px));padding:30px 28px 26px;background:#1b1b19;border:1px solid rgba(255,255,255,.07);border-radius:20px;}
  h1{font-size:22px;font-weight:650;letter-spacing:-.03em;margin:0 0 20px;}
  label{display:block;font-size:12px;font-weight:500;color:#a9a9a2;margin:0 0 6px;}
  input{width:100%;padding:10px 13px;margin:0 0 14px;border:1px solid rgba(255,255,255,.07);border-radius:12px;font:inherit;background:#232321;color:#f3f3ef;}
  input:focus{outline:none;border-color:rgba(184,242,108,.55);box-shadow:0 0 0 3px rgba(184,242,108,.11);}
  button{width:100%;padding:11px 16px;margin-top:6px;border:0;border-radius:999px;font:inherit;font-weight:600;background:#f3f3ef;color:#121211;cursor:pointer;}
  button:disabled{background:#2d2d2a;color:#73736c;cursor:default;}
  #error{color:#ff7b6d;margin:14px 0 0;}
  #error:empty{display:none;}
</style>
</head>
<body>
<form id="form">
  <h1>Консоль оператора</h1>
  <label for="login">Логин AD</label>
  <input type="text" id="login" autocomplete="username" autocapitalize="off" spellcheck="false">
  <label for="password">Пароль</label>
  <input type="password" id="password" autocomplete="current-password">
  <button id="submit">Войти</button>
  <p id="error" role="alert"></p>
</form>
<script>
const login = document.getElementById('login');
const password = document.getElementById('password');
const button = document.getElementById('submit');
const error = document.getElementById('error');
login.focus();
document.getElementById('form').addEventListener('submit', async event => {
  event.preventDefault();
  if (button.disabled) return;
  error.textContent = '';
  if (!login.value.trim() || !password.value) {
    error.textContent = 'Введите логин и пароль';
    return;
  }
  button.disabled = true;
  button.textContent = 'Проверяю...';
  try {
    const response = await fetch('/api/ad/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({login: login.value, password: password.value}),
    });
    const data = await response.json().catch(() => ({}));
    if (response.ok) {
      location.reload();
      return;
    }
    error.textContent = data.error || 'HTTP ' + response.status;
  } catch (e) {
    error.textContent = 'Консоль не отвечает';
  }
  button.disabled = false;
  button.textContent = 'Войти';
  password.value = '';
  password.focus();
});
</script>
</body>
</html>
"""


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    return json.loads(raw)


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 (this class's default) plus rapid successive local requests
    # triggered occasional connection resets in testing on Windows;
    # HTTP/1.1 with the explicit Content-Length every response already
    # sends is the standard fix for that class of http.server flakiness.
    protocol_version = "HTTP/1.1"
    # Idle keep-alive connections and stalled clients don't hold a thread forever.
    timeout = 60

    def setup(self) -> None:
        super().setup()
        # The listening socket defers the TLS handshake to here, the
        # connection's own thread, so one slow client can't stall accept().
        if isinstance(self.connection, ssl.SSLSocket):
            self.connection.do_handshake()

    # Set while answering a page load that started a new workspace.
    _new_workspace_token = ""
    # Set while answering a sign-in (its cookie) or a sign-out ("").
    _new_ad_token: str | None = None
    # The AD account this request is signed in as ("" without CONSOLE_AD_DOMAIN).
    _ad_login = ""

    def _send_common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        secure = "; Secure" if _TLS_ENABLED else ""
        if self._new_ad_token is not None:
            max_age = console_access.SESSION_SECONDS if self._new_ad_token else 0
            self.send_header(
                "Set-Cookie", f"{_AD_COOKIE}={self._new_ad_token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"
            )
        if self._new_workspace_token:
            # A year, so the browser keeps its workspace and saved logins. Lax,
            # not Strict: following a link to the console from another site
            # must not start an empty workspace in place of this one.
            self.send_header(
                "Set-Cookie",
                f"{_WORKSPACE_COOKIE}={self._new_workspace_token}; Path=/; "
                f"Max-Age={_WORKSPACE_COOKIE_MAX_AGE}; HttpOnly; SameSite=Lax{secure}",
            )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_common_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", _content_security_policy(html))
        self.send_header("Content-Length", str(len(body)))
        self._send_common_headers()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # keep stdout free of PII-bearing request logs; see artifacts.py's same posture

    def _client_ip(self) -> str:
        return str(self.client_address[0])

    def _cookie(self, name: str) -> str:
        try:
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get(name)
        except CookieError:
            return ""
        return morsel.value if morsel else ""

    def _workspace_token(self) -> str:
        """This browser's workspace cookie, if it is this AD account's."""
        token = self._cookie(_WORKSPACE_COOKIE)
        if not _WORKSPACE_TOKEN_RE.fullmatch(token):
            return ""
        return token if not _AD_DOMAIN or _owns_workspace(_workspace_id(token), self._ad_login) else ""

    def _signed_in_as(self) -> str:
        """The AD account this browser is signed in as, "" if none or if it
        isn't let in (any more)."""
        login = _sign_ins.login_of(self._cookie(_AD_COOKIE))
        return login if login and any(login in people for people in _ad_people()) else ""

    def _sign_in(self, body: dict[str, Any]) -> None:
        ip = self._client_ip()
        login = console_access.normalize_login(str(body.get("login") or ""), _AD_DOMAIN)
        listed = any(login in people for people in _ad_people())
        with _ad_lock:
            wait = max(_ad_address_throttle.wait_seconds(ip), _ad_throttle.wait_seconds(login))
            # Only those on the list are asked about: not a way to guess
            # the passwords of the whole domain, or to lock its accounts.
            if wait > 0:
                problem = "too many"
            elif listed:
                problem = console_access.check_password(login, _AD_DOMAIN, str(body.get("password") or ""))
            else:
                problem = "wrong"
            if problem == "wrong":
                _ad_address_throttle.failed(ip)
                if listed:
                    _ad_throttle.failed(login)
            elif not problem:
                _ad_address_throttle.succeeded(ip)
                _ad_throttle.succeeded(login)
        # What was typed is logged only when it is a known login: people
        # type passwords into the wrong field.
        _log_sign_in(ip, login if listed else "не из списка", problem or "ok")
        if problem:
            status, error = {
                "too many": (429, f"Слишком много попыток. Повторите через {int(wait // 60) + 1} мин"),
                "expired": (403, "Пароль AD истёк — смените его и войдите снова"),
                "unavailable": (503, "Домен не отвечает, попробуйте ещё раз"),
            }.get(problem, (403, "Неверный логин или пароль, или нет доступа к консоли"))
            self._send_json(status, {"error": error})
            return
        _sign_ins.end(self._cookie(_AD_COOKIE))
        self._new_ad_token = _sign_ins.start(login)
        self._send_json(200, {"signed_in": login})

    def _cross_site(self) -> bool:
        """The API only takes JSON from the console's own page: a form or a
        script on another site can't send this content type without a CORS
        preflight (never answered here), and browsers name their page in
        Origin. Guards a local console against hostile sites too."""
        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            return True
        origin = self.headers.get("Origin")
        return bool(origin) and urllib.parse.urlsplit(origin).netloc != (self.headers.get("Host") or "")

    def _current_operator(self) -> _Operator | None:
        """Whose console this request is: the local operator, or on a shared
        console this browser's workspace (None for a browser that has none —
        only loading the page starts one)."""
        if not _SHARED:
            return _local_operator()
        token = self._workspace_token()
        if not token:
            return None
        from crm_ai_agent.webapp.saved_secrets import workspace_key

        op = _named_operator(_workspace_id(token), workspace_key(token))
        _encrypt_old_passwords(op)
        now = time.time()
        with _workspaces_lock:
            _sweep_idle_workspaces(now)
            _workspace_seen[op.name] = now
        return op

    def _run_audited(self, op: _Operator, plan: dict[str, Any], action: Callable[[], Any]) -> Any:
        """Runs one Webitel change and records who made it (the AD account,
        the CRM and Webitel accounts this browser works under), on what, and
        whether it worked."""
        login_key = WEBITEL_CITY_KEYS.get(str(plan.get("city") or "almaty"), {}).get("login", "")
        details = {
            **({"ad_login": self._ad_login} if self._ad_login else {}),
            "crm_login": _crm_login_of(op),
            "webitel_login": _operator_env(op).get(login_key, "") if login_key else "",
            **{key: plan[key] for key in _AUDIT_PLAN_FIELDS if key in plan},
        }
        try:
            result = action()
        except Exception as exc:
            _audit(op, self._client_ip(), self.path, {**details, "ok": False, "error": str(exc)[:300]})
            raise
        for key in ("permissions_error", "route_error", "devices_error", "objects_error"):
            if isinstance(result, dict) and result.get(key):
                # The main change went through; only a follow-up step failed.
                details[key] = str(result[key])[:300]
        _audit(op, self._client_ip(), self.path, {**details, "ok": True})
        return result

    def _refuse_stranger(self, page: bool) -> bool:
        """Answers 403 to an address outside CONSOLE_ALLOWED_NETS — a short
        page for the browser, JSON for the API. True when it did."""
        ip = self._client_ip()
        if _ip_allowed(ip, _ALLOWED_NETS):
            return False
        if page:
            self._send_html(_FORBIDDEN_HTML.replace("{ip}", html_escape(ip)), status=403)
        else:
            self._send_json(403, {"error": f"Нет доступа к консоли с адреса {ip}"})
        return True

    def do_GET(self) -> None:  # noqa: N802
        self._new_workspace_token, self._new_ad_token, self._ad_login = "", None, ""
        if self._refuse_stranger(page=self.path == "/"):
            return
        if self.path == "/":
            if _AD_DOMAIN:
                self._ad_login = self._signed_in_as()
                if not self._ad_login:
                    self._send_html(_SIGN_IN_HTML)
                    return
            if _SHARED and not self._workspace_token():
                self._new_workspace_token = secrets.token_hex(16)
                home = _named_operator(_workspace_id(self._new_workspace_token)).env_path.parent
                home.mkdir(mode=0o700, parents=True, exist_ok=True)
                if _AD_DOMAIN:
                    _owns_workspace(home.name, self._ad_login)
            self._send_html(_SHARED_INDEX_HTML if _SHARED else _INDEX_HTML)
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        self._new_workspace_token, self._new_ad_token, self._ad_login = "", None, ""
        # Always drain the request body first, regardless of which path
        # branch below actually needs it. Responding without reading a
        # POST body the client already sent leaves unread bytes in the
        # socket's receive buffer, which on Windows can turn the next
        # connection close into a TCP RST instead of a clean FIN —
        # confirmed via test_webapp_server.py's intermittent
        # ConnectionResetError on exactly the two branches that used to
        # skip this (login/status and the unknown-path fallback).
        try:
            body = _read_json_body(self)
        except Exception as exc:  # noqa: BLE001
            self._send_json(400, {"error": f"invalid request body: {exc}"})
            return
        if self._refuse_stranger(page=False):
            return
        if self._cross_site():
            self._send_json(403, {"error": "cross-site request refused"})
            return
        if _AD_DOMAIN:
            if self.path == "/api/ad/login":
                try:
                    self._sign_in(body)
                except Exception as exc:  # noqa: BLE001
                    self._send_json(500, {"error": f"Вход не удался: {type(exc).__name__}"})
                return
            self._ad_login = self._signed_in_as()
            if not self._ad_login:
                self._send_json(401, {"error": "Вход в консоль закончился — обновите страницу (F5)", "ad_login": True})
                return
        if self.headers.get("X-Console-Version") != _PAGE_VERSION:
            self._send_json(409, {"error": "Консоль обновилась — обновите страницу (F5)", "outdated": True})
            return

        try:
            op = self._current_operator()
            if op is None:
                self._send_json(400, {"error": "Обновите страницу консоли — браузер не прислал свой cookie"})
                return

            if self.path == "/api/access":
                self._send_json(200, _access_info(self._ad_login))
                return

            if self.path in ("/api/access/grant", "/api/access/revoke", "/api/access/promote") and _AD_DOMAIN:
                try:
                    login, name = _change_access(self._ad_login, self.path.rsplit("/", 1)[-1], str(body.get("login") or ""))
                except (PermissionError, ValueError) as exc:
                    self._send_json(403 if isinstance(exc, PermissionError) else 400, {"error": str(exc)})
                    return
                _audit(op, self._client_ip(), self.path, {
                    "ad_login": self._ad_login, "extension": login, "name": name, "ok": True,
                })
                self._send_json(200, {**_access_info(self._ad_login), "changed": " — ".join(filter(None, (login, name)))})
                return

            if self.path == "/api/ad/logout" and _AD_DOMAIN:
                _sign_ins.end(self._cookie(_AD_COOKIE))
                self._new_ad_token = ""
                self._send_json(200, {"signed_out": True})
                return

            if self.path == "/api/settings/save":
                # No CRM session reset needed on credential changes: every
                # /api/crm/login reads the saved CRM login fresh.
                updates = {
                    "CRM_LOGIN": body.get("crm_login", "").strip(),
                    "CRM_PASSWORD": body.get("crm_password", "").strip(),
                    "CRM_VISA_OWNER_ID": body.get("crm_visa_owner_id", "").strip(),
                    "WEBITEL_LOGIN": body.get("webitel_almaty_login", "").strip(),
                    "WEBITEL_PASSWORD": body.get("webitel_almaty_password", "").strip(),
                    "WEBITEL_ASTANA_LOGIN": body.get("webitel_astana_login", "").strip(),
                    "WEBITEL_ASTANA_PASSWORD": body.get("webitel_astana_password", "").strip(),
                }
                _save_own_env(op, updates)
                if "crm_visa_owner_id" in body and not updates["CRM_VISA_OWNER_ID"]:
                    # Unlike the passwords, this field is pre-filled with the
                    # saved value, so an empty one means "use my roles".
                    _remove_env_keys(op.env_path, ("CRM_VISA_OWNER_ID",))
                self._send_json(200, {"saved": True})
                return

            if self.path == "/api/crm/credentials":
                # Which CRM login is remembered — never the password itself.
                env = _own_env(op)
                self._send_json(200, {
                    "login": env.get("CRM_LOGIN", ""),
                    "has_password": bool(env.get("CRM_PASSWORD")),
                    "visa_owner_id": env.get("CRM_VISA_OWNER_ID", ""),
                })
                return

            if self.path == "/api/crm/credentials/forget":
                # Only the remembered login goes; an already open CRM window
                # keeps working. The Visa Owner ID goes too, since it was
                # set for that account and must not carry over to the next.
                _remove_env_keys(op.env_path, ("CRM_LOGIN", "CRM_PASSWORD", "CRM_VISA_OWNER_ID"))
                self._send_json(200, {"forgotten": True})
                return

            if self.path == "/api/crm/login":
                try:
                    login, password = _resolve_crm_login(body, op)
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                _get_crm_session(op).start_login(login=login, password=password)
                self._send_json(200, {"started": True, "login": login})
                return

            if self.path == "/api/crm/login/status":
                session = _get_crm_session(op)
                status = {"ready": session.is_logged_in(), "browser_open": session.has_open_page(),
                          "code_required": False, "error_text": "", "logged_out": False}
                if status["browser_open"] and not status["ready"]:
                    status.update(session.login_progress())
                self._send_json(200, status)
                return

            if self.path == "/api/crm/login/code":
                try:
                    code = _normalize_confirmation_code(body.get("code"))
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                _get_crm_session(op).submit_code(code)
                self._send_json(200, {"submitted": True})
                return

            if self.path == "/api/crm/login/screenshot":
                png = _get_crm_session(op).screenshot_png()
                self._send_json(200, {"png": base64.b64encode(png).decode("ascii")})
                return

            if self.path == "/api/crm/debug/login_check":  # temporary diagnostic
                self._send_json(200, _get_crm_session(op).debug_login_check())
                return

            if self.path == "/api/crm/pending_tickets":
                # Empty CRM_VISA_OWNER_ID = the logged-in user plus all their roles.
                visa_owner_id = _load_env_file(op.env_path).get("CRM_VISA_OWNER_ID") or None
                tickets = _get_crm_session(op).list_pending_tickets(visa_owner_id)
                self._send_json(200, {"tickets": tickets})
                return

            if self.path == "/api/crm/ticket":
                url = body.get("url", "").strip()
                if not url:
                    self._send_json(400, {"error": "url is required"})
                    return
                session = _get_crm_session(op)
                ticket = session.read_ticket(url)
                steps: list[dict[str, Any]] = []
                entity_id = _ticket_entity_id(url)
                if entity_id:
                    # A failure here must not hide the ticket itself.
                    try:
                        approvals = session.read_approvals(entity_id)
                        steps = approvals["steps"]
                        ticket["approvals_source"] = approvals["source"]
                    except Exception as exc:  # noqa: BLE001
                        ticket["approvals_error"] = str(exc)
                ticket["approvals"] = steps
                # Webitel numbers often appear only in an approver's comment
                # (e.g. the sysadmin step), not in the ticket's own comment.
                comments = [ticket.get("comment") or ""] + [step["comment"] for step in steps]
                ticket["webitel_logins"] = _extract_webitel_logins("\n".join(comments))
                self._send_json(200, ticket)
                return

            if self.path == "/api/journal":
                if not _may_read_journal(op):
                    self._send_json(403, {"error": "Журнал виден после входа в Webitel"})
                    return
                offset, limit = body.get("offset"), body.get("limit")
                if not isinstance(offset, int) or offset < 0:
                    offset = 0
                if limit not in _JOURNAL_LIMITS:
                    limit = 25
                self._send_json(200, _read_journal(str(body.get("query") or ""), offset, limit))
                return

            if self.path == "/api/webitel/session_status":
                city = str(body.get("city", "almaty")).strip() or "almaty"
                if city not in WEBITEL_STORAGE_STATE_PATHS:
                    self._send_json(400, {"error": f"Unknown city {city!r}"})
                    return
                self._send_json(200, _webitel_session_state(op, city))
                return

            if self.path in ("/api/webitel/credentials/save", "/api/webitel/credentials/forget"):
                city = str(body.get("city", "")).strip()
                if city not in WEBITEL_STORAGE_STATE_PATHS:
                    self._send_json(400, {"error": f"Unknown city {city!r}"})
                    return
                if self.path.endswith("/forget"):
                    _forget_webitel_credentials(op, city)
                    self._send_json(200, {"forgotten": True})
                    return
                try:
                    _save_webitel_credentials(op, city, str(body.get("login") or "").strip(), str(body.get("password") or ""))
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                self._send_json(200, {"saved": True})
                return

            if self.path == "/api/webitel/login/screenshot":
                city = str(body.get("city", "")).strip()
                if city not in WEBITEL_STORAGE_STATE_PATHS:
                    self._send_json(400, {"error": f"Unknown city {city!r}"})
                    return
                png = latest_screenshot(op.storage_paths[city], city)
                if png is None:
                    self._send_json(404, {"error": "Снимков входа в Webitel ещё нет"})
                    return
                self._send_json(200, {"png": base64.b64encode(png).decode("ascii")})
                return

            if self.path == "/api/webitel/login":
                city = str(body.get("city", "almaty")).strip() or "almaty"
                if city not in WEBITEL_STORAGE_STATE_PATHS:
                    self._send_json(400, {"error": f"Unknown city {city!r}"})
                    return
                try:
                    self._send_json(200, _start_webitel_login(op, city))
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                return

            if self.path == "/api/webitel/plan":
                template_extension = body.get("template_extension", "").strip()
                target_extension = body.get("target_extension", "").strip()
                city = body.get("city", "almaty").strip() or "almaty"
                if not template_extension or not target_extension:
                    self._send_json(400, {"error": "template_extension and target_extension are required"})
                    return
                actions = _get_webitel_actions(op, city)
                plan = actions.build_plan(
                    template_extension=template_extension,
                    target_extension=target_extension,
                    copy_permissions=body.get("copy_permissions") is True,
                )
                plan["city"] = city
                # Off unless the operator ticked "Сбросить пароль" — execute_plan
                # only generates a new password when this is True.
                plan["reset_password"] = body.get("reset_password") is True
                self._send_json(200, plan)
                return

            if self.path == "/api/webitel/execute":
                plan = body.get("plan")
                if not plan:
                    self._send_json(400, {"error": "plan is required"})
                    return
                actions = _get_webitel_actions(op, plan.get("city", "almaty") or "almaty")
                result = self._run_audited(op, plan, lambda: actions.execute_plan(plan))
                self._send_json(200, result)
                return

            if self.path == "/api/webitel/delete_plan":
                extension = body.get("extension", "").strip()
                city = body.get("city", "almaty").strip() or "almaty"
                if not extension:
                    self._send_json(400, {"error": "extension is required"})
                    return
                actions = _get_webitel_actions(op, city)
                plan = actions.build_delete_plan(extension)
                plan["city"] = city
                self._send_json(200, plan)
                return

            if self.path == "/api/webitel/delete_execute":
                plan = body.get("plan")
                if not plan:
                    self._send_json(400, {"error": "plan is required"})
                    return
                actions = _get_webitel_actions(op, plan.get("city", "almaty") or "almaty")
                result = self._run_audited(op, plan, lambda: actions.execute_delete_plan(plan))
                self._send_json(200, result)
                return

            if self.path == "/api/webitel/neighbors":
                extension = body.get("extension", "").strip()
                city = body.get("city", "almaty").strip() or "almaty"
                if not extension:
                    self._send_json(400, {"error": "extension is required"})
                    return
                try:
                    radius = int(body.get("radius", 2))
                except (TypeError, ValueError):
                    self._send_json(400, {"error": "radius must be an integer"})
                    return
                if radius < 1 or radius > 50:
                    self._send_json(400, {"error": "radius must be between 1 and 50"})
                    return
                actions = _get_webitel_actions(op, city)
                neighbors = actions.find_neighbors(extension, radius=radius)
                self._send_json(200, {"neighbors": neighbors})
                return

            if self.path == "/api/webitel/create_plan":
                template_extension = body.get("template_extension", "").strip()
                new_username = body.get("new_username", "").strip()
                new_name = body.get("new_name", "").strip()
                new_extension = body.get("new_extension", "").strip()
                city = body.get("city", "almaty").strip() or "almaty"
                if not template_extension or not new_username or not new_name or not new_extension:
                    self._send_json(400, {"error": "template_extension, new_username, new_name and new_extension are required"})
                    return
                actions = _get_webitel_actions(op, city)
                plan = actions.build_create_plan(
                    template_extension=template_extension,
                    new_username=new_username,
                    new_name=new_name,
                    new_extension=new_extension,
                    copy_permissions=body.get("copy_permissions") is True,
                )
                plan["city"] = city
                self._send_json(200, plan)
                return

            if self.path == "/api/webitel/create_execute":
                plan = body.get("plan")
                if not plan:
                    self._send_json(400, {"error": "plan is required"})
                    return
                actions = _get_webitel_actions(op, plan.get("city", "almaty") or "almaty")
                result = self._run_audited(op, plan, lambda: actions.execute_create_plan(plan))
                self._send_json(200, result)
                return

            if self.path == "/api/webitel/password_plan":
                extension = str(body.get("extension") or "").strip()
                city = str(body.get("city") or "almaty").strip() or "almaty"
                if not extension:
                    self._send_json(400, {"error": "extension is required"})
                    return
                actions = _get_webitel_actions(op, city)
                plan = actions.build_password_plan(extension)
                plan["city"] = city
                self._send_json(200, plan)
                return

            if self.path == "/api/webitel/password_execute":
                plan = body.get("plan")
                if not plan:
                    self._send_json(400, {"error": "plan is required"})
                    return
                actions = _get_webitel_actions(op, plan.get("city", "almaty") or "almaty")
                result = self._run_audited(op, plan, lambda: actions.execute_password_plan(plan))
                self._send_json(200, result)
                return

            if self.path == "/api/webitel/role_plan":
                name = str(body.get("name") or "").strip()
                city = str(body.get("city") or "almaty").strip() or "almaty"
                if not name:
                    self._send_json(400, {"error": "Укажите название роли"})
                    return
                actions = _get_webitel_actions(op, city)
                plan = actions.build_role_plan(name)
                plan["city"] = city
                self._send_json(200, plan)
                return

            if self.path == "/api/webitel/role_execute":
                plan = body.get("plan")
                if not plan:
                    self._send_json(400, {"error": "plan is required"})
                    return
                actions = _get_webitel_actions(op, plan.get("city", "almaty") or "almaty")
                result = self._run_audited(op, plan, lambda: actions.execute_role_plan(plan))
                self._send_json(200, result)
                return

            if self.path == "/api/webitel/group_plan":
                from crm_ai_agent.adapters.webitel_api.actions import GROUP_RING_SECONDS

                name = str(body.get("name") or "").strip()
                number = str(body.get("number") or "").strip()
                fallback = str(body.get("fallback") or "").strip()
                extensions = body.get("extensions")
                city = str(body.get("city") or "almaty").strip() or "almaty"
                if not isinstance(extensions, list):
                    extensions = []
                extensions = [str(x).strip() for x in extensions if str(x).strip()]
                if not name or not number or not extensions:
                    self._send_json(400, {"error": "Укажите групповой номер, название и хотя бы одного участника"})
                    return
                if city not in GROUP_RING_SECONDS:
                    self._send_json(400, {"error": "Для этого города шаблона группового номера пока нет"})
                    return
                actions = _get_webitel_actions(op, city)
                plan = actions.build_group_plan(
                    city=city, name=name, number=number, extensions=extensions, fallback=fallback,
                )
                plan["city"] = city
                self._send_json(200, plan)
                return

            if self.path == "/api/webitel/group_execute":
                plan = body.get("plan")
                if not plan:
                    self._send_json(400, {"error": "plan is required"})
                    return
                actions = _get_webitel_actions(op, plan.get("city", "almaty") or "almaty")
                result = self._run_audited(op, plan, lambda: actions.execute_group_plan(plan))
                self._send_json(200, result)
                return

            self._send_json(404, {"error": "not found"})
        except CrmSessionExpired as exc:
            # The page re-logs in by itself on this flag (remembered login).
            self._send_json(401, {"error": str(exc), "crm_session_expired": True})
        except SubjectNotFoundError as exc:
            # "missing" names the number, so the page can tell a missing
            # template from a missing target.
            self._send_json(404, {"error": str(exc), "missing": exc.subject})
        except Exception as exc:  # noqa: BLE001 — surface to the operator's browser, not a crash
            self._send_json(500, {"error": _operator_error_message(exc)})


class _ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Scanners, plain-http requests to the https port and closed tabs
        # are routine for a console on the network, not tracebacks.
        if isinstance(sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)


def run(port: int | None = None) -> None:
    """Listens as info.env says: CONSOLE_HOST (default 127.0.0.1),
    CONSOLE_PORT (default 8765, a ``port`` argument wins), CONSOLE_SHARED=1
    (a workspace per browser), CONSOLE_TLS_CERT / CONSOLE_TLS_KEY (HTTPS;
    paths relative to the project root), CONSOLE_ALLOWED_NETS (from where it
    may be opened) and CONSOLE_AD_DOMAIN / _ADMINS / _USERS (by whom)."""
    global _SHARED, _TLS_ENABLED, _ALLOWED_NETS, _AD_DOMAIN
    env = _load_env_file(INFO_ENV_PATH)
    host = env.get("CONSOLE_HOST") or "127.0.0.1"
    port = port or int(env.get("CONSOLE_PORT") or 8765)
    cert, key = env.get("CONSOLE_TLS_CERT", ""), env.get("CONSOLE_TLS_KEY", "")
    shared = env.get("CONSOLE_SHARED") == "1"
    problem = _exposure_problem(host, shared, bool(cert and key))
    if problem:
        raise SystemExit(f"Консоль не запущена: {problem}")
    if shared:
        try:
            import crm_ai_agent.webapp.saved_secrets  # noqa: F401
        except ImportError as exc:
            raise SystemExit(
                f"Консоль не запущена: пароли нечем шифровать ({exc}) — "
                "выполните .venv/bin/python -m pip install cryptography"
            ) from exc
    try:
        _ALLOWED_NETS = _parse_allowed_nets(env.get("CONSOLE_ALLOWED_NETS", ""))
    except ValueError as exc:
        raise SystemExit(f"Консоль не запущена: {exc}") from exc
    problem = _ad_problem(env, shared)
    if problem:
        raise SystemExit(f"Консоль не запущена: {problem}")
    _AD_DOMAIN = env.get("CONSOLE_AD_DOMAIN", "").strip().lower()
    _SHARED = shared
    server = _ConsoleServer((host, port), Handler)
    scheme = "http"
    if cert and key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(ROOT / cert, ROOT / key)
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        _TLS_ENABLED = True
        scheme = "https"
    mode = "общая, у каждого браузера свои входы" if shared else "локальная"
    access = ", ".join(str(net) for net in _ALLOWED_NETS) or "все"
    if _AD_DOMAIN:
        access += f", вход по AD {_AD_DOMAIN}"
    print(f"Console running at {scheme}://{host}:{port}  ({mode}; доступ: {access}; Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
