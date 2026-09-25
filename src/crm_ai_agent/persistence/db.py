"""SQLite connection and migration runner.

Migrations are plain .sql files under ``migrations/``, applied in filename
order exactly once, tracked in ``schema_migrations``. Domain/application code
must go through repositories (added in a later stage), never through this
module directly.

Atomicity strategy
-------------------
``sqlite3.Connection.executescript()`` issues an implicit ``COMMIT`` before it
runs and does not participate in a transaction the caller controls, so a
migration file with three statements where the third one fails can leave the
first two committed with no corresponding ``schema_migrations`` row — the
schema and the migration ledger silently disagree after the process is
restarted.

To avoid that, this module:

1. strips ``--`` line comments, then splits each migration file into
   individual statements itself on ``;`` at statement boundaries — sufficient
   because our migrations are plain DDL/DML with no stored procedures or
   string literals containing semicolons, documented here as an explicit
   constraint on what migration files may contain;
2. opens the connection in autocommit mode (``isolation_level = None``) and
   issues an explicit ``BEGIN IMMEDIATE`` so every statement in the file plus
   the ``schema_migrations`` insert are one SQLite transaction;
3. rolls back the whole transaction on any error, so a failed migration
   leaves both the schema and the ledger exactly as they were before it ran.

SQLite's DDL is transactional (unlike most other RDBMS), which is what makes
this strategy work at all.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


def connect(database_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(database_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _strip_line_comments(sql: str) -> str:
    """Remove ``-- ...`` line comments before statement splitting.

    Comment text is free-form prose and may legitimately contain ``;``
    (as this file's own docstring does), which would otherwise confuse the
    naive ``;``-based statement split below. This assumes migration files
    contain no ``--`` inside a string literal, which holds for our DDL/DML.
    """

    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _split_statements(sql: str) -> list[str]:
    statements = [s.strip() for s in _strip_line_comments(sql).split(";")]
    return [s for s in statements if s]


class MigrationError(RuntimeError):
    def __init__(self, filename: str, cause: Exception) -> None:
        super().__init__(f"Migration {filename} failed and was rolled back: {cause}")
        self.filename = filename
        self.cause = cause


def run_migrations(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    conn.isolation_level = None  # autocommit; we manage transactions explicitly below.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    applied = {row["filename"] for row in conn.execute("SELECT filename FROM schema_migrations")}

    newly_applied: list[str] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        if path.name in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split_statements(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (?)",
                (path.name,),
            )
        except Exception as exc:
            conn.execute("ROLLBACK")
            raise MigrationError(path.name, exc) from exc
        else:
            conn.execute("COMMIT")
            newly_applied.append(path.name)
    return newly_applied
