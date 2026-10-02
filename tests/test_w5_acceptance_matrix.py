from __future__ import annotations

from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from opencoding.agent_adapter import AgentAdapterError, LocalAgentAdapter
from opencoding.host_connector import OfflineHostContract
from opencoding.target_adapters import TargetAdapterError
from opencoding.w5_acceptance import (
    CAPABILITY_IDS, DEFAULT_SCENARIOS, SUPPORTED_TARGET_LABELS,
    SyntheticAcceptanceMatrix, SyntheticBeginnerScenario, W5AcceptanceError,
    build_synthetic_acceptance_matrix,
)

ROOT = Path(__file__).resolve().parents[1]


class W5AcceptanceMatrixTests(unittest.TestCase):
    def test_fixture_matrix_covers_beginner_questions_capabilities_and_platforms(self):
        preview = build_synthetic_acceptance_matrix()
        self.assertEqual(preview["status"], "ready_for_review")
        self.assertTrue(preview["synthetic"])
        self.assertFalse(preview["authorization_granted"])
        self.assertEqual({item["id"] for item in preview["matrix"]["questions"]}, {
            "audience", "outcome", "platform", "data_persistence", "cross_device", "file_storage",
            "external_data", "admin_access", "account_access", "notifications", "payments", "multi_user",
        })
        self.assertEqual(preview["matrix"]["platforms"], list(SUPPORTED_TARGET_LABELS))
        self.assertEqual(len(preview["matrix"]["scenarios"]), len(DEFAULT_SCENARIOS))
        for scenario in preview["matrix"]["scenarios"]:
            self.assertTrue(scenario["fixture"])
            self.assertFalse(scenario["real_user"])
            self.assertFalse(scenario["provider_used"])
            self.assertEqual(set(scenario["recommendation"]["capabilities"]), set(CAPABILITY_IDS))
            self.assertTrue(scenario["all_platform_status"])
            self.assertTrue(all(item["execution"]["observed"] is False for item in scenario["all_platform_status"]))

    def test_platform_judgement_distinguishes_cli_web_and_ambiguous_targets(self):
        matrix = SyntheticAcceptanceMatrix().preview()["matrix"]["scenarios"]
        by_id = {item["scenario_id"]: item for item in matrix}
        cli = by_id["cli-local"]["recommendation"]
        self.assertEqual(cli["platforms"]["primary"], "cli")
        self.assertEqual(cli["status"], "ready")
        self.assertEqual(cli["capabilities"]["server"]["need"], "not_needed")
        self.assertEqual(cli["capabilities"]["database"]["need"], "required")
        web = by_id["web-collaboration"]["recommendation"]
        self.assertEqual(web["platforms"]["primary"], "web")
        self.assertEqual(web["capabilities"]["server"]["need"], "required")
        for capability in ("database", "auth", "admin", "storage", "notifications"):
            self.assertEqual(web["capabilities"][capability]["need"], "required")
        ambiguous = by_id["ambiguous-platform"]["recommendation"]
        self.assertEqual(ambiguous["status"], "draft")
        self.assertEqual(ambiguous["platforms"]["requested"], ["windows", "macos"])
        self.assertTrue(any("multiple_targets" in value for value in ambiguous["unresolved"]))

    def test_preview_approval_waves_evidence_and_rollback_are_digest_bound(self):
        matrix = SyntheticAcceptanceMatrix()
        preview = matrix.preview()
        with self.assertRaisesRegex(W5AcceptanceError, "approve"):
            matrix.record_wave("clarify", status="passed")
        approval = matrix.approve(expected_digest=preview["preview_digest"], synthetic=True)
        self.assertEqual(approval["status"], "approved_synthetic_fixture")
        evidence = {
            "clarify": {"questions": 12},
            "plan": {"scenarios": 3, "capabilities": list(CAPABILITY_IDS)},
            "preview": {"preview_digest": preview["preview_digest"], "writes": False},
            "execute": {"local_actions": 2, "external": False},
            "verify": {"tests_run": 3, "failures": 0},
            "rollback": {"scope": "receipt_covered_files_only", "removed": 2},
            "review": {"reviewer": "synthetic-independent-fixture", "managed_loader": None},
        }
        for wave in ("clarify", "plan", "preview", "execute", "verify", "rollback", "review"):
            matrix.record_wave(wave, status="passed", evidence=evidence[wave])
        report = matrix.report()
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["synthetic"])
        self.assertFalse(report["provider_used"])
        self.assertEqual(report["rollback"]["status"], "simulated_receipt_scope")
        with self.assertRaisesRegex(W5AcceptanceError, "recorded once"):
            matrix.record_wave("review", status="passed", evidence={})

    def test_preview_and_evidence_fail_closed_on_drift_or_invalid_wave(self):
        matrix = SyntheticAcceptanceMatrix()
        preview = matrix.preview()
        with self.assertRaisesRegex(W5AcceptanceError, "synthetic"):
            matrix.approve(expected_digest=preview["preview_digest"], synthetic=False)
        with self.assertRaisesRegex(W5AcceptanceError, "drifted"):
            matrix.approve(expected_digest="changed", synthetic=True)
        matrix.approve(expected_digest=preview["preview_digest"], synthetic=True)
        with self.assertRaisesRegex(W5AcceptanceError, "tests_run"):
            matrix.record_wave("verify", status="passed", evidence={"tests_run": 0})
        with self.assertRaisesRegex(W5AcceptanceError, "receipt_covered"):
            matrix.record_wave("rollback", status="passed", evidence={"scope": "all_files"})
        with self.assertRaisesRegex(W5AcceptanceError, "status"):
            matrix.record_wave("clarify", status="unknown")

    def test_real_external_and_target_execution_never_start_from_matrix(self):
        matrix = SyntheticAcceptanceMatrix().preview()
        with patch("socket.socket.connect", side_effect=AssertionError("network")), \
             patch("socket.create_connection", side_effect=AssertionError("network")), \
             patch("subprocess.Popen", side_effect=AssertionError("process")):
            # Building the matrix and asking W4 permission are pure local reads.
            report = OfflineHostContract(ROOT).inspect(target_platform="web")
            self.assertFalse(report["boundary"]["provider_used"])
            self.assertFalse(report["boundary"]["transport_implemented"])
            self.assertEqual(matrix["matrix"]["scenarios"][0]["provider_used"], False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            permission = OfflineHostContract(root).check_permission("reviewed_local_write")
            with self.assertRaises(AgentAdapterError):
                LocalAgentAdapter(root).execute({"type": "write_text", "path": "x.txt", "content": "x"}, authorization=permission)
            self.assertFalse((root / "x.txt").exists())

    def test_fixture_constructor_rejects_incomplete_or_sensitive_scenario(self):
        good = DEFAULT_SCENARIOS[0]
        with self.assertRaisesRegex(W5AcceptanceError, "answers"):
            SyntheticBeginnerScenario(good.scenario_id, good.sentence, {"platform": "cli"}, "cli")
        with self.assertRaisesRegex(W5AcceptanceError, "sensitive"):
            values = dict(good.answers)
            values["outcome"] = "使用 sk-" + "x" * 32
            SyntheticBeginnerScenario("secret", good.sentence, values, "cli")
        with self.assertRaisesRegex(W5AcceptanceError, "scenario id"):
            SyntheticBeginnerScenario("", good.sentence, good.answers, "cli")


if __name__ == "__main__":
    unittest.main()
