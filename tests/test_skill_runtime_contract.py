from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from opencoding.transactions import apply_changes, preview_changes
from opencoding.source_identity import source_fingerprint


class SkillRuntimeContractTests(unittest.TestCase):
    _RUNTIME_SOURCE_PATHS = (
        "opencoding",
        "skills/opencoding",
        ".agents/skills/opencoding",
        "scripts/run_skill_contract.py",
        "scripts/recover_skill_transaction.py",
    )

    @property
    def source_root(self) -> Path:
        return Path(__file__).resolve().parents[1]

    @property
    def runner(self) -> Path:
        return self.source_root / "scripts" / "run_skill_contract.py"

    @property
    def source_sha(self) -> str:
        return source_fingerprint(self.source_root)

    @property
    def recovery_runner(self) -> Path:
        return self.source_root / "scripts" / "recover_skill_transaction.py"

    def _copy_runtime_source(self, destination: Path) -> None:
        """Copy only the reviewed runtime inputs used by the skill runner.

        The repository also carries historical integration evidence, including
        links from an old Windows checkout.  Those files are outside the
        runtime source fingerprint and must not make a source-drift fixture
        fail while it is being prepared.
        """
        import shutil

        for relative in self._RUNTIME_SOURCE_PATHS:
            source = self.source_root / relative
            target = destination / relative
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target, follow_symlinks=False)

    def test_install_discover_load_changes_home_but_runs_no_project_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "codex-home"
            result = subprocess.run(
                [sys.executable, str(self.runner), "--project-root", str(self.source_root), "--codex-home", str(home), "--target-platform", "cli"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "installed_and_loaded")
            self.assertEqual(report["discovery"]["source"], "codex_home")
            self.assertTrue(report["load"]["host_adapter_loaded"])
            self.assertIsNone(report["load"]["codex_managed_loader_observed"])
            self.assertFalse(report["actions_executed"])

    def test_actions_require_explicit_synthetic_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "codex-home"
            execution = root / "execution"
            action_file = root / "actions.json"
            action_file.write_text(json.dumps([{"type": "write_text", "path": "probe.txt", "content": "x"}]), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(self.runner), "--project-root", str(self.source_root), "--codex-home", str(home),
                 "--execution-root", str(execution), "--run-actions", "--action-file", str(action_file)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("--confirm-synthetic", result.stdout)
            self.assertFalse((execution / "probe.txt").exists())
            self.assertFalse(home.exists())
            self.assertFalse(execution.exists())

    def test_actions_require_reviewed_source_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, execution, action_file = root / "home", root / "execution", root / "actions.json"
            action_file.write_text(json.dumps([{"type": "write_text", "path": "probe.txt", "content": "x"}]), encoding="utf-8")
            digest = hashlib.sha256(action_file.read_bytes()).hexdigest()
            result = subprocess.run(
                [sys.executable, str(self.runner), "--project-root", str(self.source_root), "--codex-home", str(home),
                 "--execution-root", str(execution), "--run-actions", "--confirm-synthetic", "--action-file", str(action_file),
                 "--expected-action-file-sha256", digest], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("--expected-source-sha256", result.stdout)
            self.assertFalse(home.exists())
            self.assertFalse(execution.exists())

    def test_repeat_runs_same_action_file_in_one_isolated_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "codex-home"
            execution = root / "execution"
            action_file = root / "actions.json"
            action_file.write_text(json.dumps([
                {"type": "write_text", "path": "repeat/main.py", "content": "VALUE = 4\n"},
                {"type": "write_text", "path": "repeat/test_main.py", "content": "from .main import VALUE\nassert VALUE == 4\nprint('repeat ok')\n"},
                {"type": "python_module", "module": "repeat.test_main", "args": []},
            ]), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(self.runner), "--project-root", str(self.source_root), "--codex-home", str(home),
                 "--execution-root", str(execution), "--run-actions", "--confirm-synthetic", "--repeat", "2", "--action-file", str(action_file),
                 "--expected-action-file-sha256", hashlib.sha256(action_file.read_bytes()).hexdigest(),
                 "--expected-source-sha256", self.source_sha],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["repeat_count"], 2)
            self.assertEqual(len(report["runs"]), 2)
            self.assertTrue(all(item["status"] == "succeeded" for item in report["actions"]))
            self.assertEqual(len({item["run_id"] for item in report["actions"]}), 6)
            self.assertTrue((execution / "repeat/main.py").is_file())

    def test_source_drift_blocks_before_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copied = root / "source-copy"
            self._copy_runtime_source(copied)
            self.assertEqual(source_fingerprint(copied), self.source_sha)
            with (copied / "skills" / "opencoding" / "SKILL.md").open("a", encoding="utf-8") as changed_skill:
                changed_skill.write("\nchanged in test\n")
            action_file = root / "actions.json"
            action_file.write_text(json.dumps([{"type": "write_text", "path": "probe.txt", "content": "x"}]), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(copied / "scripts" / "run_skill_contract.py"), "--project-root", str(copied),
                 "--codex-home", str(root / "home"), "--execution-root", str(root / "execution"), "--run-actions",
                 "--confirm-synthetic", "--action-file", str(action_file),
                 "--expected-action-file-sha256", hashlib.sha256(action_file.read_bytes()).hexdigest(),
                 "--expected-source-sha256", self.source_sha], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("source_drift", result.stdout)
            self.assertFalse((root / "home").exists())
            self.assertFalse((root / "execution").exists())

    def test_recovery_helper_rolls_back_pending_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = preview_changes(root, {"pending/recovery.txt": "pending\n"})
            applied = apply_changes(root, plan, approved_digest=plan["plan_digest"])
            self.assertTrue((root / "pending/recovery.txt").is_file())
            result = subprocess.run(
                [sys.executable, str(self.recovery_runner), "--execution-root", str(root), "--transaction-id", applied["transaction_id"], "--rollback"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "recovered")
            self.assertEqual(report["residual_paths"], [])
            self.assertFalse((root / "pending/recovery.txt").exists())


    def test_preflight_rejects_drift_links_overlap_invalid_later_action_before_writes(self):
        for mode in ("drift", "missing_digest", "invalid_later", "symlink", "overlap", "probe"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                home, execution = base / "home", base / "execution"
                actions = [{"type": "write_text", "path": "first.txt", "content": "x"}]
                if mode == "invalid_later":
                    actions.append({"type": "unknown", "path": "bad"})
                path = base / "actions.json"
                path.write_text(json.dumps(actions), encoding="utf-8")
                expected = hashlib.sha256(path.read_bytes()).hexdigest()
                if mode == "drift":
                    path.write_text(json.dumps([{"type": "write_text", "path": "changed.txt", "content": "changed"}]))
                if mode == "symlink":
                    outside = base / "outside"
                    outside.mkdir()
                    (base / "alias").symlink_to(outside, target_is_directory=True)
                    execution = base / "alias" / "child"
                if mode == "overlap":
                    execution = home / "project"
                command = [sys.executable, str(self.runner), "--project-root", str(self.source_root), "--codex-home", str(home),
                           "--execution-root", str(execution), "--run-actions", "--confirm-synthetic", "--action-file", str(path)]
                if mode != "missing_digest":
                    command += ["--expected-action-file-sha256", expected]
                command += ["--expected-source-sha256", self.source_sha]
                if mode == "probe":
                    command += ["--rollback-probe-path", "../outside.txt"]
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["status"], "blocked")
                self.assertFalse(home.exists())
                self.assertFalse(execution.exists())
                self.assertFalse((base / "outside.txt").exists())

    def test_failed_action_stops_batch_and_repeats(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base / "actions.json"
            path.write_text(json.dumps([
                {"type": "write_text", "path": "failer.py", "content": "raise RuntimeError('expected failure')"},
                {"type": "python_module", "module": "failer", "args": []},
                {"type": "write_text", "path": "after.txt", "content": "must not execute"},
            ]))
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
            result = subprocess.run([sys.executable, str(self.runner), "--project-root", str(self.source_root),
                "--codex-home", str(base / "home"), "--execution-root", str(base / "project"),
                "--run-actions", "--confirm-synthetic", "--action-file", str(path),
                "--expected-action-file-sha256", expected, "--expected-source-sha256", self.source_sha,
                "--repeat", "2"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["stopped_at"], {"repeat": 0, "action": 1})
            self.assertEqual(len(report["actions"]), 2)
            self.assertEqual(report["repeat_count"], 0)
            self.assertFalse((base / "project/after.txt").exists())
            self.assertTrue((base / "project/failer.py").exists())  # no invented auto-rollback


if __name__ == "__main__":
    unittest.main()
