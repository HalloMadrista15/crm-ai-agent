"""Who may open the shared console: sign-in with an AD account, and the list
of accounts let in (CONSOLE_AD_* in info.env; webapp/server.py puts it
together).

The password is checked by the server the console runs on, which is joined
to the domain (PAM -> SSSD -> Kerberos, verified with the machine's own key),
so the console needs neither root nor a service account, and keeps no
password. It is checked in a process of its own (this file run as a script):
PAM modules are native code, and one that hangs or crashes must not take the
console with it.

Only accounts on the list are ever asked about, and only a few times in a
row: the console can't be used to guess passwords of the whole domain, nor
to lock colleagues' accounts.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

SESSION_SECONDS = 12 * 3600
# Below what AD usually locks an account at.
MAX_FAILURES = 4
FAILURE_WINDOW_SECONDS = 30 * 60
_CHECK_TIMEOUT_SECONDS = 40
_MAX_PASSWORD_LENGTH = 256

_LOGIN_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_TOKEN_RE = re.compile(r"[0-9a-f]{64}")


def normalize_login(raw: str, domain: str) -> str:
    """"M.Muratbay", "MK\\m.muratbay" and "m.muratbay@mk.int" are the same
    account of ``domain``: "m.muratbay". "" for anything else."""
    login = raw.strip().lower()
    domain = domain.lower()
    for prefix in (domain + "\\", domain.split(".")[0] + "\\"):
        login = login.removeprefix(prefix)
    login = login.removesuffix("@" + domain)
    return login if _LOGIN_RE.fullmatch(login) else ""


def parse_logins(raw: str, domain: str) -> list[str]:
    """CONSOLE_AD_USERS / CONSOLE_AD_ADMINS: accounts separated by commas or
    spaces. A typo raises ValueError rather than being skipped."""
    logins: list[str] = []
    for item in re.split(r"[\s,;]+", raw.strip()):
        if not item:
            continue
        login = normalize_login(item, domain)
        if not login:
            raise ValueError(f"не логин AD: {item!r}")
        if login not in logins:
            logins.append(login)
    return logins


def account_name(login: str, domain: str) -> str | None:
    """The person's name in AD ("" when AD has none), None when there is no
    such account. Asks the server's own account database (SSSD)."""
    try:
        import pwd
    except ImportError:  # Windows: no such database
        return None
    try:
        return pwd.getpwnam(f"{login}@{domain}").pw_gecos.split(",")[0].strip()
    except KeyError:
        return None


def check_problem() -> str:
    """Why passwords can't be checked on this machine, or ""."""
    if not sys.platform.startswith("linux"):
        return "вход по AD работает только на сервере Linux в домене"
    try:
        ctypes.CDLL("libpam.so.0")
    except OSError as exc:
        return f"нет libpam: {exc}"
    return ""


_check_lock = threading.Lock()


