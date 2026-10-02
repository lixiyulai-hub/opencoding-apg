#!/usr/bin/env python3
"""Run a minimal, APG-independent local Agent adapter check.

The default mode only reports the host/target capability matrix and imports the
local adapter.  ``--probe-local-write`` opts into one synthetic write in a new
temporary root; it requires ``--confirm-synthetic`` and never calls a model,
network, shell, cargo, browser, or target-specific toolchain.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import uuid

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from opencoding.agent_adapter import LocalAgentAdapter
from opencoding.host_capabilities import normalize_target_platform
from opencoding.toolchain_probe import TOOLCHAIN_PROFILES, probe_target_toolchain


def _check(project_root: Path, target_platform: str, *, probe: bool, toolchain_profile: str | None) -> dict:
    adapter = LocalAgentAdapter(project_root)
    toolchain_observation = None
    if toolchain_profile:
        toolchain_observation = probe_target_toolchain(toolchain_profile, target_platform=target_platform)
    capabilities = adapter.capabilities(
        target_platform=target_platform,
        toolchain_observation=toolchain_observation,
    )
    report = {
        "schema": "opencoding-adapter-check-v1",
        "project_root": str(project_root),
        "target": normalize_target_platform(target_platform),
        "capabilities": capabilities,
        "probe": {"requested": probe, "status": "not_run", "synthetic_confirmation": False},
        "toolchain_probe": toolchain_observation,
        "managed_loader_observed": None,
        "external_actions_executed": False,
        "status": "capability_reported",
    }
    if not probe:
        return report

    with tempfile.TemporaryDirectory(prefix="opencoding-adapter-check-") as directory:
        probe_root = Path(directory).resolve()
        probe_adapter = LocalAgentAdapter(probe_root)
        action = {
            "type": "write_text",
            "path": "adapter-check/probe.txt",
            "content": "opencoding adapter probe\n",
        }
        authorization = probe_adapter.authorization_for(
            action, scope="synthetic-adapter-check", confirmation_id="synthetic-adapter-check-" + uuid.uuid4().hex
        )
        result = probe_adapter.execute(action, authorization=authorization, run_id="adapter-check")
        output = probe_root / action["path"]
        report["probe"] = {
            "requested": True,
            "status": result.get("status"),
            "synthetic_confirmation": True,
            "root": str(probe_root),
            "file_written": output.is_file(),
            "content_matches": output.read_text(encoding="utf-8") == action["content"],
            "removed_with_temporary_root": True,
            "authorization_binding": result.get("authorization_binding"),
        }
        report["status"] = "capability_and_local_write_probe_passed"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--target-platform", default="unspecified")
    parser.add_argument("--probe-local-write", action="store_true")
    parser.add_argument("--confirm-synthetic", action="store_true")
    parser.add_argument("--probe-toolchain", metavar="PROFILE", choices=sorted(TOOLCHAIN_PROFILES), help="run one fixed local no-shell toolchain profile")
    args = parser.parse_args(argv)
    root = args.project_root
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        parser.error("--project-root must be an existing absolute unlinked directory")
    if args.probe_local_write and not args.confirm_synthetic:
        parser.error("--probe-local-write requires --confirm-synthetic")
    try:
        report = _check(root, args.target_platform, probe=args.probe_local_write, toolchain_profile=args.probe_toolchain)
    except (OSError, ValueError) as exc:
        print(json.dumps({"schema": "opencoding-adapter-check-v1", "status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
