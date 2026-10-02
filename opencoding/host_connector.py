"""W4 offline contracts; declarations and synthetic responses grant no authority.

No transport, credentials, callbacks or dynamic connector imports are supported.
Existing reviewed local actions still go through LocalAgentAdapter.  These
same-user integrity records are not signatures, attestations or a sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Iterable

from .codex_host import CodexHostError, CodexSkillHost
from .safety import canonical_json, inspect_sensitive, sha256_bytes

CONTRACT_SCHEMA = "opencoding-host-connector-contract-v1"
_NAME = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
LOCAL_HOST_OPERATIONS = (
    "skill_discover", "skill_load", "reviewed_local_write", "reviewed_local_test",
)
EXTERNAL_BOUNDARIES = (
    "provider", "network", "credentials", "remote_git", "deployment", "publication",
)


class HostConnectorError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


@dataclass(frozen=True)
class ConnectorContract:
    """Immutable declaration only, including a local declaration with no driver."""

    name: str
    operations: tuple[str, ...]
    external: bool = True
    credentials: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise HostConnectorError("connector_name_invalid", "connector name is invalid")
        if (type(self.operations) is not tuple or not 1 <= len(self.operations) <= 16
                or any(not isinstance(op, str) or not _NAME.fullmatch(op) for op in self.operations)
                or len(set(self.operations)) != len(self.operations)):
            raise HostConnectorError("connector_operations_invalid", "operations must be a bounded tuple of unique names")
        if type(self.external) is not bool or type(self.credentials) is not bool:
            raise HostConnectorError("connector_flags_invalid", "connector flags must be boolean")
        if not self.external and self.credentials:
            raise HostConnectorError("connector_flags_invalid", "a local connector cannot require credentials")

    def snapshot(self) -> dict[str, Any]:
        return {"name": self.name, "operations": list(self.operations),
                "external": self.external, "credentials": self.credentials,
                "implementation": "contract_only", "live_verified": False}


# M10's eight capability categories plus M09's model/provider boundary.
# These operation names are our contract vocabulary, not vendor API claims.
DEFAULT_CONNECTORS = (
    ConnectorContract("server", ("inspect", "provision")),
    ConnectorContract("database", ("query", "write")),
    ConnectorContract("api", ("read", "write")),
    ConnectorContract("auth", ("login", "revoke")),
    ConnectorContract("payment", ("charge", "refund")),
    ConnectorContract("notification", ("send",)),
    ConnectorContract("admin", ("read", "write")),
    ConnectorContract("storage", ("read", "write")),
    ConnectorContract("provider", ("complete",)),
)


class OfflineConnectorRegistry:
    """Declarations can never dispatch a real connector, even a local one."""

    def __init__(self, contracts: Iterable[ConnectorContract] = DEFAULT_CONNECTORS):
        self._contracts: dict[str, ConnectorContract] = {}
        for contract in contracts:
            self.register(contract)

    def register(self, contract: ConnectorContract) -> None:
        if type(contract) is not ConnectorContract:
            raise HostConnectorError("connector_contract_invalid", "only ConnectorContract is accepted")
        if contract.name in self._contracts:
            raise HostConnectorError("connector_duplicate", "connector is already registered")
        if len(self._contracts) >= 32:
            raise HostConnectorError("connector_limit", "at most 32 declarations are supported")
        self._contracts[contract.name] = contract

    def snapshot(self) -> list[dict[str, Any]]:
        return [self._contracts[name].snapshot() for name in sorted(self._contracts)]

    def request(self, name: str, operation: str) -> dict[str, Any]:
        if not isinstance(name, str) or not isinstance(operation, str):
            raise HostConnectorError("connector_request_invalid", "connector and operation are required")
        contract = self._contracts.get(name)
        if contract is None:
            raise HostConnectorError("connector_unknown", "connector is not declared")
        if operation not in contract.operations:
            raise HostConnectorError("connector_operation_unknown", "operation is not declared")
        external = contract.external or contract.credentials
        request = {"schema": CONTRACT_SCHEMA, "connector": name,
                   "operation": operation, "contract": contract.snapshot()}
        return {
            **request, "request_digest": _digest(request),
            "status": "blocked_human_gate" if external else "blocked_unimplemented",
            "human_gate": {"required": external, "recorded": False,
                           "reason": "外部能力需单独授权及真实适配器" if external else "尚无本地执行器"},
            "permission": {"network": False, "credentials": False, "external": False, "cost_limit": 0},
            "authorization_granted": False, "executed": False,
        }


class FixtureConnector:
    """Bounded JSON responses in memory; simulates results, never service effects.

    The caller reviews preview() and passes that digest with synthetic=True.
    Repeated calls are deterministic reads, not retried external operations.
    Digests are local integrity checks and cannot prove a human's identity.
    """

    def __init__(self, contract: ConnectorContract, responses: dict[str, dict[str, Any]]):
        self._registry = OfflineConnectorRegistry((contract,))
        self._name = contract.name
        if type(responses) is not dict or set(responses) != set(contract.operations):
            raise HostConnectorError("fixture_invalid", "fixture must cover exactly the declared operations")
        for response in responses.values():
            if (type(response) is not dict or set(response) != {"status", "data"}
                    or response["status"] not in ("ok", "failed")):
                raise HostConnectorError("fixture_invalid", "fixture response must contain status and data")
        try:
            encoded = canonical_json(responses)
        except (ValueError, TypeError, RecursionError) as error:
            raise HostConnectorError("fixture_invalid", "fixture must be bounded JSON") from error
        if len(encoded) > 65536 or inspect_sensitive(encoded.decode("utf-8"))["sensitive"]:
            raise HostConnectorError("fixture_invalid", "fixture is too large or contains sensitive material")
        # Keep canonical immutable bytes; neither input nor output mutation may
        # change what the reviewed digest means on a later read.
        self._responses = encoded

    def preview(self, operation: str) -> dict[str, Any]:
        request = self._registry.request(self._name, operation)
        response = json.loads(self._responses)[operation]
        preview = {"schema": CONTRACT_SCHEMA, "request": request,
                   "fixture_sha256": sha256_bytes(self._responses), "response": response,
                   "synthetic": True, "live_verified": False}
        return {**preview, "preview_digest": _digest(preview)}

    def simulate(self, operation: str, *, expected_digest: str, synthetic: bool) -> dict[str, Any]:
        if synthetic is not True:
            raise HostConnectorError("synthetic_confirmation_required", "fixture use must be explicitly synthetic")
        preview = self.preview(operation)
        if expected_digest != preview["preview_digest"]:
            raise HostConnectorError("fixture_drifted", "fixture, contract or operation differs from the preview")
        return {"schema": CONTRACT_SCHEMA, "status": "simulated", "synthetic": True,
                "live_verified": False, "external_executed": False,
                "authorization_granted": False, "preview_digest": expected_digest,
                "request_digest": preview["request"]["request_digest"],
                "response": preview["response"]}


class OfflineHostContract:
    """Observe the local loader only; no managed-host response is manufactured."""

    def __init__(self, project_root: str | Path, *, codex_home: str | Path | None = None,
                 extra_roots: Iterable[str | Path] = (),
                 connectors: OfflineConnectorRegistry | None = None):
        self.project_root = Path(project_root)
        self.codex_home = codex_home
        self.extra_roots = tuple(extra_roots)
        if connectors is not None and type(connectors) is not OfflineConnectorRegistry:
            raise HostConnectorError("connector_registry_invalid", "only an offline declaration registry is accepted")
        self.connectors = connectors if connectors is not None else OfflineConnectorRegistry()

    def inspect(self, *, target_platform: str = "unspecified") -> dict[str, Any]:
        try:
            loader = CodexSkillHost(self.project_root, codex_home=self.codex_home, extra_roots=self.extra_roots)
            loaded = loader.load(target_platform=target_platform)
        except (CodexHostError, OSError, ValueError) as error:
            raise HostConnectorError("host_resource_invalid", str(error)) from error
        return {
            "schema": CONTRACT_SCHEMA, "root": str(loader.project_root),
            "status": "local_adapter_observed_managed_loader_unverified",
            "host": {"mode": "offline_local_adapter", "adapter_loaded": loaded["host_adapter_loaded"],
                     "managed_loader_observed": loaded["codex_managed_loader_observed"],
                     "managed_loader_status": loaded["codex_managed_loader_status"],
                     "resource_source": loaded["resource_source"]},
            "permissions": {"local_operations": list(LOCAL_HOST_OPERATIONS),
                            "external_boundaries": list(EXTERNAL_BOUNDARIES),
                            "authorization_granted": False},
            "boundary": {"provider_used": False, "transport_implemented": False,
                         "credentials_read": False, "sandbox": False,
                         "local_execution": "existing LocalAgentAdapter authorization required"},
            "connectors": self.connectors.snapshot(),
        }

    def check_permission(self, operation: str) -> dict[str, Any]:
        if not isinstance(operation, str) or operation not in LOCAL_HOST_OPERATIONS + EXTERNAL_BOUNDARIES:
            raise HostConnectorError("permission_operation_unknown", "operation is outside the contract")
        external = operation in EXTERNAL_BOUNDARIES
        status = ("blocked_human_gate" if external else "requires_local_authorization"
                  if operation in ("reviewed_local_write", "reviewed_local_test") else "available_read_only")
        return {
            "schema": CONTRACT_SCHEMA, "status": status, "operation": operation,
            "authorization_granted": False, "executed": False,
            "human_gate": {"required": external, "recorded": False},
        }

    def request_connector(self, name: str, operation: str) -> dict[str, Any]:
        return self.connectors.request(name, operation)


__all__ = [
    "CONTRACT_SCHEMA", "ConnectorContract", "DEFAULT_CONNECTORS", "EXTERNAL_BOUNDARIES",
    "FixtureConnector", "HostConnectorError", "LOCAL_HOST_OPERATIONS", "OfflineConnectorRegistry", "OfflineHostContract",
]
