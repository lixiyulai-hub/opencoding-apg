from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from opencoding.agent_adapter import LocalAgentAdapter
from opencoding.host_capabilities import build_capability_matrix, normalize_target_platform


class Stage18CapabilityMatrixTests(unittest.TestCase):
    @staticmethod
    def _script() -> Path:
        return Path(__file__).resolve().parents[1] / "scripts" / "check_agent_adapter.py"

    def test_host_and_target_are_separate_for_windows_target_on_linux_fact(self):
        matrix = build_capability_matrix(
            target_platform="windows", host_os="posix", host_platform="linux",
            python_version="3.test", python_executable="/tmp/python",
        )
        self.assertEqual(matrix["host"]["family"], "linux")
        self.assertEqual(matrix["target"]["normalized"], "windows")
        self.assertTrue(matrix["compatibility"]["planning_supported"])
        self.assertFalse(matrix["compatibility"]["target_toolchain_verified"])
        self.assertEqual(matrix["toolchain"]["status"], "unverified")

    def test_alias_and_unknown_target_are_reported_without_execution_claim(self):
        self.assertEqual(normalize_target_platform("miniapp")["normalized"], "mini-program")
        unknown = normalize_target_platform("quantum-os")
        self.assertFalse(unknown["recognized"])
        matrix = build_capability_matrix(target_platform="quantum-os")
        self.assertTrue(matrix["compatibility"]["planning_supported"])
        self.assertFalse(matrix["compatibility"]["target_toolchain_verified"])

    def test_python_module_effect_is_not_described_as_network_safe(self):
        matrix = build_capability_matrix(target_platform="cli")
        self.assertEqual(matrix["actions"]["python_module"]["effect"], "arbitrary-same-user-python-side-effects")
        self.assertEqual(matrix["actions"]["python_module"]["network"], "not-controlled-by-adapter")
        self.assertFalse(matrix["sandbox"]["enabled"])
        self.assertEqual(matrix["process_boundary"], "same-user-subprocess")

    def test_adapter_capabilities_include_matrix_and_preserve_legacy_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            report = LocalAgentAdapter(Path(directory)).capabilities(target_platform="web")
            self.assertEqual(report["schema_version"], "1.1")
            self.assertEqual(report["target_platform"], "web")
            self.assertEqual(report["capability_matrix"]["target"]["normalized"], "web")
            self.assertFalse(report["model"]["available"])
            self.assertFalse(report["external"])
            self.assertFalse(report["sandbox"])

    def test_check_script_dry_run_has_no_project_write_and_reports_matrix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            result = subprocess.run(
                [sys.executable, str(self._script()), "--project-root", str(root), "--target-platform", "ios"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "capability_reported")
            self.assertEqual(report["target"]["normalized"], "ios")
            self.assertEqual(report["probe"]["status"], "not_run")
            self.assertFalse((root / ".opencoding").exists())

    def test_check_script_probe_requires_synthetic_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            result = subprocess.run(
                [sys.executable, str(self._script()), "--project-root", str(root), "--probe-local-write"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("--confirm-synthetic", result.stderr)
            self.assertFalse((root / ".opencoding").exists())

    def test_check_script_probe_executes_only_disposable_local_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            result = subprocess.run(
                [sys.executable, str(self._script()), "--project-root", str(root), "--target-platform", "web",
                 "--probe-local-write", "--confirm-synthetic"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "capability_and_local_write_probe_passed")
            self.assertEqual(report["probe"]["status"], "succeeded")
            self.assertTrue(report["probe"]["file_written"])
            self.assertTrue(report["probe"]["content_matches"])
            self.assertTrue(report["probe"]["synthetic_confirmation"])
            self.assertFalse(report["external_actions_executed"])


if __name__ == "__main__":
    unittest.main()
