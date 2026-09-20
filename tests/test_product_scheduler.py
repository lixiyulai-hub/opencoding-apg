import contextlib
import json
import hashlib
import multiprocessing
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import opencoding.scheduler as scheduler_module
from opencoding.executor import action_digest
from opencoding.scheduler import Scheduler, SchedulerSnapshotError, _process_alive, read_snapshot
from opencoding.scheduler_migrations import database_path, migrate
from opencoding.safety import canonical_json, sha256_bytes


@contextlib.contextmanager
def closing_connection(*values, **keywords):
    """Open a sqlite3 connection that is really closed on exit.

    ``sqlite3.Connection`` used as a context manager only commits or rolls back
    the open transaction; it does NOT close the handle, so the connection stays
    open until the garbage collector reclaims it and CPython then emits
    ``ResourceWarning: unclosed database``.  This wrapper keeps the transaction
    semantics (commit on success, rollback on exception) and closes the handle.
    """
    connection = sqlite3.connect(*values, **keywords)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def task(task_id, action, depends_on=None, max_attempts=2, timeout_seconds=2, key=None):
    return {
        "task_id": task_id,
        "input": {"fixture": task_id},
        "action": action,
        "depends_on": depends_on or [],
        "max_attempts": max_attempts,
        "timeout_seconds": timeout_seconds,
        "idempotency_key": key or task_id + "-key",
    }


def _inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _controlled_claim_worker(root, start, results):
    start.wait(5)
    results.put(Scheduler(root).run_next())


