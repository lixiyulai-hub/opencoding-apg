import json
import tempfile
import unittest
from pathlib import Path

from scripts.apg_deployment_preview import evaluate_preview, rollback_copy


class APGReleaseOrchestrationTests(unittest.TestCase):
    def test_complete_local_evidence_is_ready_for_preview(self):
        result = evaluate_preview({"release_approval": True, "rollback_evidence": True})
        self.assertEqual(result["status"], "ready-for-preview")
        self.assertEqual(result["blocker_codes"], [])
        self.assertTrue(result["preview_only"])

    def test_missing_approval_is_blocked(self):
        result = evaluate_preview({"rollback_evidence": True})
        self.assertEqual(result["status"], "BLOCK")
        self.assertEqual(result["blocker_codes"], ["missing_release_approval"])

    def test_missing_rollback_evidence_is_blocked(self):
        result = evaluate_preview({"release_approval": True})
        self.assertEqual(result["status"], "BLOCK")
        self.assertEqual(result["blocker_codes"], ["missing_rollback_evidence"])

    def test_provider_and_network_requirements_are_blocked(self):
        result = evaluate_preview({
            "release_approval": True,
            "rollback_evidence": True,
            "provider_required": True,
            "network_required": True,
        })
        self.assertEqual(result["status"], "BLOCK")
        self.assertEqual(result["blocker_codes"], ["provider_required", "network_required"])

    def test_real_data_requirement_is_blocked(self):
        result = evaluate_preview({
            "release_approval": True,
            "rollback_evidence": True,
            "real_data_required": True,
        })
        self.assertEqual(result["status"], "BLOCK")
        self.assertEqual(result["blocker_codes"], ["real_data_required"])

    def test_release_and_publication_actions_are_never_executed(self):
        result = evaluate_preview({"release_approval": True, "rollback_evidence": True})
        self.assertFalse(result["release_action_executed"])
        self.assertFalse(result["publication_action_executed"])
        self.assertFalse(result["deployment_action_executed"])
        self.assertEqual(result["external_actions"], [])

    def test_rollback_restores_disposable_copy_and_leaves_fixture_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pre_state = root / "pre-state.txt"
            disposable = root / "disposable.txt"
            pre_state.write_text("before\n", encoding="utf-8")
            disposable.write_text("after\n", encoding="utf-8")
            self.assertEqual(rollback_copy(pre_state, disposable), "before\n")
            self.assertEqual(disposable.read_text(encoding="utf-8"), "before\n")

        fixture = Path(__file__).resolve().parents[1] / "artifacts" / "apg-deployment-preview" / "MODIFIED_FILE"
        self.assertIn("deployment-preview test artifact", fixture.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
