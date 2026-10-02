"""Build a conservative, reproducible cross-platform capability contract."""

from __future__ import annotations

from typing import Any

from .host_capabilities import build_capability_matrix
from .target_adapters import target_adapter_status
from .toolchain_probe import probe_target_toolchain


CAPABILITY_CONTRACT_SCHEMA_VERSION = "opencoding-capability-contract-v1"
_TARGET_PROFILES = {
    "cli": "python-cli-runtime",
    "web": "node-web-runtime",
    "windows": "windows-dotnet-runtime",
    "macos": "macos-swift-runtime",
}


def build_capability_contract() -> dict[str, Any]:
    """Probe fixed profiles and publish facts without broadening their scope."""
    targets: dict[str, Any] = {}
    host: dict[str, Any] | None = None
    for target, profile in _TARGET_PROFILES.items():
        probe = probe_target_toolchain(profile, target_platform=target)
        matrix = build_capability_matrix(target_platform=target, toolchain_observation=probe)
        host = host or matrix["host"]
        targets[target] = {
            "target": matrix["target"],
            "toolchain": matrix["toolchain"],
            "toolchain_status": matrix["toolchain"]["status"],
            "target_toolchain_verified": matrix["compatibility"]["target_toolchain_verified"],
            "target_execution_status": "unverified",
            "target_execution_verified": False,
            "target_execution_reason": "No target-specific executor, browser, SDK or deployment was exercised in this offline run.",
            "probe": probe,
        }
    assert host is not None
    target_adapters = {
        target: target_adapter_status(target, toolchain_observation=targets[target]["probe"])
        for target in ("windows", "macos", "web")
    }
    return {
        "schema_version": CAPABILITY_CONTRACT_SCHEMA_VERSION,
        "host": host,
        "targets": targets,
        "target_adapters": target_adapters,
        "actions": {
            "write_text": {"supported": True, "effect": "local-files-under-reviewed-root", "rollback": "transaction-layer-only"},
            "python_module": {"supported": True, "effect": "arbitrary-same-user-python-side-effects", "rollback": "not-automatic", "network": "not-controlled-by-adapter"},
        },
        "model": {"available": False, "provider": None, "model": None},
        "external": {"adapter_calls": False, "network_requested": False, "cost_limit": 0},
        "sandbox": {"enabled": False, "kind": None},
        "process_boundary": "same-user-subprocess",
        "managed_loader": {"observed": None, "status": "unverified-without-host-response"},
        "acceptance": {
            "status_vocabulary": ["observed", "unverified", "blocked"],
            "human_gate_policy": "provider, external service, deployment and real user confirmation remain blocked until explicitly approved",
        },
        "claims": {
            "host_runtime_observed": True,
            "target_execution_observed": False,
            "browser_execution_observed": False,
            "deployment_observed": False,
            "provider_or_model_observed": False,
            "scope": "fixed local version probes and structured offline adapter actions only",
        },
    }


__all__ = ["CAPABILITY_CONTRACT_SCHEMA_VERSION", "build_capability_contract"]