def _wal_switch_worker(database_text, start, results):
    if not start.wait(10):
        results.put(("timeout", None))
        return
    connection = None
    try:
        connection = sqlite3.connect(database_text, timeout=0.0, isolation_level=None)
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower()
        results.put(("mode", mode))
    except sqlite3.Error as error:
        results.put(("error", getattr(error, "sqlite_errorcode", None)))
    finally:
        if connection is not None:
            connection.close()


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.scheduler = Scheduler(self.root)

    def test_migration_reopen_and_explicit_root(self):
        self.assertEqual(database_path(self.root), self.root / ".opencoding/scheduler/state.sqlite3")
        self.scheduler = Scheduler(self.root)
        self.assertEqual(Scheduler(self.root).recover()["status"], "recovered")
        with closing_connection(database_path(self.root)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        with self.assertRaises(ValueError):
            Scheduler(".")

    def test_database_rejects_linked_metadata_and_database_files(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory).resolve()
            root = fixture / "root"
            outside = fixture / "outside"
            root.mkdir()
            outside.mkdir()
            (root / ".opencoding").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                Scheduler(root)
            self.assertFalse((outside / "scheduler" / "state.sqlite3").exists())

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory).resolve()
            root = fixture / "root"
            root.mkdir()
            outside = fixture / "outside.sqlite3"
            outside.write_bytes(b"")
            parent = root / ".opencoding" / "scheduler"
            parent.mkdir(parents=True)
            os.link(outside, parent / "state.sqlite3")
            with self.assertRaises(ValueError):
                Scheduler(root)
            self.assertEqual(outside.read_bytes(), b"")

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory).resolve()
            root = fixture / "root"
            root.mkdir()
            outside = fixture / "outside.sqlite3-wal"
            outside.write_bytes(b"")
            parent = root / ".opencoding" / "scheduler"
            parent.mkdir(parents=True)
            os.link(outside, parent / "state.sqlite3-wal")
            with self.assertRaises(ValueError):
                Scheduler(root)
            self.assertEqual(outside.read_bytes(), b"")

    def test_scheduler_closes_every_opened_connection(self):
        opened = []
        original = sqlite3.connect

        def tracked_connect(*values, **keywords):
            connection = original(*values, **keywords)
            opened.append(connection)
            return connection

        try:
            with mock.patch.object(scheduler_module.sqlite3, "connect", side_effect=tracked_connect):
                Scheduler(self.root)
            self.assertTrue(opened)
            for connection in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
        finally:
            for connection in opened:
                connection.close()

    def test_read_snapshot_absent_store_is_zero_write_and_does_not_construct_scheduler(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            before = _inventory(root)
            with (
                mock.patch.object(scheduler_module, "Scheduler", side_effect=AssertionError("snapshot constructed Scheduler")),
                mock.patch.object(scheduler_module, "migrate", side_effect=AssertionError("snapshot migrated database")),
            ):
                snapshot = read_snapshot(root)
            self.assertEqual(snapshot, {
                "schema_version": "1.0",
                "root": str(root),
                "status": "not_initialized",
                "tasks": [],
                "runs": [],
            })
            self.assertEqual(_inventory(root), before)

    def test_read_snapshot_non_windows_absent_store_stays_zero_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            before = _inventory(root)
            with (
                mock.patch.object(scheduler_module, "_snapshot_platform_supported", return_value=False),
                mock.patch.object(Path, "open", side_effect=AssertionError("absent snapshot opened a database")),
                mock.patch.object(scheduler_module.sqlite3, "connect", side_effect=AssertionError("absent snapshot opened SQLite")),
            ):
                snapshot = read_snapshot(root)
            self.assertEqual(snapshot["status"], "not_initialized")
            self.assertEqual(_inventory(root), before)

    def test_read_snapshot_non_windows_initialized_fails_before_open_and_preserves_existing_lock(self):
        database = database_path(self.root)
        before = _inventory(self.root)
        context = multiprocessing.get_context("spawn")

        def competing_result():
            start = context.Event()
            results = context.Queue()
            writer = context.Process(target=_wal_switch_worker, args=(str(database), start, results))
            try:
                writer.start()
                start.set()
                outcome = results.get(timeout=10)
            finally:
                writer.join(10)
                if writer.is_alive():
                    writer.terminate()
                    writer.join(10)
                self.assertEqual(writer.exitcode, 0)
            return outcome

        held = sqlite3.connect(database, isolation_level=None)
        try:
            held.execute("BEGIN EXCLUSIVE")
            self.assertNotEqual(competing_result(), ("mode", "wal"))
            with (
                mock.patch.object(scheduler_module, "_snapshot_platform_supported", return_value=False),
                mock.patch.object(Path, "open", side_effect=AssertionError("initialized snapshot opened a database")),
                mock.patch.object(scheduler_module.sqlite3, "connect", side_effect=AssertionError("initialized snapshot opened SQLite")),
            ):
                with self.assertRaises(SchedulerSnapshotError) as unsupported:
                    read_snapshot(self.root)
            self.assertEqual(unsupported.exception.code, "unsupported_platform")
            self.assertNotEqual(competing_result(), ("mode", "wal"))
            self.assertEqual(_inventory(self.root), before)
        finally:
            held.execute("ROLLBACK")
            held.close()

    def test_read_snapshot_is_coherent_ordered_and_does_not_recover_running_task(self):
        self.scheduler.enqueue(task("second", {"type": "write_text", "path": "second.txt", "content": "two"}))
        self.scheduler.enqueue(task("first", {"type": "write_text", "path": "first.txt", "content": "one"}))
        database = database_path(self.root)
        with closing_connection(database) as connection:
            connection.execute("UPDATE tasks SET state='running', attempt=1, last_run_id='run-first', runner_pid=999999 WHERE task_id='first'")
            connection.execute("INSERT INTO runs(run_id,task_id,attempt,input_sha256,action_digest,started_at,status) VALUES('run-first','first',1,'input','action','0000','running')")
            connection.commit()
        completed = self.scheduler.run_next()
        self.assertEqual(completed["task"]["task_id"], "second")
        self.assertEqual(completed["task"]["state"], "succeeded")
        before = _inventory(self.root)
        with (
            mock.patch.object(scheduler_module, "Scheduler", side_effect=AssertionError("snapshot constructed Scheduler")),
            mock.patch.object(scheduler_module, "migrate", side_effect=AssertionError("snapshot migrated database")),
        ):
            snapshot = read_snapshot(self.root)
        self.assertEqual(snapshot["status"], "ready")
        self.assertEqual([item["task_id"] for item in snapshot["tasks"]], ["second", "first"])
        self.assertEqual(snapshot["tasks"][0]["state"], "succeeded")
        self.assertEqual(snapshot["tasks"][1]["state"], "running")
        self.assertEqual([item["run_id"] for item in snapshot["runs"]][0], "run-first")
        self.assertEqual(snapshot["runs"][1]["status"], "succeeded")
        self.assertEqual(_inventory(self.root), before)
        self.assertEqual(self.scheduler.get_task("first")["state"], "running")

    def test_read_snapshot_filter_not_found_and_connections_close(self):
        self.scheduler.enqueue(task("visible", {"type": "write_text", "path": "visible.txt", "content": "visible"}))
        before = _inventory(self.root)
        missing = read_snapshot(self.root, "missing")
        self.assertEqual(missing["status"], "not_found")
        self.assertEqual(missing["tasks"], [])
        self.assertEqual(missing["runs"], [])
        visible = read_snapshot(self.root, "visible")
        self.assertEqual([item["task_id"] for item in visible["tasks"]], ["visible"])
        self.assertEqual(_inventory(self.root), before)

        opened = []
        original = sqlite3.connect

        def tracked_connect(*values, **keywords):
            connection = original(*values, **keywords)
            opened.append(connection)
            return connection

        with mock.patch.object(scheduler_module.sqlite3, "connect", side_effect=tracked_connect):
            self.assertEqual(read_snapshot(self.root, "visible")["status"], "ready")
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_read_snapshot_rejects_invalid_and_unsafe_storage_without_writes(self):
        with self.assertRaises(SchedulerSnapshotError) as invalid:
            read_snapshot(self.root, "bad task id")
        self.assertEqual(invalid.exception.code, "invalid_task_id")
        with self.assertRaises(ValueError):
            read_snapshot(".")

        database = database_path(self.root)
        with closing_connection(database) as connection:
            connection.execute("PRAGMA user_version = 3")
            connection.commit()
        before = _inventory(self.root)
        with self.assertRaises(SchedulerSnapshotError) as unsupported:
            read_snapshot(self.root)
        self.assertEqual(unsupported.exception.code, "unsupported_schema")
        self.assertEqual(_inventory(self.root), before)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            scheduler_root = root / ".opencoding" / "scheduler"
            scheduler_root.mkdir(parents=True)
            (scheduler_root / "state.sqlite3").write_bytes(b"not a sqlite database")
            before = _inventory(root)
            with self.assertRaises(SchedulerSnapshotError) as corrupt:
                read_snapshot(root)
            self.assertEqual(corrupt.exception.code, "database_unavailable")
            self.assertEqual(_inventory(root), before)

    def test_read_snapshot_rejects_schema_defects_and_nonfinite_json_on_all_views(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            Scheduler(root)
            database = database_path(root)
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute("DROP TABLE tasks")
                connection.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, input_json TEXT NOT NULL, action_json TEXT NOT NULL, depends_json TEXT NOT NULL, max_attempts INTEGER NOT NULL, timeout_seconds REAL NOT NULL, state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0, runner_pid INTEGER, last_run_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
                connection.commit()
            finally:
                connection.close()
            before = _inventory(root)
            opened = []
            original = sqlite3.connect

            def tracked_connect(*values, **keywords):
                snapshot_connection = original(*values, **keywords)
                opened.append(snapshot_connection)
                return snapshot_connection

            with mock.patch.object(scheduler_module.sqlite3, "connect", side_effect=tracked_connect):
                with self.assertRaises(SchedulerSnapshotError) as missing_column:
                    read_snapshot(root, "absent")
            self.assertEqual(missing_column.exception.code, "unsupported_schema")
            self.assertEqual(len(opened), 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                opened[0].execute("SELECT 1")
            self.assertEqual(_inventory(root), before)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            Scheduler(root)
            database = database_path(root)
            connection = sqlite3.connect(database)
            try:
                connection.execute("DROP TABLE runs")
                connection.commit()
            finally:
                connection.close()
            before = _inventory(root)
            with self.assertRaises(SchedulerSnapshotError) as missing_table:
                read_snapshot(root, "absent")
            self.assertEqual(missing_table.exception.code, "unsupported_schema")
            self.assertEqual(_inventory(root), before)

        self.scheduler.enqueue(task("nan", {"type": "write_text", "path": "nan.txt", "content": "safe"}))
        database = database_path(self.root)
        with closing_connection(database) as connection:
            connection.execute("UPDATE tasks SET input_json='NaN' WHERE task_id='nan'")
            connection.commit()
        before = _inventory(self.root)
        with self.assertRaises(SchedulerSnapshotError) as nonfinite:
            read_snapshot(self.root, "nan")
        self.assertEqual(nonfinite.exception.code, "database_unavailable")
        self.assertEqual(_inventory(self.root), before)

    def test_read_snapshot_rejects_locked_and_linked_metadata_without_writes(self):
        database = database_path(self.root)
        lock = sqlite3.connect(database, isolation_level=None)
        try:
            lock.execute("BEGIN EXCLUSIVE")
            before = _inventory(self.root)
            with self.assertRaises(SchedulerSnapshotError) as busy:
                read_snapshot(self.root)
            self.assertEqual(busy.exception.code, "database_busy")
            self.assertEqual(_inventory(self.root), before)
        finally:
            lock.execute("ROLLBACK")
            lock.close()

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory).resolve()
            root = fixture / "root"
            outside = fixture / "outside"
            root.mkdir()
            outside.mkdir()
            (root / ".opencoding").symlink_to(outside, target_is_directory=True)
            before = _inventory(root)
            with self.assertRaises(ValueError):
                read_snapshot(root)
            self.assertEqual(_inventory(root), before)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory).resolve()
            root = fixture / "root"
            root.mkdir()
            outside = fixture / "outside.sqlite3"
            outside.write_bytes(b"")
            parent = root / ".opencoding" / "scheduler"
            parent.mkdir(parents=True)
            os.link(outside, parent / "state.sqlite3")
            before = _inventory(root)
            with self.assertRaises(ValueError):
                read_snapshot(root)
            self.assertEqual(_inventory(root), before)

    def test_read_snapshot_rejects_journal_and_active_wal_without_side_effects(self):
        database = database_path(self.root)
        self.scheduler.enqueue(task("wal-task", {"type": "write_text", "path": "wal.txt", "content": "pending"}))
        journal = database.with_name(database.name + "-journal")
        journal.write_bytes(b"untrusted journal")
        before = _inventory(self.root)
        with self.assertRaises(SchedulerSnapshotError) as journal_error:
            read_snapshot(self.root)
        self.assertEqual(journal_error.exception.code, "unsafe_journal_state")
        self.assertEqual(_inventory(self.root), before)
        journal.unlink()

        connection = sqlite3.connect(database)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            connection.execute("UPDATE tasks SET updated_at='wal-pending' WHERE task_id='wal-task'")
            connection.commit()
            self.assertTrue(database.with_name(database.name + "-wal").exists())
            before = _inventory(self.root)
            with self.assertRaises(SchedulerSnapshotError) as wal_error:
                read_snapshot(self.root)
            self.assertEqual(wal_error.exception.code, "unsafe_journal_state")
            self.assertEqual(_inventory(self.root), before)
        finally:
            connection.close()

    def test_read_snapshot_rejects_closed_wal_mode_database_without_sidecars(self):
        database = database_path(self.root)
        with closing_connection(database) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            connection.commit()
        for suffix in ("-wal", "-shm"):
            sidecar = database.with_name(database.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
        before = _inventory(self.root)
        with self.assertRaises(SchedulerSnapshotError) as wal_error:
            read_snapshot(self.root)
        self.assertEqual(wal_error.exception.code, "unsafe_journal_state")
        self.assertEqual(_inventory(self.root), before)

    def test_read_snapshot_blocks_a_competing_wal_switch_without_sidecar_writes(self):
        database = database_path(self.root)
        before = _inventory(self.root)
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        writer = context.Process(target=_wal_switch_worker, args=(str(database), start, results))
        original_connect = scheduler_module.sqlite3.connect

        def gated_connect(*values, **keywords):
            start.set()
            writer_result = results.get(timeout=10)
            self.assertNotEqual(writer_result, ("mode", "wal"))
            return original_connect(*values, **keywords)

        try:
            writer.start()
            with mock.patch.object(scheduler_module.sqlite3, "connect", side_effect=gated_connect):
                snapshot = read_snapshot(self.root)
            self.assertEqual(snapshot["status"], "ready")
        finally:
            start.set()
            writer.join(10)
            if writer.is_alive():
                writer.terminate()
                writer.join(10)
            self.assertEqual(writer.exitcode, 0)
        self.assertEqual(_inventory(self.root), before)

    def test_read_snapshot_uses_escaped_uri_for_platform_valid_special_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "space # unicode-\u6d4b\u8bd5"
            root.mkdir()
            Scheduler(root)
            self.assertEqual(read_snapshot(root)["root"], str(root))

    def test_migration_failure_rolls_back_schema_and_version(self):
        with closing_connection(":memory:", isolation_level=None) as connection:

            def deny_runs(action, arg1, _arg2, _database, _source):
                if action == sqlite3.SQLITE_CREATE_TABLE and arg1 == "runs":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(deny_runs)
            with self.assertRaises(sqlite3.DatabaseError):
                migrate(connection)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
            connection.set_authorizer(None)
            self.assertEqual(migrate(connection), 2)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_migration_reads_version_after_immediate_lock(self):
        with closing_connection(":memory:", isolation_level=None) as connection:
            trace = []
            connection.set_trace_callback(trace.append)
            self.assertEqual(migrate(connection), 2)
            begin_index = next(index for index, statement in enumerate(trace) if statement.upper() == "BEGIN IMMEDIATE")
            version_index = next(index for index, statement in enumerate(trace) if statement.upper() == "PRAGMA USER_VERSION")
            self.assertLess(begin_index, version_index)

    def test_process_liveness_uses_non_signaling_windows_query_and_preserves_posix_probe(self):
        import ctypes

        kernel32 = mock.Mock()
        kernel32.OpenProcess.return_value = 99
        kernel32.GetExitCodeProcess.return_value = 0
        with (
            mock.patch.object(scheduler_module.os, "name", "nt"),
            mock.patch.object(ctypes, "WinDLL", return_value=kernel32, create=True),
            mock.patch.object(scheduler_module.os, "kill", side_effect=AssertionError("Windows must not signal probe")),
        ):
            self.assertTrue(_process_alive(12345))
        kernel32.OpenProcess.assert_called_once()
        kernel32.CloseHandle.assert_called_once_with(99)

        with mock.patch.object(scheduler_module.os, "name", "posix"), mock.patch.object(scheduler_module.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(_process_alive(12345))
        with mock.patch.object(scheduler_module.os, "name", "posix"), mock.patch.object(scheduler_module.os, "kill", side_effect=PermissionError):
            self.assertTrue(_process_alive(12345))

    def test_dependency_order_and_success(self):
        first = task("first", {"type": "write_text", "path": "out/first.txt", "content": "one"})
        second = task("second", {"type": "write_text", "path": "out/second.txt", "content": "two"}, ["first"])
        self.scheduler.enqueue(first)
        self.scheduler.enqueue(second)
        self.assertEqual(self.scheduler.run_next()["task"]["task_id"], "first")
        self.assertEqual(self.scheduler.run_next()["task"]["task_id"], "second")
        self.assertEqual(self.scheduler.get_task("second")["state"], "succeeded")

    def test_failure_freezes_successor_nonzero_and_receipt(self):
        bad = task("bad", {"type": "python_module", "module": "definitely_missing_fixture", "args": []}, max_attempts=1)
        child = task("child", {"type": "write_text", "path": "child.txt", "content": "no"}, ["bad"])
        self.scheduler.enqueue(bad)
        self.scheduler.enqueue(child)
        result = self.scheduler.run_next()
        self.assertEqual(result["run"]["status"], "failed")
        self.assertEqual(result["run"]["exit_code"] != 0, True)
        self.assertEqual(self.scheduler.get_task("child")["state"], "frozen")
        run = self.scheduler.list_runs("bad")[0]
        self.assertEqual(run["receipt_digest"], result["run"]["receipt_digest"])

    def test_cancel_timeout_retry_limit_and_idempotent_requeue(self):
        queued = task("queued", {"type": "write_text", "path": "queued.txt", "content": "x"})
        self.scheduler.enqueue(queued)
        self.assertEqual(self.scheduler.requeue("queued", "queued-key")["status"], "already_queued")
        self.assertEqual(len(self.scheduler.list_runs()), 0)
        self.assertEqual(self.scheduler.cancel("queued")["task"]["state"], "cancelled")

        module = self.root / "fixture_task.py"
        module.write_text("import sys, time\nif sys.argv[1] == 'fail': raise SystemExit(9)\ntime.sleep(2)\n", encoding="utf-8")
        retry = task("retry", {"type": "python_module", "module": "fixture_task", "args": ["fail"]}, max_attempts=2)
        self.scheduler.enqueue(retry)
        self.assertEqual(self.scheduler.run_next()["task"]["state"], "queued")
        self.assertEqual(self.scheduler.run_next()["task"]["state"], "failed")
        self.assertEqual(self.scheduler.requeue("retry", "retry-key")["status"], "requeue_rejected")

        slow = task("slow", {"type": "python_module", "module": "fixture_task", "args": ["sleep"]}, timeout_seconds=0.05, max_attempts=1)
        self.scheduler.enqueue(slow)
        self.assertEqual(self.scheduler.run_next()["run"]["status"], "timed_out")

    def test_crash_recovery_requeues_unfinished_run(self):
        self.scheduler.enqueue(task("recover", {"type": "write_text", "path": "recover.txt", "content": "ok"}))
        with closing_connection(database_path(self.root)) as db:
            db.execute("UPDATE tasks SET state='running', attempt=1, last_run_id='run-crash'")
            db.execute("INSERT INTO runs(run_id,task_id,attempt,input_sha256,action_digest,started_at,status) VALUES('run-crash','recover',1,'in','action','now','running')")
            db.commit()
        recovered = Scheduler(self.root).get_task("recover")
        self.assertEqual(recovered["state"], "queued")
        run = Scheduler(self.root).list_runs("recover")[0]
        self.assertEqual(run["reason"], "crash_recovery")
        receipt = run["receipt"]
        self.assertEqual(
            set(receipt),
            {"schema_version", "run_id", "task_id", "attempt", "input_sha256", "action_digest", "status", "exit_code", "timed_out", "cancelled", "stdout_summary", "stderr_summary", "artifacts", "reason", "receipt_digest"},
        )
        unsigned = dict(receipt)
        unsigned.pop("receipt_digest")
        self.assertEqual(receipt["receipt_digest"], sha256_bytes(canonical_json(unsigned)))
        self.assertEqual(receipt["receipt_digest"], run["receipt_digest"])

    def test_crash_recovery_honors_pending_cancellation(self):
        self.scheduler.enqueue(task("cancel-recover", {"type": "write_text", "path": "cancel-recover.txt", "content": "ok"}))
        with closing_connection(database_path(self.root)) as db:
            db.execute("UPDATE tasks SET state='running', attempt=1, last_run_id='run-cancel-crash', cancel_requested=1")
            db.execute("INSERT INTO runs(run_id,task_id,attempt,input_sha256,action_digest,started_at,status) VALUES('run-cancel-crash','cancel-recover',1,'in','action','now','running')")
            db.commit()
        recovered = Scheduler(self.root).get_task("cancel-recover")
        self.assertEqual(recovered["state"], "cancelled")
        run = Scheduler(self.root).list_runs("cancel-recover")[0]
        self.assertEqual(run["status"], "cancelled")
        self.assertTrue(run["cancelled"])
        self.assertFalse(run["timed_out"])
        self.assertEqual(run["reason"], "cancel_requested")

    def test_running_task_can_be_cancelled_from_another_scheduler(self):
        self.scheduler.enqueue(task("cancel-run", {"type": "write_text", "path": "cancel.txt", "content": "x"}, timeout_seconds=5, max_attempts=1))
        started = threading.Event()

        def controlled_execute(_executor, action, _context, **kwargs):
            started.set()
            self.assertTrue(kwargs["cancel_event"].wait(3), "cross-instance cancellation was not observed")
            return {
                "schema_version": "1.0",
                "run_id": kwargs["run_id"],
                "status": "succeeded",
                "exit_code": 0,
                "timed_out": False,
                "cancelled": False,
                "duration_ms": 0,
                "input_sha256": sha256_bytes(canonical_json(kwargs["input_payload"])),
                "action_digest": action_digest(action),
                "stdout_summary": "",
                "stderr_summary": "",
                "artifacts": [],
                "reason": "cancel_requested",
                "live_verified": False,
            }

        result = []
        other = Scheduler(self.root)
        with mock.patch("opencoding.scheduler.Executor.execute", new=controlled_execute):
            thread = threading.Thread(target=lambda: result.append(self.scheduler.run_next()))
            thread.start()
            self.assertTrue(started.wait(2), "task did not enter controlled execution")
            self.assertEqual(other.cancel("cancel-run")["status"], "cancel_requested")
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["run"]["status"], "cancelled")
        self.assertTrue(result[0]["run"]["cancelled"])
        self.assertFalse(result[0]["run"]["timed_out"])
        self.assertEqual(result[0]["run"]["reason"], "cancel_requested")
        self.assertEqual(result[0]["task"]["state"], "cancelled")

    def test_terminal_cancellation_is_idempotent_and_preserves_completed_receipt(self):
        complete = task("complete", {"type": "write_text", "path": "complete.txt", "content": "ok"})
        self.scheduler.enqueue(complete)
        completed = self.scheduler.run_next()
        receipt = completed["run"]
        rejected = self.scheduler.cancel("complete")
        self.assertEqual(rejected["status"], "cancel_rejected")
        self.assertEqual(rejected["task"]["state"], "succeeded")
        self.assertEqual(self.scheduler.list_runs("complete")[0]["receipt"], receipt)

        queued = task("idempotent-cancel", {"type": "write_text", "path": "cancelled.txt", "content": "x"})
        self.scheduler.enqueue(queued)
        self.assertEqual(self.scheduler.cancel("idempotent-cancel")["status"], "cancelled")
        self.assertEqual(self.scheduler.cancel("idempotent-cancel")["status"], "already_cancelled")

    def test_two_processes_claim_only_one_task(self):
        module = self.root / "fixture_claim.py"
        module.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "root = Path.cwd()\n"
            "ready = root / 'claim-ready'\n"
            "release = root / 'claim-release'\n"
            "ready.write_text('ready', encoding='utf-8')\n"
            "while not release.exists(): pass\n",
            encoding="utf-8",
        )
        self.scheduler.enqueue(task("only-once", {"type": "python_module", "module": "fixture_claim", "args": []}, timeout_seconds=5))
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        first = context.Process(target=_controlled_claim_worker, args=(str(self.root), start, results))
        second = None
        release = self.root / "claim-release"
        try:
            first.start()
            start.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (self.root / "claim-ready").exists():
                time.sleep(0.01)
            self.assertTrue((self.root / "claim-ready").exists(), "first worker did not claim task")
            second_start = context.Event()
            second_start.set()
            second = context.Process(target=_controlled_claim_worker, args=(str(self.root), second_start, results))
            second.start()
            idle = results.get(timeout=5)
            self.assertEqual(idle["status"], "idle")
            release.write_text("release", encoding="utf-8")
            completed = results.get(timeout=10)
            for worker in (first, second):
                worker.join(10)
                self.assertEqual(worker.exitcode, 0)
            outcomes = [idle, completed]
            completed_runs = [outcome for outcome in outcomes if outcome["status"] == "completed"]
            idle = [outcome for outcome in outcomes if outcome["status"] == "idle"]
            self.assertEqual(len(completed_runs), 1)
            self.assertEqual(len(idle), 1)
            self.assertEqual(len(self.scheduler.list_runs("only-once")), 1)
        finally:
            if not release.exists():
                release.write_text("release", encoding="utf-8")
            for worker in (first, second):
                if worker is not None and worker.pid is not None:
                    if worker.is_alive():
                        worker.terminate()
                    worker.join(10)
                    self.assertFalse(worker.is_alive(), "owned synthetic worker leaked")

    def test_requeue_rejects_running_and_idempotency_binds_every_task_field(self):
        original = task("immutable", {"type": "write_text", "path": "immutable.txt", "content": "x"}, max_attempts=2)
        self.scheduler.enqueue(original)
        altered = dict(original, max_attempts=3)
        with self.assertRaises(ValueError):
            self.scheduler.enqueue(altered)
        with closing_connection(database_path(self.root)) as db:
            db.execute("UPDATE tasks SET state='running', attempt=1")
            db.commit()
        rejected = self.scheduler.requeue("immutable", "immutable-key")
        self.assertEqual(rejected["status"], "requeue_rejected")


if __name__ == "__main__":
    unittest.main()
