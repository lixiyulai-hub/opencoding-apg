"""Transactional, recoverable local task scheduler for W2-B."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .executor import Executor, action_digest
from .safety import SCHEMA_VERSION, _root_path, canonical_json, inspect_sensitive, sanitize_text, sha256_bytes
from .scheduler_migrations import MIGRATION_VERSION, database_path, migrate

TASK_STATES = {"queued", "running", "succeeded", "failed", "frozen", "cancelled", "timed_out"}
_TASK_FIELDS = {"task_id", "input", "action", "depends_on", "max_attempts", "timeout_seconds", "idempotency_key"}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
MAX_TIMEOUT_SECONDS = 30.0
_SQLITE_SHARED_LOCK = 0x40000002
_SNAPSHOT_TASK_COLUMNS = {
    "task_id", "idempotency_key", "input_json", "action_json", "depends_json",
    "max_attempts", "timeout_seconds", "state", "attempt", "cancel_requested",
    "runner_pid", "last_run_id", "last_error", "created_at", "updated_at",
}
_SNAPSHOT_RUN_COLUMNS = {
    "run_id", "task_id", "attempt", "input_sha256", "action_digest", "started_at",
    "finished_at", "status", "exit_code", "timed_out", "cancelled", "stdout_summary",
    "stderr_summary", "artifacts_json", "reason", "receipt_json", "receipt_digest",
}


class SchedulerSnapshotError(ValueError):
    """A fail-closed error for the zero-write scheduler snapshot API."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _explicit_root(root: str | os.PathLike[str] | Path) -> Path:
    path = Path(root)
    if not path.is_absolute():
        raise ValueError("root must be an explicit absolute path")
    return _root_path(path)


def _json(value: Any) -> str:
    return canonical_json(value).decode("utf-8")


def _load(value: str) -> Any:
    return json.loads(value)


def _validate_snapshot_task_id(task_id: str | None) -> None:
    if task_id is not None and (not isinstance(task_id, str) or not _ID.fullmatch(task_id)):
        raise SchedulerSnapshotError("invalid_task_id", "task_id is invalid")


def _snapshot_platform_supported() -> bool:
    return os.name == "nt"


def _snapshot_sidecars(database: Path) -> list[Path]:
    return [database.with_name(database.name + suffix) for suffix in ("-journal", "-wal", "-shm") if database.with_name(database.name + suffix).exists()]


def _snapshot_sqlite_error(error: sqlite3.Error) -> SchedulerSnapshotError:
    code = getattr(error, "sqlite_errorcode", None)
    primary = code & 0xFF if isinstance(code, int) else None
    status = "database_busy" if primary in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} else "database_unavailable"
    return SchedulerSnapshotError(status, sanitize_text(str(error)))


@contextmanager
def _snapshot_lock(database: Path):
    """Hold one SQLite shared-lock byte while inspecting and opening the DB."""

    if not _snapshot_platform_supported():
        raise SchedulerSnapshotError("unsupported_platform", "zero-write scheduler snapshots are unsupported on this platform")
    try:
        handle = database.open("rb")
    except OSError as exc:
        raise SchedulerSnapshotError("database_unavailable", sanitize_text(str(exc))) from exc
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class _Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
        kernel32.LockFileEx.restype = wintypes.BOOL
        kernel32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
        kernel32.UnlockFileEx.restype = wintypes.BOOL
        overlap = _Overlapped(Offset=_SQLITE_SHARED_LOCK & 0xFFFFFFFF, OffsetHigh=_SQLITE_SHARED_LOCK >> 32)
        if not kernel32.LockFileEx(msvcrt.get_osfhandle(handle.fileno()), 0x00000001, 0, 1, 0, ctypes.byref(overlap)):
            raise SchedulerSnapshotError("database_busy", "scheduler database is locked")
        try:
            yield
        finally:
            kernel32.UnlockFileEx(msvcrt.get_osfhandle(handle.fileno()), 0, 1, 0, ctypes.byref(overlap))
    finally:
        handle.close()


