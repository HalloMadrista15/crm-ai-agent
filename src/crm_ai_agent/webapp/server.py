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

Runs only on localhost, only for the one operator running it on their own
machine (see docs/open_questions.md's "Обновление после реальной
разведки" — Telegram/multi-user approval was explicitly dropped in favor
of this). Nothing here is meant to be exposed beyond 127.0.0.1.
"""

from __future__ import annotations

import base64
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from crm_ai_agent.webapp.crm_session import CrmSessionExpired, CrmSessionManager
from crm_ai_agent.webapp.webitel_login import WEBITEL_CITY_KEYS, WebitelLoginJob, city_config, latest_screenshot

ROOT = Path(__file__).resolve().parents[3]
INFO_ENV_PATH = ROOT / "info.env"
WEBITEL_STORAGE_STATE_PATHS = {
    "almaty": ROOT / ".auth" / "webitel_almaty_storage_state.json",
    "astana": ROOT / ".auth" / "webitel_astana_storage_state.json",
}


_SETTINGS_KEYS = (
    "CRM_LOGIN",
    "CRM_PASSWORD",
    "CRM_VISA_OWNER_ID",
    "WEBITEL_LOGIN",
    "WEBITEL_PASSWORD",
    "WEBITEL_ASTANA_LOGIN",
    "WEBITEL_ASTANA_PASSWORD",
)


def _save_env_updates(path: Path, updates: dict[str, str]) -> None:
    """Read-modify-write: only overwrites keys with a non-empty new value,
    so leaving a field blank in the settings form keeps whatever is
    already saved (never blanks out a working credential by accident).
    Shared infra config (base URLs, domains) isn't touched here — those
    stay in info.env, edited by whoever first sets this up, same for
    everyone; only the per-operator keys in _SETTINGS_KEYS are writable
    from this endpoint."""

    existing = _load_env_file(path)
    for key, value in updates.items():
        if key not in _SETTINGS_KEYS:
            continue
        if value:
            existing[key] = value
    lines = [f"{k}={v}" for k, v in existing.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _remove_env_keys(path: Path, keys: tuple[str, ...]) -> None:
    existing = _load_env_file(path)
    if not any(key in existing for key in keys):
        return
    lines = [f"{k}={v}" for k, v in existing.items() if k not in keys]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


_CITY_NAMES = {"almaty": "Алматы", "astana": "Астана"}

# At most one visible-browser Webitel login per city at a time.
_webitel_login_jobs: dict[str, WebitelLoginJob] = {}


def _webitel_session_state(city: str) -> dict[str, Any]:
    """Who the console logs into this city's Webitel as, and whether the
    saved session still works (one read-only request)."""
    from crm_ai_agent.adapters.webitel_api.actions import WebitelApiActions, WebitelApiError
    from crm_ai_agent.adapters.webitel_api.session import WebitelSessionError

    config = city_config(_load_env_file(INFO_ENV_PATH), city)
    job = _webitel_login_jobs.get(city)
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
        WebitelApiActions.from_info_env(INFO_ENV_PATH, WEBITEL_STORAGE_STATE_PATHS[city], city=city).check_session()
        state["session"] = "active"
    except WebitelSessionError:
        state["session"] = "missing"
    except WebitelApiError as exc:
        state["session"] = "expired" if ("HTTP 401" in str(exc) or "HTTP 403" in str(exc)) else "error"
        state["error"] = str(exc)
    return state


def _save_webitel_credentials(city: str, login: str, password: str) -> None:
    """This operator's own Webitel account for one city, into info.env.
    Raises ValueError with an operator-facing message."""
    if not login or not password:
        raise ValueError("Введите и логин, и пароль Webitel")
    keys = WEBITEL_CITY_KEYS[city]
    _save_env_updates(INFO_ENV_PATH, {keys["login"]: login, keys["password"]: password})


def _forget_webitel_credentials(city: str) -> None:
    """Removes this city's remembered Webitel login/password AND its saved
    session — otherwise the console would keep acting as that account."""
    keys = WEBITEL_CITY_KEYS[city]
    _remove_env_keys(INFO_ENV_PATH, (keys["login"], keys["password"]))
    WEBITEL_STORAGE_STATE_PATHS[city].unlink(missing_ok=True)


def _start_webitel_login(city: str) -> dict[str, Any]:
    """Raises ValueError with an operator-facing message when this city's
    login can't start (missing credentials or address)."""
    config = city_config(_load_env_file(INFO_ENV_PATH), city)
    name = _CITY_NAMES.get(city, city)
    if not config["login"] or not config["password"]:
        raise ValueError(f"Впишите логин и пароль Webitel {name} в «Настройках» и нажмите «Сохранить»")
    if not config["base_url"] or not config["domain"]:
        raise ValueError(f"В info.env нет адреса или домена Webitel {name} — см. info.env.example")
    job = _webitel_login_jobs.get(city)
    if job and job.state == "running":
        return {"started": False, "running": True}
    _webitel_login_jobs[city] = WebitelLoginJob(
        city=city,
        config=config,
        storage_state_path=WEBITEL_STORAGE_STATE_PATHS[city],
        show_browser=_load_env_file(INFO_ENV_PATH).get("WEBITEL_SHOW_BROWSER") == "1",
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


def _get_webitel_actions(city: str) -> Any:
    from crm_ai_agent.adapters.webitel_api.actions import WebitelApiActions

    storage_state_path = WEBITEL_STORAGE_STATE_PATHS.get(city)
    if storage_state_path is None:
        raise ValueError(f"Unknown city {city!r} — must be one of: {', '.join(WEBITEL_STORAGE_STATE_PATHS)}")
    return WebitelApiActions.from_info_env(INFO_ENV_PATH, storage_state_path, city=city)


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


_crm_session: CrmSessionManager | None = None


def _get_crm_session() -> CrmSessionManager:
    # Credentials are not baked in here: /api/crm/login passes them on every
    # login (typed in the console or the remembered ones from info.env), so
    # an operator with nothing saved yet can still log in.
    global _crm_session
    if _crm_session is None:
        env = _load_env_file(INFO_ENV_PATH)
        base_url = env.get("CRM_BASE_URL")
        if not base_url:
            raise RuntimeError("Missing CRM_BASE_URL in info.env")
        _crm_session = CrmSessionManager(base_url=base_url, show_browser=env.get("CRM_SHOW_BROWSER") == "1")
    return _crm_session


def _normalize_confirmation_code(raw: Any) -> str:
    """The authenticator code typed into the console: always exactly 6
    digits for this CRM (spaces and dashes people paste in are dropped)."""
    code = re.sub(r"[\s-]", "", str(raw or ""))
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("Код подтверждения — 6 цифр из приложения-аутентификатора")
    return code


def _resolve_crm_login(body: dict[str, Any]) -> tuple[str, str]:
    """Credentials for /api/crm/login: the ones typed into the console if
    any (saved to info.env only when "remember" is ticked), otherwise the
    remembered ones. Raises ValueError with an operator-facing message."""
    login = str(body.get("login") or "").strip()
    password = str(body.get("password") or "")
    if login or password:
        if not login or not password:
            raise ValueError("Введите и логин, и пароль CRM")
        if body.get("remember"):
            saved_login = _load_env_file(INFO_ENV_PATH).get("CRM_LOGIN", "")
            if saved_login and saved_login != login:
                # The saved Visa Owner ID belonged to the previous account;
                # dropping it makes the console detect the new one after login.
                _remove_env_keys(INFO_ENV_PATH, ("CRM_VISA_OWNER_ID",))
            _save_env_updates(INFO_ENV_PATH, {"CRM_LOGIN": login, "CRM_PASSWORD": password})
        return login, password
    env = _load_env_file(INFO_ENV_PATH)
    login, password = env.get("CRM_LOGIN", ""), env.get("CRM_PASSWORD", "")
    if not login or not password:
        raise ValueError("Логин и пароль CRM не сохранены — введите их")
    return login, password


_INDEX_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Консоль оператора</title>
<style>
  :root{
    --surface:#11162a; --surface-2:#161c36; --border:#262d4d;
    --text:#eef0f8; --text-soft:#a3aac4; --text-faint:#6b7291;
    --accent:#ffffff; --accent-dark:#dde1ee; --accent-soft:#1d2444; --accent-ink:#0a0e1a;
    --good:#3ddc84; --good-soft:#123524; --warn:#f5b64d; --warn-soft:#3a2b10;
    --danger:#ff6b6b; --danger-dark:#ff8787; --danger-soft:#3a1620;
    --shadow:0 1px 2px rgba(0,0,0,.2), 0 8px 24px -12px rgba(0,0,0,.5);
    --radius:14px;
    --page-max:1120px; --gutter:clamp(12px,3vw,32px);
  }
  *{box-sizing:border-box;}
  html{-webkit-font-smoothing:antialiased;background:#0d1526;}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif;
    background:linear-gradient(180deg,#0d1526 0%,#152449 40%,#1e3566 100%) fixed;
    color:var(--text);margin:0;padding:0 0 64px;font-size:14px;line-height:1.5;min-height:100vh;
  }
  .page{max-width:var(--page-max);margin:0 auto;padding:24px var(--gutter) 0;}
  .topnav{position:sticky;top:0;z-index:10;background:rgba(5,6,15,.72);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--border);}
  .topnav-inner{max-width:var(--page-max);margin:0 auto;padding:0 var(--gutter);display:flex;align-items:center;gap:28px;height:56px;}
  .topnav-brand{font-weight:700;font-size:15px;color:var(--text);letter-spacing:-.01em;flex-shrink:0;}
  .topnav-links{display:flex;gap:4px;flex:1;}
  .nav-link{background:transparent;color:var(--text-soft);font-weight:600;font-size:13.5px;padding:8px 14px;border-radius:7px;}
  .nav-link:hover{background:var(--surface-2);color:var(--text);}
  .nav-link.active{background:var(--surface-2);color:var(--text);}
  .nav-account-btn{background:transparent;border:1px solid var(--border);color:var(--text);font-weight:600;font-size:13px;padding:7px 14px;}
  .nav-account-btn:hover{background:var(--surface-2);}
  .nav-account-btn.active{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);}
  .status-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px;vertical-align:1px;background:var(--text-faint);}
  .status-dot.good{background:var(--good);}
  .status-dot.warn{background:var(--warn);}
  .saved-login{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;padding:10px 12px;margin-bottom:12px;background:var(--surface-2);border:1px solid var(--border);border-radius:8px;font-size:13px;color:var(--text-soft);}
  .saved-login b{color:var(--text);}
  .saved-login-actions{display:flex;gap:6px;}
  button.icon-btn{display:inline-flex;align-items:center;justify-content:center;padding:6px 8px;}
  button.icon-btn:hover{color:var(--danger-dark);border-color:var(--danger-dark);background:var(--danger-soft);}
  .card-head .pill{margin-left:auto;}
  .top-sub{color:var(--text-soft);font-size:13px;max-width:56ch;margin-bottom:24px;}
  .steps{display:flex;flex-direction:column;gap:16px;}
  .card{
    background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
    padding:20px 22px;box-shadow:var(--shadow);
  }
  .card-head{display:flex;align-items:center;gap:10px;margin-bottom:16px;}
  .card-head h2{font-size:15.5px;font-weight:600;margin:0;color:var(--text);letter-spacing:0;text-transform:none;}
  .card-body{padding-left:0;}
  label.field-label{display:block;font-size:12px;font-weight:600;color:var(--text-soft);margin:0 0 5px;}
  .row{display:flex;gap:10px;align-items:flex-end;margin-bottom:12px;flex-wrap:wrap;}
  .row:last-child{margin-bottom:0;}
  .field-group{display:flex;flex-direction:column;flex:1;min-width:200px;}
  .field-group.narrow{flex:none;min-width:64px;width:64px;}
  input[type=text],input[type=password]{
    padding:9px 11px;border:1px solid var(--border);border-radius:8px;font:inherit;
    background:var(--surface-2);color:var(--text);transition:border-color .12s,box-shadow .12s;
  }
  input[type=text]::placeholder,input[type=password]::placeholder{color:var(--text-faint);}
  input[type=text]:focus,input[type=password]:focus{outline:none;border-color:var(--text-soft);box-shadow:0 0 0 3px rgba(255,255,255,.06);}
  textarea{
    padding:9px 11px;border:1px solid var(--border);border-radius:8px;font:inherit;resize:vertical;
    background:var(--surface-2);color:var(--text);transition:border-color .12s,box-shadow .12s;
  }
  textarea::placeholder{color:var(--text-faint);font-style:italic;}
  textarea:focus{outline:none;border-color:var(--text-soft);box-shadow:0 0 0 3px rgba(255,255,255,.06);}
  .subsection{padding:14px 16px;background:var(--surface-2);border:1px solid var(--border);border-radius:10px;margin-bottom:14px;}
  .subsection:last-of-type{margin-bottom:0;}
  .subsection-title{font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--text-faint);margin-bottom:12px;}
  .city-picker{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:16px;}
  .city-picker-label{font-size:12.5px;font-weight:600;color:var(--text-soft);}
  .city-tabs{margin-bottom:0;}
  .tabs{display:flex;gap:4px;background:var(--surface-2);border:1px solid var(--border);border-radius:9px;padding:3px;margin-bottom:16px;width:fit-content;}
  .tab-btn,.city-btn{background:transparent;color:var(--text-soft);font-weight:600;font-size:13px;padding:6px 14px;border-radius:7px;}
  .tab-btn:hover,.city-btn:hover{background:transparent;color:var(--text);}
  .tab-btn.active{background:var(--border);color:var(--text);}
  .tab-btn.active:hover{background:var(--border);}
  .city-btn.active{background:var(--accent);color:var(--accent-ink);box-shadow:0 1px 2px rgba(0,0,0,.3);}
  .city-btn.active:hover{background:var(--accent);color:var(--accent-ink);}
  .hint{font-size:12px;color:var(--text-faint);margin-top:6px;}
  code{background:var(--accent-soft);color:var(--text);border-radius:4px;padding:1px 5px;font-size:11.5px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}
  .checkbox-row{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--text-soft);margin-bottom:12px;cursor:pointer;user-select:none;}
  .checkbox-row input[type=checkbox]{width:15px;height:15px;accent-color:var(--accent);cursor:pointer;}
  .list .field.list-error{background:var(--danger-soft);border-color:var(--danger-soft);}
  .list .field.list-error .k{color:var(--danger-dark);}
  .list .field.list-ok .status{color:var(--good);font-weight:700;}
  .secret{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--text);user-select:all;}
  .list .field.list-warn{background:var(--warn-soft);border-color:var(--warn);}
  .list .field.list-warn .status{color:var(--text);font-weight:700;}
  .presence{display:block;margin-top:3px;font-size:12px;}
  .presence.idle{color:var(--text-soft);font-weight:500;}
  .presence.active{color:var(--warn);}
  .list .field.list-error .presence{color:var(--danger-dark);font-weight:500;}
  button{
    padding:9px 16px;border:none;border-radius:8px;background:var(--accent);color:var(--accent-ink);
    font:inherit;font-weight:600;cursor:pointer;transition:background-color .12s,transform .05s;white-space:nowrap;
  }
  button:hover{background:var(--accent-dark);}
  button:active{transform:translateY(1px);}
  button:disabled{background:var(--border);color:var(--text-faint);cursor:not-allowed;transform:none;}
  button.danger{background:var(--danger);color:#2b0a0a;}
  button.danger:hover{background:var(--danger-dark);}
  button.ghost{background:transparent;color:var(--text);border:1px solid var(--border);padding:6px 12px;font-size:12.5px;}
  button.ghost:hover{background:var(--surface-2);border-color:var(--text-soft);}
  button.ghost:disabled{opacity:.45;cursor:not-allowed;background:transparent;border-color:var(--border);}
  .pill{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;font-weight:700;padding:4px 10px;border-radius:100px;letter-spacing:.01em;}
  .pill.good{background:var(--good-soft);color:var(--good);}
  .pill.warn{background:var(--warn-soft);color:var(--warn);}
  .pill.critical{background:var(--danger-soft);color:var(--danger-dark);}
  .pill.neutral{background:var(--surface-2);color:var(--text-soft);border:1px solid var(--border);}
  .result-card{background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:4px 14px;margin-top:12px;}
  .field{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px solid var(--border);font-size:13px;}
  .field:last-child{border-bottom:none;}
  .field .k{color:var(--text-soft);flex-shrink:0;}
  .field .v{font-weight:600;text-align:right;color:var(--text);min-width:0;overflow-wrap:anywhere;}
  .form-step-title{font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--text-faint);margin:4px 0 10px;}
  .form-step-title ~ .form-step-title{margin-top:18px;padding-top:14px;border-top:1px solid var(--border);}
  .neighbors-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;}
  .radius-label{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--text-soft);}
  .radius-label input{width:52px;padding:6px 8px;}
  .neighbor-meta{display:block;font-weight:400;font-size:12px;color:var(--text-faint);}
  .list .field.neighbor.picked{border-color:var(--good);background:var(--good-soft);}
  .form-submit{margin-top:18px;}
  .approvals{padding:12px 0 10px;}
  .approvals-title{font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--text-faint);margin-bottom:8px;}
  .approvals-note{font-size:12.5px;color:var(--danger-dark);overflow-wrap:anywhere;}
  .approval{padding:8px 10px;border:1px solid var(--border);border-radius:8px;margin-bottom:6px;font-size:12.5px;}
  .approval.current{border-color:var(--warn);background:var(--warn-soft);}
  .approval.quiet{display:none;}
  .approvals.show-all .approval.quiet{display:block;}
  .approval-head{display:flex;align-items:baseline;gap:8px;}
  .approval-pos{color:var(--text-faint);font-variant-numeric:tabular-nums;min-width:18px;}
  .approval-owner{font-weight:600;color:var(--text);flex:1;min-width:0;overflow-wrap:anywhere;}
  .approval-status{color:var(--text-soft);text-align:right;}
  .approval-meta{color:var(--text-faint);margin:2px 0 0 26px;}
  .approval-comment{margin:6px 0 0 26px;padding:6px 10px;border-left:2px solid var(--text-soft);background:var(--surface);border-radius:0 6px 6px 0;color:var(--text);white-space:pre-wrap;overflow-wrap:anywhere;}
  .approvals-toggle{margin-top:2px;}
  .list{display:flex;flex-direction:column;gap:6px;margin-top:10px;}
  .list .field{background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:9px 12px;align-items:center;}
  .list .field .k{font-weight:500;color:var(--text);flex-shrink:1;min-width:0;overflow-wrap:anywhere;}
  pre{background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:11px 13px;font-size:12px;overflow-x:auto;white-space:pre-wrap;word-break:break-word;color:var(--text);}
  .msg{font-size:12.5px;margin-top:10px;color:var(--text-soft);overflow-wrap:anywhere;}
  .msg:empty{margin-top:0;}
  .msg.error{color:var(--danger-dark);}
  .msg.info{color:var(--text-soft);}

  /* Two-column layouts: stacked by default, side by side once there is room.
     Left (.split-main) is what the operator fills in, right (.split-side) is
     what comes back — ticket details, a plan, execution results. */
  .split{display:grid;grid-template-columns:minmax(0,1fr);gap:0 28px;align-items:start;}
  .split-side .result-card{margin-top:12px;}
  .split-side > .row{margin-top:12px;}
  .split-placeholder{display:none;}
  .login-column{display:flex;flex-direction:column;gap:16px;}
  .code-banner{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:16px;padding:10px 14px;border:1px solid var(--warn);background:var(--warn-soft);border-radius:10px;color:var(--text);font-weight:600;}
  .code-box{margin:12px 0 0;padding:12px;border:1px solid var(--warn);background:var(--warn-soft);border-radius:10px;}
  .code-box input{font-size:18px;letter-spacing:.2em;font-variant-numeric:tabular-nums;}
  .screenshot-btn{margin-top:10px;}
  .crm-screenshot{display:block;width:100%;margin-top:10px;border:1px solid var(--border);border-radius:8px;}
  .webitel-city{padding:12px 0;border-bottom:1px solid var(--border);}
  .webitel-city:first-child{padding-top:0;}
  .webitel-city [data-role="form"]{margin-bottom:12px;}
  .webitel-city-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:4px;}
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
      display:flex;align-items:center;justify-content:center;text-align:center;min-height:180px;padding:20px 28px;
      border:1px dashed var(--border);border-radius:10px;color:var(--text-faint);font-size:13px;
    }
    .steps.cols-2{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);align-items:start;}
  }

  /* Phones and narrow windows. */
  @media (max-width:640px){
    .topnav-inner{flex-wrap:wrap;height:auto;gap:8px 12px;padding-top:10px;padding-bottom:8px;}
    .topnav-brand{flex:1;}
    .topnav-links{order:3;flex:1 0 100%;overflow-x:auto;scrollbar-width:none;}
    .topnav-links::-webkit-scrollbar{display:none;}
    .nav-link{flex-shrink:0;padding:8px 12px;}
    .nav-account-btn{padding:6px 10px;font-size:12.5px;}
    .page{padding-top:16px;}
    .top-sub{margin-bottom:16px;}
    .card{padding:16px 14px;}
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
<nav class="topnav">
  <div class="topnav-inner">
    <span class="topnav-brand">Консоль оператора</span>
    <div class="topnav-links">
      <button type="button" class="nav-link active" data-section="ticket">Заявка</button>
      <button type="button" class="nav-link" data-section="copy">Копировать</button>
      <button type="button" class="nav-link" data-section="create">Создать</button>
      <button type="button" class="nav-link" data-section="delete">Удалить</button>
    </div>
    <button type="button" class="nav-account-btn" data-section="account"><span class="status-dot" id="navLoginDot" title="CRM: не выполнен вход"></span>Вход и настройки</button>
  </div>
</nav>
<div class="page">
  <div id="crmCodeBanner" class="code-banner" hidden>CRM просит код подтверждения. <button type="button" id="btnGoToCode">Ввести код</button></div>
  <div class="sub top-sub">Локальный инструмент. Только на этом компьютере. Каждое реальное изменение требует отдельного подтверждения.</div>

  <div class="city-picker" id="cityPicker">
    <span class="city-picker-label">Город:</span>
    <div class="tabs city-tabs">
      <button type="button" class="city-btn active" data-city="almaty">Алматы</button>
      <button type="button" class="city-btn" data-city="astana">Астана</button>
    </div>
  </div>

  <section class="app-section" data-section-panel="account" hidden>
  <div class="steps cols-2">

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
            <label class="field-label" for="templateExt">Логин пользователя-образца</label>
            <input type="text" id="templateExt" placeholder="напр. 10078">
          </div>
        </div>
        <div class="row">
          <div class="field-group">
            <label class="field-label" for="targetExt">Кому скопировать (можно несколько через запятую)</label>
            <input type="text" id="targetExt" placeholder="напр. 10079, 10080">
          </div>
        </div>
        <label class="checkbox-row">
          <input type="checkbox" id="resetPasswordCheck">
          Сбросить пароль
        </label>
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

        <div class="row form-submit">
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
            <label class="field-label" for="deleteExt">Логин/добавочный (можно несколько через запятую)</label>
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

</div>

<script>
async function api(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || ('HTTP ' + res.status));
    err.data = data;
    throw err;
  }
  return data;
}

