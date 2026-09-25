"""Application settings loaded from environment variables.

No secrets are hardcoded or committed. See ``.env.example`` at the repo root
for the full list of expected variables; real values live in a local
untracked ``.env`` or in a secret manager, never in source control.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    database_path: str
    crm_base_url: str | None
    webitel_base_url: str | None
    telegram_bot_token: str | None
    telegram_webhook_secret: str | None
    llm_api_key: str | None
    policy_profile: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_path=os.environ.get("CRM_AGENT_DB_PATH", "crm_ai_agent.sqlite3"),
            crm_base_url=os.environ.get("CRM_BASE_URL"),
            webitel_base_url=os.environ.get("WEBITEL_BASE_URL"),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
            telegram_webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET"),
            llm_api_key=os.environ.get("LLM_API_KEY"),
            policy_profile=os.environ.get("CRM_AGENT_POLICY_PROFILE", "mvp"),
        )