def _database_uses_wal(database: Path) -> bool:
    """Inspect the SQLite header before opening it, because RO WAL opens can create SHM."""

    try:
        with database.open("rb") as handle:
            header = handle.read(20)
    except OSError as exc:
        raise SchedulerSnapshotError("database_unavailable", sanitize_text(str(exc))) from exc
    if len(header) != 20 or header[:16] != b"SQLite format 3\x00":
        raise SchedulerSnapshotError("database_unavailable", "scheduler database is corrupt")
    read_version, write_version = header[18], header[19]
    if (read_version, write_version) == (2, 2):
        return True
    if (read_version, write_version) != (1, 1):
        raise SchedulerSnapshotError("database_unavailable", "scheduler database journal mode is invalid")
    return False


def _readonly_uri(database: Path) -> str:
    """Build an escaped SQLite URI from a validated absolute database path."""

    return database.as_uri() + "?mode=ro&cache=private"


def _validate_snapshot_schema(connection: sqlite3.Connection) -> None:
    for table, required in (("tasks", _SNAPSHOT_TASK_COLUMNS), ("runs", _SNAPSHOT_RUN_COLUMNS)):
        row = connection.execute("SELECT type FROM sqlite_master WHERE name = ?", (table,)).fetchone()
        if row is None or row["type"] != "table":
            raise SchedulerSnapshotError("unsupported_schema", "scheduler snapshot table is missing")
        columns = {item["name"] for item in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if not required <= columns:
            raise SchedulerSnapshotError("unsupported_schema", "scheduler snapshot table columns are unsupported")


def _snapshot_load(value: str) -> Any:
    def reject_constant(_value: str) -> Any:
        raise ValueError("stored JSON contains a non-finite constant")

    payload = json.loads(value, parse_constant=reject_constant)
    canonical_json(payload)
    return payload


def _snapshot_task_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": row["task_id"],
        "idempotency_key": row["idempotency_key"],
        "input": _snapshot_load(row["input_json"]),
        "action": _snapshot_load(row["action_json"]),
        "depends_on": _snapshot_load(row["depends_json"]),
        "max_attempts": row["max_attempts"],
        "timeout_seconds": row["timeout_seconds"],
        "state": row["state"],
        "attempt": row["attempt"],
        "last_run_id": row["last_run_id"],
        "last_error": row["last_error"],
    }


def _snapshot_run_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "attempt": row["attempt"],
        "input_sha256": row["input_sha256"],
        "action_digest": row["action_digest"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "exit_code": row["exit_code"],
        "timed_out": bool(row["timed_out"]),
        "cancelled": bool(row["cancelled"]),
        "stdout_summary": row["stdout_summary"],
        "stderr_summary": row["stderr_summary"],
        "artifacts": _snapshot_load(row["artifacts_json"]),
        "reason": row["reason"],
        "receipt_digest": row["receipt_digest"],
    }
    result["receipt"] = _snapshot_load(row["receipt_json"]) if row["receipt_json"] else None
    return result