def check_password(login: str, domain: str, password: str) -> str:
    """"" when it is this account's password, else why not: "wrong" (also a
    disabled or locked account), "expired" or "unavailable"."""
    if not password or "\0" in password or len(password) > _MAX_PASSWORD_LENGTH:
        return "wrong"
    request = json.dumps({"user": f"{login}@{domain}", "password": password}).encode("utf-8")
    with _check_lock:  # one at a time: nobody can flood the server with checks
        try:
            done = subprocess.run(
                [sys.executable, "-I", str(Path(__file__).resolve())],
                input=request, capture_output=True, timeout=_CHECK_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return "unavailable"
    return {_OK: "", _WRONG: "wrong", _EXPIRED: "expired"}.get(done.returncode, "unavailable")


class Throttle:
    """Counts recent failures by key (an account, an address)."""

    def __init__(self, limit: int = MAX_FAILURES, window: float = FAILURE_WINDOW_SECONDS) -> None:
        self._limit, self._window = limit, window
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def wait_seconds(self, key: str) -> float:
        """0 when ``key`` may try (again)."""
        with self._lock:
            recent = self._recent(key)
            return recent[0] + self._window - time.time() if len(recent) >= self._limit else 0

    def failed(self, key: str) -> None:
        with self._lock:
            self._failures[key] = [*self._recent(key), time.time()][-self._limit:]

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def _recent(self, key: str) -> list[float]:
        recent = [at for at in self._failures.get(key, []) if time.time() - at < self._window]
        if not recent:
            self._failures.pop(key, None)
        return recent


class SignIns:
    """Which browser is signed in as whom. The cookie itself is never on disk,
    only its hash; a restart of the console keeps everyone signed in."""

    def __init__(self, path: Path, lifetime: float = SESSION_SECONDS) -> None:
        self._path, self._lifetime = path, lifetime
        self._lock = threading.Lock()
        self._known: dict[str, dict] | None = None

    def start(self, login: str) -> str:
        token = secrets.token_hex(32)
        with self._lock:
            known = self._load()
            known[self._key(token)] = {"login": login, "until": time.time() + self._lifetime}
            self._save()
        return token

    def login_of(self, token: str) -> str:
        if not _TOKEN_RE.fullmatch(token):
            return ""
        with self._lock:
            entry = self._load().get(self._key(token))
        return entry["login"] if entry and time.time() < entry["until"] else ""

    def end(self, token: str) -> None:
        with self._lock:
            if self._load().pop(self._key(token), None):
                self._save()

    def end_all(self, login: str) -> None:
        with self._lock:
            known = self._load()
            for key in [key for key, entry in known.items() if entry["login"] == login]:
                del known[key]
            self._save()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _load(self) -> dict[str, dict]:
        if self._known is None:
            try:
                stored = json.loads(self._path.read_text(encoding="utf-8"))
                self._known = {
                    key: {"login": str(entry["login"]), "until": float(entry["until"])} for key, entry in stored.items()
                }
            except (OSError, ValueError, AttributeError, KeyError, TypeError):
                self._known = {}  # none yet, or damaged: everyone signs in again
        return self._known

    def _save(self) -> None:
        now = time.time()
        self._known = {key: entry for key, entry in self._load().items() if entry["until"] > now}
        replace_file(self._path, json.dumps(self._known))


def replace_file(path: Path, text: str) -> None:
    """Whole or not at all, and readable by its owner only."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
        file.write(text)
    os.replace(temporary, path)


def set_env_line(path: Path, key: str, value: str) -> None:
    """Changes one line of info.env and leaves the rest, comments included."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    mine = [i for i, line in enumerate(lines) if line.split("=", 1)[0].strip() == key and "=" in line]
    for i in reversed(mine[1:]):
        del lines[i]
    if mine:
        lines[mine[0]] = f"{key}={value}"
    else:
        lines.append(f"{key}={value}")
    replace_file(path, "\n".join(lines) + "\n")


# --- Run as a script: one password check, the answer is the exit code ---

_OK, _WRONG, _EXPIRED, _UNAVAILABLE = 0, 11, 12, 13

_PAM_SERVICE = b"sshd"  # the server's own domain sign-in
_PAM_SUCCESS, _PAM_BUF_ERR, _PAM_NEW_AUTHTOK_REQD, _PAM_ACCT_EXPIRED = 0, 5, 12, 13
_PAM_PROMPT_ECHO_OFF = 1
_PAM_SILENT, _PAM_DISALLOW_NULL_AUTHTOK = 0x8000, 0x1


def _ask_pam(user: str, password: str) -> int:
    libpam, libc = ctypes.CDLL("libpam.so.0"), ctypes.CDLL(None)
    libc.calloc.restype, libc.calloc.argtypes = ctypes.c_void_p, [ctypes.c_size_t, ctypes.c_size_t]
    libc.strdup.restype, libc.strdup.argtypes = ctypes.c_void_p, [ctypes.c_char_p]

    class Message(ctypes.Structure):
        _fields_ = [("style", ctypes.c_int), ("text", ctypes.c_char_p)]

    class Response(ctypes.Structure):
        _fields_ = [("text", ctypes.c_void_p), ("code", ctypes.c_int)]

    conversation_type = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.POINTER(Message)),
        ctypes.POINTER(ctypes.POINTER(Response)), ctypes.c_void_p,
    )

    class Conversation(ctypes.Structure):
        _fields_ = [("function", conversation_type), ("data", ctypes.c_void_p)]

    secret = password.encode("utf-8")

    @conversation_type
    def converse(count, messages, responses, _data):  # PAM frees what this allocates
        memory = libc.calloc(count, ctypes.sizeof(Response))
        if not memory:
            return _PAM_BUF_ERR
        answers = ctypes.cast(memory, ctypes.POINTER(Response))
        for i in range(count):
            if messages[i].contents.style == _PAM_PROMPT_ECHO_OFF:
                answers[i].text = libc.strdup(secret)
        responses[0] = answers
        return _PAM_SUCCESS

    libpam.pam_start.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(Conversation), ctypes.POINTER(ctypes.c_void_p),
    ]
    for name in ("pam_authenticate", "pam_acct_mgmt", "pam_end"):
        getattr(libpam, name).argtypes = [ctypes.c_void_p, ctypes.c_int]

    handle, conversation = ctypes.c_void_p(), Conversation(converse, None)
    if libpam.pam_start(_PAM_SERVICE, user.encode("utf-8"), ctypes.byref(conversation), ctypes.byref(handle)) != _PAM_SUCCESS:
        return _UNAVAILABLE
    result = libpam.pam_authenticate(handle, _PAM_SILENT | _PAM_DISALLOW_NULL_AUTHTOK)
    answer = _OK if result == _PAM_SUCCESS else _WRONG
    if answer == _OK:
        # The password is right; may it still be used? Whether this account may
        # log on to the server itself doesn't matter: the list decides here.
        result = libpam.pam_acct_mgmt(handle, _PAM_SILENT)
        answer = {_PAM_NEW_AUTHTOK_REQD: _EXPIRED, _PAM_ACCT_EXPIRED: _WRONG}.get(result, _OK)
    libpam.pam_end(handle, result)
    return answer


if __name__ == "__main__":
    asked = json.loads(sys.stdin.buffer.read())
    sys.exit(_ask_pam(asked["user"], asked["password"]))
