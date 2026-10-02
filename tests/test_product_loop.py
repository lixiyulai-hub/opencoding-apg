"""Beginner-facing offline product loop contract."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from opencoding.product_loop import ProductLoopError, read_product_run, resume_product_run, rollback_product_run, start_product_run
from opencoding.service import adopt_evaluation_plan, apply_approved, approve_preview, create_session, preview_session, submit_answer
from opencoding.intake import QUESTION_DEFINITIONS


def _ready(root: Path, *, payments: str = "不需要") -> str:
    view = create_session(root, "社区活动报名与提醒")
    answers = {
        "audience": "社区居民和管理员",
        "outcome": "登记报名并查看结果",
        "platform": "网页",
        "payments": payments,
    }
    for item in QUESTION_DEFINITIONS:
        view = submit_answer(root, view["session"]["id"], view["session"]["revision"], item["id"], answers.get(item["id"], "不需要"))
    session_id = view["session"]["id"]
    adopt_evaluation_plan(root, session_id)
    preview = preview_session(root, session_id)
    transaction = apply_approved(root, approve_preview(preview))["transaction"]
    return session_id


class ProductLoopTests(unittest.TestCase):
    def test_offline_loop_records_task_evidence_and_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_id = _ready(root)
            result = start_product_run(root, session_id)
            self.assertEqual(result["status"], "blocked_human_gate")
            self.assertEqual(result["human_gate"]["required"], True)
            self.assertFalse("acceptance" in result)
            self.assertTrue(any(task["status"] == "blocked_human_gate" for task in result["tasks"]))
            succeeded = [task for task in result["tasks"] if task["status"] == "succeeded"]
            self.assertTrue(all(task.get("evidence") for task in succeeded))
            persisted = read_product_run(root, result["run_id"])
            self.assertEqual(persisted["plan_digest"], result["plan_digest"])

    def test_external_capability_stops_at_human_gate_and_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_id = _ready(root, payments="需要")
            blocked = start_product_run(root, session_id)
            self.assertEqual(blocked["status"], "blocked_human_gate")
            self.assertTrue(blocked["human_gate"]["required"])
            resumed = resume_product_run(root, blocked["run_id"], human_confirmed=True)
            self.assertEqual(resumed["status"], "blocked_capability")
            gates = [task for task in resumed["tasks"] if task.get("human_gate", {}).get("required")]
            self.assertTrue(gates)
            self.assertTrue(any(task.get("human_gate", {}).get("recorded") for task in gates))

    def test_drift_blocks_resume_and_rollback_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_id = _ready(root)
            started = start_product_run(root, session_id)
            run_id = started["run_id"]
            # Change an answer after adoption: the product loop must stop before another task.
            from opencoding.service import session_view
            current = session_view(root, session_id)
            submit_answer(root, session_id, current["session"]["revision"], "outcome", "改成另一个结果")
            with self.assertRaises(ProductLoopError) as ctx:
                resume_product_run(root, run_id)
            self.assertEqual(ctx.exception.code, "input_drift")
            rolled = rollback_product_run(root, run_id, reason="用户要求停止本轮")
            self.assertEqual(rolled["status"], "rolled_back")
            self.assertIn("文档", rolled["rollback"]["effects"])


if __name__ == "__main__":
    unittest.main()
