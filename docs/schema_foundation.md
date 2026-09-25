# Schema Foundation — Stage 1 Remediation

Dated 2026-09-03. Records the data-integrity decisions made while fixing the
Stage 1 skeleton, so Stage 2 (Policy Engine, Action Registry) can build on
them without re-deriving them.

## Idempotency and retries: operation vs. run

`action_operations` holds one row per logical mutation, keyed by
`idempotency_key` (`UNIQUE`). `action_runs` holds one row per execution
*attempt* of an operation, keyed by `(operation_id, attempt_no)`.

- Two independent mutations for the same idempotency key can never be
  created: the second `INSERT` into `action_operations` violates the
  `UNIQUE` constraint.
- Any number of attempts can be recorded against one operation — this is
  what a retry appends.
- `ActionOperationStatus.UNKNOWN_OUTCOME` is a terminal-for-automation state
  on the *operation*. Reaching it must route to reconciliation (a read-only
  check of what the CRM actually shows), never to starting another attempt
  automatically. `ActionRunStatus.UNKNOWN_OUTCOME` marks the *attempt* that
  produced the ambiguous result; `ActionOperationStatus.RECONCILING` is the
  operation-level status while that check is in flight. Stage 2/7 implement
  the reconciliation worker itself; this schema only fixes the shape.

## Deep immutability

`TicketSnapshot`, `LLMInterpretation`, `PlanAction`, and `ExecutionPlan` are
`frozen=True` dataclasses whose nested `dict`/`list` fields are converted to
`MappingProxyType`/`tuple` in `__post_init__` via
`crm_ai_agent.domain.immutability.deep_freeze`. This closes the gap where a
frozen dataclass still allowed in-place mutation of a dict it held — which
would have let a plan's arguments change after it was hashed and approved.
`crm_ai_agent.domain.immutability.canonicalize` is the deterministic
projection that Stage 2's canonical hashing (`snapshot_hash`, `plan_hash`)
will hash — it is added now, unused, so the hashing implementation has a
fixed contract to target.

## Approval policy persistence

`approvals` stores `policy_version`, `allowed_approver_roles_json`, and
`ttl_seconds` alongside the approval row, in addition to the Telegram
identity, decision, and timestamps that already existed. This is the frozen
policy snapshot from the moment the approval was created — after a restart,
`crm_ai_agent.persistence.approvals.load_approval` reconstructs the exact
`ApprovalPolicySnapshot` that governed the approval, independent of whatever
the live policy configuration says today.

## State machine guards

Structural reachability (`ALLOWED_TRANSITIONS`) and *permission to make the
transition right now* (`GUARDS`) are separate concerns in
`state_machines.py`:

- `MANUAL_REVIEW -> CLOSED` does not exist. A human closing a ticket
  directly in the CRM while it is under manual review is recorded as
  `MANUAL_REVIEW -> EXTERNALLY_CLOSED`, gated on `actor in {"operator",
  "admin"}`. `CLOSED` is reachable only via `FINALIZING -> CLOSED`, gated on
  `actor == "finalizer"`.
- `APPROVED -> EXECUTION_QUEUED` requires `revalidated=True` in the
  transition context — the caller must have actually re-read the ticket and
  re-checked the plan/snapshot hash before claiming this, the state machine
  does not infer it.
- `VERIFYING -> FINALIZING` requires `verification_passed=True` for the same
  reason.

## Domain validation vs. DB constraints

Status columns (`tickets.status`, `approvals.status`, `executions.status`,
`action_operations.status`, `action_runs.status`) are stored as free-text
`TEXT NOT NULL` in SQLite. The database enforces:

- referential integrity (foreign keys),
- uniqueness (`idempotency_key`, `(tenant_id, external_ticket_id)`,
  `(operation_id, attempt_no)`, etc.),
- `NOT NULL`.

It does **not** constrain a status column to a fixed set of values — that is
enforced once, in Python, by `crm_ai_agent.domain.enums` (as the type of the
value) and `crm_ai_agent.domain.state_machines` (as the transition rules).
Duplicating an enum as a SQLite `CHECK (status IN (...))` was considered and
rejected: it would need to be kept in sync with the Python enum by hand,
which is a second source of truth that WILL drift as the enum grows in
Stage 2+. Any code path that writes a status to the database must go through
the domain layer first.

`classification_confidence` (`LLMInterpretation`), `action_type`
(`PlanAction`), and `ticket_type` (`LLMInterpretation`) are validated the
same way: at domain-object construction, not at the database boundary. See
`crm_ai_agent.domain.validation`. `KnownActionType` and `KnownTicketType`
(`crm_ai_agent.domain.enums`) are foundation-level allowlists only — the real
Action Registry (argument schemas, preconditions, verifiers) is Stage 2 work,
not a change to this enum.

## Tenant on the domain Ticket

`Ticket.tenant_id` (default `"default"`) now exists on the domain entity to
match the `tickets.tenant_id` column and its `UNIQUE (tenant_id,
external_ticket_id)` constraint, which existed in the schema before this
remediation but had no domain-layer counterpart.

## Audit event foundation

`AuditEvent` (`crm_ai_agent.domain.entities`) and the `audit_events` table
now carry `correlation_id`, `actor_type`, `actor_id`, and `sequence_no` in
addition to the previously-existing `prev_event_hash`/`event_hash` pair.
`sequence_no` gives a total order per ticket (`UNIQUE (ticket_id,
sequence_no)`) that does not depend on timestamp resolution. This is
foundation only: the hash-chaining service (computing `event_hash` from
`prev_event_hash` plus payload, verifying the chain, exporting it externally)
is Stage 2 work.

## Cross-ticket integrity

The same composite-foreign-key pattern used for the operation/run/execution
chain also closes a gap one level up: `plans.(ticket_id, snapshot_id)` FKs to
`ticket_snapshots.(ticket_id, snapshot_id)`, and `approvals.(ticket_id,
plan_id)` FKs to `plans.(ticket_id, plan_id)`. Before this, `plans.ticket_id`
and `plans.snapshot_id` were validated independently (each existed
somewhere), which let a plan be inserted with `ticket_id = A` and a
`snapshot_id` that actually belonged to ticket B — same shape of bug as the
plan/execution mixing the operation/run chain already guards against, one
level higher. Proven by `tests/unit/test_cross_ticket_integrity.py`.

## Migration atomicity

See the docstring in `crm_ai_agent/persistence/db.py`. Summary: SQLite DDL is
transactional, but `sqlite3.Connection.executescript()` is not — it commits
before running and cannot be wrapped in a caller-controlled transaction. The
runner instead strips `--` line comments, splits each file on `;` itself, and
wraps every statement plus the `schema_migrations` insert in one explicit
`BEGIN IMMEDIATE` / `COMMIT` (or `ROLLBACK` on any error). A failed migration
therefore leaves neither a partial schema nor a stale ledger entry behind.
This constrains migration files to not use `;` inside string literals or
trigger bodies outside of comments — acceptable for the plain DDL/DML this
project uses.
