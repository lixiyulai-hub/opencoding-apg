#!/usr/bin/env python3
"""Run one synthetic, non-template CLI project through the Agent bridge."""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import shutil
import sys

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from opencoding.acceptance_report import build_acceptance_report
from opencoding.agent_tasks import LocalAgentTaskExecutor, preview_agent_tasks
from opencoding.intake import QUESTION_DEFINITIONS
from opencoding.product_evidence import verify_product_evidence
from opencoding.product_loop import rollback_product_run, resume_product_run, start_product_run
from opencoding.service import adopt_evaluation_plan, apply_approved, approve_preview, create_session, preview_session, submit_answer
from opencoding.safety import sha256_bytes


def _actions(source: str, test: str, *, repair: bool) -> dict[str, list[dict[str, object]]]:
    verify = [
        {"type": "write_text", "path": "tests/features/test_scenario_1.py", "content": test},
        {"type": "python_module", "module": "unittest", "args": ["discover", "-s", "tests/features", "-p", "test_scenario_1.py", "-v"]},
    ]
    if repair:
        verify.insert(0, {"type": "write_text", "path": "src/features/scenario_1.py", "content": source})
        return {"verify-scenario-1": verify}
    return {
        "implement-scenario-1": [{"type": "write_text", "path": "src/features/scenario_1.py", "content": source}],
        "verify-scenario-1": verify,
    }


def run(root: Path) -> dict[str, object]:
    if root.exists() and any(root.iterdir()):
        raise SystemExit("--root must be new or empty")
    root.mkdir(parents=True, exist_ok=True)
    goal = "我想要一个离线命令行植物浇水追踪器，记录植物和最近浇水日期，找出逾期植物。"
    view = create_session(root, goal)
    answers = {
        "audience": "家庭成员", "outcome": "记录植物和最近浇水日期，找出逾期植物。", "platform": "命令行",
        "data_persistence": "需要", "cross_device": "不需要", "file_storage": "不需要", "external_data": "不需要",
        "admin_access": "不需要", "account_access": "不需要", "notifications": "不需要", "payments": "不需要", "multi_user": "不需要",
    }
    for question in QUESTION_DEFINITIONS:
        view = submit_answer(root, view["session"]["id"], view["session"]["revision"], question["id"], answers[question["id"]])
    session_id = view["session"]["id"]
    adopt_evaluation_plan(root, session_id)
    apply_approved(root, approve_preview(preview_session(root, session_id)))
    wrong = """from datetime import date\n\ndef overdue_plants(records, today, interval_days=7):\n    now = date.fromisoformat(today)\n    return [r['name'] for r in records if (now - date.fromisoformat(r['last_watered'])).days > interval_days]\n"""
    fixed = wrong.replace(").days > interval_days", ").days >= interval_days")
    test = """import unittest\nfrom src.features.scenario_1 import overdue_plants\n\nclass PlantTests(unittest.TestCase):\n    def test_due_today_is_overdue(self):\n        self.assertEqual(overdue_plants([{'name': '绿萝', 'last_watered': '2026-09-24'}], '2026-10-01'), ['绿萝'])\n\nif __name__ == '__main__':\n    unittest.main()\n"""
    first_preview = preview_agent_tasks(root, session_id, _actions(wrong, test, repair=False))
    first_executor = LocalAgentTaskExecutor(root, first_preview, expected_digest=first_preview["preview_digest"], confirmation_id="synthetic-stage24-failure", confirmed=True)
    failed = start_product_run(root, session_id, human_confirmed=True, run_id="product-a1b2c3d4e5f6", project_executor=first_executor)
    second_preview = preview_agent_tasks(root, session_id, _actions(fixed, test, repair=True))
    second_executor = LocalAgentTaskExecutor(root, second_preview, expected_digest=second_preview["preview_digest"], confirmation_id="synthetic-stage24-repair", confirmed=True)
    repaired = resume_product_run(root, failed["run_id"], human_confirmed=True, project_executor=second_executor)
    evidence = verify_product_evidence(root, repaired["run_id"], repaired["record_digest"])
    report = build_acceptance_report(run_root=root, run_id=repaired["run_id"], expected_run_digest=repaired["record_digest"])
    rolled = rollback_product_run(root, repaired["run_id"], reason="synthetic acceptance rollback")
    residual = [str(path.relative_to(root)) for path in (root / "src", root / "tests") for path in (path.rglob("*.py") if path.exists() else [])]
    result = {
        "schema": "opencoding-stage24-agent-bridge-v1", "goal": goal,
        "synthetic_answers": True, "synthetic_confirmation": True,
        "host": "linux", "target_platform": "cli", "model_used": False, "external_actions": False,
        "managed_loader": None, "first_run": {"status": failed["status"], "failure": failed.get("failure")},
        "repaired_run": {"status": repaired["status"], "run_id": repaired["run_id"], "record_digest": repaired["record_digest"]},
        "acceptance_checks": {item["id"]: item["status"] for item in report["checks"] if item["id"] in {"local_structured_actions", "skill_project_discovery"}},
        "evidence": evidence, "rollback": {"status": rolled["status"], "file_rollbacks": rolled.get("file_rollbacks", []), "residual_project_py": residual},
        "skill_source_sha256": evidence["skill"]["source_sha256"],
    }
    (root / "evidence").mkdir(exist_ok=True)
    (root / "evidence/STAGE24_AGENT_BRIDGE.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    if not args.root.is_absolute():
        parser.error("--root must be absolute")
    print(json.dumps(run(args.root), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
