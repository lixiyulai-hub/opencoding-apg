"""Cross-platform host/target capability facts for the local Agent adapter.

This module is deliberately independent of APG, Rust, frontend, cargo, model
providers, and managed-host APIs.  It reports what the local structured
adapter can do and keeps target-toolchain verification explicitly unknown.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import sys
from typing import Any


CAPABILITY_MATRIX_SCHEMA_VERSION = "1.1"

_TARGET_ALIASES = {
    "win32": "windows",
    "win": "windows",
    "darwin": "macos",
    "mac": "macos",
    "web-browser": "web",
    "browser": "web",
    "miniapp": "mini-program",
    "mini-program": "mini-program",
    "miniprogram": "mini-program",
}
_KNOWN_TARGETS = frozenset({
    "android", "cli", "ios", "linux", "macos", "mini-program", "web", "windows",
})


def normalize_target_platform(value: str | None) -> dict[str, Any]:
    """Normalize a planning label without claiming target execution support."""
    if value is None or not str(value).strip():
        return {"requested": value, "normalized": "unspecified", "recognized": False}
    requested = str(value).strip()
    compact = re.sub(r"\s+", "-", requested.casefold())
    normalized = _TARGET_ALIASES.get(compact, compact)
    return {
        "requested": requested,
        "normalized": normalized,
        "recognized": normalized in _KNOWN_TARGETS,
    }


def _host_family(host_os: str, host_platform: str) -> str:
    if host_os == "nt" or host_platform.startswith("win"):
        return "windows"
    if host_platform.startswith("darwin"):
        return "macos"
    if host_platform.startswith("linux"):
        return "linux"
    if host_os == "posix":
        return "posix-other"
    return "unknown"


def build_capability_matrix(
    *,
    target_platform: str | None = None,
    host_os: str | None = None,
    host_platform: str | None = None,
    python_version: str | None = None,
    python_executable: str | None = None,
    toolchain_observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return host facts, target intent, and conservative adapter boundaries.

    ``host_os`` and related arguments are injectable only for deterministic
    reporting tests.  Injected values are facts supplied by the caller; they
    are never evidence that the corresponding operating system was executed.
    """
    actual_os = host_os if host_os is not None else os.name
    actual_platform = host_platform if host_platform is not None else sys.platform
    target = normalize_target_platform(target_platform)
    executable = python_executable or shutil.which(sys.executable) or sys.executable
    toolchain = {
        "status": "unverified",
        "commands": [],
        "reason": "target-specific toolchains require a separate verified adapter",
    }
    target_toolchain_verified = False
    if toolchain_observation is not None:
        if not isinstance(toolchain_observation, dict):
            raise ValueError("toolchain_observation must be an object")
        if toolchain_observation.get("schema_version") != "opencoding-toolchain-probe-v1":
            raise ValueError("toolchain observation schema is invalid")
        if toolchain_observation.get("verified_by") != "opencoding.toolchain_probe":
            raise ValueError("toolchain observation provenance is invalid")
        if toolchain_observation.get("target", {}).get("normalized") != target["normalized"]:
            raise ValueError("toolchain observation target does not match capability target")
        if toolchain_observation.get("status") not in {"observed", "unverified"}:
            raise ValueError("toolchain observation status is invalid")
        commands = toolchain_observation.get("commands", [])
        if not isinstance(commands, list):
            raise ValueError("toolchain observation commands are invalid")
        if toolchain_observation.get("status") == "observed":
            if toolchain_observation.get("observed") is not True or not commands:
                raise ValueError("observed toolchain requires successful command evidence")
            if any(item.get("status") != "passed" for item in commands if isinstance(item, dict)):
                raise ValueError("observed toolchain contains a non-passing command")
            if any(not isinstance(item, dict) for item in commands):
                raise ValueError("observed toolchain command evidence is invalid")
            if toolchain_observation.get("network_requested") is not False or toolchain_observation.get("shell") is not False:
                raise ValueError("observed toolchain must be no-network and no-shell")
        toolchain = {
            "status": toolchain_observation["status"],
            "profile": toolchain_observation.get("profile"),
            "scope": toolchain_observation.get("scope"),
            "commands": commands,
            "reason": toolchain_observation.get("reason"),
            "network_requested": toolchain_observation.get("network_requested", False),
            "shell": toolchain_observation.get("shell", False),
        }
        target_toolchain_verified = toolchain["status"] == "observed"
    matrix = {
        "schema_version": CAPABILITY_MATRIX_SCHEMA_VERSION,
        "host": {
            "os": actual_os,
            "platform": actual_platform,
            "family": _host_family(actual_os, actual_platform),
            "python": python_version or platform.python_version(),
        },
        "target": target,
        "actions": {
            "write_text": {
                "supported": True,
                "effect": "local-files-under-reviewed-root",
                "rollback": "transaction-layer-only",
            },
            "python_module": {
                "supported": True,
                "effect": "arbitrary-same-user-python-side-effects",
                "rollback": "not-automatic",
                "network": "not-controlled-by-adapter",
            },
            "node_script": {
                "supported": True,
                "effect": "arbitrary-same-user-node-side-effects",
                "rollback": "not-automatic",
                "network": "not-controlled-by-adapter",
            },
        },
        "model": {
            "available": False,
            "provider": None,
            "model": None,
            "evidence": "no-model-in-local-adapter",
        },
        "external": {
            "adapter_calls": False,
            "cost_limit": 0,
            "network": "not-requested-by-adapter",
            "python_module_side_effects": "not-controlled",
        },
        "sandbox": {"enabled": False, "kind": None},
        "process_boundary": "same-user-subprocess",
        "toolchain": toolchain,
        "guards": {
            "path_policy": "executor-reviewed-root-and-link-checks",
            "reparse_policy": "executor-platform-checks",
            "timeout_enforced": True,
        },
        "compatibility": {
            "host_target_independent": True,
            "planning_supported": True,
            "structured_local_actions_supported": True,
            "target_toolchain_verified": target_toolchain_verified,
            "status": "host-independent-target-adapter-required",
        },
        "python": {"executable": executable, "available": bool(executable)},
        "managed_loader": {"observed": None, "status": "unverified-without-host-response"},
    }
    return matrix


__all__ = ["CAPABILITY_MATRIX_SCHEMA_VERSION", "build_capability_matrix", "normalize_target_platform"]
