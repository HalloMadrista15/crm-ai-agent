"""Checks, on the server the shared console runs on, that it can check AD
passwords — before CONSOLE_AD_DOMAIN is set. Asks for the password, sends it
only to the server's own domain sign-in and keeps it nowhere.

Usage:
    .venv/bin/python scripts/check_ad_login.py m.ivanov [mk.int]
"""

from __future__ import annotations

import getpass
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from crm_ai_agent.webapp import console_access  # noqa: E402

ANSWERS = {
    "": "пароль верный, вход по AD работает",
    "wrong": "пароль не подошёл (или учётка отключена)",
    "expired": "пароль верный, но истёк",
    "unavailable": "проверить не удалось",
}


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    domain = sys.argv[2] if len(sys.argv) > 2 else "mk.int"
    login = console_access.normalize_login(sys.argv[1], domain)
    problem = console_access.check_problem() or ("" if login else "не логин AD")
    if problem:
        raise SystemExit(problem)
    print(f"{login}@{domain}:", console_access.account_name(login, domain) or "в AD не найден")
    password = getpass.getpass("Пароль AD (не показывается): ")
    started = time.monotonic()
    answer = console_access.check_password(login, domain, password)
    print(f"{ANSWERS[answer]}, проверка заняла {time.monotonic() - started:.1f} с")


if __name__ == "__main__":
    main()
