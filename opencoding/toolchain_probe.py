"""Explicit, bounded local probes for target-toolchain observability.

The adapter never runs these probes implicitly.  A caller must choose one of
the fixed profiles.  ``observed`` means every profile command was found and
returned zero within the local timeout, with the stated scope; it never means
that a complete target application toolchain, browser, SDK, or deployment path
was validated.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from typing import Any

from .host_capabilities import normalize_target_platform


TOOLCHAIN_PROFILES: dict[str, dict[str, Any]] = {
    "python-cli-runtime": {
        "target": "cli",
        "scope": "Python interpreter runtime only; CLI packaging/framework not verified",
        "commands": [[sys.executable, "--version"]],
        "host_families": {"linux", "macos", "windows", "posix-other"},
    },
    "node-web-runtime": {
        "target": "web",
        "scope": "Node/npm runtime only; browser, bundler, framework, and deployment not verified",
        "commands": [["node", "--version"], ["npm", "--version"]],
        "host_families": {"linux", "macos", "windows", "posix-other"},
    },
    "rust-cli-runtime": {
        "target": "cli",
        "scope": "rustc/cargo runtime only; project dependencies and packaging not verified",
        "commands": [["rustc", "--version"], ["cargo", "--version"]],
        "host_families": {"linux", "macos", "windows", "posix-other"},
    },
    "windows-dotnet-runtime": {
        "target": "windows",
        "scope": ".NET runtime only; Windows SDK, GUI, signing, and packaging not verified",
        "commands": [["dotnet", "--version"]],
        "host_families": {"windows"},
    },
    "macos-swift-runtime": {
        "target": "macos",
        "scope": "Swift runtime only; Xcode, signing, packaging, browser and target execution not verified",
        "commands": [["swift", "--version"]],
        "host_families": {"macos"},
    },
}


def _host_family() -> str:
    if os.name == "nt" or sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("darwin"):
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if os.name == "posix":
        return "posix-other"
    return "unknown"


def _command_text(output: bytes | str | None) -> str:
    text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output or "")
    return text.replace("\x00", "")[:512].strip()


def probe_target_toolchain(profile: str, *, target_platform: str | None = None, timeout_seconds: float = 3.0) -> dict[str, Any]:
    """Probe one fixed local profile without shell or network access."""
    if profile not in TOOLCHAIN_PROFILES:
        raise ValueError(f"unknown toolchain profile: {profile}")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 10:
        raise ValueError("timeout_seconds must be between 0 and 10")
    spec = TOOLCHAIN_PROFILES[profile]
    expected = normalize_target_platform(spec["target"])
    requested = normalize_target_platform(target_platform or spec["target"])
    if requested["normalized"] != expected["normalized"]:
        raise ValueError("toolchain profile target does not match requested target")
    host = _host_family()
    records: list[dict[str, Any]] = []
    host_ok = host in spec["host_families"]
    all_ok = host_ok
    if not host_ok:
        return {
            "schema_version": "opencoding-toolchain-probe-v1",
            "verified_by": "opencoding.toolchain_probe",
            "profile": profile,
            "target": expected,
            "host": {"family": host, "os": os.name, "platform": sys.platform, "python": platform.python_version()},
            "status": "unverified",
            "observed": False,
            "scope": spec["scope"],
            "commands": [],
            "reason": "host family is incompatible with this target profile",
            "network_requested": False,
            "shell": False,
        }
    for command in spec["commands"]:
        executable = command[0]
        resolved = executable if os.path.isabs(executable) else shutil.which(executable)
        record: dict[str, Any] = {"command": list(command), "resolved": resolved, "returncode": None, "status": "unavailable"}
        if not resolved:
            all_ok = False
            records.append(record)
            continue
        try:
            completed = subprocess.run(
                list(command), shell=False, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=float(timeout_seconds), check=False,
                env={"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"},
            )
            record["resolved"] = resolved
            record["returncode"] = completed.returncode
            record["stdout"] = _command_text(completed.stdout)
            record["stderr"] = _command_text(completed.stderr)
            record["status"] = "passed" if completed.returncode == 0 else "failed"
            all_ok = all_ok and completed.returncode == 0
        except subprocess.TimeoutExpired as error:
            all_ok = False
            record["status"] = "timeout"
            record["stdout"] = _command_text(error.stdout)
            record["stderr"] = _command_text(error.stderr)
        except OSError as error:
            all_ok = False
            record["status"] = "error"
            record["stderr"] = _command_text(str(error))
        records.append(record)
    return {
        "schema_version": "opencoding-toolchain-probe-v1",
        "verified_by": "opencoding.toolchain_probe",
        "profile": profile,
        "target": expected,
        "host": {"family": host, "os": os.name, "platform": sys.platform, "python": platform.python_version()},
        "status": "observed" if all_ok else "unverified",
        "observed": bool(all_ok),
        "scope": spec["scope"],
        "commands": records,
        "reason": "all fixed profile commands passed" if all_ok else "one or more profile commands unavailable or failed",
        "network_requested": False,
        "shell": False,
    }


__all__ = ["TOOLCHAIN_PROFILES", "probe_target_toolchain"]
