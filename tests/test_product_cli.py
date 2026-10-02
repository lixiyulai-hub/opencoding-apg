"""Real subprocess coverage for the Chinese W2 entry point."""

from __future__ import annotations

import hashlib
from itertools import combinations
import json
import multiprocessing
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from typing import Any
from unittest import mock

import opencoding.cli as cli_module
from opencoding.intake import QUESTION_DEFINITIONS
from opencoding.scheduler import Scheduler
from opencoding.scheduler_migrations import database_path
from opencoding.service import create_session, preview_session, submit_answer


ROOT = Path(__file__).resolve().parents[1]


def inventory(root: Path) -> dict[str, str]:
    entries: dict[str, str] = {}

    def visit(directory: Path) -> None:
        with os.scandir(directory) as children:
            for child in children:
                path = Path(child.path)
                relative = path.relative_to(root).as_posix()
                metadata = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    entries[relative] = "link:" + os.readlink(child.path)
                elif stat.S_ISDIR(metadata.st_mode):
                    entries[relative] = "directory"
                    visit(path)
                elif stat.S_ISREG(metadata.st_mode):
                    entries[relative] = "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
                else:
                    entries[relative] = f"other:{stat.S_IFMT(metadata.st_mode)}:{metadata.st_size}"

    visit(root)
    return entries


def journey_input(goal: str, confirmation: str, *, audience: str = "社区居民") -> str:
    answers = [goal, audience, "网页", "登记借用并确认归还"]
    answers.extend(["不需要"] * (len(QUESTION_DEFINITIONS) - 3))
    answers.append(confirmation)
    return "\n".join(answers) + "\n"


