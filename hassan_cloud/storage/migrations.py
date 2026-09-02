"""Schema migrations for SQLite — additive only."""

from __future__ import annotations

import sqlite3


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def migrate_jobs_table(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "jobs", "idempotency_key", "TEXT")
    ensure_column(conn, "jobs", "lease_expires_at", "INTEGER")
    ensure_column(conn, "jobs", "worker_id", "TEXT")
    ensure_column(conn, "jobs", "checkpoint_stage", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "jobs", "cancel_requested", "INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency ON jobs(idempotency_key) "
        "WHERE idempotency_key IS NOT NULL AND idempotency_key != ''"
    )


def migrate_agent_runs_table(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "agent_runs", "started_at", "INTEGER")
    ensure_column(conn, "agent_runs", "finished_at", "INTEGER")
