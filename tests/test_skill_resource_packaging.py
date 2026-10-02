from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from opencoding.agent_tasks import skill_identity
from opencoding.skill_resources import SkillResourceError, locate_skill_resource


ROOT = Path(__file__).resolve().parents[1]
RESOURCE_FILES = ("SKILL.md", "skill.json")


class SkillResourcePackagingTests(unittest.TestCase):
    def test_packaged_resource_is_byte_identical_to_canonical_checkout(self):
        canonical = ROOT / ".agents" / "skills" / "opencoding"
        packaged = ROOT / "opencoding" / "resources" / "skill"
        self.assertEqual(locate_skill_resource(ROOT).identifier, ".agents/skills/opencoding")
        self.assertEqual(skill_identity()["resource"], ".agents/skills/opencoding")
        for filename in RESOURCE_FILES:
            self.assertEqual((packaged / filename).read_bytes(), (canonical / filename).read_bytes(), filename)

    def test_partial_standard_resource_fails_closed_before_package_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            partial = root / ".agents" / "skills" / "opencoding"
            partial.mkdir(parents=True)
            (partial / "SKILL.md").write_text((ROOT / ".agents/skills/opencoding/SKILL.md").read_text(encoding="utf-8"), encoding="utf-8")
            with self.assertRaises(SkillResourceError):
                locate_skill_resource(root)


if __name__ == "__main__":
    unittest.main()
