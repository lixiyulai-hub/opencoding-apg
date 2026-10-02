from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from opencoding.acceptance_report import build_acceptance_report, render_acceptance_markdown
from opencoding.agent_adapter import AgentAdapterError, LocalAgentAdapter


class Stage22AcceptanceReportTests(unittest.TestCase):
    def test_report_has_tri_state_checks_and_consistent_gates(self):
        report = build_acceptance_report()
        statuses = {item["status"] for item in report["checks"]}
        self.assertTrue({"observed", "unverified", "blocked"}.issubset(statuses))
        self.assertEqual(next(item for item in report["checks"] if item["id"] == "local_structured_actions")["status"], "unverified")
        self.assertEqual(next(item for item in report["checks"] if item["id"] == "skill_project_discovery")["status"], "unverified")
        for check in report["checks"]:
            self.assertIn(check["status"], report["status_vocabulary"])
            self.assertTrue(check["reason"])
            if check["status"] == "blocked":
                self.assertTrue(check.get("human_gate", {}).get("required"))
        self.assertIn("toolchain_observation_invalid", report["error_messages"])
        markdown = render_acceptance_markdown(report)
        self.assertIn("Human Gates", markdown)
        self.assertIn("provider_or_model", markdown)

    def test_toolchain_error_message_matches_fail_closed_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            adapter = LocalAgentAdapter(root)
            action = {"type": "write_text", "path": "blocked.txt", "content": "x"}
            authorization = adapter.authorization_for(action, scope="stage22")
            invalid = {"schema_version": "bad", "verified_by": "bad", "target": {"normalized": "cli"}, "status": "observed", "observed": True, "commands": []}
            with self.assertRaises(AgentAdapterError) as ctx:
                adapter.execute(action, authorization=authorization, target_platform="cli", toolchain_observation=invalid)
            self.assertEqual(ctx.exception.code, "toolchain_observation_invalid")
            self.assertIn("工具链观察", build_acceptance_report()["error_messages"][ctx.exception.code])
            self.assertFalse((root / "blocked.txt").exists())

    def test_real_confirmation_gate_is_not_synthetic_success(self):
        report = build_acceptance_report()
        check = next(item for item in report["checks"] if item["id"] == "real_user_confirmation")
        self.assertEqual(check["status"], "blocked")
        self.assertIn("real user", check["reason"])
        self.assertTrue(check["human_gate"]["required"])


if __name__ == "__main__":
    unittest.main()
