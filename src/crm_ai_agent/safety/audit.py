"""Append-only, hash-chained audit trail over the ``audit_events`` table.

``docs/schema_foundation.md`` fixed the shape (``correlation_id``,
``sequence_no``, ``actor_type``/``actor_id``, ``prev_event_hash``/
``event_hash``) as Stage 1 foundation; this module is the Stage 2 service
that actually computes the chain and can verify it.

Only ``append_event`` and ``verify_chain`` exist here — deliberately no
update/delete function. "Append-only" is enforced by this module simply
never offering a way to do anything else; nothing here stops a caller from
issuing a raw ``UPDATE audit_events ...``, which is why ``verify_chain``
exists at all — to detect exactly that kind of out-of-band tampering after
the fact. A production deployment would also want DB-level grants that
revoke UPDATE/DELETE on this table from the application's own role; that is
an infrastructure/deployment concern out of scope for this local SQLite
foundation.

Transaction boundary: ``append_event`` does not commit, matching every
other persistence function added in this project — see
``persistence/approvals.py`` for the reasoning.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime

from crm_ai_agent.domain.entities import AuditEvent
from crm_ai_agent.domain.immutability import canonicalize


def compute_event_hash(prev_event_hash: str | None, payload_json: str) -> str:
    base = (prev_event_hash or "") + payload_json
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _canonical_payload_json(payload: dict[str, str]) -> str:
    return json.dumps(canonicalize(dict(payload)), sort_keys=True, separators=(",", ":"))


def append_event(
    conn: sqlite3.Connection,
    *,
    ticket_id: str | None,
    correlation_id: str,
    actor_type: str,
    actor_id: str,
    event_type: str,
    payload: dict[str, str],
    now: datetime,
) -> AuditEvent:
    """Append one event to the chain for ``ticket_id``.

    ``sequence_no`` and ``prev_event_hash`` are derived from the last row
    for this ticket (read inside the caller's transaction), so this must run
    with a lock or serializable transaction covering the read+insert when
    multiple writers could append for the same ticket concurrently — a
    concern for the future Unit of Work / execution lock, not solved here.
    """

    last = conn.execute(
        "SELECT sequence_no, event_hash FROM audit_events WHERE ticket_id = ? ORDER BY sequence_no DESC LIMIT 1",
        (ticket_id,),
    ).fetchone()

    sequence_no = (last["sequence_no"] + 1) if last is not None else 1
    prev_event_hash = last["event_hash"] if last is not None else None

    payload_json = _canonical_payload_json(payload)
    event_hash = compute_event_hash(prev_event_hash, payload_json)
    event_id = str(uuid.uuid4())

    conn.execute(
        """
        INSERT INTO audit_events (
            event_id, ticket_id, correlation_id, sequence_no,
            actor_type, actor_id, event_type, created_at,
            prev_event_hash, event_hash, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            ticket_id,
            correlation_id,
            sequence_no,
            actor_type,
            actor_id,
            event_type,
            now.isoformat(),
            prev_event_hash,
            event_hash,
            payload_json,
        ),
    )

    return AuditEvent(
        event_id=event_id,
        ticket_id=ticket_id,
        correlation_id=correlation_id,
        sequence_no=sequence_no,
        actor_type=actor_type,
        actor_id=actor_id,
        event_type=event_type,
        created_at=now,
        prev_event_hash=prev_event_hash,
        event_hash=event_hash,
        payload=payload,
    )


def verify_chain(conn: sqlite3.Connection, ticket_id: str | None) -> bool:
    """Recompute every hash in the chain for ``ticket_id`` and check it
    against what's stored, and that sequence numbers are contiguous from 1.
    Returns False on the first mismatch — including any row inserted,
    edited, or deleted out of band since the chain was built.
    """

    rows = conn.execute(
        "SELECT sequence_no, prev_event_hash, event_hash, payload_json "
        "FROM audit_events WHERE ticket_id = ? ORDER BY sequence_no ASC",
        (ticket_id,),
    ).fetchall()

    expected_prev: str | None = None
    for expected_sequence_no, row in enumerate(rows, start=1):
        if row["sequence_no"] != expected_sequence_no:
            return False
        if row["prev_event_hash"] != expected_prev:
            return False
        expected_hash = compute_event_hash(expected_prev, row["payload_json"])
        if row["event_hash"] != expected_hash:
            return False
        expected_prev = row["event_hash"]

    return True
