# crm-ai-agent

Human-in-the-loop AI agent for CRM/Webitel ticket processing. See
`docs/architecture.md`, `docs/threat_model.md`, `docs/action_catalog.md`,
`docs/schema_foundation.md` and `docs/open_questions.md` for the design.

Status: Stage 1 (domain/persistence skeleton) and Stage 2 (Safety Core:
canonical hashing, Action Registry, Policy Engine, idempotency, lease locks,
audit chain) are done. No Playwright, Telegram, or LLM integration exists
yet, and no mutation actions are actually executed anywhere — the Policy
Engine only evaluates hypothetical plans built in tests.

## Requirements

- Python 3.11+ (verified against 3.14.7 in this repo's development
  environment via `python --version`). No third-party packages are required
  for Stage 1 — only the standard library (`dataclasses`, `enum`, `sqlite3`,
  `unittest`).
- On Windows, `python`/`pip` on `PATH` can resolve to the Microsoft Store's
  "App Execution Alias" stub instead of a real interpreter. If
  `python --version` fails, prints a Store-install prompt, or `python -m
  pip --version` errors, that stub is what's on `PATH` — a real Python
  3.11+ install is required; test results cannot be trusted from the stub.

## Install (editable, for later stages that add dependencies)

```powershell
python -m pip install -e .
```

This is optional for Stage 1: running tests only needs `PYTHONPATH` set to
`src`, shown below.

## Run tests

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m unittest discover -s tests\unit -v
```

Expected: all tests under `tests/unit` pass. There is no `tests/integration`
or `tests/e2e` content yet — those are added in later stages once Playwright
and Telegram adapters exist.

## Database

SQLite migrations live in `migrations/*.sql` and are applied by
`crm_ai_agent.persistence.db.run_migrations`. See that module's docstring for
the atomicity strategy (each migration file runs as one explicit SQLite
transaction; a failed migration leaves both the schema and the
`schema_migrations` ledger untouched).

No `.env` file is committed. Copy `.env.example` to `.env` locally and fill
in real values; `.env` is git-ignored.