const navButtons = document.querySelectorAll('.nav-link, .nav-account-btn');
const CITY_PICKER_SECTIONS = ['copy', 'create', 'delete'];

navButtons.forEach(btn => {
  btn.addEventListener('click', () => {
    navButtons.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const target = btn.dataset.section;
    document.querySelectorAll('.app-section').forEach(panel => {
      panel.hidden = panel.dataset.sectionPanel !== target;
    });
    document.getElementById('cityPicker').hidden = !CITY_PICKER_SECTIONS.includes(target);
  });
});

document.getElementById('cityPicker').hidden = !CITY_PICKER_SECTIONS.includes('ticket');

let selectedCity = 'almaty';

// Example numbers in the Webitel forms follow the selected city, so an
// Astana operator isn't shown Almaty's 100xx extensions as the pattern.
const CITY_PLACEHOLDERS = {
  almaty: {
    templateExt: 'напр. 10078', targetExt: 'напр. 10079, 10080',
    singleNewLogin: 'напр. 10087', singleTemplateExt: 'напр. 10078',
    createTemplateExt: 'напр. 10078', createBulkList: '10087,Иван Иванов\\n10097,Мария Петрова',
    deleteExt: 'напр. 10087, 10097',
  },
  astana: {
    templateExt: 'напр. 28070', targetExt: 'напр. 28071, 28072',
    singleNewLogin: 'напр. 28071', singleTemplateExt: 'напр. 28070',
    createTemplateExt: 'напр. 28070', createBulkList: '28071,Иван Иванов\\n28075,Мария Петрова',
    deleteExt: 'напр. 28071, 28075',
  },
};

