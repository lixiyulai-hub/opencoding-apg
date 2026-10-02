from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from opencoding.codex_host import CodexHostError, CodexSkillHost, install_skill


class CodexHostAdapterTests(unittest.TestCase):
    def test_private_codex_home_install_and_load_are_observable(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "codex-home"
            installed = install_skill(project, home)
            self.assertTrue(installed["installed"])
            self.assertEqual(installed["destination"], str(home / "skills" / "opencoding"))
            report = CodexSkillHost(project, codex_home=home).load(target_platform="cli")
            self.assertTrue(report["format_valid"])
            self.assertTrue(report["resource_discovered"])
            self.assertEqual(report["resource_source"], "codex_home")
            self.assertTrue(report["host_adapter_loaded"])
            self.assertIsNone(report["codex_managed_loader_observed"])
            self.assertFalse(report["capabilities"]["sandbox"])

    def test_install_is_non_destructive_without_overwrite(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "codex-home"
            install_skill(project, home)
            with self.assertRaises(CodexHostError):
                install_skill(project, home)
            self.assertTrue((home / "skills" / "opencoding" / "SKILL.md").is_file())

    def test_host_root_without_entrypoint_is_not_claimed_loaded(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "codex-home"
            target = home / "skills" / "opencoding"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text(
                "---\nname: opencoding\ndescription: x\n---\n", encoding="utf-8"
            )
            with self.assertRaises(CodexHostError):
                CodexSkillHost(project, codex_home=home).load()

    def test_symlinked_codex_home_is_rejected_before_copy(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            real = parent / "real-home"
            real.mkdir()
            alias = parent / "alias-home"
            try:
                alias.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation unavailable")
            with self.assertRaises(CodexHostError):
                install_skill(project, alias)
            self.assertFalse((real / "skills" / "opencoding").exists())
            with self.assertRaises(CodexHostError):
                CodexSkillHost(project, codex_home=alias).discover()


if __name__ == "__main__":
    unittest.main()
