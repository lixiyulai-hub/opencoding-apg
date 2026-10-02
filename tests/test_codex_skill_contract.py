from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.verify_codex_skill import SkillVerificationError, verify


class CodexSkillContractTests(unittest.TestCase):
    def test_standard_project_resource_is_valid_and_host_unverified(self):
        root = Path(__file__).resolve().parents[1]
        report = verify(root, exercise=True)
        self.assertEqual(report["resource"], ".agents/skills/opencoding")
        self.assertTrue(report["format_valid"])
        self.assertTrue(report["project_discovered"])
        self.assertTrue(report["project_loader_exercised"])
        self.assertIsNone(report["host_loaded"])
        self.assertEqual(report["status"], "format_valid_project_discovered_host_unverified")
        self.assertFalse(report["capabilities"]["sandbox"])

    def test_incomplete_resource_is_not_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / ".agents" / "skills" / "opencoding"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: opencoding\ndescription: x\n---\n", encoding="utf-8")
            with self.assertRaises(SkillVerificationError):
                verify(root)


if __name__ == "__main__":
    unittest.main()
