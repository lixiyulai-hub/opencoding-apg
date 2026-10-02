from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from opencoding.agent_tasks import LocalAgentTaskExecutor
from opencoding.agent_tasks import preview_agent_tasks
from opencoding.product_evidence import verify_product_evidence
from opencoding.acceptance_report import build_acceptance_report
from opencoding.product_loop import read_product_run, resume_product_run, rollback_product_run, start_product_run
from opencoding.service import adopt_evaluation_plan, apply_approved, approve_preview, create_session, preview_session, submit_answer
from opencoding.intake import QUESTION_DEFINITIONS


def _ready(root: Path) -> str:
    view = create_session(root, "我想要一个离线命令行植物浇水追踪器")
    answers = {
        "audience": "家庭成员",
        "outcome": "记录植物和最近浇水日期，找出逾期植物",
        "platform": "命令行",
        "data_persistence": "需要",
    }
    for item in QUESTION_DEFINITIONS:
        view = submit_answer(root, view["session"]["id"], view["session"]["revision"], item["id"], answers.get(item["id"], "不需要"))
    session_id = view["session"]["id"]
    adopt_evaluation_plan(root, session_id)
    apply_approved(root, approve_preview(preview_session(root, session_id)))
    return session_id


class AgentProductLoopBridgeTests(unittest.TestCase):
    def test_reviewed_agent_completes_cli_plan_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_id = _ready(root)
            source = """from datetime import date\n\ndef overdue_plants(records, today, interval_days=7):\n    now = date.fromisoformat(today)\n    return [r['name'] for r in records if (now - date.fromisoformat(r['last_watered'])).days >= interval_days]\n"""
            test = """import unittest\nfrom src.features.scenario_1 import overdue_plants\n\nclass PlantTests(unittest.TestCase):\n    def test_due_today_is_overdue(self):\n        self.assertEqual(overdue_plants([{'name': '绿萝', 'last_watered': '2026-09-24'}], '2026-10-01'), ['绿萝'])\n\nif __name__ == '__main__':\n    unittest.main()\n"""
            actions = {
                "implement-scenario-1": [{"type": "write_text", "path": "src/features/scenario_1.py", "content": source}],
                "verify-scenario-1": [
                    {"type": "write_text", "path": "tests/features/test_scenario_1.py", "content": test},
                    {"type": "python_module", "module": "unittest", "args": ["discover", "-s", "tests/features", "-p", "test_scenario_1.py", "-v"]},
                ],
            }
            preview = preview_agent_tasks(root, session_id, actions)
            executor = LocalAgentTaskExecutor(root, preview, expected_digest=preview["preview_digest"], confirmation_id="synthetic-plant-run", confirmed=True)
            result = start_product_run(root, session_id, human_confirmed=True, run_id="product-a1b2c3d4e5f6", project_executor=executor)
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["tasks"][-1]["status"], "succeeded")
            evidence = verify_product_evidence(root, result["run_id"], result["record_digest"])
            self.assertEqual(evidence["tests"][0]["tests_run"], 1)
            self.assertIsNone(evidence["managed_loader"])
            report = build_acceptance_report(run_root=root, run_id=result["run_id"], expected_run_digest=result["record_digest"])
            self.assertEqual(next(item for item in report["checks"] if item["id"] == "local_structured_actions")["status"], "observed")
            self.assertEqual(next(item for item in report["checks"] if item["id"] == "skill_project_discovery")["status"], "observed")
            self.assertTrue((root / "src/features/scenario_1.py").is_file())
            rolled = rollback_product_run(root, result["run_id"], reason="synthetic acceptance rollback")
            self.assertEqual(rolled["status"], "files_rolled_back")
            self.assertFalse((root / "src/features/scenario_1.py").exists())
            self.assertFalse((root / "tests/features/test_scenario_1.py").exists())

    def test_no_executor_stays_blocked_and_resume_requires_same_explicit_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_id = _ready(root)
            blocked = start_product_run(root, session_id, human_confirmed=True, run_id="product-b1c2d3e4f5a6")
            self.assertEqual(blocked["status"], "blocked_capability")
            self.assertFalse((root / "src/features/scenario_1.py").exists())
            resumed = resume_product_run(root, blocked["run_id"], human_confirmed=True)
            self.assertEqual(resumed["status"], "blocked_capability")

    def test_failed_test_stops_and_repair_uses_new_preview_and_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_id = _ready(root)
            wrong = """from datetime import date\n\ndef overdue_plants(records, today, interval_days=7):\n    now = date.fromisoformat(today)\n    return [r['name'] for r in records if (now - date.fromisoformat(r['last_watered'])).days > interval_days]\n"""
            fixed = wrong.replace(").days > interval_days", ").days >= interval_days")
            test = """import unittest\nfrom src.features.scenario_1 import overdue_plants\n\nclass PlantTests(unittest.TestCase):\n    def test_due_today_is_overdue(self):\n        self.assertEqual(overdue_plants([{'name': '绿萝', 'last_watered': '2026-09-24'}], '2026-10-01'), ['绿萝'])\n\nif __name__ == '__main__':\n    unittest.main()\n"""
            def actions(source, include_impl=True):
                value = {"verify-scenario-1": [
                    {"type": "write_text", "path": "tests/features/test_scenario_1.py", "content": test},
                    {"type": "python_module", "module": "unittest", "args": ["discover", "-s", "tests/features", "-p", "test_scenario_1.py", "-v"]},
                ]}
                if include_impl:
                    value["implement-scenario-1"] = [{"type": "write_text", "path": "src/features/scenario_1.py", "content": source}]
                else:
                    value["verify-scenario-1"].insert(0, {"type": "write_text", "path": "src/features/scenario_1.py", "content": source})
                return value
            first_preview = preview_agent_tasks(root, session_id, actions(wrong))
            first_executor = LocalAgentTaskExecutor(root, first_preview, expected_digest=first_preview["preview_digest"], confirmation_id="synthetic-failure", confirmed=True)
            failed = start_product_run(root, session_id, human_confirmed=True, run_id="product-c1d2e3f4a5b6", project_executor=first_executor)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["failure"]["task_id"], "verify-scenario-1")
            second_preview = preview_agent_tasks(root, session_id, actions(fixed, include_impl=False))
            second_executor = LocalAgentTaskExecutor(root, second_preview, expected_digest=second_preview["preview_digest"], confirmation_id="synthetic-repair", confirmed=True)
            repaired = resume_product_run(root, failed["run_id"], human_confirmed=True, project_executor=second_executor)
            self.assertEqual(repaired["status"], "succeeded")
            self.assertTrue(any(item.get("status") == "failed" for item in repaired["tasks"][8].get("history", [])))
            evidence = verify_product_evidence(root, repaired["run_id"], repaired["record_digest"])
            self.assertEqual(evidence["tests"][0]["tests_run"], 1)


if __name__ == "__main__":
    unittest.main()
