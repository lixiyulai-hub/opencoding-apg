from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from opencoding.agent_adapter import LocalAgentAdapter
from opencoding.target_adapters import (
    MacOSTargetAdapter,
    TargetAdapterError,
    WebTargetAdapter,
    WindowsTargetAdapter,
    get_target_adapter,
    target_adapter_status,
)
from opencoding.toolchain_probe import probe_target_toolchain
from opencoding.capability_contract import build_capability_contract


class Stage26TargetAdapterContractTests(unittest.TestCase):
    def test_registry_normalizes_cross_platform_labels(self):
        self.assertIsInstance(get_target_adapter("win32"), WindowsTargetAdapter)
        self.assertIsInstance(get_target_adapter("darwin"), MacOSTargetAdapter)
        self.assertIsInstance(get_target_adapter("browser"), WebTargetAdapter)

    def test_capability_contract_includes_macos_without_linux_observation(self):
        contract = build_capability_contract()
        macos = contract["targets"]["macos"]
        self.assertEqual(macos["toolchain"]["status"], "unverified")
        self.assertFalse(macos["target_toolchain_verified"])
        self.assertFalse(macos["target_execution_verified"])

    def test_linux_host_keeps_windows_macos_and_web_execution_unverified(self):
        for label in ("windows", "macos", "web"):
            report = get_target_adapter(label).status_report(host_family="linux")
            self.assertEqual(report["status"], "unverified")
            self.assertEqual(report["execution"]["status"], "unverified")
            self.assertFalse(report["execution"]["observed"])
            self.assertFalse(report["execution"]["attempted"])
            self.assertIsNone(report["managed_loader"]["observed"])
            self.assertFalse(report["sandbox"]["enabled"])

    def test_toolchain_observation_does_not_promote_target_execution(self):
        node = probe_target_toolchain("node-web-runtime", target_platform="web")
        report = WebTargetAdapter().status_report(toolchain_observation=node, host_family="linux")
        self.assertIn(report["toolchain"]["status"], {"observed", "unverified"})
        self.assertFalse(report["execution"]["observed"])
        self.assertEqual(report["execution"]["status"], "unverified")

    def test_contract_execute_fails_closed_without_target_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            report = WindowsTargetAdapter().status_report(host_family="linux")
            with self.assertRaises(TargetAdapterError) as context:
                WindowsTargetAdapter().execute({"type": "write_text", "path": "never.txt", "content": "x"}, host_family="linux")
            self.assertEqual(context.exception.code, "target_execution_unverified")
            self.assertEqual(context.exception.report["execution"], report["execution"])
            self.assertFalse((root / "never.txt").exists())

    def test_invalid_observation_is_blocked_before_execution(self):
        bad = {"schema_version": "wrong", "status": "observed"}
        report = target_adapter_status("web", toolchain_observation=bad, host_family="linux")
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["error"]["code"], "toolchain_observation_invalid")
        with self.assertRaises(TargetAdapterError) as context:
            WebTargetAdapter().execute({"type": "browser"}, toolchain_observation=bad, host_family="linux")
        self.assertEqual(context.exception.code, "target_capability_blocked")

    def test_local_adapter_exposes_target_contract_without_claiming_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            capabilities = LocalAgentAdapter(Path(directory).resolve()).capabilities(target_platform="macos")
            target = capabilities["target_adapter"]
            self.assertEqual(target["target"]["normalized"], "macos")
            self.assertEqual(target["execution"]["status"], "unverified")
            self.assertFalse(target["execution"]["observed"])


if __name__ == "__main__":
    unittest.main()
