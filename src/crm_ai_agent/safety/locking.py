"""Lease locks over the ``locks`` table.

A lease lock (as opposed to a plain mutex) always carries an expiry: if the
holder crashes without releasing it, the lock becomes stealable once
``expires_at`` passes rather than deadlocking every future attempt forever.
This is what lets the Stage 8 recovery worker make progress on a ticket
whose previous worker died mid-mutation, without a human having to manually
clear a stuck lock.

Transaction boundary: like ``persistence/approvals.py``, these functions
issue their statements and nothing else — no commit, no BEGIN. The caller
(eventually a Unit of Work) controls the transaction. In particular,
acquiring a lock and then doing the work it protects should typically be
separate transactions (you cannot hold a mutation's CRM round-trip inside a
single SQLite transaction), so ``acquire_lock`` committing on its own would
be actively wrong, not just premature.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta


class LockError(Exception):
    pass


class LockHeldError(LockError):
    def __init__(self, lock_key: str, holder: str, expires_at: str) -> None:
        super().__init__(f"lock {lock_key!r} is held by {holder!r} until {expires_at}")
        self.lock_key = lock_key
        self.holder = holder
        self.expires_at = expires_at


def acquire_lock(
    conn: sqlite3.Connection,
    *,
    lock_key: str,
    holder: str,
    ttl_seconds: int,
    now: datetime,
) -> None:
    """Acquire or renew a lease lock.

    Succeeds if: no row exists yet, the existing row is already held by
    ``holder`` (renewal), or the existing lease has expired (steal). Raises
    ``LockHeldError`` if a different, still-live holder has it.
    """

    if ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be > 0, got {ttl_seconds!r}")

    row = conn.execute(
        "SELECT holder, expires_at FROM locks WHERE lock_key = ?", (lock_key,)
    ).fetchone()

    now_iso = now.isoformat()
    expires_at_iso = (now + timedelta(seconds=ttl_seconds)).isoformat()

    if row is not None:
        existing_holder = row["holder"]
        existing_expires_at = datetime.fromisoformat(row["expires_at"])
        if existing_holder != holder and existing_expires_at > now:
            raise LockHeldError(lock_key, existing_holder, row["expires_at"])
        conn.execute(
            "UPDATE locks SET holder = ?, acquired_at = ?, expires_at = ? WHERE lock_key = ?",
            (holder, now_iso, expires_at_iso, lock_key),
        )
    else:
        conn.execute(
            "INSERT INTO locks (lock_key, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
            (lock_key, holder, now_iso, expires_at_iso),
        )


def release_lock(conn: sqlite3.Connection, *, lock_key: str, holder: str) -> None:
    """Release a lock. A no-op if the lock does not exist.

    Raises ``LockError`` if the lock exists but is held by someone else —
    releasing a lock you don't hold (e.g. because your lease already expired
    and someone else took it) would silently drop their protection.
    """

    row = conn.execute("SELECT holder FROM locks WHERE lock_key = ?", (lock_key,)).fetchone()
    if row is None:
        return
    if row["holder"] != holder:
        raise LockError(f"cannot release lock {lock_key!r}: held by {row['holder']!r}, not {holder!r}")
    conn.execute("DELETE FROM locks WHERE lock_key = ?", (lock_key,))
