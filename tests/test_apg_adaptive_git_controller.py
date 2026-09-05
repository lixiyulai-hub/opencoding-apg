import unittest

from scripts.apg_adaptive_git_controller import replay_digest, simulate


class AdaptiveGitControllerTests(unittest.TestCase):
    def setUp(self):
        self.success = {
            "success_node": "validation-passed",
            "verified_stages": ["intake-ready", "plan-ready"],
            "evidence_complete": True,
            "tests_passed": True,
            "scope_clean": True,
        }

    def test_controller_bridges_prg_git_checkpoint_and_ledger(self):
        result = simulate(self.success, request="我想做一个离线学习工具")
        self.assertEqual(result["schema_version"], "1.0")
        self.assertEqual(result["git_checkpoint"]["status"], "CHECKPOINT_RECOMMENDED")
        self.assertEqual(result["git_checkpoint"]["git_action"], "PREVIEW_ONLY")
        self.assertEqual(result["next_action"]["id"], "record-checkpoint-preview")
        self.assertTrue(result["ledger_event"]["event_id"].startswith("ledger-"))
        self.assertEqual(result["checkpoint_ledger"]["status"], "APPEND_CANDIDATE")
        self.assertTrue(result["checkpoint_ledger"]["append_candidate"])

    def test_failure_freezes_and_preserves_revert_point(self):
        state = dict(self.success, success_node="implementation-slice", failure="tests-failed")
        result = simulate(state)
        self.assertEqual(result["git_checkpoint"]["status"], "FREEZE")
        self.assertEqual(result["git_checkpoint"]["revert_point"], "plan-ready")
        self.assertEqual(result["next_action"]["id"], "freeze-and-repair-before-new-checkpoint")
        self.assertEqual(result["checkpoint_ledger"]["status"], "NO_CHECKPOINT_APPEND")

    def test_wait_and_duplicate_are_automatic(self):
        wait = simulate({"verified_stages": []})
        repeated_state = dict(self.success, success_node="plan-ready", verified_stages=["plan-ready"], repeated=True)
        duplicate_seed = simulate(repeated_state)
        duplicate = simulate(dict(repeated_state, checkpoint_ledger_events=[duplicate_seed["ledger_event"]]))
        self.assertEqual(wait["git_checkpoint"]["status"], "WAIT")
        self.assertEqual(duplicate["git_checkpoint"]["status"], "ALREADY_RECOMMENDED")
        self.assertTrue(duplicate["git_checkpoint"]["duplicate_suppressed"])
        self.assertEqual(duplicate["checkpoint_ledger"]["status"], "REUSE_EXISTING")

    def test_replay_and_external_actions_are_deterministic(self):
        first = simulate(self.success)
        second = simulate(self.success)
        self.assertEqual(first, second)
        self.assertEqual(replay_digest(self.success), first["replay_digest"])
        self.assertFalse(any(first["external_actions"].values()))
        self.assertFalse(any(first["checkpoint_ledger"]["external_actions"].values()))
        self.assertFalse(first["execution_performed"])


if __name__ == "__main__":
    unittest.main()
