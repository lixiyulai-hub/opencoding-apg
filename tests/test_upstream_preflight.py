import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from opencoding.preflight import inspect_installation


class InstallationTests(unittest.TestCase):
    def test_six_installation_states_without_importing_or_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "native" / "opencoding"
            package.mkdir(parents=True)
            # Executing a discovered package would fail this test.
            (package / "__init__.py").write_text("raise RuntimeError('must not import')", encoding="utf-8")
            skill = root / "SKILL.md"
            skill.write_text('---\nname: opencoding\nmetadata:\n  version: "0.1.1"\n---\n', encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"runtime": {"version": "0.2.8", "files": {
                "__init__.py": hashlib.sha256((package / "__init__.py").read_bytes()).hexdigest()}},
                "skill": {"sha256": hashlib.sha256(skill.read_bytes()).hexdigest()}}), encoding="utf-8")
            def probe(**kwargs):
                before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                result = inspect_installation(manifest_path=manifest, search_path=[], **kwargs)
                self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()})
                return result
            self.assertEqual(probe()["route"], "unavailable")
            self.assertEqual(probe(skill_path=skill)["route"], "unavailable")
            apg = root / "apg"
            (apg / "scripts").mkdir(parents=True)
            (apg / "scripts/project_governance.py").write_text("raise RuntimeError()")
            (apg / "project_governance/commands").mkdir(parents=True)
            (apg / "project_governance/commands/check.py").write_text("# controller")
            (apg / "VERSION").write_text("0.3.0")
            self.assertEqual(probe(apg_root=apg)["route"], "apg-only")
            both = probe(apg_root=apg, runtime_root=package.parent, skill_path=skill)
            self.assertEqual(both["route"], "native+apg")
            self.assertEqual(both["native"]["origin"], str((package / "__init__.py").resolve()))
            skill.write_text('metadata:\n  version: "0.1.0"\nold router', encoding="utf-8")
            old = probe(runtime_root=package.parent, skill_path=skill)
            self.assertIn("skill_content_mismatch", old["issues"])
            (package / "__init__.py").write_text("# unrelated same-name package")
            shadowed = probe(runtime_root=package.parent)
            self.assertEqual(shadowed["native"]["integrity"], "mismatch")
            self.assertEqual(shadowed["route"], "unavailable")