@contextmanager
def _readonly_snapshot_connection(root: Path):
    """Open a stable scheduler database view without touching its filesystem state."""

    database = database_path(root)
    if not database.exists():
        yield None
        return
    if not _snapshot_platform_supported():
        raise SchedulerSnapshotError("unsupported_platform", "zero-write scheduler snapshots are unsupported on this platform")
    with _snapshot_lock(database):
        if _snapshot_sidecars(database):
            raise SchedulerSnapshotError("unsafe_journal_state", "scheduler journal or WAL state cannot be read without writes")
        if _database_uses_wal(database):
            raise SchedulerSnapshotError("unsafe_journal_state", "scheduler WAL mode cannot be read without sidecar creation")
        try:
            connection = sqlite3.connect(_readonly_uri(database), uri=True, timeout=0.0, isolation_level=None)
        except sqlite3.Error as exc:
            raise _snapshot_sqlite_error(exc) from exc
        transaction_started = False
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            transaction_started = True
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != MIGRATION_VERSION:
                raise SchedulerSnapshotError("unsupported_schema", "scheduler database schema is unsupported")
            _validate_snapshot_schema(connection)
            if _snapshot_sidecars(database) or _database_uses_wal(database):
                raise SchedulerSnapshotError("unsafe_journal_state", "scheduler journal or WAL state changed during snapshot")
            yield connection
        except SchedulerSnapshotError:
            raise
        except sqlite3.Error as exc:
            raise _snapshot_sqlite_error(exc) from exc
        except (ValueError, json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
            raise SchedulerSnapshotError("database_unavailable", sanitize_text(str(exc))) from exc
        finally:
            if transaction_started:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            connection.close()


def _validate_task(task: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(task, Mapping) or set(task) != _TASK_FIELDS:
        raise ValueError("task fields are invalid")
    value = dict(task)
    if not isinstance(value["task_id"], str) or not _ID.fullmatch(value["task_id"]):
        raise ValueError("task_id is invalid")
    if not isinstance(value["idempotency_key"], str) or not _ID.fullmatch(value["idempotency_key"]):
        raise ValueError("idempotency_key is invalid")
    if not isinstance(value["input"], (dict, list, str, int, float, bool, type(None))):
        raise ValueError("input is not JSON-compatible")
    json.dumps(value["input"], allow_nan=False)
    if inspect_sensitive(_json(value["input"]))["sensitive"]:
        raise ValueError("input contains sensitive material")
    if not isinstance(value["depends_on"], list) or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in value["depends_on"]):
        raise ValueError("depends_on is invalid")
    if len(set(value["depends_on"])) != len(value["depends_on"]):
        raise ValueError("depends_on contains duplicates")
    if value["task_id"] in value["depends_on"]:
        raise ValueError("task cannot depend on itself")
    if type(value["max_attempts"]) is not int or not 1 <= value["max_attempts"] <= 100:
        raise ValueError("max_attempts is outside the local limit")
    if not isinstance(value["timeout_seconds"], (int, float)) or isinstance(value["timeout_seconds"], bool) or not 0 < value["timeout_seconds"] <= MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout_seconds is outside the local limit")
    if not isinstance(value["action"], Mapping):
        raise ValueError("action is invalid")
    action_digest(value["action"])
    if inspect_sensitive(_json(value["action"]))["sensitive"]:
        raise ValueError("action contains sensitive material")
    return value


@contextmanager
def _connect(root: Path):
    path = database_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path = database_path(root)
    connection = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        migrate(connection)
        with connection:
            yield connection
    finally:
        connection.close()


def _transition(connection, task_id: str, to_state: str, reason: str, run_id: str | None = None) -> None:
    row = connection.execute("SELECT state FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError("task not found")
    from_state = row["state"]
    if from_state == to_state:
        return
    connection.execute("UPDATE tasks SET state = ?, updated_at = ? WHERE task_id = ?", (to_state, _now(), task_id))
    connection.execute(
        "INSERT INTO state_transitions(task_id, from_state, to_state, reason, run_id, created_at) VALUES(?,?,?,?,?,?)",
        (task_id, from_state, to_state, sanitize_text(reason), run_id, _now()),
    )


def _freeze_successors(connection, failed_task_id: str, reason: str) -> None:
    pending = [failed_task_id]
    seen: set[str] = set()
    while pending:
        parent = pending.pop(0)
        if parent in seen:
            continue
        seen.add(parent)
        rows = connection.execute("SELECT task_id FROM task_dependencies WHERE depends_on = ?", (parent,)).fetchall()
        for row in rows:
            child = row["task_id"]
            state = connection.execute("SELECT state FROM tasks WHERE task_id = ?", (child,)).fetchone()["state"]
            if state in {"queued", "running"}:
                _transition(connection, child, "frozen", reason)
            pending.append(child)


def _ready(connection, row: sqlite3.Row) -> bool:
    dependencies = connection.execute("SELECT depends_on FROM task_dependencies WHERE task_id = ?", (row["task_id"],)).fetchall()
    return all(connection.execute("SELECT state FROM tasks WHERE task_id = ?", (item["depends_on"],)).fetchone()["state"] == "succeeded" for item in dependencies)


def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": row["task_id"],
        "idempotency_key": row["idempotency_key"],
        "input": _load(row["input_json"]),
        "action": _load(row["action_json"]),
        "depends_on": _load(row["depends_json"]),
        "max_attempts": row["max_attempts"],
        "timeout_seconds": row["timeout_seconds"],
        "state": row["state"],
        "attempt": row["attempt"],
        "last_run_id": row["last_run_id"],
        "last_error": row["last_error"],
    }


def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "attempt": row["attempt"],
        "input_sha256": row["input_sha256"],
        "action_digest": row["action_digest"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "exit_code": row["exit_code"],
        "timed_out": bool(row["timed_out"]),
        "cancelled": bool(row["cancelled"]),
        "stdout_summary": row["stdout_summary"],
        "stderr_summary": row["stderr_summary"],
        "artifacts": _load(row["artifacts_json"]),
        "reason": row["reason"],
        "receipt_digest": row["receipt_digest"],
    }
    result["receipt"] = _load(row["receipt_json"]) if row["receipt_json"] else None
    return result


def read_snapshot(root: str | os.PathLike[str] | Path, task_id: str | None = None) -> dict[str, Any]:
    """Read scheduler task/run state without initialization, migration, or recovery."""

    project_root = _explicit_root(root)
    _validate_snapshot_task_id(task_id)
    with _readonly_snapshot_connection(project_root) as connection:
        if connection is None:
            return {
                "schema_version": SCHEMA_VERSION,
                "root": str(project_root),
                "status": "not_initialized",
                "tasks": [],
                "runs": [],
            }
        if task_id is None:
            task_rows = connection.execute("SELECT * FROM tasks ORDER BY created_at, task_id").fetchall()
            run_rows = connection.execute("SELECT * FROM runs ORDER BY started_at, run_id").fetchall()
        else:
            task_rows = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchall()
            if not task_rows:
                return {
                    "schema_version": SCHEMA_VERSION,
                    "root": str(project_root),
                    "status": "not_found",
                    "tasks": [],
                    "runs": [],
                }
            run_rows = connection.execute("SELECT * FROM runs WHERE task_id = ? ORDER BY attempt, run_id", (task_id,)).fetchall()
        if _snapshot_sidecars(database_path(project_root)):
            raise SchedulerSnapshotError("unsafe_journal_state", "scheduler journal or WAL state changed during snapshot")
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "root": str(project_root),
            "status": "ready",
            "tasks": [_snapshot_task_dict(row) for row in task_rows],
            "runs": [_snapshot_run_dict(row) for row in run_rows],
        }
        canonical_json(snapshot)
        return snapshot


