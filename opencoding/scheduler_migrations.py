"""Versioned SQLite schema for the local W2 scheduler."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

from .safety import _is_reparse, _root_path

SCHEMA_VERSION = "1.0"
MIGRATION_VERSION = 2

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        input_json TEXT NOT NULL,
        action_json TEXT NOT NULL,
        depends_json TEXT NOT NULL,
        max_attempts INTEGER NOT NULL,
        timeout_seconds REAL NOT NULL,
        state TEXT NOT NULL,
        attempt INTEGER NOT NULL DEFAULT 0,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        runner_pid INTEGER,
        last_run_id TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_dependencies (
        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
        depends_on TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
        PRIMARY KEY (task_id, depends_on)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS state_transitions (
        transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
        from_state TEXT,
        to_state TEXT NOT NULL,
        reason TEXT NOT NULL,
        run_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
        attempt INTEGER NOT NULL,
        input_sha256 TEXT NOT NULL,
        action_digest TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        exit_code INTEGER,
        timed_out INTEGER NOT NULL DEFAULT 0,
        cancelled INTEGER NOT NULL DEFAULT 0,
        stdout_summary TEXT NOT NULL DEFAULT '',
        stderr_summary TEXT NOT NULL DEFAULT '',
        artifacts_json TEXT NOT NULL DEFAULT '[]',
        reason TEXT,
        receipt_json TEXT,
        receipt_digest TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, attempt)",
)


def migrate(connection: sqlite3.Connection) -> int:
    """Apply the known schema exactly once and reject newer unknown schemas."""

    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("BEGIN IMMEDIATE")
    try:
        # The schema version must be read only after the write lock is held.
        # Concurrent initializers otherwise can both act on a stale version.
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current > MIGRATION_VERSION:
            raise RuntimeError("scheduler database schema is newer than this worker")
        if current == 0:
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {MIGRATION_VERSION}")
        elif current == 1:
            connection.execute("ALTER TABLE tasks ADD COLUMN runner_pid INTEGER")
            connection.execute(f"PRAGMA user_version = {MIGRATION_VERSION}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return MIGRATION_VERSION


def _validate_metadata_directory(path: Path) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    if path.is_symlink() or _is_reparse(path):
        raise ValueError("scheduler metadata path contains a link or reparse point")
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("scheduler metadata parent is not a directory")


def _validate_metadata_file(path: Path) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    if path.is_symlink() or _is_reparse(path):
        raise ValueError("scheduler database path contains a link or reparse point")
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("scheduler database target is not a regular file")
    if getattr(info, "st_nlink", 1) > 1:
        raise ValueError("scheduler database target is a hardlink")


def database_path(root: Path) -> Path:
    """Return a metadata DB path only after link and hardlink checks."""

    project_root = _root_path(root)
    metadata_root = project_root / ".opencoding"
    scheduler_root = metadata_root / "scheduler"
    _validate_metadata_directory(metadata_root)
    _validate_metadata_directory(scheduler_root)
    database = scheduler_root / "state.sqlite3"
    _validate_metadata_file(database)
    for suffix in ("-journal", "-wal", "-shm"):
        _validate_metadata_file(database.with_name(database.name + suffix))
    return database


__all__ = ["MIGRATION_VERSION", "SCHEMA_VERSION", "database_path", "migrate"]