function applyCityPlaceholders(city) {
  for (const [id, text] of Object.entries(CITY_PLACEHOLDERS[city] || {})) {
    const el = document.getElementById(id);
    if (el) el.placeholder = text;
  }
}

document.querySelectorAll('.city-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.city-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    selectedCity = btn.dataset.city;
    applyCityPlaceholders(selectedCity);
  });
});
applyCityPlaceholders(selectedCity);

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
    if (!confirm(`Удалить сохранённый вход ${login} для Webitel ${WEBITEL_CITY_NAMES[city]}? Логин, пароль и сохранённая сессия этого города сотрутся с этого компьютера.`)) return;
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
  if (!confirm(`Удалить сохранённый вход ${savedCrmLogin} с этого компьютера? Логин и пароль CRM (и Visa Owner ID, если был) сотрутся из info.env. Текущий вход продолжит работать, пока открыто окно CRM.`)) return;
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
      `<div class="field"><span class="k">${t.name}</span>` +
      `<button type="button" class="open-pending ghost" data-url="${t.url}">Открыть</button></div>`
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
      <div class="field"><span class="k">Статус</span><span class="v">${data.status}</span></div>
      <div class="field"><span class="k">ФИО сотрудника</span><span class="v">${data.employee_full_name}</span></div>
      <div class="field"><span class="k">Учётная запись</span><span class="v">${data.employee_account}</span></div>
      <div class="field"><span class="k">Информационная система</span><span class="v">${data.information_system}</span></div>
      <div class="field"><span class="k">Комментарии</span><span class="v">${data.comment || '—'}</span></div>
      <div class="field"><span class="k">Логин(ы) Webitel из комментариев</span><span class="v">${data.webitel_logins.join(', ') || 'не найдены — укажите вручную'}</span></div>
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

