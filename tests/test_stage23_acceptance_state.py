from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from opencoding.acceptance_report import build_acceptance_report
from opencoding.acceptance_state import (
    AcceptanceStateError,
    initialize_acceptance_state,
    load_acceptance_state,
    require_observed,
    resume_acceptance_state,
)
from opencoding.agent_adapter import LocalAgentAdapter


class Stage23AcceptanceStateTests(unittest.TestCase):
    def test_statuses_stay_stable_after_reload_and_repeat_resume(self):
        report = build_acceptance_report()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            initial = initialize_acceptance_state(root, report, state_id="state-stable")
            first = load_acceptance_state(root, "state-stable")
            resumed = resume_acceptance_state(root, "state-stable", report=report)
            repeated = resume_acceptance_state(root, "state-stable", report=report)
            statuses = lambda value: {key: item["status"] for key, item in value["checks"].items()}
            self.assertEqual(statuses(initial), statuses(first))
            self.assertEqual(statuses(initial), statuses(resumed))
            self.assertEqual(statuses(initial), statuses(repeated))
            self.assertEqual(repeated["resume_count"], 2)

    def test_blocked_and_unverified_gates_fail_without_adapter_side_effects(self):
        report = build_acceptance_report()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = initialize_acceptance_state(root, report, state_id="state-gates")
            for check_id, code in (("provider_or_model", "acceptance_gate_blocked"), ("windows_target_execution", "acceptance_status_unverified")):
                with self.assertRaises(AcceptanceStateError) as ctx:
                    require_observed(state, check_id)
                self.assertEqual(ctx.exception.code, code)
            self.assertFalse((root / ".opencoding" / "authorizations").exists())
            self.assertFalse((root / "provider.txt").exists())
            self.assertFalse((root / "windows.txt").exists())

    def test_observed_gate_allows_action_and_tampered_state_is_rejected(self):
        report = build_acceptance_report()
        for check in report["checks"]:
            if check["id"] in {"local_structured_actions", "skill_project_discovery"}:
                check["status"] = "observed"
                check["reason"] = "test fixture supplies current run evidence"
        report["run_evidence"] = {"run_id": "test-run", "run_digest": "fixture"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = initialize_acceptance_state(root, report, state_id="state-observed")
            require_observed(state, "local_structured_actions")
            adapter = LocalAgentAdapter(root)
            action = {"type": "write_text", "path": "observed.txt", "content": "ok\n"}
            result = adapter.execute(action, authorization=adapter.authorization_for(action, scope="stage23"), run_id="stage23-test")
            self.assertEqual(result["status"], "succeeded")
            state_path = root / ".opencoding" / "acceptance" / "state-observed.json"
            text = state_path.read_text(encoding="utf-8").replace('"observed"', '"blocked"', 1)
            state_path.write_text(text, encoding="utf-8")
            with self.assertRaises(AcceptanceStateError) as ctx:
                load_acceptance_state(root, "state-observed")
            self.assertEqual(ctx.exception.code, "state_drifted")

    def test_resume_rejects_changed_report(self):
        report = build_acceptance_report()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            initialize_acceptance_state(root, report, state_id="state-report")
            changed = dict(report)
            changed["human_gate_summary"] = "changed"
            with self.assertRaises(AcceptanceStateError) as ctx:
                resume_acceptance_state(root, "state-report", report=changed)
            self.assertEqual(ctx.exception.code, "report_drifted")


if __name__ == "__main__":
    unittest.main()
