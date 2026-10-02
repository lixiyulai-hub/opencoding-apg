from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from opencoding.agent_adapter import AgentAdapterError, LocalAgentAdapter
from opencoding.capability_contract import build_capability_contract
from opencoding.toolchain_probe import probe_target_toolchain


class Stage21CapabilityContractTests(unittest.TestCase):
    def test_contract_separates_observed_runtimes_from_target_execution(self):
        contract = build_capability_contract()
        self.assertEqual(contract["schema_version"], "opencoding-capability-contract-v1")
        self.assertEqual(contract["host"]["family"], "linux")
        self.assertTrue(contract["targets"]["cli"]["target_toolchain_verified"])
        self.assertTrue(contract["targets"]["web"]["target_toolchain_verified"])
        self.assertFalse(contract["targets"]["windows"]["target_toolchain_verified"])
        self.assertFalse(all(item["target_execution_verified"] for item in contract["targets"].values()))
        self.assertFalse(contract["claims"]["browser_execution_observed"])
        self.assertFalse(contract["claims"]["provider_or_model_observed"])

    def test_observed_profile_requires_probe_provenance_and_passing_commands(self):
        probe = probe_target_toolchain("python-cli-runtime", target_platform="cli")
        bad = dict(probe, verified_by="caller", observed=True)
        with self.assertRaises(ValueError):
            from opencoding.host_capabilities import build_capability_matrix
            build_capability_matrix(target_platform="cli", toolchain_observation=bad)
        bad = dict(probe, commands=[dict(probe["commands"][0], status="failed")])
        with self.assertRaises(ValueError):
            from opencoding.host_capabilities import build_capability_matrix
            build_capability_matrix(target_platform="cli", toolchain_observation=bad)

    def test_invalid_toolchain_observation_fails_before_write_or_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            adapter = LocalAgentAdapter(root)
            action = {"type": "write_text", "path": "must-not-exist.txt", "content": "x"}
            authorization = adapter.authorization_for(action, scope="stage21-boundary")
            invalid = {"schema_version": "wrong", "verified_by": "caller", "target": {"normalized": "cli"}, "status": "observed", "observed": True, "commands": []}
            with self.assertRaises(AgentAdapterError) as ctx:
                adapter.execute(action, authorization=authorization, target_platform="cli", toolchain_observation=invalid)
            self.assertEqual(ctx.exception.code, "toolchain_observation_invalid")
            self.assertFalse((root / "must-not-exist.txt").exists())
            claims = root / ".opencoding" / "authorizations"
            self.assertFalse(claims.exists())

    def test_external_authorization_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            adapter = LocalAgentAdapter(root)
            action = {"type": "write_text", "path": "external.txt", "content": "x"}
            authorization = adapter.authorization_for(action, scope="stage21-boundary")
            authorization["external"] = True
            with self.assertRaises(AgentAdapterError) as ctx:
                adapter.execute(action, authorization=authorization)
            self.assertEqual(ctx.exception.code, "external_action_rejected")
            self.assertFalse((root / "external.txt").exists())


if __name__ == "__main__":
    unittest.main()
