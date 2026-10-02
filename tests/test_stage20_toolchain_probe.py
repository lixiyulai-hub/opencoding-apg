from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from opencoding.agent_adapter import LocalAgentAdapter
from opencoding.host_capabilities import build_capability_matrix
from opencoding.toolchain_probe import probe_target_toolchain


class Stage20ToolchainProbeTests(unittest.TestCase):
    def test_default_matrix_stays_unverified(self):
        matrix = build_capability_matrix(target_platform="web")
        self.assertEqual(matrix["toolchain"]["status"], "unverified")
        self.assertFalse(matrix["compatibility"]["target_toolchain_verified"])

    def test_python_profile_observed_is_scoped(self):
        report = probe_target_toolchain("python-cli-runtime", target_platform="cli")
        self.assertEqual(report["status"], "observed")
        self.assertTrue(report["observed"])
        self.assertFalse(report["network_requested"])
        self.assertFalse(report["shell"])
        self.assertIn("runtime only", report["scope"])
        matrix = build_capability_matrix(target_platform="cli", toolchain_observation=report)
        self.assertTrue(matrix["compatibility"]["target_toolchain_verified"])
        self.assertEqual(matrix["toolchain"]["profile"], "python-cli-runtime")

    def test_web_profile_only_observed_when_node_and_npm_pass(self):
        report = probe_target_toolchain("node-web-runtime", target_platform="web")
        self.assertIn(report["status"], {"observed", "unverified"})
        if report["status"] == "observed":
            self.assertTrue(all(item["status"] == "passed" for item in report["commands"]))
        self.assertFalse(report["network_requested"])
        self.assertFalse(report["shell"])

    def test_windows_profile_does_not_claim_linux_target_observation(self):
        report = probe_target_toolchain("windows-dotnet-runtime", target_platform="windows")
        self.assertEqual(report["status"], "unverified")
        self.assertFalse(report["observed"])
        self.assertIn("incompatible", report["reason"])

    def test_adapter_execute_can_attach_verified_profile_observation(self):
        report = probe_target_toolchain("python-cli-runtime", target_platform="cli")
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalAgentAdapter(Path(directory).resolve())
            action = {"type": "write_text", "path": "toolchain.txt", "content": "observed\n"}
            authorization = adapter.authorization_for(action, scope="stage20-synthetic")
            result = adapter.execute(
                action,
                authorization=authorization,
                target_platform="cli",
                toolchain_observation=report,
                run_id="stage20-observed-profile",
            )
            observation = result["capability_observation"]
            self.assertEqual(result["status"], "succeeded")
            self.assertTrue(observation["target_toolchain_verified"])
            self.assertEqual(observation["toolchain"]["profile"], "python-cli-runtime")

    def test_mismatched_observation_is_rejected(self):
        report = probe_target_toolchain("python-cli-runtime", target_platform="cli")
        with self.assertRaises(ValueError):
            build_capability_matrix(target_platform="web", toolchain_observation=report)


if __name__ == "__main__":
    unittest.main()
