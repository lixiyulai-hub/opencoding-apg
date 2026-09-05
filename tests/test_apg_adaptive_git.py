import unittest
from scripts.apg_adaptive_git_preview import replay_digest, simulate

class AdaptiveGitCheckpointTests(unittest.TestCase):
    def test_success_node_recommends_checkpoint(self):
        result=simulate({"success_node":"validation-passed","verified_stages":["intake-ready","plan-ready"]})
        self.assertEqual(result["status"],"CHECKPOINT_RECOMMENDED")
        self.assertEqual(result["git_action"],"PREVIEW_ONLY")
        self.assertEqual(result["checkpoint"]["checkpoint_id"].split("-")[1],"validation")
        self.assertEqual(result["revert_point"],"plan-ready")
        self.assertFalse(result["execution_performed"])
    def test_duplicate_recommendation_is_suppressed(self):
        result=simulate({"success_node":"plan-ready","verified_stages":["plan-ready"]})
        self.assertEqual(result["status"],"ALREADY_RECOMMENDED")
        self.assertTrue(result["duplicate_suppressed"])
    def test_failure_freezes_and_points_to_latest_verified(self):
        result=simulate({"success_node":"implementation-slice","verified_stages":["plan-ready"]},failure="tests failed")
        self.assertEqual(result["status"],"FREEZE")
        self.assertEqual(result["revert_point"],"plan-ready")
        self.assertEqual(result["resume_condition"],"resume.after-tests-failed-is-resolved")
    def test_missing_success_node_waits(self):
        result=simulate({"verified_stages":[]})
        self.assertEqual(result["status"],"WAIT")
        self.assertEqual(result["resume_condition"],"resume.after-success-node-is-verified")
    def test_replay_is_deterministic_and_side_effect_free(self):
        state={"success_node":"release-candidate","verified_stages":["validation-passed"]}
        self.assertEqual(replay_digest(state),replay_digest(state))
        self.assertFalse(any(simulate(state)["external_actions"].values()))

if __name__=='__main__': unittest.main()