document.getElementById('btnPlan').addEventListener('click', async () => {
  const template_extension = document.getElementById('templateExt').value.trim();
  const targets = document.getElementById('targetExt').value
    .split(',')
    .map(x => x.trim())
    .filter(x => x.length > 0);
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
  if (targets.length === 0) {
    msgEl.className = 'msg error';
    msgEl.textContent = 'Укажите, кому скопировать';
    return;
  }

  msgEl.className = 'msg info';
  msgEl.textContent = `Строю план для ${targets.length} — только чтение...`;

  for (const target_extension of targets) {
    try {
      const plan = await api('/api/webitel/plan', {
        template_extension, target_extension, city: selectedCity,
        reset_password: document.getElementById('resetPasswordCheck').checked,
      });
      currentPlans.push({ok: true, plan});
    } catch (e) {
      currentPlans.push({ok: false, target: target_extension, error: e.message});
    }
  }

  const okCount = currentPlans.filter(r => r.ok).length;
  msgEl.textContent = `Готово: ${okCount} из ${targets.length} — план построен. Ошибки ниже, если есть.`;
  listEl.innerHTML = currentPlans.map(r => {
    if (r.ok) {
      const roleNames = r.plan.roles.map(x => x.name).join(', ') || '—';
      const licenseNames = r.plan.license.map(x => x.prod || x.id).join(', ') || '—';
      const passwordNote = r.plan.reset_password ? ' · пароль: будет сброшен' : ' · пароль: не меняется';
      return `<div class="field list-ok"><span class="k">${r.plan.target_name} (${r.plan.target_extension})</span>` +
        `<span class="v status">✓ роли: ${roleNames} · лицензии: ${licenseNames} · группа: ${r.plan.group || '—'}${passwordNote}</span></div>`;
    }
    return `<div class="field list-error"><span class="k">${r.target}</span><span class="v">${r.error}</span></div>`;
  }).join('');

  if (okCount > 0) confirmRow.hidden = false;
});

