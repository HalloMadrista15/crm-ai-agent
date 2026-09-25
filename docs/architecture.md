# Architecture — Stage 1 + Stage 2 Snapshot

Minimal reference so `README.md` doesn't link to a missing file. This
describes what exists after Stage 1 remediation and Stage 2 (Safety Core),
not the full target system (see `docs/action_catalog.md` and
`docs/threat_model.md` for what's planned beyond Stage 2; Playwright,
Telegram, the LLM client, and real mutation execution are explicitly not
built yet).

## Layering

```text
domain/         entities, enums, state machine, validation, immutability —
                 no I/O, no third-party dependency
safety/          canonical hashing, Action Registry, Policy Engine,
                 lease locks, audit chain, Telegram-approval consumption
                 logic — pure logic + SQLite only, no Playwright/Telegram
                 network code/LLM SDK
ports/           CrmTicketReader, TicketInterpreter, EntityResolver —
                 read-only interfaces application code depends on,
                 independent of what implements them
adapters/        crm_fixture (JSON-fixture-backed reader + entity resolver,
                 used by tests), crm_playwright (real adapter — NOT
                 implemented, raises unconditionally; see its module
                 docstring), rule_based_interpreter (free, regex-based
                 stand-in for an LLM — see its module docstring for why
                 this is legitimate for the MVP's narrow scope)
persistence/     SQLite connection, migration runner, approval save/load +
                 update, action-operation/action-run idempotency
config/          environment-variable settings, no secrets in source
```

`domain/` must stay free of imports from `persistence/`, and free of any
Playwright/Telegram/LLM SDK — that boundary is what lets the safety rules
(state machine transitions, validation, immutability) be unit-tested without
a database or a browser.

## What's real vs. what's a placeholder

| Component | Status |
|---|---|
| Domain entities (`Ticket`, `TicketSnapshot`, `LLMInterpretation`, `PlanAction`, `ExecutionPlan`, `Approval`, `ActionOperation`, `ActionRun`, `AuditEvent`) | Implemented, with validation and deep immutability |
| Ticket lifecycle state machine + guards | Implemented; **not** a security boundary — see the module docstring in `state_machines.py` |
| SQLite schema + atomic migration runner | Implemented |
| Approval policy persistence (save/load) | Implemented, minimal (no full repository layer yet) |
| Canonical snapshot/plan hashing | Implemented (`safety/canonical_hash.py`) |
| Action Registry | Implemented for the 4 MVP action types (`safety/action_registry.py`) |
| Policy Engine | Implemented, MVP rule subset (`safety/policy_engine.py`) — see its module docstring for what's not yet covered |
| Idempotency (operation/attempt), lease locks, hash-chained audit log | Implemented (`persistence/action_operations.py`, `safety/locking.py`, `safety/audit.py`) |
| Telegram approval consumption logic (`consume_approval`: webhook-secret check, identity match, PENDING/TTL/role/hash checks) | Implemented (`safety/telegram_approval.py`) — no network code, no bot |
| CRM read port (`ports/crm.py`) | Implemented — a `Protocol`, no implementation details |
| Fixture-backed CRM reader for tests (`adapters/crm_fixture/`) | Implemented, reads static anonymized JSON, never touches a network |
| Real Playwright CRM reader (`adapters/crm_playwright/reader.py`) | NOT implemented — raises unconditionally; blocked on docs/open_questions.md #1 (no test CRM access, no real selectors to observe) |
| Entity resolution | Implemented fixture-backed (`adapters/crm_fixture/entity_resolver.py`) — real CRM lookup blocked on the same lack of test access as the reader |
| Ticket interpretation | Done for free via `adapters/rule_based_interpreter/` (regex-based, no LLM/API key) instead of a real LLM client — see that module's docstring for scope and limits |
| Real LLM client (optional upgrade) | Not built — would satisfy `ports.interpreter.TicketInterpreter` as a drop-in replacement if the rule-based approach stops covering enough phrasing/ticket types |
| Plan Compiler (`safety/plan_compiler.py`) | Implemented for `edit_crm_user_name`: requires a unique resolution, compiles a plan only on `ALLOW_FOR_APPROVAL`, computes `snapshot_hash`/`plan_hash`, derives a stable idempotency key |
| Telegram webhook server, bot registration, real allow-list lookup | Not built — Stage 6 |
| Execution, recovery, finalizer | Not built — Stage 7/8 |

See `docs/schema_foundation.md` for the reasoning behind specific schema and
domain-layer decisions made during Stage 1 remediation.
