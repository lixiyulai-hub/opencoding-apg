"""Read-only TaskPlan CLI contracts, including real subprocess disk inventories."""

from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import opencoding.cli as cli
from opencoding.intake import QUESTION_DEFINITIONS
from opencoding.scheduler import Scheduler
from opencoding.service import ServiceError, create_session, submit_answer
from opencoding.sessions import session_write_lock
from opencoding.taskplan_scheduler import preview_task_plan
from tests.test_product_cli import inventory
from tests.test_product_service import _complete


ROOT = Path(__file__).resolve().parents[1]
WRITE_ENTRYPOINTS = (
    "opencoding.cli._wizard",
    "opencoding.cli.create_session",
    "opencoding.cli.submit_answer",
    "opencoding.cli.build_caller_confirmation",
    "opencoding.cli.approve_preview",
    "opencoding.cli.apply_approved",
    "opencoding.cli.rollback",
    "opencoding.service.create_session",
    "opencoding.service.submit_answer",
    "opencoding.service.save_session",
    "opencoding.service.apply_approved",
    "opencoding.service.rollback",
    "opencoding.sessions.save_session",
    "opencoding.sessions.load_session",
    "opencoding.sessions._acquire_lock",
    "opencoding.taskplan_scheduler.build_task_plan_confirmation",
    "opencoding.taskplan_scheduler.approve_task_plan",
    "opencoding.taskplan_scheduler.execute_task_plan",
    "opencoding.scheduler.Scheduler.__init__",
    "opencoding.scheduler.Scheduler.enqueue",
    "opencoding.scheduler.Scheduler.run_next",
    "opencoding.scheduler.Scheduler.requeue",
    "opencoding.scheduler.Scheduler.recover",
    "opencoding.scheduler_migrations.migrate",
    "opencoding.transactions.apply_changes",
    "opencoding.transactions.rollback_changes",
)


def run_cli(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", CARGO_NET_OFFLINE="true")
    return subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-m", "opencoding", "--root", str(root), *args],
        input="确认\n", encoding="utf-8", capture_output=True, cwd=ROOT,
        env=environment, timeout=30, check=False,
    )


class TaskPlanCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.container = Path(self.temporary.name).resolve()
        self.root = self.container / "project"
        self.root.mkdir()
        (self.root / "empty").mkdir()
        (self.root / "sentinel.bin").write_bytes(b"unchanged\x00\xff")
        self.view = _complete(self.root)
        self.session_id = self.view["session"]["id"]
        self.session_path = self.root / ".opencoding" / "sessions" / (self.session_id + ".json")

    def read(self, *, structured=False, root=None, session_id=None):
        arguments = ["--task-preview", self.session_id if session_id is None else session_id]
        if structured:
            arguments.append("--json")
        before = inventory(self.container)
        result = run_cli(self.root if root is None else root, *arguments)
        self.assertEqual(inventory(self.container), before)
        return result

    def assert_json_preview(self, session_id=None):
        expected = preview_task_plan(self.root, session_id or self.session_id)
        result = self.read(structured=True, session_id=session_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), expected)
        return expected

    def test_ready_human_preview_has_exact_scope_diff_ids_dependencies_and_no_execution(self):
        (self.root / "PRG.md").write_text("existing requirements\n", encoding="utf-8")
        preview = self.assert_json_preview()
        self.assertEqual(preview["status"], "ready")
        result = self.read()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"项目根目录：{preview['root']}", result.stdout)
        self.assertIn(f"当前会话：{self.session_id}；版本：{self.view['session']['revision']}", result.stdout)
        self.assertIn("方案状态：ready（仅表示方案状态，不表示任务运行成功）", result.stdout)
        for target in preview["targets"]:
            self.assertIn("- " + target + "\n", result.stdout)
        self.assertIn(preview["diff"], result.stdout)
        for task in preview["tasks"]:
            self.assertIn(f"任务 ID：{task['task_id']}；方案任务 ID：{task['input']['plan_task_id']}", result.stdout)
            self.assertIn("依赖：" + ("、".join(task["depends_on"]) or "无"), result.stdout)
        self.assertIn("document（本地文档，未激活）", result.stdout)
        self.assertIn("预计效果（仅在另外确认并调用执行 API 后可能发生，本次未发生）", result.stdout)
        self.assertIn("未审批、未执行、未创建或恢复 Scheduler", result.stdout)
        self.assertFalse((self.root / ".opencoding" / "scheduler").exists())

    def test_unresolved_preview_is_successful_read_with_same_api_data(self):
        view = create_session(self.root, "尚未澄清的项目")
        session_id = view["session"]["id"]
        preview = self.assert_json_preview(session_id)
        self.assertNotEqual(preview["status"], "ready")
        self.assertTrue(preview["service_preview"]["task_plan"]["unresolved"])
        result = self.read(session_id=session_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("仍待确认：", result.stdout)
        for unresolved in preview["service_preview"]["task_plan"]["unresolved"]:
            self.assertIn(unresolved, result.stdout)
        self.assertIn("未执行", result.stdout)

    def test_external_capability_is_offline_design_and_never_activated(self):
        view = create_session(self.root, "社区离线登记")
        answers = {"audience": "社区居民", "outcome": "登记并查看记录", "platform": "网页", "data_persistence": "需要"}
        for question in QUESTION_DEFINITIONS:
            view = submit_answer(self.root, view["session"]["id"], view["session"]["revision"], question["id"], answers.get(question["id"], "不需要"))
        preview = self.assert_json_preview(view["session"]["id"])
        designs = [task for task in preview["tasks"] if task["input"]["classification"] == "offline_design"]
        self.assertTrue(designs)
        self.assertTrue(all(task["input"]["activation_status"] == "not_activated" for task in designs))
        self.assertFalse(preview["effects"]["external"])
        result = self.read(session_id=view["session"]["id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("offline_design（仅离线设计，未激活外部能力）", result.stdout)
        self.assertFalse((self.root / "integrations").exists())

    def test_host_missing_is_a_preview_blocker_not_a_runtime_or_frozen_claim(self):
        preview = self.assert_json_preview()
        self.assertTrue(any(task["input"]["classification"] == "host_missing" for task in preview["tasks"]))
        self.assertTrue(all("state" not in task for task in preview["tasks"]))
        result = self.read()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("host_missing（缺少 Host 执行器，无法执行实现或验证）", result.stdout)
        self.assertIn("未查询实际运行或冻结状态", result.stdout)
        self.assertNotIn("已成功", result.stdout)
        self.assertNotIn("已冻结", result.stdout)
        self.assertFalse((self.root / "src").exists())

    def test_repeated_previews_ignore_confirmation_stdin_and_preserve_guard_bytes(self):
        guard = self.root / ".opencoding" / ".session-write.lock"
        before = guard.read_bytes()
        first = self.read(structured=True)
        second = self.read(structured=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(guard.read_bytes(), before)
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_absent_guard_is_not_recreated_and_write_locked_guard_is_not_touched(self):
        guard = self.root / ".opencoding" / ".session-write.lock"
        guard.unlink()
        self.assert_json_preview()
        self.assertFalse(guard.exists())
        with session_write_lock(self.root):
            before = guard.read_bytes()
            self.assert_json_preview()
            self.assertEqual(guard.read_bytes(), before)

    def test_busy_existing_scheduler_is_neither_read_nor_recovered(self):
        scheduler = Scheduler(self.root)
        scheduler.enqueue({"task_id": "unrelated", "idempotency_key": "unrelated", "input": {},
                           "action": {"type": "write_text", "path": "unrelated.txt", "content": "untouched"},
                           "depends_on": [], "max_attempts": 1, "timeout_seconds": 1})
        connection = sqlite3.connect(self.root / ".opencoding" / "scheduler" / "state.sqlite3", timeout=0.0, isolation_level=None)
        try:
            connection.execute("BEGIN EXCLUSIVE")
            self.assert_json_preview()
        finally:
            connection.execute("ROLLBACK")
            connection.close()
        self.assertFalse((self.root / "unrelated.txt").exists())
        self.assertEqual(scheduler.get_task("unrelated")["state"], "queued")

    def test_unrelated_symlinks_and_target_symlink_failures_preserve_all_entries(self):
        outside = self.container / "outside.txt"
        outside.write_text("outside unchanged", encoding="utf-8")
        try:
            (self.root / "unrelated-link").symlink_to(outside)
            (self.root / "directory-link").symlink_to(self.root / "empty", target_is_directory=True)
        except OSError as error:
            self.skipTest("symlink creation unavailable: " + str(error))
        self.assert_json_preview()
        (self.root / "PRG.md").symlink_to(outside)
        result = self.read(structured=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("error", json.loads(result.stderr))
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside unchanged")

    def test_missing_root_session_and_invalid_ids_fail_without_creating_paths(self):
        empty = self.container / "empty-project"
        empty.mkdir()
        cases = [(self.container / "missing", "session-missing"), (empty, "session-missing"),
                 (self.root, "session-missing"), (Path("."), self.session_id)]
        cases.extend((self.root, value) for value in ("", "../escape", "bad/id", "bad id", "x" * 129))
        for root, session_id in cases:
            for structured in (False, True):
                with self.subTest(root=root.name, session_id=session_id, json=structured):
                    result = self.read(root=root, session_id=session_id, structured=structured)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    if structured:
                        self.assertEqual(set(json.loads(result.stderr)), {"error"})
                    else:
                        self.assertIn("无法读取 TaskPlan 预览", result.stderr)
        self.assertEqual(inventory(empty), {})
        self.assertFalse((self.container / "missing").exists())

    def test_conflicting_modes_and_orphan_task_id_stop_before_reads_or_writes(self):
        modes = (["--resume", self.session_id], ["--list"], ["--preview", self.session_id],
                 ["--change", self.session_id, "audience", "学生"], ["--rollback", "transaction-id"],
                 ["--status"], ["--task-id", ""], ["--task-id", "task-1"])
        before = inventory(self.container)
        with mock.patch.object(cli, "preview_task_plan") as preview, mock.patch.object(cli, "_wizard") as wizard:
            for mode in modes:
                arguments = ["--root", str(self.root), "--task-preview", self.session_id, *mode]
                with self.subTest(mode=mode), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                    cli.main(arguments)
                self.assertEqual(error.exception.code, 2)
                result = run_cli(self.root, "--task-preview", self.session_id, *mode)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
            preview.assert_not_called()
            wizard.assert_not_called()
        self.assertEqual(inventory(self.container), before)

    def test_corrupt_session_is_not_repaired_or_replaced(self):
        original = json.loads(self.session_path.read_text(encoding="utf-8"))
        bad_values = [b"not-json", b"[]", b"{}", b"\xff", json.dumps({**original, "schema_version": "999"}).encode(),
                      json.dumps({**original, "id": "different-session"}).encode()]
        for value in bad_values:
            self.session_path.write_bytes(value)
            for structured in (False, True):
                with self.subTest(value=value[:20], json=structured):
                    result = self.read(structured=structured)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertNotIn("Traceback", result.stderr)

    def test_real_unreadable_session_shape_fails_without_repair(self):
        self.session_path.unlink()
        self.session_path.mkdir()
        result = self.read(structured=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr), {"error": {"code": "session_read_failed"}})

    def test_read_sharing_failure_is_sanitized_and_never_uses_write_entrypoints(self):
        # The snapshot API takes no read lock. Inject the OSError that a denied
        # read / Windows sharing lock produces, without claiming a Windows run.
        before = inventory(self.container)
        for error in (PermissionError("private read path"), OSError("private sharing lock detail")):
            for structured in (False, True):
                stdout, stderr = StringIO(), StringIO()
                arguments = ["--root", str(self.root), "--task-preview", self.session_id]
                if structured:
                    arguments.append("--json")
                with ExitStack() as stack:
                    calls = [stack.enter_context(mock.patch(name, side_effect=AssertionError(name))) for name in WRITE_ENTRYPOINTS]
                    stack.enter_context(mock.patch("opencoding.sessions._read_existing", side_effect=error))
                    stack.enter_context(redirect_stdout(stdout))
                    stack.enter_context(redirect_stderr(stderr))
                    self.assertEqual(cli.main(arguments), 2)
                    for call in calls:
                        call.assert_not_called()
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotIn("private", stderr.getvalue())
                if structured:
                    self.assertEqual(json.loads(stderr.getvalue()), {"error": {"code": "task_preview_read_failed"}})
        self.assertEqual(inventory(self.container), before)

    def test_real_preview_calls_no_input_write_approval_scheduler_or_transaction_entrypoints(self):
        before = inventory(self.container)
        with ExitStack() as stack:
            calls = [stack.enter_context(mock.patch(name, side_effect=AssertionError(name))) for name in WRITE_ENTRYPOINTS]
            read = stack.enter_context(mock.patch.object(cli, "preview_task_plan", wraps=preview_task_plan))
            input_fn = mock.Mock(side_effect=AssertionError("must not read stdin"))
            for structured in (False, True):
                arguments = ["--root", str(self.root), "--task-preview", self.session_id]
                if structured:
                    arguments.append("--json")
                with redirect_stdout(StringIO()):
                    self.assertEqual(cli.main(arguments, input_fn=input_fn), 0)
            self.assertEqual(read.call_args_list, [mock.call(str(self.root), self.session_id)] * 2)
            input_fn.assert_not_called()
            for call in calls:
                call.assert_not_called()
        self.assertEqual(inventory(self.container), before)

    def test_service_error_code_is_preserved_without_private_error_text(self):
        with mock.patch.object(cli, "preview_task_plan", side_effect=ServiceError("session_read_failed", "private detail")):
            stdout, stderr = StringIO(), StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(["--root", str(self.root), "--task-preview", self.session_id, "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue()), {"error": {"code": "session_read_failed"}})


if __name__ == "__main__":
    unittest.main()
