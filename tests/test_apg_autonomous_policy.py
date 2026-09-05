import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("apg_autonomous_policy_preview", ROOT / "scripts" / "apg_autonomous_policy_preview.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class AutonomousPrgPolicyTests(unittest.TestCase):
    def test_beginner_routine_is_fully_automatic(self):
        result = MODULE.simulate("我想做一个离线学习工具")
        self.assertEqual(result["route"], "auto")
        self.assertFalse(result["human_gate"])
        self.assertTrue(result["dispatch_permitted"])
        self.assertEqual(result["loop_states"], ["INSPECT", "PROGRESS", "PLAN", "DISPATCH", "VALIDATE", "REPORT", "REQUEUE"])
        self.assertEqual(result["default_policy"], "auto-select-safe-defaults")
        self.assertFalse(any(result["external_actions"].values()))

    def test_recommendation_does_not_become_approval(self):
        result = MODULE.simulate("自动选择一个安全的本地默认方案")
        self.assertEqual(result["route"], "auto")
        self.assertFalse(result["human_gate"])

    def test_consequential_reasons_merge_to_one_gate(self):
        result = MODULE.simulate("请联网并使用密钥部署到线上并付款")
        self.assertEqual(result["route"], "consequential-gate")
        self.assertTrue(result["human_gate"])
        self.assertEqual(result["gate_reasons"], ["secret", "money", "network", "deployment-choice"])
        self.assertFalse(result["dispatch_permitted"])
        self.assertEqual(result["loop_states"][-1], "REQUEUE")

    def test_failure_freezes_and_provides_resume_condition(self):
        result = MODULE.simulate("继续自动检查", failure="missing evidence")
        self.assertEqual(result["loop_states"][-1], "FREEZE")
        self.assertFalse(result["dispatch_permitted"])
        self.assertEqual(result["resume_condition"], "resume.after-missing-evidence-is-resolved")

    def test_secret_values_are_redacted_and_replay_is_deterministic(self):
        request = "只做离线检查 token=sk_test_123456789"
        result = MODULE.simulate(request)
        self.assertNotIn("sk_test_123456789", json.dumps(result, ensure_ascii=False))
        self.assertEqual(MODULE.replay_digest(request), MODULE.replay_digest(request))
        self.assertFalse(result["external_actions"]["network"])


if __name__ == "__main__":
    unittest.main()
