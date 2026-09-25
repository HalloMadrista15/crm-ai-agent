-- Initial schema for crm_ai_agent (MVP: single change_user_name scenario).
-- Applied by crm_ai_agent.persistence.db.run_migrations, tracked in schema_migrations.
--
-- The migration runner strips '--' line comments, then splits the rest of
-- this file on ';' to run it as one SQLite transaction (see db.py). Only
-- constraint that follows from that: no statement (outside a comment) may
-- contain a literal ';' inside a string value or trigger body.
--
-- Status columns (tickets.status, approvals.status, executions.status,
-- action_operations.status, action_runs.status) are stored as free-text
-- here. Validity of a given status *value* is a domain-layer concern,
-- enforced by crm_ai_agent.domain.enums and crm_ai_agent.domain.state_machines
-- before a row is ever written; the database only enforces referential
-- integrity, uniqueness and NOT NULL — it does not itself constrain status
-- to a fixed set of values (SQLite CHECK constraints on an enum-like column
-- would have to be duplicated and kept in sync with the Python enums, which
-- is worse than a single source of truth in domain code).

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY,
    external_ticket_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, external_ticket_id)
);

CREATE TABLE IF NOT EXISTS ticket_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets (ticket_id),
    schema_version TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE (ticket_id, source_hash),
    -- Lets plans below FK-check (ticket_id, snapshot_id) as a pair, which is
    -- what stops a plan from citing a snapshot that actually belongs to a
    -- different ticket than the plan's own ticket_id.
    UNIQUE (ticket_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS llm_interpretations (
    interpretation_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES ticket_snapshots (snapshot_id),
    schema_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resolutions (
    resolution_id TEXT PRIMARY KEY,
    interpretation_id TEXT NOT NULL REFERENCES llm_interpretations (interpretation_id),
    created_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

-- FOREIGN KEY (ticket_id, snapshot_id) REFERENCES ticket_snapshots (ticket_id,
-- snapshot_id), rather than a plain FK on snapshot_id alone, is what stops a
-- plan from being created with ticket_id = ticket A and a snapshot_id that
-- actually belongs to ticket B: no such (ticket_id, snapshot_id) row would
-- exist in ticket_snapshots if the two disagreed.
--
-- UNIQUE (ticket_id, plan_id) lets approvals below FK-check (ticket_id,
-- plan_id) as a pair, which is what stops an approval from citing a plan
-- that actually belongs to a different ticket.
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets (ticket_id),
    snapshot_id TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    UNIQUE (ticket_id, plan_id),
    FOREIGN KEY (ticket_id, snapshot_id) REFERENCES ticket_snapshots (ticket_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS plan_actions (
    action_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans (plan_id),
    action_type TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE (plan_id, action_id),
    UNIQUE (idempotency_key)
);

CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans (plan_id),
    status TEXT NOT NULL,
    claimed_by TEXT,
    started_at TEXT,
    heartbeat_at TEXT,
    finished_at TEXT,
    -- Lets action_runs below FK-check (execution_id, plan_id) as a pair,
    -- which is what stops a run from citing an execution that belongs to a
    -- different plan than the run's own operation.
    UNIQUE (execution_id, plan_id)
);

-- One row per logical mutation operation, keyed by idempotency_key. This is
-- what prevents two independent mutations from ever being dispatched for the
-- same plan_action: a second attempt looks up (or creates, via INSERT OR
-- IGNORE on idempotency_key) the existing operation row and appends an
-- action_runs attempt to it rather than starting a new operation.
--
-- FOREIGN KEY (plan_id, action_id) REFERENCES plan_actions (plan_id,
-- action_id), rather than a plain FK on action_id alone, is what stops an
-- operation from being created for a plan_action that actually belongs to a
-- different plan: no such (plan_id, action_id) row would exist in
-- plan_actions if the two disagreed.
CREATE TABLE IF NOT EXISTS action_operations (
    operation_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans (plan_id),
    action_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (idempotency_key),
    -- Lets action_runs below FK-check (operation_id, plan_id, action_id) as
    -- a triple.
    UNIQUE (operation_id, plan_id, action_id),
    FOREIGN KEY (plan_id, action_id) REFERENCES plan_actions (plan_id, action_id)
);

-- Many attempts may exist per operation (attempt_no 1, 2, 3, ...). What must
-- never happen is two operations for the same idempotency_key: that
-- invariant lives on action_operations.idempotency_key above, not here.
--
-- plan_id is redundant with operation_id in memory (the operation already
-- pins a plan), but it is what lets SQLite enforce, via the two composite
-- foreign keys below, that a run's operation and its execution belong to the
-- SAME plan. Without plan_id here there would be no column pair the database
-- could check that constraint on, and a run could silently pair an
-- operation from plan A with an execution from plan B.
CREATE TABLE IF NOT EXISTS action_runs (
    action_run_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    plan_id TEXT NOT NULL REFERENCES plans (plan_id),
    action_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    status TEXT NOT NULL,
    before_state_hash TEXT,
    after_state_hash TEXT,
    error_class TEXT,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE (operation_id, attempt_no),
    FOREIGN KEY (operation_id, plan_id, action_id) REFERENCES action_operations (operation_id, plan_id, action_id),
    FOREIGN KEY (execution_id, plan_id) REFERENCES executions (execution_id, plan_id)
);

-- FOREIGN KEY (ticket_id, plan_id) REFERENCES plans (ticket_id, plan_id),
-- rather than a plain FK on plan_id alone, is what stops an approval from
-- being created with ticket_id = ticket A and a plan_id that actually
-- belongs to ticket B.
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets (ticket_id),
    plan_id TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    -- Policy in force when this approval was created, frozen at creation
    -- time so a later policy config change cannot retroactively change who
    -- was allowed to approve an already-issued approval.
    policy_version TEXT NOT NULL,
    allowed_approver_roles_json TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    status TEXT NOT NULL,
    telegram_user_id TEXT,
    telegram_chat_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    consumed_at TEXT,
    decision_reason TEXT,
    FOREIGN KEY (ticket_id, plan_id) REFERENCES plans (ticket_id, plan_id)
);

CREATE TABLE IF NOT EXISTS locks (
    lock_key TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Foundation for the append-only, hash-chained audit trail. The chaining
-- service itself (verifying prev_event_hash links, external export) is
-- built in Stage 2; this schema only fixes the shape so later stages don't
-- have to migrate it.
CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    ticket_id TEXT REFERENCES tickets (ticket_id),
    correlation_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    prev_event_hash TEXT,
    event_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE (ticket_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    ticket_id TEXT REFERENCES tickets (ticket_id),
    action_run_id TEXT REFERENCES action_runs (action_run_id),
    artifact_type TEXT NOT NULL,
    storage_reference TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox_messages (
    message_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tickets_status_updated ON tickets (status, updated_at);
CREATE INDEX IF NOT EXISTS idx_audit_events_ticket_created ON audit_events (ticket_id, created_at);

-- Foreign-key indexes: SQLite does not auto-index the referencing side of a
-- foreign key (only the referenced side, via its PK/UNIQUE), so lookups and
-- ON DELETE/UPDATE cascade checks on these columns would otherwise be full
-- table scans as data grows.
CREATE INDEX IF NOT EXISTS idx_ticket_snapshots_ticket_id ON ticket_snapshots (ticket_id);
CREATE INDEX IF NOT EXISTS idx_llm_interpretations_snapshot_id ON llm_interpretations (snapshot_id);
CREATE INDEX IF NOT EXISTS idx_resolutions_interpretation_id ON resolutions (interpretation_id);
CREATE INDEX IF NOT EXISTS idx_plans_ticket_id ON plans (ticket_id);
CREATE INDEX IF NOT EXISTS idx_plans_snapshot_id ON plans (snapshot_id);
CREATE INDEX IF NOT EXISTS idx_plan_actions_plan_id ON plan_actions (plan_id);
CREATE INDEX IF NOT EXISTS idx_action_operations_plan_id ON action_operations (plan_id);
CREATE INDEX IF NOT EXISTS idx_action_operations_action_id ON action_operations (action_id);
CREATE INDEX IF NOT EXISTS idx_action_runs_operation_id ON action_runs (operation_id);
CREATE INDEX IF NOT EXISTS idx_action_runs_execution_id ON action_runs (execution_id);
CREATE INDEX IF NOT EXISTS idx_action_runs_action_id ON action_runs (action_id);
CREATE INDEX IF NOT EXISTS idx_action_runs_plan_id ON action_runs (plan_id);
CREATE INDEX IF NOT EXISTS idx_approvals_ticket_id ON approvals (ticket_id);
CREATE INDEX IF NOT EXISTS idx_approvals_plan_id ON approvals (plan_id);
CREATE INDEX IF NOT EXISTS idx_executions_plan_id ON executions (plan_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_ticket_id ON artifacts (ticket_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_action_run_id ON artifacts (action_run_id);
