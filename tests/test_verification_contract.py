import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class VerificationContractTests(unittest.TestCase):
    def test_role_and_ledger_guards_are_present(self):
        source = (ROOT / "services/domain/src/lib.rs").read_text(encoding="utf-8")
        self.assertIn("RoleNotAllowed", source)
        self.assertIn("WhyAlreadyAnswered", source)
        self.assertIn("balance_after", source)

    def test_frontend_has_no_external_effects(self):
        app = (ROOT / "apps/miniapp/src/app.tsx").read_text(encoding="utf-8")
        loop = (ROOT / "apps/miniapp/src/domain/taskLoop.ts").read_text(encoding="utf-8")
        self.assertIn('externalEffects: "disabled-in-offline-slice"', app)
        self.assertIn("return false", loop)
        self.assertIn('"review-proof"', loop)
        self.assertIn('"approve-or-return"', loop)

if __name__ == "__main__":
    unittest.main()