document.getElementById('btnExecute').addEventListener('click', async () => {
  const toApply = currentPlans.filter(r => r.ok);
  if (toApply.length === 0) return;
  const msgEl = document.getElementById('executeMsg');
  const btn = document.getElementById('btnExecute');
  const names = toApply.map(r => r.plan.target_name + ' (' + r.plan.target_extension + ')').join(', ');
  const resetNote = toApply.some(r => r.plan.reset_password) ? '\\n\\nПароли будут СБРОШЕНЫ на новые.' : '';
  if (!confirm(`Точно применить план к ${toApply.length} пользователям? ${names}${resetNote}`)) return;
  btn.disabled = true;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Применяю...';
  const results = [];
  for (const r of toApply) {
    try {
      const result = await api('/api/webitel/execute', {plan: r.plan});
      const passwordNote = result.new_password ? ` · новый пароль: <b class="secret">${esc(result.new_password)}</b>` : '';
      results.push(`<div class="field list-ok"><span class="k">${r.plan.target_name} (${r.plan.target_extension})</span>` +
        `<span class="v status">✓ обновлён${passwordNote}</span></div>`);
    } catch (e) {
      results.push(`<div class="field list-error"><span class="k">${r.plan.target_name} (${r.plan.target_extension})</span>` +
        `<span class="v">${e.message}</span></div>`);
    }
  }
  msgEl.className = 'msg info';
  msgEl.innerHTML = 'Готово.<div class="list">' + results.join('') + '</div>';
  btn.disabled = false;
});

