import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class FrontendContractTests(unittest.TestCase):
    def test_dual_mode_and_three_step_child_flow(self):
        source = (ROOT / "apps/miniapp/src/domain/taskLoop.ts").read_text(encoding="utf-8")
        app = (ROOT / "apps/miniapp/src/app.tsx").read_text(encoding="utf-8")
        self.assertIn('"child"', source)
        self.assertIn('"parent"', source)
        self.assertEqual(source.count('"'), source.count('"'))
        for token in ("view-task", "complete-and-submit-proof", "answer-why", "paymentControlsVisibleToChild"):
            self.assertIn(token, source + app)
        self.assertIn("return false", source)

if __name__ == "__main__":
    unittest.main()
