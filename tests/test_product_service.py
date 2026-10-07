"""W2 service contract tests, including real process coordination."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from opencoding import autorun, grants
from opencoding.aiadapter import MockAdapter
from opencoding.facts import add_fact
from opencoding.intake import QUESTION_DEFINITIONS
from opencoding.scheduler import SchedulerSnapshotError
import opencoding.service as service_module
from opencoding.service import (
    adopt_evaluation_plan,
    ServiceError,
    apply_approved,
    approve_preview,
    create_session,
    evaluate_session,
    execution_status,
    preview_session,
    query_autonomous_run,
    rollback,
    session_view,
    submit_answer,
)
from tests import REVIEWED_FIXTURE
from tests.test_product_autorun import MockResponder


def _inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _complete(root: Path, goal: str = "社区借还登记", platform: str = "网页") -> dict:
    view = create_session(root, goal)
    answers = {
        "audience": "社区居民和管理员",
        "platform": platform,
        "outcome": "登记借用并确认归还",
    }
    for question in QUESTION_DEFINITIONS:
        answer = answers.get(question["id"], "不需要")
        view = submit_answer(root, view["session"]["id"], view["session"]["revision"], question["id"], answer)
    return session_view(root, view["session"]["id"])


def _apply_child(root_text: str, approval: dict, ready, release, queue) -> None:
    real_lock = service_module.session_write_lock

    @contextmanager
    def checkpoint(root):
        with real_lock(root):
            ready.set()
            if not release.wait(10):
                raise RuntimeError("apply checkpoint timed out")
            yield

    service_module.session_write_lock = checkpoint
    try:
        queue.put(("apply", apply_approved(Path(root_text), approval)))
    except BaseException as exc:  # pragma: no cover - child diagnostic
        queue.put(("apply-error", type(exc).__name__, str(exc)))
        raise


def _save_child(root_text: str, session_id: str, revision: int, queue) -> None:
    try:
        queue.put(("save", submit_answer(Path(root_text), session_id, revision, "outcome", "另一个业务结果")))
    except BaseException as exc:  # pragma: no cover - child diagnostic
        queue.put(("save-error", type(exc).__name__, str(exc)))
        raise


class ProductServiceTests(unittest.TestCase):
    def test_evaluation_requires_explicit_persisted_adoption_before_execution_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            view = create_session(root, "社区借还登记")
            for question in QUESTION_DEFINITIONS:
                answer = {
                    "audience": "社区居民",
                    "outcome": "登记借用并确认归还",
                    "platform": "命令行",
                    "data_persistence": "需要",
                }.get(question["id"], "不需要")
                view = submit_answer(root, view["session"]["id"], view["session"]["revision"], question["id"], answer)
            session_id = view["session"]["id"]
            evaluated = evaluate_session(root, session_id)
            self.assertFalse((root / ".opencoding" / "adoptions" / (session_id + ".json")).exists())
            adopted = adopt_evaluation_plan(root, session_id)
            self.assertEqual(adopted["status"], "adopted")
            record = json.loads((root / ".opencoding" / "adoptions" / (session_id + ".json")).read_text(encoding="utf-8"))
            self.assertEqual(record["plan_digest"], evaluated["adopted_plan_digest"])
            self.assertTrue(record["validated"])
            self.assertEqual(record["session_id"], session_id)

    def test_formal_adoption_produces_lendreg_mapping_and_drives_offline_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            view = create_session(root, "社区借还登记")
            answers = {
                "audience": "社区居民和管理员",
                "platform": "命令行",
                "outcome": "登记借用并确认归还",
                "data_persistence": "需要",
                "cross_device": "不需要",
                "multi_user": "不需要",
            }
            for question in QUESTION_DEFINITIONS:
                view = submit_answer(
                    root, view["session"]["id"], view["session"]["revision"],
                    question["id"], answers.get(question["id"], "不需要"),
                )
            session_id = view["session"]["id"]
            adopted = adopt_evaluation_plan(root, session_id)
            adoption = adopted["adoption"]
            self.assertTrue(adoption["executor_mapping"]["supported"])
            self.assertEqual(adoption["executor_mapping"]["executor_id"], "lendreg")
            self.assertEqual(
                set(adoption["executor_mapping"]["task_bindings"]),
                {task["task_id"] for task in autorun.LENDREG_SCENARIO["tasks"]},
            )
            grant = grants.issue_batch_grant(
                root,
                goal="正式采用驱动离线闭环",
                allowed_paths=["lendreg", "tests", "scripts", "data", "RECOVERY.md"],
                action_kinds=["local_write", "local_run", "ai_request"],
                issued_by="合成测试",
                budget={"max_ai_requests": 40, "max_repair_rounds": 12},
            )
            summary = autorun.run_batch(
                root, autorun.LENDREG_SCENARIO,
                grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()),
                run_id="run-adopted-offline",
                trusted_fixture=REVIEWED_FIXTURE,
                adopted_plan_digest=adoption["plan_digest"],
            )
            self.assertEqual(summary["succeeded"], 10)
            self.assertTrue(summary["deliverable"])

    def test_evaluation_adopts_answers_and_source_layered_facts_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cases = {
                "offline": {"platform": "命令行", "cross_device": "不需要", "multi_user": "不需要"},
                "website": {"platform": "网页", "cross_device": "不需要", "multi_user": "不需要"},
                "collaboration": {
                    "platform": "网页", "cross_device": "需要",
                    "multi_user": "需要", "account_access": "需要",
                },
            }
            graphs = {}
            for label, overrides in cases.items():
                view = create_session(root, "社区借还登记")
                for question in QUESTION_DEFINITIONS:
                    answer = {
                        "audience": "社区居民",
                        "outcome": "登记借用并确认归还",
                        "platform": "网页",
                        "data_persistence": "需要",
                        **overrides,
                    }.get(question["id"], "不需要")
                    view = submit_answer(
                        root, view["session"]["id"], view["session"]["revision"], question["id"], answer,
                    )
                session_id = view["session"]["id"]
                add_fact(
                    root, session_id, content="现有网站页面需要增量修改" if label == "website" else "只用合成数据",
                    source_type="repository" if label == "website" else "user",
                    source_ref="authorized-test-fixture",
                )
                add_fact(
                    root, session_id, content="技术方案仍需独立验证",
                    source_type="ai", source_ref="synthetic-inference",
                )
                before = _inventory(root)
                evaluated = evaluate_session(root, session_id)
                self.assertEqual(_inventory(root), before)
                self.assertTrue(evaluated["plan_validation"]["valid"])
                self.assertEqual(evaluated["decision_record"]["ai_inferences_unconfirmed"], ["技术方案仍需独立验证"])
                self.assertEqual(evaluated["decision_record"]["user_decisions_required"], [])
                graphs[label] = evaluated
                cli = subprocess.run(
                    [sys.executable, "-m", "opencoding", "--root", str(root), "--evaluate", session_id, "--json"],
                    capture_output=True, text=True, encoding="utf-8",
                    env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
                    check=False,
                )
                self.assertEqual(cli.returncode, 0, cli.stderr)
                self.assertEqual(json.loads(cli.stdout), evaluated)
                self.assertEqual(_inventory(root), before)

            offline = graphs["offline"]["task_plan"]
            website = graphs["website"]["task_plan"]
            collaboration = graphs["collaboration"]["task_plan"]
            self.assertNotEqual(offline["tasks"], website["tasks"])
            self.assertNotEqual(website["tasks"], collaboration["tasks"])
            self.assertEqual(graphs["offline"]["recommendation"]["platforms"]["primary"], "cli")
            self.assertEqual(graphs["collaboration"]["recommendation"]["capabilities"][0]["need"], "required")
            self.assertIn("permissions", {task["id"] for task in collaboration["tasks"]})

    def test_execution_status_reads_autonomous_run_without_writes_or_session_aliasing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = grants.issue_batch_grant(
                root,
                goal="状态快照合成任务",
                allowed_paths=["lendreg"],
                action_kinds=["local_write", "local_run", "ai_request"],
                issued_by="service contract test",
                budget={"max_ai_requests": 2, "max_repair_rounds": 1},
            )
            task = autorun.LENDREG_SCENARIO["tasks"][0]
            scenario = {
                "name": "状态快照合成任务",
                "goal": "验证自主状态只读入口",
                "tasks": [task],
                "verifiers": {"d01-models": autorun.LENDREG_SCENARIO["verifiers"]["d01-models"]},
            }
            model_source = (
                "def new_item(item_id, name):\n"
                "    if not item_id or len(item_id) > 64 or not name:\n"
                "        raise ValueError('invalid item')\n"
                "    return {'id': item_id, 'name': name, 'status': 'available'}\n"
                "def new_loan(loan_id, item_id, borrower):\n"
                "    return {'id': loan_id, 'item_id': item_id, 'borrower': borrower, "
                "'borrowed_at': 'synthetic-time', 'closed': False}\n"
            )

            # r8/Y01 材料身份绑定后，D01 候选必须逐字节等于已审查参考材料；
            # 本测试意图是状态只读语义，改用参考夹具替身（model_source 保留注释说明原用途）。
            def responder(_messages, *, request_kind, nonce):
                self.assertEqual(request_kind, "implement")
                return json.dumps({
                    "nonce": nonce,
                    "summary": "fixed synthetic output",
                    "files": [
                        {"path": "lendreg/__init__.py", "content": ""},
                        {"path": "lendreg/models.py", "content": model_source},
                    ],
                })

            autorun.run_batch(
                root, scenario, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-status-shared",
                trusted_fixture=REVIEWED_FIXTURE,
            )
            before = _inventory(root)
            status = execution_status(root)
            filtered = execution_status(root, "d01-models")
            query = query_autonomous_run(root, "run-status-shared")
            after = _inventory(root)
            self.assertEqual(status["status"], "ready")
            self.assertEqual(status["runs"][0]["kind"], "autonomous")
            self.assertEqual(status["runs"][0]["run_id"], "run-status-shared")
            self.assertEqual(status["tasks"][0]["state"], "succeeded")
            self.assertTrue(status["events"])
            self.assertEqual([item["task_id"] for item in filtered["tasks"]], ["d01-models"])
            self.assertEqual(query["status"], "verified")
            self.assertEqual(query["new_dispatches"], 0)
            self.assertFalse(query["budget_changed"])
            self.assertEqual(before, after)
            self.assertNotIn("def new_item", json.dumps(status, ensure_ascii=False))
            cli = subprocess.run(
                [sys.executable, "-m", "opencoding", "--root", str(root), "--status", "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(cli.returncode, 0, cli.stderr)
            cli_status = json.loads(cli.stdout)
            self.assertEqual(cli_status["runs"], status["runs"])
            self.assertEqual(cli_status["tasks"], status["tasks"])

    def test_cancelled_autonomous_run_is_visible_in_cli_and_cannot_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = grants.issue_batch_grant(
                root,
                goal="取消状态合成验证",
                allowed_paths=["lendreg"],
                action_kinds=["local_write", "local_run", "ai_request"],
                issued_by="service contract test",
                budget={"max_ai_requests": 2, "max_repair_rounds": 1},
            )
            autorun.cancel_run(root, "run-cancel-visible", reason="用户取消合成运行")
            dispatched = []

            def responder(*_args, **_kwargs):
                dispatched.append(True)
                return "{}"

            summary = autorun.run_batch(
                root,
                autorun.LENDREG_SCENARIO,
                grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=responder),
                run_id="run-cancel-visible",
                trusted_fixture=REVIEWED_FIXTURE,
            )
            self.assertEqual(summary["cancelled"], 10)
            self.assertEqual(dispatched, [])
            cli = subprocess.run(
                [sys.executable, "-m", "opencoding", "--root", str(root), "--status", "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(cli.returncode, 0, cli.stderr)
            status = json.loads(cli.stdout)
            self.assertEqual(status["autonomous_runs"][0]["run_id"], "run-cancel-visible")
            self.assertEqual(status["autonomous_runs"][0]["status"], "cancelled")

    def test_cli_autonomous_rollback_preserves_user_post_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = grants.issue_batch_grant(
                root,
                goal="回滚入口合成验证",
                allowed_paths=["lendreg"],
                action_kinds=["local_write", "local_run", "ai_request"],
                issued_by="service contract test",
                budget={"max_ai_requests": 2, "max_repair_rounds": 1},
            )
            task = autorun.LENDREG_SCENARIO["tasks"][0]
            scenario = {
                "name": "回滚入口合成验证",
                "goal": "验证中文自主回滚命令",
                "tasks": [task],
                "verifiers": {"d01-models": autorun.LENDREG_SCENARIO["verifiers"]["d01-models"]},
            }
            model_source = (
                "def new_item(item_id, name):\n"
                "    if not item_id or len(item_id) > 64 or not name:\n"
                "        raise ValueError('invalid item')\n"
                "    return {'id': item_id, 'name': name, 'status': 'available'}\n"
                "def new_loan(loan_id, item_id, borrower):\n"
                "    return {'id': loan_id, 'item_id': item_id, 'borrower': borrower, "
                "'borrowed_at': 'synthetic-time', 'closed': False}\n"
            )

            # r8/Y01 材料身份绑定后，D01 候选必须逐字节等于已审查参考材料；
            # 本测试意图是中文回滚入口与用户后改保留，改用参考夹具替身。
            def responder(_messages, *, request_kind, nonce):
                return json.dumps({
                    "nonce": nonce,
                    "summary": "fixed synthetic output",
                    "files": [
                        {"path": "lendreg/__init__.py", "content": ""},
                        {"path": "lendreg/models.py", "content": model_source},
                    ],
                })

            result = autorun.run_batch(
                root,
                scenario,
                grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()),
                run_id="run-rollback-cli",
                trusted_fixture=REVIEWED_FIXTURE,
            )
            self.assertEqual(result["succeeded"], 1)
            target = root / "lendreg" / "models.py"
            target.write_text(target.read_text(encoding="utf-8") + "\n# 用户后来补充\n", encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            rolled = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "opencoding",
                    "--root",
                    str(root),
                    "--rollback-run",
                    "run-rollback-cli",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                check=False,
            )
            self.assertEqual(rolled.returncode, 2, rolled.stderr)
            self.assertIn("partial_failure", rolled.stdout)
            self.assertIn("# 用户后来补充", target.read_text(encoding="utf-8"))

    def test_execution_status_is_json_safe_zero_write_and_maps_snapshot_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            before = _inventory(root)
            status = execution_status(root)
            self.assertEqual(status, {
                "schema_version": "1.0",
                "root": str(root),
                "status": "not_initialized",
                "tasks": [],
                "runs": [],
            })
            self.assertEqual(_inventory(root), before)
            with self.assertRaises(ServiceError) as invalid:
                execution_status(root, "bad task id")
            self.assertEqual(invalid.exception.code, "execution_status_invalid_task_id")
            with mock.patch.object(service_module, "read_snapshot", side_effect=SchedulerSnapshotError("database_busy", "token=sk-test-1234567890")):
                with self.assertRaises(ServiceError) as busy:
                    execution_status(root)
            self.assertEqual(busy.exception.code, "execution_status_database_busy")
            self.assertNotIn("sk-test-1234567890", str(busy.exception))
            with mock.patch.object(service_module, "read_snapshot", side_effect=SchedulerSnapshotError("unsupported_schema", "schema broken")):
                with self.assertRaises(ServiceError) as schema:
                    execution_status(root, "absent")
            self.assertEqual(schema.exception.code, "execution_status_unsupported_schema")
            with mock.patch.object(service_module, "read_snapshot", side_effect=SchedulerSnapshotError("unsupported_platform", "platform unsupported")):
                with self.assertRaises(ServiceError) as platform:
                    execution_status(root)
            self.assertEqual(platform.exception.code, "execution_status_unsupported_platform")

    def test_preview_is_zero_write_and_service_state_is_json_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = create_session(root, "离线记录工具")
            before = _inventory(root)
            preview = preview_session(root, view["session"]["id"])
            after = _inventory(root)
            self.assertEqual(after, before)
            self.assertEqual(preview["status"], "draft")
            self.assertTrue(preview["file_plan"]["entries"])
            self.assertIsInstance(service_module.as_json(preview), str)

    def test_preview_and_approval_use_one_canonical_root_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = _complete(root)
            preview = preview_session(root, view["session"]["id"])
            self.assertEqual(preview["root"], preview["file_plan"]["root"])
            self.assertEqual(preview["root"], str(root.resolve(strict=True)))
            self.assertTrue(approve_preview(preview)["approved"])

    def test_frontier_follows_dependencies_and_changed_answer_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = create_session(root, "借还工具")
            self.assertEqual([item["id"] for item in view["frontier"]], ["audience", "platform"])
            view = submit_answer(root, view["session"]["id"], 0, "audience", "居民")
            self.assertEqual([item["id"] for item in view["frontier"]], ["platform", "outcome"])
            view = submit_answer(root, view["session"]["id"], 1, "platform", "网页")
            view = submit_answer(root, view["session"]["id"], 2, "outcome", "登记借用")
            self.assertIn("data_persistence", [item["id"] for item in view["frontier"]])
            changed = submit_answer(root, view["session"]["id"], view["session"]["revision"], "audience", "学生")
            self.assertEqual(changed["session"]["answer_history"][-1]["changed"], True)
            self.assertEqual(changed["session"]["answers"]["audience"], "学生")

    def test_confirm_apply_and_rollback_delegate_to_transaction_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = _complete(root)
            preview = preview_session(root, view["session"]["id"])
            approval = approve_preview(preview)
            self.assertEqual(approval["action"]["kind"], "local_write")
            self.assertFalse(approval["action"]["external"])
            self.assertEqual(approval["action"]["cost_limit"], 0)
            applied = apply_approved(root, approval)
            self.assertEqual(applied["status"], "applied")
            transaction = applied["transaction"]
            self.assertTrue(transaction["changed_paths"])
            self.assertTrue((root / "AGENTS.md").exists())
            rolled = rollback(root, transaction["transaction_id"])
            self.assertEqual(rolled["status"], "rolled_back")
            self.assertTrue(Path(transaction["receipt_path"]).exists())
            self.assertFalse((root / "AGENTS.md").exists())

    def test_approval_tampering_expiry_target_change_and_business_drift_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = _complete(root)
            preview = preview_session(root, view["session"]["id"])
            with self.assertRaises(ServiceError):
                approve_preview(dict(preview, unknown_field=True))
            with self.assertRaises(ServiceError):
                approve_preview(preview, expires_in_seconds=-1)
            approval = approve_preview(preview)
            wrong_targets = deepcopy(approval)
            wrong_targets["targets"] = list(reversed(wrong_targets["targets"]))
            with self.assertRaises(ServiceError):
                apply_approved(root, wrong_targets)
            (root / "memory.md").write_text("用户后来修改的内容", encoding="utf-8")
            result = apply_approved(root, approval)
            self.assertEqual(result["status"], "stale")
            self.assertIn("business_or_file_plan_drift", result["reason_codes"])
            self.assertEqual((root / "memory.md").read_text(encoding="utf-8"), "用户后来修改的内容")

    def test_revision_change_invalidates_approval_and_preserves_later_user_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = _complete(root)
            preview = preview_session(root, view["session"]["id"])
            approval = approve_preview(preview)
            changed = submit_answer(root, view["session"]["id"], view["session"]["revision"], "audience", "后来用户")
            self.assertEqual(changed["status"], "saved")
            result = apply_approved(root, approval)
            self.assertEqual(result["status"], "stale")
            current = session_view(root, view["session"]["id"])
            self.assertEqual(current["session"]["answers"]["audience"], "后来用户")

    def test_real_process_apply_lock_rejects_competing_save(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = _complete(root)
            preview = preview_session(root, view["session"]["id"])
            approval = approve_preview(preview)
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            queue = context.Queue()
            apply_process = context.Process(target=_apply_child, args=(str(root), approval, ready, release, queue))
            save_process = None
            try:
                apply_process.start()
                self.assertTrue(ready.wait(10))
                save_process = context.Process(target=_save_child, args=(str(root), view["session"]["id"], view["session"]["revision"], queue))
                save_process.start()
                save_result = queue.get(timeout=10)
                self.assertEqual(save_result[0], "save")
                self.assertEqual(save_result[1]["status"], "busy")
                release.set()
                apply_result = queue.get(timeout=10)
                self.assertEqual(apply_result[0], "apply")
                self.assertEqual(apply_result[1]["status"], "applied")
            finally:
                release.set()
                for process in (apply_process, save_process):
                    if process is not None:
                        process.join(10)
                        if process.is_alive():
                            process.terminate()
                            process.join(10)
                        self.assertEqual(process.exitcode, 0)

    def test_secret_is_redacted_and_host_boundary_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = create_session(root, "做工具 token=sk-test-1234567890")
            self.assertNotIn("sk-test-1234567890", service_module.as_json(view))
            serialized = service_module.as_json(preview_session(root, view["session"]["id"]))
            self.assertIn("未在本次离线事务中联网核实", serialized)
            self.assertIn("不调用 Host", serialized)


if __name__ == "__main__":
    unittest.main()