let currentDeletePlans = [];  // [{ok: true, plan} | {ok: false, extension, error}]

document.getElementById('btnDeletePlan').addEventListener('click', async () => {
  const extensions = document.getElementById('deleteExt').value
    .split(',')
    .map(x => x.trim())
    .filter(x => x.length > 0);
  const msgEl = document.getElementById('deletePlanMsg');
  const listEl = document.getElementById('deletePlanList');
  const confirmRow = document.getElementById('deleteConfirmRow');
  listEl.innerHTML = '';
  confirmRow.hidden = true;
  currentDeletePlans = [];

  if (extensions.length === 0) {
    msgEl.className = 'msg error';
    msgEl.textContent = 'Укажите хотя бы один логин/добавочный';
    return;
  }

  msgEl.className = 'msg info';
  msgEl.textContent = `Ищу ${extensions.length} — только чтение...`;

  for (const extension of extensions) {
    try {
      const plan = await api('/api/webitel/delete_plan', {extension, city: selectedCity});
      currentDeletePlans.push({ok: true, plan});
    } catch (e) {
      currentDeletePlans.push({ok: false, extension, error: e.message});
    }
  }

  const deletable = currentDeletePlans.filter(r => r.ok && !r.plan.in_call);
  const inCall = currentDeletePlans.filter(r => r.ok && r.plan.in_call);
  const notFound = currentDeletePlans.filter(r => !r.ok);
  msgEl.textContent = `Будет удалено: ${deletable.length}` +
    (inCall.length ? ` · пропущено (идёт разговор): ${inCall.length}` : '') +
    (notFound.length ? ` · не найдено / ошибка: ${notFound.length}` : '');
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
  }).join('');

  document.getElementById('btnDeleteExecute').textContent = `Подтвердить и удалить (${deletable.length})`;
  confirmRow.hidden = deletable.length === 0;
});

document.getElementById('btnDeleteExecute').addEventListener('click', async () => {
  const toDelete = currentDeletePlans.filter(r => r.ok && !r.plan.in_call);
  if (toDelete.length === 0) return;
  const msgEl = document.getElementById('deleteExecuteMsg');
  const btn = document.getElementById('btnDeleteExecute');
  const label = r => `${r.plan.name} (${r.plan.extension})`;
  const online = toDelete.filter(r => (r.plan.presence || []).length > 0);
  const skipped = currentDeletePlans.filter(r => r.ok && r.plan.in_call);
  const text = `УДАЛИТЬ НАВСЕГДА ${toDelete.length}:\\n${toDelete.map(label).join('\\n')}` +
    (online.length ? `\\n\\nСейчас в сети — их сессии будут завершены:\\n${online.map(r => label(r) + ' — ' + r.plan.presence.join(', ')).join('\\n')}` : '') +
    (skipped.length ? `\\n\\nНЕ будут удалены (идёт разговор):\\n${skipped.map(label).join('\\n')}` : '');
  if (!confirm(text)) return;
  btn.disabled = true;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Удаляю...';
  const results = [];
  for (const r of toDelete) {
    try {
      const result = await api('/api/webitel/delete_execute', {plan: r.plan});
      const deviceNote = result.deleted_device_ids.length > 0 ? ` (устройств удалено: ${result.deleted_device_ids.length})` : '';
      results.push(`<div class="field list-ok"><span class="k">${result.name} (${result.extension})</span>` +
        `<span class="v status">✓ удалён${deviceNote}</span></div>`);
    } catch (e) {
      results.push(`<div class="field list-error"><span class="k">${r.plan.name} (${r.plan.extension})</span>` +
        `<span class="v">${e.message}</span></div>`);
    }
  }
  msgEl.className = 'msg info';
  msgEl.innerHTML = 'Готово.<div class="list">' + results.join('') + '</div>';
  btn.disabled = false;
});

