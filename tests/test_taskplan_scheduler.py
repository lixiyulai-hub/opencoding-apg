"""Synthetic offline bridge regressions; confirmations are fixture assertions.

Checkpoint test_product_loop and test_agent_product_loop_bridge scenarios are
adapted to the current work caller receipt and Scheduler transaction contracts.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opencoding.intake import QUESTION_DEFINITIONS
from opencoding.executor import action_digest
from opencoding.scheduler import Scheduler
from opencoding.service import ServiceError, build_caller_confirmation, create_session, rollback, submit_answer
from opencoding.taskplan_scheduler import approve_task_plan, build_task_plan_confirmation, execute_task_plan, preview_task_plan
from tests.test_product_service import _complete, _inventory


class TaskPlanSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.view = _complete(self.root)
        self.session_id = self.view["session"]["id"]

    def prepared(self):
        preview = preview_task_plan(self.root, self.session_id)
        receipt = build_task_plan_confirmation(preview, statement="Synthetic fixture: confirm exact offline graph and diff", actor="synthetic-test")
        return preview, approve_task_plan(preview, confirmation=receipt), receipt

    def run_approved(self, approval, receipt):
        return execute_task_plan(self.root, approval, authorization_context=receipt)

    def test_preview_and_confirmation_are_zero_write_and_deterministic(self):
        before = _inventory(self.root)
        preview, approval, receipt = self.prepared()
        self.assertEqual(preview, preview_task_plan(self.root, self.session_id))
        self.assertEqual(before, _inventory(self.root))
        mapped = {task["input"]["plan_task_id"]: task for task in preview["tasks"]}
        self.assertEqual({task["id"] for task in preview["service_preview"]["task_plan"]["tasks"]}, set(mapped) - {"adapter-context-documents"})
        self.assertTrue(any(task["input"]["classification"] == "host_missing" for task in mapped.values()))
        targets = [entry["path"] for task in mapped.values() if task["action"]["type"] == "document" for entry in task["action"]["plan"]["entries"]]
        self.assertEqual(sorted(targets), sorted(preview["targets"]))
        self.assertEqual(len(targets), len(set(targets)))

    def test_order_evidence_host_missing_and_transitive_freeze(self):
        preview, approval, receipt = self.prepared()
        result = self.run_approved(approval, receipt)
        self.assertEqual(result["status"], "blocked")
        tasks = {task["input"]["plan_task_id"]: task for task in result["tasks"]}
        self.assertEqual(tasks["requirements-confirmed"]["state"], "succeeded")
        self.assertEqual(tasks["implement-scenario-1"]["last_error"], "host_missing")
        self.assertEqual(tasks["verify-scenario-1"]["input"]["classification"], "host_missing")
        self.assertEqual(tasks["verify-scenario-1"]["state"], "frozen")
        self.assertEqual(tasks["delivery-plan"]["state"], "frozen")
        self.assertFalse((self.root / "src").exists())
        self.assertFalse((self.root / "plan.md").exists())
        position = {run["task_id"]: i for i, run in enumerate(result["runs"])}
        for task in result["tasks"]:
            if task["state"] == "succeeded":
                for dependency in task["depends_on"]:
                    self.assertLess(position[dependency], position[task["task_id"]])
        successes = [run for run in result["runs"] if run["status"] == "succeeded"]
        self.assertTrue(successes)
        for run in successes:
            self.assertTrue(run["receipt"]["receipt_digest"])
            self.assertTrue(run["artifacts"])
            transaction = json.loads(run["stdout_summary"])
            self.assertEqual(transaction["status"], "applied")
            self.assertTrue(all(item["transaction_id"] == transaction["transaction_id"] for item in run["artifacts"]))

    def test_repeat_is_idempotent_with_no_new_receipts_or_document_writes(self):
        _, approval, receipt = self.prepared()
        first = self.run_approved(approval, receipt)
        before = _inventory(self.root)
        second = self.run_approved(approval, receipt)
        self.assertEqual(first, second)
        self.assertEqual({k: v for k, v in before.items() if k != ".opencoding/.session-write.lock"},
                         {k: v for k, v in _inventory(self.root).items() if k != ".opencoding/.session-write.lock"})

    def test_wrong_confirmation_expiry_tamper_and_root_rejected_before_scheduler(self):
        preview, approval, receipt = self.prepared()
        before = _inventory(self.root)
        bad = deepcopy(approval)
        bad["preview"]["tasks"][0]["action"] = {"type": "python_module", "module": "unittest", "args": []}
        expired = deepcopy(approval)
        expired["confirmation"]["expires_at"] = "2000-01-01T00:00:00+00:00"
        for candidate, context in [(bad, receipt), (expired, receipt), (approval, {})]:
            with self.subTest(candidate=candidate is expired, context=bool(context)), self.assertRaises(ServiceError):
                self.run_approved(candidate, context)
        with tempfile.TemporaryDirectory() as other, self.assertRaises(ServiceError):
            execute_task_plan(Path(other).resolve(), approval, authorization_context=receipt)
        self.assertEqual(before, _inventory(self.root))

    def test_tampered_and_stale_preview_cannot_be_approved(self):
        preview, _, receipt = self.prepared()
        changed = deepcopy(preview)
        changed["tasks"][0]["depends_on"] = []
        changed["targets"] = []
        with self.assertRaises(ServiceError):
            approve_task_plan(changed, confirmation=receipt)
        (self.root / "PRG.md").write_text("later local edit", encoding="utf-8")
        with self.assertRaises(ServiceError):
            approve_task_plan(preview, confirmation=receipt)

    def test_session_revision_change_stops_before_enqueue(self):
        _, approval, receipt = self.prepared()
        submit_answer(self.root, self.session_id, self.view["session"]["revision"], "outcome", "登记新的活动")
        before = _inventory(self.root)
        self.assertEqual(self.run_approved(approval, receipt)["status"], "stale")
        self.assertEqual({k: v for k, v in before.items() if k != ".opencoding/.session-write.lock"},
                         {k: v for k, v in _inventory(self.root).items() if k != ".opencoding/.session-write.lock"})

    def test_preimage_drift_fails_task_and_freezes_successors(self):
        _, approval, receipt = self.prepared()
        (self.root / "PRG.md").write_text("user edit", encoding="utf-8")
        result = self.run_approved(approval, receipt)
        tasks = {task["input"]["plan_task_id"]: task for task in result["tasks"]}
        self.assertEqual(tasks["requirements-confirmed"]["state"], "failed")
        self.assertEqual(tasks["product-and-ui"]["state"], "frozen")
        self.assertEqual(tasks["delivery-plan"]["state"], "frozen")
        self.assertEqual((self.root / "PRG.md").read_text(), "user edit")
        self.assertFalse((self.root / "product.md").exists())
        failed = next(run for run in result["runs"] if run["status"] == "failed")
        self.assertEqual(json.loads(failed["stdout_summary"])["status"], "blocked")

    def test_partial_document_failure_uses_transaction_rollback(self):
        _, approval, receipt = self.prepared()
        import opencoding.transactions as transactions
        original = transactions._atomic_write

        def fail_second_output(path, content):
            if path == self.root / "ui.md":
                raise OSError("synthetic interrupted document write")
            return original(path, content)

        with patch.object(transactions, "_atomic_write", side_effect=fail_second_output):
            result = self.run_approved(approval, receipt)
        task = next(task for task in result["tasks"] if task["input"]["plan_task_id"] == "product-and-ui")
        self.assertEqual(task["state"], "failed")
        self.assertTrue((self.root / "product.md").exists())
        self.assertFalse((self.root / "ui.md").exists())
        run = next(run for run in result["runs"] if run["task_id"] == task["task_id"])
        transaction = json.loads(run["stdout_summary"])
        self.assertEqual(transaction["status"], "partial_failure")
        self.assertEqual(rollback(self.root, transaction["transaction_id"])["status"], "rolled_back")
        self.assertFalse((self.root / "product.md").exists())

    def test_rollback_keeps_receipts_and_blocks_completed_output_replay(self):
        _, approval, receipt = self.prepared()
        result = self.run_approved(approval, receipt)
        runs = [run for run in result["runs"] if run["status"] == "succeeded"]
        for run in reversed(runs):
            transaction = json.loads(run["stdout_summary"])
            self.assertEqual(rollback(self.root, transaction["transaction_id"])["status"], "rolled_back")
        self.assertFalse((self.root / "PRG.md").exists())
        self.assertEqual(self.run_approved(approval, receipt)["reason"], "completed_output_drift")
        self.assertEqual(len(Scheduler(self.root).list_runs()), len(result["runs"]))

    def test_offline_design_never_activates_or_dispatches_subprocess(self):
        current = create_session(self.root, "社区离线登记")
        answers = {"audience": "社区居民", "outcome": "登记并查看记录", "platform": "网页", "data_persistence": "需要"}
        for question in QUESTION_DEFINITIONS:
            current = submit_answer(self.root, current["session"]["id"], current["session"]["revision"], question["id"], answers.get(question["id"], "不需要"))
        self.session_id = current["session"]["id"]
        _, approval, receipt = self.prepared()
        with patch("opencoding.executor.subprocess.Popen", side_effect=AssertionError("no subprocess")):
            result = self.run_approved(approval, receipt)
        designs = [task for task in result["tasks"] if task["input"]["classification"] == "offline_design"]
        self.assertTrue(designs)
        self.assertTrue(all(task["state"] == "succeeded" for task in designs))
        self.assertTrue(all(task["input"]["activation_status"] == "not_activated" for task in designs))
        self.assertIn("offline_design_only", (self.root / "integrations/database.md").read_text())

    def test_adapter_runs_only_its_own_graph(self):
        scheduler = Scheduler(self.root)
        unrelated = {"task_id": "unrelated", "idempotency_key": "unrelated", "input": {},
                     "action": {"type": "write_text", "path": "unrelated.txt", "content": "untouched"},
                     "depends_on": [], "max_attempts": 1, "timeout_seconds": 1}
        scheduler.enqueue(unrelated)
        _, approval, receipt = self.prepared()
        self.run_approved(approval, receipt)
        self.assertEqual(scheduler.get_task("unrelated")["state"], "queued")
        self.assertFalse((self.root / "unrelated.txt").exists())

    def test_new_executor_actions_reject_extra_fields_and_non_documents(self):
        with self.assertRaises(ValueError):
            action_digest({"type": "host_missing", "module": "unittest"})
        preview, _, _ = self.prepared()
        action = deepcopy(preview["tasks"][0]["action"])
        action["plan"]["entries"][0]["path"] = "run.py"
        with self.assertRaises(ValueError):
            action_digest(action)

    def test_draft_cannot_be_approved(self):
        view = create_session(self.root, "尚未澄清的离线笔记")
        preview = preview_task_plan(self.root, view["session"]["id"])
        receipt = build_task_plan_confirmation(preview, statement="synthetic draft", actor="synthetic-test")
        before = _inventory(self.root)
        with self.assertRaises(ServiceError):
            approve_task_plan(preview, confirmation=receipt)
        self.assertEqual(before, _inventory(self.root))

    def test_deadline_extension_cannot_reuse_original_authorization_context(self):
        _, approval, receipt = self.prepared()
        before = _inventory(self.root)
        extended = deepcopy(approval)
        original_expiry = datetime.fromisoformat(receipt["expires_at"])
        extended["confirmation"]["expires_at"] = (original_expiry + timedelta(seconds=60)).isoformat()
        with self.assertRaises(ServiceError) as error:
            self.run_approved(extended, receipt)
        self.assertEqual(error.exception.code, "human_confirmation_scope_mismatch")
        self.assertEqual(before, _inventory(self.root))

    def test_old_confirmation_cannot_renew_expired_approval(self):
        preview = preview_task_plan(self.root, self.session_id)
        receipt = build_task_plan_confirmation(preview, statement="synthetic one-second scope",
                                                actor="synthetic-test", expires_in_seconds=1)
        approval = approve_task_plan(preview, confirmation=receipt)
        self.assertEqual(approval["confirmation"]["expires_at"], receipt["expires_at"])
        before = _inventory(self.root)
        with patch("opencoding.taskplan_scheduler.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromisoformat(receipt["expires_at"]) + timedelta(seconds=1)
            for operation in (lambda: approve_task_plan(preview, confirmation=receipt),
                              lambda: self.run_approved(approval, receipt)):
                with self.assertRaises(ServiceError) as error:
                    operation()
                self.assertEqual(error.exception.code, "approval_expired")
        self.assertEqual(before, _inventory(self.root))

    def test_plain_service_receipt_and_invalid_deadline_are_rejected(self):
        preview, _, receipt = self.prepared()
        plain = build_caller_confirmation(preview, statement="synthetic plain service receipt")
        invalid = deepcopy(receipt)
        invalid["expires_at"] = "2999-01-01T00:00:00+00:00"
        before = _inventory(self.root)
        for context in (plain, invalid):
            with self.assertRaises(ServiceError):
                approve_task_plan(preview, confirmation=context)
        self.assertEqual(before, _inventory(self.root))

    def test_symlink_replacement_after_confirmation_preserves_external_file(self):
        _, approval, receipt = self.prepared()
        with tempfile.TemporaryDirectory() as outside:
            sentinel = Path(outside) / "sentinel.md"
            sentinel.write_text("external sentinel", encoding="utf-8")
            try:
                (self.root / "PRG.md").symlink_to(sentinel)
            except OSError as error:
                self.skipTest("symlink creation unavailable: " + str(error))
            result = self.run_approved(approval, receipt)
            task = next(task for task in result["tasks"] if task["input"]["plan_task_id"] == "requirements-confirmed")
            self.assertEqual(task["state"], "failed")
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(sentinel.read_text(), "external sentinel")

    def test_edited_completed_output_is_preserved_and_blocks_replay(self):
        _, approval, receipt = self.prepared()
        result = self.run_approved(approval, receipt)
        (self.root / "PRG.md").write_text("later user content", encoding="utf-8")
        self.assertEqual(self.run_approved(approval, receipt)["reason"], "completed_output_drift")
        self.assertEqual((self.root / "PRG.md").read_text(), "later user content")
        run = next(run for run in result["runs"] if any(artifact["path"] == "PRG.md" for artifact in run["artifacts"]))
        transaction = json.loads(run["stdout_summary"])
        self.assertNotEqual(rollback(self.root, transaction["transaction_id"])["status"], "rolled_back")
        self.assertEqual((self.root / "PRG.md").read_text(), "later user content")

    def test_unknown_completion_does_not_replay_document_transaction(self):
        _, approval, receipt = self.prepared()
        with patch.object(Scheduler, "_finish", side_effect=RuntimeError("synthetic lost terminal receipt")):
            with self.assertRaises(RuntimeError):
                self.run_approved(approval, receipt)
        before = _inventory(self.root)
        result = self.run_approved(approval, receipt)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual(result["tasks"][0]["state"], "running")
        self.assertEqual({k: v for k, v in before.items() if k != ".opencoding/.session-write.lock"},
                         {k: v for k, v in _inventory(self.root).items() if k != ".opencoding/.session-write.lock"})


if __name__ == "__main__":
    unittest.main()