def _receipt_digest(receipt: Mapping[str, Any]) -> str:
    """Digest the final receipt excluding its self-referential digest field."""

    payload = dict(receipt)
    payload.pop("receipt_digest", None)
    return sha256_bytes(canonical_json(payload))


def _finalize_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    receipt["receipt_digest"] = _receipt_digest(receipt)
    return receipt


def _windows_process_alive(pid: int) -> bool:
    """Query a Windows process without delivering a signal to it.

    Access or API-query failures are deliberately treated as alive.  Recovery
    must not claim a running task is abandoned when Windows cannot establish
    that its recorded owner has exited.
    """

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    error_invalid_parameter = 87
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() != error_invalid_parameter
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    pid = int(pid)
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class Scheduler:
    def __init__(self, root: str | os.PathLike[str] | Path) -> None:
        self.root = _explicit_root(root)
        self._cancel_events: dict[str, threading.Event] = {}
        self.recover()

    def enqueue(self, task: Mapping[str, Any]) -> dict[str, Any]:
        task = _validate_task(task)
        with _connect(self.root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM tasks WHERE idempotency_key = ?", (task["idempotency_key"],)).fetchone()
            if existing:
                same = (
                    existing["task_id"] == task["task_id"]
                    and existing["input_json"] == _json(task["input"])
                    and existing["action_json"] == _json(task["action"])
                    and existing["depends_json"] == _json(task["depends_on"])
                    and existing["max_attempts"] == task["max_attempts"]
                    and float(existing["timeout_seconds"]) == float(task["timeout_seconds"])
                )
                if not same:
                    raise ValueError("idempotency_key is already bound to another task")
                connection.commit()
                return {"status": "already_enqueued", "task": _task_dict(existing)}
            for dependency in task["depends_on"]:
                if connection.execute("SELECT 1 FROM tasks WHERE task_id = ?", (dependency,)).fetchone() is None:
                    raise ValueError("dependency is not enqueued")
            connection.execute(
                "INSERT INTO tasks(task_id,idempotency_key,input_json,action_json,depends_json,max_attempts,timeout_seconds,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (task["task_id"], task["idempotency_key"], _json(task["input"]), _json(task["action"]), _json(task["depends_on"]), task["max_attempts"], float(task["timeout_seconds"]), "queued", _now(), _now()),
            )
            for dependency in task["depends_on"]:
                connection.execute("INSERT INTO task_dependencies(task_id, depends_on) VALUES(?,?)", (task["task_id"], dependency))
            connection.execute("INSERT INTO state_transitions(task_id,from_state,to_state,reason,created_at) VALUES(?,?,?,?,?)", (task["task_id"], None, "queued", "enqueued", _now()))
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task["task_id"],)).fetchone()
            connection.commit()
            return {"status": "enqueued", "task": _task_dict(row)}

    def run_next(self, task_id: str | None = None) -> dict[str, Any]:
        with _connect(self.root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = "SELECT * FROM tasks WHERE state = 'queued' ORDER BY created_at, task_id"
            rows = connection.execute(query).fetchall()
            selected = next((row for row in rows if (task_id is None or row["task_id"] == task_id) and _ready(connection, row)), None)
            if selected is None:
                connection.commit()
                return {"status": "idle", "reason": "no_ready_task"}
            attempt = int(selected["attempt"]) + 1
            run_id = f"run-{uuid.uuid4().hex}"
            started = _now()
            connection.execute("UPDATE tasks SET attempt = ?, last_run_id = ?, cancel_requested = 0, runner_pid = ?, updated_at = ? WHERE task_id = ?", (attempt, run_id, os.getpid(), started, selected["task_id"]))
            _transition(connection, selected["task_id"], "running", "dispatch", run_id)
            input_hash = sha256_bytes(canonical_json(_load(selected["input_json"])))
            connection.execute("INSERT INTO runs(run_id,task_id,attempt,input_sha256,action_digest,started_at,status) VALUES(?,?,?,?,?,?,?)", (run_id, selected["task_id"], attempt, input_hash, action_digest(_load(selected["action_json"])), started, "running"))
            connection.commit()
            task = _task_dict(selected)
            task["attempt"] = attempt
            task["last_run_id"] = run_id

        cancel_event = threading.Event()
        self._cancel_events[task["task_id"]] = cancel_event
        stop_monitor = threading.Event()

        def monitor() -> None:
            while not stop_monitor.wait(0.02):
                with _connect(self.root) as check:
                    flag = check.execute("SELECT cancel_requested FROM tasks WHERE task_id = ?", (task["task_id"],)).fetchone()
                    if flag and flag["cancel_requested"]:
                        cancel_event.set()
                        return

        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        try:
            context = {"schema_version": SCHEMA_VERSION, "root": str(self.root), "action_digest": action_digest(task["action"]), "targets": [task["action"]["path"]] if task["action"]["type"] == "write_text" else [], "external": False, "cost_limit": 0, "data_scope": "synthetic-local", "irreversible": False}
            result = Executor(self.root).execute(task["action"], context, run_id=run_id, timeout_seconds=task["timeout_seconds"], cancel_event=cancel_event, input_payload=task["input"])
        except Exception as error:
            result = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "status": "failed", "exit_code": None, "timed_out": False, "cancelled": False, "duration_ms": 0, "action_digest": action_digest(task["action"]), "stdout_summary": "", "stderr_summary": sanitize_text(str(error)), "artifacts": [], "reason": "executor_error", "live_verified": False}
        finally:
            stop_monitor.set()
            watcher.join(timeout=1)
            self._cancel_events.pop(task["task_id"], None)
        return self._finish(task, result, run_id)

    def _finish(self, task: Mapping[str, Any], result: Mapping[str, Any], run_id: str) -> dict[str, Any]:
        with _connect(self.root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task["task_id"],)).fetchone()
            status = result["status"]
            if status not in {"succeeded", "failed", "cancelled", "timed_out"}:
                status = "failed"
            if row["cancel_requested"]:
                status = "cancelled"
            timed_out = status == "timed_out"
            cancelled = status == "cancelled"
            if status == "failed" and int(row["attempt"]) < int(row["max_attempts"]):
                final_task_state = "queued"
                reason = "retry_available"
            else:
                final_task_state = status
                reason = "cancel_requested" if cancelled and row["cancel_requested"] else result.get("reason") or status
            receipt = {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "task_id": task["task_id"],
                "attempt": row["attempt"],
            "input_sha256": result.get("input_sha256", sha256_bytes(canonical_json(task["input"]))),
                "action_digest": result["action_digest"],
                "status": status,
                "exit_code": result.get("exit_code"),
                "timed_out": timed_out,
                "cancelled": cancelled,
                "stdout_summary": sanitize_text(str(result.get("stdout_summary", "")))[:16384],
                "stderr_summary": sanitize_text(str(result.get("stderr_summary", "")))[:16384],
                "artifacts": result.get("artifacts", []),
                "reason": sanitize_text(str(reason)) if reason else None,
            }
            receipt = _finalize_receipt(receipt)
            receipt_digest = receipt["receipt_digest"]
            finished = _now()
            connection.execute("UPDATE runs SET finished_at=?,status=?,exit_code=?,timed_out=?,cancelled=?,stdout_summary=?,stderr_summary=?,artifacts_json=?,reason=?,receipt_json=?,receipt_digest=? WHERE run_id=?", (finished, status, result.get("exit_code"), int(timed_out), int(cancelled), receipt["stdout_summary"], receipt["stderr_summary"], _json(receipt["artifacts"]), receipt["reason"], _json(receipt), receipt_digest, run_id))
            connection.execute("UPDATE tasks SET last_error=?, runner_pid=NULL, updated_at=? WHERE task_id=?", (receipt["stderr_summary"] or receipt["reason"], finished, task["task_id"]))
            _transition(connection, task["task_id"], final_task_state, reason, run_id)
            if final_task_state in {"failed", "cancelled", "timed_out"}:
                _freeze_successors(connection, task["task_id"], "dependency_failed:" + task["task_id"])
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task["task_id"],)).fetchone()
            connection.commit()
            return {"status": "completed", "task": _task_dict(row), "run": receipt}

    def cancel(self, task_id: str) -> dict[str, Any]:
        with _connect(self.root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("task not found")
            if row["state"] == "queued":
                _transition(connection, task_id, "cancelled", "cancel_requested")
                _freeze_successors(connection, task_id, "dependency_cancelled:" + task_id)
            elif row["state"] == "running":
                connection.execute("UPDATE tasks SET cancel_requested=1,updated_at=? WHERE task_id=?", (_now(), task_id))
            elif row["state"] == "cancelled":
                connection.commit()
                return {"status": "already_cancelled", "task": _task_dict(row)}
            else:
                connection.commit()
                return {"status": "cancel_rejected", "reason": "terminal_state", "task": _task_dict(row)}
            connection.commit()
            return {"status": "cancel_requested" if row["state"] == "running" else "cancelled", "task": _task_dict(connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())}

    def requeue(self, task_id: str, idempotency_key: str) -> dict[str, Any]:
        if not isinstance(idempotency_key, str) or not _ID.fullmatch(idempotency_key):
            raise ValueError("idempotency_key is invalid")
        with _connect(self.root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("task not found")
            if row["idempotency_key"] != idempotency_key:
                raise ValueError("idempotency_key does not match task")
            if row["state"] == "queued":
                connection.commit()
                return {"status": "already_queued", "task": _task_dict(row)}
            if row["state"] in {"running", "succeeded", "cancelled", "timed_out", "frozen"} or row["attempt"] >= row["max_attempts"]:
                connection.commit()
                return {"status": "requeue_rejected", "reason": "state_or_attempt_limit", "task": _task_dict(row)}
            _transition(connection, task_id, "queued", "requeued")
            connection.commit()
            return {"status": "requeued", "task": _task_dict(connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())}

    def recover(self) -> dict[str, Any]:
        with _connect(self.root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            recovered = []
            for row in connection.execute("SELECT * FROM tasks WHERE state = 'running'").fetchall():
                if _process_alive(row["runner_pid"]):
                    continue
                run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (row["last_run_id"],)).fetchone()
                cancelled = bool(row["cancel_requested"])
                recovery_status = "cancelled" if cancelled else "failed"
                recovery_reason = "cancel_requested" if cancelled else "crash_recovery"
                if run and run["finished_at"] is None:
                    receipt = _finalize_receipt({
                        "schema_version": SCHEMA_VERSION,
                        "run_id": row["last_run_id"],
                        "task_id": row["task_id"],
                        "attempt": row["attempt"],
                        "input_sha256": run["input_sha256"],
                        "action_digest": run["action_digest"],
                        "status": recovery_status,
                        "exit_code": run["exit_code"],
                        "timed_out": False,
                        "cancelled": cancelled,
                        "stdout_summary": sanitize_text(run["stdout_summary"] or "")[:16384],
                        "stderr_summary": sanitize_text(run["stderr_summary"] or "")[:16384],
                        "artifacts": _load(run["artifacts_json"]),
                        "reason": recovery_reason,
                    })
                    connection.execute("UPDATE runs SET finished_at=?,status=?,timed_out=0,cancelled=?,stdout_summary=?,stderr_summary=?,artifacts_json=?,reason=?,receipt_json=?,receipt_digest=? WHERE run_id=?", (_now(), recovery_status, int(cancelled), receipt["stdout_summary"], receipt["stderr_summary"], _json(receipt["artifacts"]), recovery_reason, _json(receipt), receipt["receipt_digest"], row["last_run_id"]))
                if cancelled:
                    _transition(connection, row["task_id"], "cancelled", recovery_reason, row["last_run_id"])
                    _freeze_successors(connection, row["task_id"], "dependency_cancelled:" + row["task_id"])
                elif row["attempt"] < row["max_attempts"]:
                    _transition(connection, row["task_id"], "queued", "crash_recovery", row["last_run_id"])
                else:
                    _transition(connection, row["task_id"], "failed", "crash_recovery", row["last_run_id"])
                    _freeze_successors(connection, row["task_id"], "dependency_failed:" + row["task_id"])
                recovered.append(row["task_id"])
            connection.commit()
            return {"status": "recovered", "task_ids": recovered}

    def get_task(self, task_id: str) -> dict[str, Any]:
        with _connect(self.root) as connection:
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("task not found")
            return _task_dict(row)

    def list_runs(self, task_id: str | None = None) -> list[dict[str, Any]]:
        with _connect(self.root) as connection:
            if task_id is None:
                rows = connection.execute("SELECT * FROM runs ORDER BY started_at, run_id").fetchall()
            else:
                rows = connection.execute("SELECT * FROM runs WHERE task_id = ? ORDER BY attempt, run_id", (task_id,)).fetchall()
            return [_run_dict(row) for row in rows]


def enqueue(root, task):
    return Scheduler(root).enqueue(task)


def run_next(root, task_id=None):
    return Scheduler(root).run_next(task_id)


def cancel(root, task_id):
    return Scheduler(root).cancel(task_id)


def requeue(root, task_id, idempotency_key):
    return Scheduler(root).requeue(task_id, idempotency_key)


def recover(root):
    return Scheduler(root).recover()


def get_task(root, task_id):
    return Scheduler(root).get_task(task_id)


def list_runs(root, task_id=None):
    return Scheduler(root).list_runs(task_id)


__all__ = ["Scheduler", "SchedulerSnapshotError", "cancel", "enqueue", "get_task", "list_runs", "read_snapshot", "recover", "requeue", "run_next"]