document.querySelectorAll('.tab-btn').forEach(tabBtn => {
  tabBtn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    tabBtn.classList.add('active');
    const target = tabBtn.dataset.tab;
    document.querySelectorAll('.tab-panel').forEach(panel => {
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
    const plan = await api('/api/webitel/create_plan', {template_extension, new_username, new_name, new_extension, city: selectedCity});
    currentCreatePlanSingle = plan;
    msgEl.textContent = '';
    const roleNames = plan.roles.map(r => r.name).join(', ') || '—';
    const licenseIds = plan.license.map(l => l.id).join(', ') || '—';
    cardEl.innerHTML = `
      <div class="field"><span class="k">Образец</span><span class="v">${plan.template_name} (${plan.template_extension})</span></div>
      <div class="field"><span class="k">Новый пользователь</span><span class="v">${plan.new_name} (${plan.new_username})</span></div>
      <div class="field"><span class="k">Roles → будет</span><span class="v">${roleNames}</span></div>
      <div class="field"><span class="k">License → будет</span><span class="v">${licenseIds}</span></div>
      <div class="field"><span class="k">Group → будет</span><span class="v">${plan.group || '—'}</span></div>
      <div class="field"><span class="k">Устройство (SIP)</span><span class="v">${plan.new_extension} — создастся вместе с пользователем</span></div>
      <div class="field"><span class="k">Пароль</span><span class="v">сгенерируется при создании, постоянный (без принудительной смены)</span></div>
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
    msgEl.className = 'msg info';
    msgEl.innerHTML = `Готово. ${result.name} (${result.extension}) создан. Временный пароль: <b>${result.temporary_password}</b>`;
  } catch (e) {
    msgEl.textContent = 'Ошибка: ' + e.message;
    msgEl.className = 'msg error';
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('btnNeighbors').addEventListener('click', async () => {
  // Searches around the new user's own login — no second number to type.
  const extension = document.getElementById('singleNewLogin').value.trim();
  const msgEl = document.getElementById('neighborsMsg');
  const listEl = document.getElementById('neighborsList');
  listEl.innerHTML = '';
  if (!extension) {
    msgEl.className = 'msg error';
    msgEl.textContent = 'Сначала укажите логин нового пользователя (шаг 1) — соседей ищу вокруг него';
    return;
  }
  const radius = parseInt(document.getElementById('neighborsRadius').value, 10) || 2;
  msgEl.className = 'msg info';
  msgEl.textContent = 'Ищу соседние номера (только чтение)...';
  try {
    const data = await api('/api/webitel/neighbors', {extension, radius, city: selectedCity});
    if (data.neighbors.length === 0) {
      msgEl.textContent = 'Соседних номеров не найдено — выберите образец вручную ниже.';
      return;
    }
    msgEl.textContent = `Соседи ${extension} — выберите образец (переговорки и «Vacant» лучше пропустить):`;
    listEl.innerHTML = data.neighbors.map(n =>
      `<div class="field neighbor" data-ext="${esc(n.extension)}"><span class="k">${esc(n.extension)} — ${esc(n.name)}` +
      `<span class="neighbor-meta">${esc(n.group || '')}${n.roles && n.roles.length ? ' · ' + esc(n.roles.join(', ')) : ''}</span></span>` +
      `<button type="button" class="pick-neighbor ghost" data-ext="${esc(n.extension)}">Взять</button></div>`
    ).join('');
    const markPicked = () => {
      const picked = document.getElementById('singleTemplateExt').value.trim();
      listEl.querySelectorAll('.neighbor').forEach(row => {
        const isPicked = row.dataset.ext === picked;
        row.classList.toggle('picked', isPicked);
        row.querySelector('.pick-neighbor').textContent = isPicked ? '✓ Образец' : 'Взять';
      });
    };
    listEl.querySelectorAll('.pick-neighbor').forEach(btn => {
      btn.addEventListener('click', () => {
        document.getElementById('singleTemplateExt').value = btn.dataset.ext;
        markPicked();
      });
    });
    markPicked();
  } catch (e) {
    msgEl.className = 'msg error';
    msgEl.textContent = 'Ошибка: ' + e.message;
  }
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

  for (const entry of entries) {
    try {
      const plan = await api('/api/webitel/create_plan', {
        template_extension, new_username: entry.login, new_name: entry.name, new_extension: entry.login, city: selectedCity,
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
        `<span class="k">${r.plan.new_name} (${r.plan.new_username})</span>` +
        `<span class="v status">✓ роли: ${roleNames} · лицензии: ${licenseNames} · группа: ${r.plan.group || '—'}</span></div>`;
    }
    return `<div class="field list-error"><span class="k">${r.login}</span><span class="v">${r.error}</span></div>`;
  }).join('');

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
  msgEl.textContent = 'Создаю...';
  const results = [];
  for (const r of toCreate) {
    try {
      const result = await api('/api/webitel/create_execute', {plan: r.plan});
      results.push(`<div class="field list-ok"><span class="k">${r.plan.new_name} (${r.plan.new_username})</span>` +
        `<span class="v status">✓ пароль: ${result.temporary_password}</span></div>`);
    } catch (e) {
      results.push(`<div class="field list-error"><span class="k">${r.plan.new_name} (${r.plan.new_username})</span>` +
        `<span class="v">${e.message}</span></div>`);
    }
  }
  msgEl.className = 'msg info';
  msgEl.innerHTML = 'Готово.<div class="list">' + results.join('') + '</div>';
  btn.disabled = false;
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

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # keep stdout free of PII-bearing request logs; see artifacts.py's same posture

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._send_html(_INDEX_HTML)
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
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

        try:
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
                _save_env_updates(INFO_ENV_PATH, updates)
                if "crm_visa_owner_id" in body and not updates["CRM_VISA_OWNER_ID"]:
                    # Unlike the passwords, this field is pre-filled with the
                    # saved value, so an empty one means "use my roles".
                    _remove_env_keys(INFO_ENV_PATH, ("CRM_VISA_OWNER_ID",))
                self._send_json(200, {"saved": True})
                return

            if self.path == "/api/crm/credentials":
                # Which CRM login is remembered — never the password itself.
                env = _load_env_file(INFO_ENV_PATH)
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
                _remove_env_keys(INFO_ENV_PATH, ("CRM_LOGIN", "CRM_PASSWORD", "CRM_VISA_OWNER_ID"))
                self._send_json(200, {"forgotten": True})
                return

            if self.path == "/api/crm/login":
                try:
                    login, password = _resolve_crm_login(body)
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                _get_crm_session().start_login(login=login, password=password)
                self._send_json(200, {"started": True, "login": login})
                return

            if self.path == "/api/crm/login/status":
                session = _get_crm_session()
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
                _get_crm_session().submit_code(code)
                self._send_json(200, {"submitted": True})
                return

            if self.path == "/api/crm/login/screenshot":
                png = _get_crm_session().screenshot_png()
                self._send_json(200, {"png": base64.b64encode(png).decode("ascii")})
                return

            if self.path == "/api/crm/debug/login_check":  # temporary diagnostic
                self._send_json(200, _get_crm_session().debug_login_check())
                return

            if self.path == "/api/crm/pending_tickets":
                # Empty CRM_VISA_OWNER_ID = the logged-in user plus all their roles.
                visa_owner_id = _load_env_file(INFO_ENV_PATH).get("CRM_VISA_OWNER_ID") or None
                tickets = _get_crm_session().list_pending_tickets(visa_owner_id)
                self._send_json(200, {"tickets": tickets})
                return

            if self.path == "/api/crm/ticket":
                url = body.get("url", "").strip()
                if not url:
                    self._send_json(400, {"error": "url is required"})
                    return
                session = _get_crm_session()
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

            if self.path == "/api/webitel/session_status":
                city = str(body.get("city", "almaty")).strip() or "almaty"
                if city not in WEBITEL_STORAGE_STATE_PATHS:
                    self._send_json(400, {"error": f"Unknown city {city!r}"})
                    return
                self._send_json(200, _webitel_session_state(city))
                return

            if self.path in ("/api/webitel/credentials/save", "/api/webitel/credentials/forget"):
                city = str(body.get("city", "")).strip()
                if city not in WEBITEL_STORAGE_STATE_PATHS:
                    self._send_json(400, {"error": f"Unknown city {city!r}"})
                    return
                if self.path.endswith("/forget"):
                    _forget_webitel_credentials(city)
                    self._send_json(200, {"forgotten": True})
                    return
                try:
                    _save_webitel_credentials(city, str(body.get("login") or "").strip(), str(body.get("password") or ""))
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
                png = latest_screenshot(WEBITEL_STORAGE_STATE_PATHS[city], city)
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
                    self._send_json(200, _start_webitel_login(city))
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
                actions = _get_webitel_actions(city)
                plan = actions.build_plan(template_extension=template_extension, target_extension=target_extension)
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
                actions = _get_webitel_actions(plan.get("city", "almaty") or "almaty")
                result = actions.execute_plan(plan)
                self._send_json(200, result)
                return

            if self.path == "/api/webitel/delete_plan":
                extension = body.get("extension", "").strip()
                city = body.get("city", "almaty").strip() or "almaty"
                if not extension:
                    self._send_json(400, {"error": "extension is required"})
                    return
                actions = _get_webitel_actions(city)
                plan = actions.build_delete_plan(extension)
                plan["city"] = city
                self._send_json(200, plan)
                return

            if self.path == "/api/webitel/delete_execute":
                plan = body.get("plan")
                if not plan:
                    self._send_json(400, {"error": "plan is required"})
                    return
                actions = _get_webitel_actions(plan.get("city", "almaty") or "almaty")
                result = actions.execute_delete_plan(plan)
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
                actions = _get_webitel_actions(city)
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
                actions = _get_webitel_actions(city)
                plan = actions.build_create_plan(
                    template_extension=template_extension,
                    new_username=new_username,
                    new_name=new_name,
                    new_extension=new_extension,
                )
                plan["city"] = city
                self._send_json(200, plan)
                return

            if self.path == "/api/webitel/create_execute":
                plan = body.get("plan")
                if not plan:
                    self._send_json(400, {"error": "plan is required"})
                    return
                actions = _get_webitel_actions(plan.get("city", "almaty") or "almaty")
                result = actions.execute_create_plan(plan)
                self._send_json(200, result)
                return

            self._send_json(404, {"error": "not found"})
        except CrmSessionExpired as exc:
            # The page re-logs in by itself on this flag (remembered login).
            self._send_json(401, {"error": str(exc), "crm_session_expired": True})
        except Exception as exc:  # noqa: BLE001 — surface to the operator's browser, not a crash
            self._send_json(500, {"error": _operator_error_message(exc)})


def run(port: int = 8765) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Console running at http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
