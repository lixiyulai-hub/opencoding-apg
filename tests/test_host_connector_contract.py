from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opencoding.agent_adapter import AgentAdapterError, LocalAgentAdapter
from opencoding.host_connector import (
    ConnectorContract, DEFAULT_CONNECTORS, FixtureConnector, HostConnectorError,
    OfflineConnectorRegistry, OfflineHostContract,
)

ROOT = Path(__file__).resolve().parents[1]


def fixture(contract):
    return FixtureConnector(contract, {
        operation: {"status": "ok", "data": {"synthetic_id": "example-1"}}
        for operation in contract.operations
    })


class HostConnectorContractTests(unittest.TestCase):
    def test_local_host_observation_is_read_only_and_keeps_managed_loader_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = OfflineHostContract(root).inspect(target_platform="cli")
            self.assertEqual(report["status"], "local_adapter_observed_managed_loader_unverified")
            self.assertTrue(report["host"]["adapter_loaded"])
            self.assertIsNone(report["host"]["managed_loader_observed"])
            self.assertFalse(report["permissions"]["authorization_granted"])
            self.assertFalse(report["boundary"]["sandbox"])
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual({c["name"] for c in report["connectors"]},
                             {"server", "database", "api", "auth", "payment", "notification", "admin", "storage", "provider"})
            self.assertTrue(all(c["implementation"] == "contract_only" and not c["live_verified"]
                                for c in report["connectors"]))

    def test_local_permission_query_cannot_authorize_an_adapter_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = OfflineHostContract(root)
            permission = host.check_permission("reviewed_local_write")
            self.assertEqual(permission["status"], "requires_local_authorization")
            self.assertFalse(permission["authorization_granted"])
            with self.assertRaises(AgentAdapterError) as error:
                LocalAgentAdapter(root).execute(
                    {"type": "write_text", "path": "not-authorized.txt", "content": "fixture"},
                    authorization=permission)
            self.assertEqual(error.exception.code, "authorization_required")
            self.assertEqual(list(root.iterdir()), [])

    def test_external_boundaries_and_unknown_operations_fail_closed(self):
        host = OfflineHostContract(ROOT)
        for operation in ("provider", "network", "credentials", "remote_git", "deployment", "publication"):
            with self.subTest(operation=operation):
                blocked = host.check_permission(operation)
                self.assertEqual(blocked["status"], "blocked_human_gate")
                self.assertTrue(blocked["human_gate"]["required"])
                self.assertFalse(blocked["executed"])
                self.assertFalse(blocked["authorization_granted"])
        for operation in ("shell", "", None, [], {"approved": True}):
            with self.subTest(operation=operation), self.assertRaises(HostConnectorError):
                host.check_permission(operation)

    def test_connector_declarations_and_fixture_matrix_do_not_call_transports(self):
        # These sentinels catch accidental transport/process dispatch in this
        # path; they are test coverage, not an OS network isolation claim.
        with ExitStack() as stack:
            for target in ("socket.socket.connect", "socket.create_connection", "urllib.request.urlopen", "subprocess.Popen"):
                stack.enter_context(patch(target, side_effect=AssertionError("unexpected transport or process")))
            registry = OfflineConnectorRegistry()
            for contract in DEFAULT_CONNECTORS:
                for operation in contract.operations:
                    with self.subTest(connector=contract.name, operation=operation):
                        blocked = registry.request(contract.name, operation)
                        self.assertEqual(blocked["status"], "blocked_human_gate")
                        self.assertFalse(blocked["executed"])
                        self.assertFalse(blocked["permission"]["credentials"])
                        connector = fixture(contract)
                        preview = connector.preview(operation)
                        result = connector.simulate(operation, expected_digest=preview["preview_digest"], synthetic=True)
                        self.assertEqual(result["status"], "simulated")
                        self.assertEqual(result["response"], {"status": "ok", "data": {"synthetic_id": "example-1"}})
                        self.assertTrue(result["synthetic"])
                        self.assertFalse(result["live_verified"])
                        self.assertFalse(result["external_executed"])

    def test_unknown_connector_operation_and_nonstring_requests_are_rejected(self):
        registry = OfflineConnectorRegistry()
        for name, operation in (("unknown", "read"), ("api", "delete"), (None, "read"), ("api", [])):
            with self.subTest(name=name, operation=operation), self.assertRaises(HostConnectorError):
                registry.request(name, operation)

    def test_registry_declarations_are_immutable_and_cannot_activate_local_code(self):
        registry = OfflineConnectorRegistry(())
        contract = ConnectorContract("local", ("read",), external=False, credentials=False)
        registry.register(contract)
        snapshot = registry.snapshot()
        snapshot[0]["operations"].append("write")
        with self.assertRaises(HostConnectorError):
            registry.request("local", "write")
        self.assertEqual(registry.request("local", "read")["status"], "blocked_unimplemented")
        with self.assertRaises(HostConnectorError):
            registry.register(contract)
        with self.assertRaises(HostConnectorError):
            registry.register(lambda: None)
        with self.assertRaises(HostConnectorError):
            OfflineHostContract(ROOT, connectors=object())

    def test_malformed_and_mutable_declarations_are_rejected(self):
        for kwargs in (
            {"name": "../network", "operations": ("read",)},
            {"name": "api", "operations": ["read"]},
            {"name": "api", "operations": ([],)},
            {"name": "api", "operations": ("read", "read")},
            {"name": "api", "operations": ()},
            {"name": "api", "operations": ("read",), "external": 0},
            {"name": "api", "operations": ("read",), "credentials": 1},
            {"name": "api", "operations": ("read",), "external": False, "credentials": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(HostConnectorError):
                ConnectorContract(**kwargs)

    def test_fixture_rejects_missing_synthetic_confirmation_and_foreign_digests(self):
        contract = ConnectorContract("api", ("read", "write"))
        connector = fixture(contract)
        preview = connector.preview("read")
        for synthetic in (False, None, 1, "true"):
            with self.subTest(synthetic=synthetic), self.assertRaises(HostConnectorError) as error:
                connector.simulate("read", expected_digest=preview["preview_digest"], synthetic=synthetic)
            self.assertEqual(error.exception.code, "synthetic_confirmation_required")
        for operation, digest in (("write", preview["preview_digest"]), ("read", "changed")):
            with self.subTest(operation=operation), self.assertRaises(HostConnectorError) as error:
                connector.simulate(operation, expected_digest=digest, synthetic=True)
            self.assertEqual(error.exception.code, "fixture_drifted")
        changed = FixtureConnector(contract, {op: {"status": "failed", "data": None} for op in contract.operations})
        with self.assertRaises(HostConnectorError):
            changed.simulate("read", expected_digest=preview["preview_digest"], synthetic=True)

    def test_fixture_failure_and_repeated_reads_are_deterministic_without_shared_mutation(self):
        responses = {"read": {"status": "failed", "data": {"reason": "synthetic unavailable"}}}
        connector = FixtureConnector(ConnectorContract("api", ("read",)), responses)
        preview = connector.preview("read")
        responses["read"]["status"] = "ok"
        first = connector.simulate("read", expected_digest=preview["preview_digest"], synthetic=True)
        self.assertEqual(first["response"]["status"], "failed")
        first["response"]["data"]["reason"] = "mutated"
        preview["response"]["status"] = "ok"
        second = connector.simulate("read", expected_digest=preview["preview_digest"], synthetic=True)
        self.assertEqual(second["response"], {"status": "failed", "data": {"reason": "synthetic unavailable"}})
        self.assertEqual(connector.preview("read")["preview_digest"], preview["preview_digest"])

    def test_fixture_rejects_credentials_nonjson_oversize_and_wrong_operation_maps(self):
        contract = ConnectorContract("api", ("read",))
        invalid = [{}, {"write": {"status": "ok", "data": None}},
                   {"read": {"status": "unknown", "data": None}},
                   {"read": {"status": "ok", "data": object()}},
                   {"read": {"status": "ok", "data": "x" * 65537}},
                   {"read": {"status": "ok", "data": {"api_key": "sk-" + "x" * 32}}}]
        for responses in invalid:
            with self.subTest(kind=str(responses)[:60]), self.assertRaises(HostConnectorError):
                FixtureConnector(contract, responses)

    def test_invalid_project_or_partial_explicit_host_has_no_success_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(HostConnectorError):
                OfflineHostContract(root / "missing").inspect()
            home = root / "home"
            resource = home / "skills/opencoding"
            resource.mkdir(parents=True)
            (resource / "SKILL.md").write_text("incomplete", encoding="utf-8")
            with self.assertRaises(HostConnectorError):
                OfflineHostContract(ROOT, codex_home=home).inspect()


if __name__ == "__main__":
    unittest.main()