def run_cli(root: Path, text: str = "", *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CARGO_NET_OFFLINE"] = "true"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "opencoding", "--root", str(root), *args],
        cwd=ROOT,
        input=text,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def scheduler_task(task_id: str) -> dict[str, object]:
    return {
        "task_id": task_id,
        "input": {"private_input": "must-not-appear"},
        "action": {"type": "write_text", "path": f"{task_id}.txt", "content": "must-not-appear"},
        "depends_on": [],
        "max_attempts": 2,
        "timeout_seconds": 2,
        "idempotency_key": task_id + "-key",
    }


def _exclusive_lock_worker(database_text: str, ready: Any, release: Any) -> None:
    """Hold SQLite's writer lock in a separate process for the CLI probe.

    Reading the database with ``Path.read_bytes`` in the parent can release
    process-associated POSIX locks, so a same-process fixture is not a valid
    cross-process busy check.  A separate process keeps the lock owner and
    the CLI reader independent on Linux and Windows.
    """
    connection = sqlite3.connect(database_text, timeout=0.0, isolation_level=None)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        ready.set()
        release.wait(10)
        connection.execute("ROLLBACK")
    finally:
        connection.close()


class ProductCliSubprocessTests(unittest.TestCase):
    def test_help_and_host_boundary_are_available_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_cli(Path(directory), "", "--help")
            self.assertEqual(result.returncode, 0)
            self.assertIn("中文入口", result.stdout)

    def test_rejection_journey_writes_no_business_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_cli(root, journey_input("社区借还工具", "拒绝"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("已拒绝", result.stdout)
            self.assertIn("Host 未接通", result.stdout)
            self.assertFalse((root / "AGENTS.md").exists())
            self.assertFalse((root / "memory.md").exists())

    def test_confirmed_journey_generates_documents_and_can_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_cli(root, journey_input("社区借还工具", "确认"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("文档已生成", result.stdout)
            self.assertIn("外部能力仍未接通", result.stdout)
            match = re.search(r"事务：([^；\s]+)", result.stdout)
            self.assertIsNotNone(match, result.stdout)
            transaction_id = match.group(1)
            self.assertTrue((root / "AGENTS.md").exists())
            rolled = run_cli(root, "", "--rollback", transaction_id)
            self.assertEqual(rolled.returncode, 0, rolled.stderr)
            self.assertIn("rolled_back", rolled.stdout)
            self.assertFalse((root / "AGENTS.md").exists())

    def test_eof_pause_and_resume_keep_the_same_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paused = run_cli(root, "暂停目标\n社区居民\n")
            self.assertEqual(paused.returncode, 0, paused.stderr)
            self.assertIn("已暂停", paused.stdout)
            match = re.search(r"session-[0-9a-f]+", paused.stdout)
            self.assertIsNotNone(match, paused.stdout)
            session_id = match.group(0)
            remaining = "网页\n登记借用并确认归还\n" + "\n".join(["不需要"] * (len(QUESTION_DEFINITIONS) - 3)) + "\n拒绝\n"
            resumed = run_cli(root, remaining, "--resume", session_id)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertIn("已拒绝", resumed.stdout)
            listing = run_cli(root, "", "--list")
            self.assertEqual(listing.returncode, 0)
            self.assertIn(session_id, listing.stdout)

    def test_change_command_shows_updated_diff_and_invalidates_old_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = create_session(root, "差异工具")
            session_id = view["session"]["id"]
            changed = run_cli(root, "", "--change", session_id, "audience", "学生")
            self.assertEqual(changed.returncode, 0, changed.stderr)
            self.assertIn("答案已修改", changed.stdout)
            self.assertIn("更新后的方案与差异", changed.stdout)

    def test_preview_command_is_zero_write_including_existing_guard_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = create_session(root, "只读预览")
            before = inventory(root)
            result = run_cli(root, "", "--preview", view["session"]["id"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("只读预览（未写入）", result.stdout)
            self.assertEqual(inventory(root), before)

    def test_status_absent_and_empty_initialized_are_successful_zero_write_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            before = inventory(root)
            absent = run_cli(root, "", "--status", "--json")
            self.assertEqual(absent.returncode, 0, absent.stderr)
            self.assertEqual(json.loads(absent.stdout)["status"], "not_initialized")
            self.assertEqual(inventory(root), before)

            Scheduler(root)
            before = inventory(root)
            empty = run_cli(root, "", "--status", "--json")
            self.assertEqual(empty.returncode, 0, empty.stderr)
            self.assertEqual(json.loads(empty.stdout)["status"], "ready")
            self.assertEqual(json.loads(empty.stdout)["tasks"], [])
            self.assertEqual(inventory(root), before)

    def test_status_lists_ordered_tasks_without_action_or_input_bodies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            scheduler = Scheduler(root)
            scheduler.enqueue(scheduler_task("second"))
            scheduler.enqueue(scheduler_task("first"))
            before = inventory(root)

            structured = run_cli(root, "", "--status", "--json")
            self.assertEqual(structured.returncode, 0, structured.stderr)
            snapshot = json.loads(structured.stdout)
            self.assertEqual(snapshot["status"], "ready")
            self.assertEqual([item["task_id"] for item in snapshot["tasks"]], ["second", "first"])
            self.assertEqual(inventory(root), before)

            human = run_cli(root, "", "--status")
            self.assertEqual(human.returncode, 0, human.stderr)
            self.assertIn("任务摘要", human.stdout)
            self.assertLess(human.stdout.index("任务=second"), human.stdout.index("任务=first"))
            self.assertNotIn("must-not-appear", human.stdout)
            self.assertNotIn("write_text", human.stdout)
            self.assertEqual(inventory(root), before)

    def test_status_filters_missing_tasks_and_rejects_orphan_or_conflicting_modes_before_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            scheduler = Scheduler(root)
            scheduler.enqueue(scheduler_task("visible"))
            before = inventory(root)

            filtered = run_cli(root, "", "--status", "--task-id", "visible", "--json")
            self.assertEqual(filtered.returncode, 0, filtered.stderr)
            self.assertEqual([item["task_id"] for item in json.loads(filtered.stdout)["tasks"]], ["visible"])
            missing = run_cli(root, "", "--status", "--task-id", "missing", "--json")
            self.assertEqual(missing.returncode, 0, missing.stderr)
            self.assertEqual(json.loads(missing.stdout)["status"], "not_found")
            self.assertEqual(inventory(root), before)

            conflict = run_cli(root, "", "--status", "--list")
            self.assertEqual(conflict.returncode, 2)
            orphan = run_cli(root, "", "--task-id", "visible")
            self.assertEqual(orphan.returncode, 2)
            self.assertEqual(inventory(root), before)

    def test_status_json_errors_are_sanitized_and_leave_stores_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            invalid_root = run_cli(Path("."), "", "--status", "--json")
            self.assertEqual(invalid_root.returncode, 2)
            self.assertEqual(invalid_root.stdout, "")
            self.assertEqual(json.loads(invalid_root.stderr), {"error": {"code": "invalid_root"}})

            invalid_id = run_cli(root, "", "--status", "--task-id", "bad task id", "--json")
            self.assertEqual(invalid_id.returncode, 2)
            self.assertEqual(invalid_id.stdout, "")
            self.assertEqual(json.loads(invalid_id.stderr), {"error": {"code": "execution_status_invalid_task_id"}})
            self.assertEqual(inventory(root), {})

            database = root / ".opencoding" / "scheduler" / "state.sqlite3"
            database.parent.mkdir(parents=True)
            database.write_bytes(b"not a sqlite database")
            before = inventory(root)
            corrupt = run_cli(root, "", "--status", "--json")
            self.assertEqual(corrupt.returncode, 2)
            self.assertEqual(corrupt.stdout, "")
            self.assertEqual(json.loads(corrupt.stderr), {"error": {"code": "execution_status_database_unavailable"}})
            self.assertEqual(inventory(root), before)

    def test_status_rejects_busy_and_unsafe_stores_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            Scheduler(root)
            database = database_path(root)
            before = inventory(root)
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            locker = context.Process(
                target=_exclusive_lock_worker,
                args=(str(database), ready, release),
                daemon=True,
            )
            locker.start()
            try:
                self.assertTrue(ready.wait(10), "separate lock process did not acquire SQLite writer lock")
                busy = run_cli(root, "", "--status", "--json")
                self.assertEqual(busy.returncode, 2)
                self.assertEqual(busy.stdout, "")
                self.assertEqual(json.loads(busy.stderr), {"error": {"code": "execution_status_database_busy"}})
            finally:
                release.set()
                locker.join(10)
                if locker.is_alive():
                    locker.terminate()
                    locker.join(2)
            self.assertEqual(locker.exitcode, 0)
            self.assertEqual(inventory(root), before)

            journal = database.with_name(database.name + "-journal")
            journal.write_bytes(b"unsafe journal")
            before = inventory(root)
            unsafe = run_cli(root, "", "--status", "--json")
            self.assertEqual(unsafe.returncode, 2)
            self.assertEqual(unsafe.stdout, "")
            self.assertEqual(json.loads(unsafe.stderr), {"error": {"code": "execution_status_unsafe_journal_state"}})
            self.assertEqual(inventory(root), before)

    def test_status_delegates_only_through_the_service_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            snapshot = {"status": "ready", "tasks": [], "runs": []}
            with mock.patch.object(cli_module, "execution_status", return_value=snapshot) as execution_status:
                with redirect_stdout(StringIO()):
                    result = cli_module.main(["--root", str(root), "--status", "--task-id", "visible"])
            self.assertEqual(result, 0)
            execution_status.assert_called_once_with(str(root), "visible")

    def test_orphan_task_id_presence_and_mode_conflicts_stop_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            modes = {
                "resume": ["--resume", "session-1"],
                "list": ["--list"],
                "preview": ["--preview", "session-1"],
                "change": ["--change", "session-1", "audience", "学生"],
                "rollback": ["--rollback", "transaction-1"],
                "status": ["--status"],
            }
            blocked = [
                "_wizard",
                "execution_status",
                "rollback",
                "session_view",
                "preview_session",
                "submit_answer",
            ]
            with mock.patch.multiple(cli_module, **{name: mock.DEFAULT for name in blocked}) as calls:
                for left, right in combinations(modes, 2):
                    with self.subTest(left=left, right=right), redirect_stderr(StringIO()):
                        with self.assertRaises(SystemExit) as rejected:
                            cli_module.main(["--root", str(root), *modes[left], *modes[right]])
                    self.assertEqual(rejected.exception.code, 2)

                for task_id in ("",):
                    for name, arguments in (("wizard", []), *modes.items()):
                        if name == "status":
                            continue
                        with self.subTest(task_id=task_id, mode=name), redirect_stderr(StringIO()):
                            with self.assertRaises(SystemExit) as rejected:
                                cli_module.main(["--root", str(root), "--task-id", task_id, *arguments])
                        self.assertEqual(rejected.exception.code, 2)

            for call in calls.values():
                call.assert_not_called()

    def test_human_status_errors_are_chinese_and_json_errors_remain_machine_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            failure = cli_module.ServiceError("execution_status_database_busy", "token=sk-test-1234567890")
            with mock.patch.object(cli_module, "execution_status", side_effect=failure):
                human_stdout = StringIO()
                human_stderr = StringIO()
                with redirect_stdout(human_stdout), redirect_stderr(human_stderr):
                    self.assertEqual(cli_module.main(["--root", str(root), "--status"]), 2)
                self.assertEqual(human_stdout.getvalue(), "")
                self.assertIn("本地状态正在被占用", human_stderr.getvalue())
                self.assertNotIn("sk-test-1234567890", human_stderr.getvalue())

                json_stdout = StringIO()
                json_stderr = StringIO()
                with redirect_stdout(json_stdout), redirect_stderr(json_stderr):
                    self.assertEqual(cli_module.main(["--root", str(root), "--status", "--json"]), 2)
                self.assertEqual(json_stdout.getvalue(), "")
                self.assertEqual(json.loads(json_stderr.getvalue()), {"error": {"code": "execution_status_database_busy"}})

    def test_human_status_redacts_control_spliced_persisted_last_run_ids_without_writes(self):
        for control in ("\x1b", "\x00", "\x7f", "\u0085"):
            with self.subTest(control=ord(control)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                scheduler = Scheduler(root)
                scheduler.enqueue(scheduler_task("visible"))
                database = database_path(root)
                connection = sqlite3.connect(database)
                try:
                    connection.execute(
                        "UPDATE tasks SET last_run_id=? WHERE task_id=?",
                        ("sk-" + control + "test-1234567890", "visible"),
                    )
                    connection.commit()
                finally:
                    connection.close()

                before = inventory(root)
                result = run_cli(root, "", "--status")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("状态=排队中", result.stdout)
                self.assertNotIn("sk-test-1234567890", result.stdout)
                self.assertNotIn(control, result.stdout)
                self.assertEqual(inventory(root), before)


if __name__ == "__main__":
    unittest.main()
