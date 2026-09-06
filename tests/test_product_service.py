"""W2 service contract tests, including real process coordination."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import multiprocessing
from pathlib import Path
import tempfile
import unittest

from opencoding.intake import QUESTION_DEFINITIONS
import opencoding.service as service_module
from opencoding.service import (
    ServiceError,
    apply_approved,
    approve_preview,
    create_session,
    preview_session,
    rollback,
    session_view,
    submit_answer,
)


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
