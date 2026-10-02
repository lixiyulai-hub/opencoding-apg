#!/usr/bin/env python3
"""Validate and locally exercise the OpenCoding project skill resource.

This is deliberately a project-side verifier.  It can prove that a standard
`.agents/skills` resource is well formed and that the declared Python entrypoint
can be imported.  It cannot prove that a remote or managed Agent host loaded
the resource; the current Codex tool API exposes no such observation hook.
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import re
import sys
from typing import Any


class SkillVerificationError(ValueError):
    pass


_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _read_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        raise SkillVerificationError("SKILL.md must start with YAML frontmatter")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise SkillVerificationError("SKILL.md frontmatter has no closing delimiter") from exc
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise SkillVerificationError("frontmatter line is not key: value")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if not _KEY.fullmatch(key) or not value:
            raise SkillVerificationError("frontmatter key/value is invalid")
        if value[0:1] in {"'", '"'} and value[-1:] == value[0]:
            value = value[1:-1]
        values[key] = value
    for required in ("name", "description"):
        if not values.get(required):
            raise SkillVerificationError(f"frontmatter missing {required}")
    return values


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillVerificationError("skill.json is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SkillVerificationError("skill.json root must be an object")
    required = {
        "schema_version": "1.0",
        "name": "opencoding",
        "entrypoint": "opencoding.agent_adapter:LocalAgentAdapter",
        "host_independent": True,
        "model_available": False,
        "external_actions": False,
        "requires_user_authorization_for_local_write": True,
        "sandbox": False,
        "host_root_contract": "$CODEX_HOME/skills/<name>/SKILL.md",
    }
    for key, expected in required.items():
        if value.get(key) != expected:
            raise SkillVerificationError(f"skill.json {key} does not match the OpenCoding contract")
    actions = value.get("actions")
    if actions != ["write_text", "python_module", "node_script"]:
        raise SkillVerificationError("skill.json actions do not match the adapter contract")
    if value.get("capability_matrix") != "opencoding.host_capabilities:build_capability_matrix":
        raise SkillVerificationError("skill.json capability matrix entrypoint is missing")
    if value.get("adapter_check") != "scripts/check_agent_adapter.py":
        raise SkillVerificationError("skill.json adapter check entrypoint is missing")
    if value.get("execution_observation") != "LocalAgentAdapter.execute:capability_observation":
        raise SkillVerificationError("skill.json execution observation contract is missing")
    if value.get("non_template_validation") != "scripts/run_stage19_non_template.py":
        raise SkillVerificationError("skill.json non-template validation entrypoint is missing")
    if value.get("toolchain_probe") != "opencoding.toolchain_probe:probe_target_toolchain":
        raise SkillVerificationError("skill.json toolchain probe entrypoint is missing")
    if value.get("multi_project_validation") != "scripts/run_stage20_multi_project.py":
        raise SkillVerificationError("skill.json multi-project validation entrypoint is missing")
    if value.get("capability_contract") != "opencoding.capability_contract:build_capability_contract":
        raise SkillVerificationError("skill.json capability contract entrypoint is missing")
    if value.get("stage21_project_matrix") != "scripts/run_stage21_project_matrix.py":
        raise SkillVerificationError("skill.json Stage21 project matrix entrypoint is missing")
    if value.get("acceptance_report") != "opencoding.acceptance_report:build_acceptance_report":
        raise SkillVerificationError("skill.json acceptance report entrypoint is missing")
    if value.get("acceptance_cli") != "scripts/report_acceptance.py":
        raise SkillVerificationError("skill.json acceptance CLI entrypoint is missing")
    if value.get("acceptance_state") != "opencoding.acceptance_state:resume_acceptance_state":
        raise SkillVerificationError("skill.json acceptance state entrypoint is missing")
    if value.get("stage23_state_recovery") != "scripts/run_stage23_state_recovery.py":
        raise SkillVerificationError("skill.json Stage23 state recovery entrypoint is missing")
    if value.get("product_loop_bridge") != "opencoding.agent_tasks:LocalAgentTaskExecutor":
        raise SkillVerificationError("skill.json product loop bridge entrypoint is missing")
    if value.get("product_loop_preview") != "opencoding.agent_tasks:preview_agent_tasks":
        raise SkillVerificationError("skill.json product loop preview entrypoint is missing")
    if value.get("target_adapters") != "opencoding.target_adapters:get_target_adapter":
        raise SkillVerificationError("skill.json target adapter entrypoint is missing")
    if value.get("target_status_report") != "opencoding.target_adapters:target_adapter_status":
        raise SkillVerificationError("skill.json target status report entrypoint is missing")
    if not isinstance(value.get("planning"), str) or not value["planning"]:
        raise SkillVerificationError("skill.json planning entrypoint is missing")
    return value


def discover_skill(root: Path) -> tuple[Path, str]:
    candidates = (
        root / ".agents" / "skills" / "opencoding",
        root / "skills" / "opencoding",
    )
    for directory in candidates:
        skill = directory / "SKILL.md"
        manifest = directory / "skill.json"
        if skill.is_file() and manifest.is_file():
            return directory, "project_standard" if ".agents" in directory.parts else "source_fallback"
    raise SkillVerificationError("no complete .agents/skills/opencoding or skills/opencoding resource")


def verify(root: Path, *, exercise: bool = False) -> dict[str, Any]:
    root = root.resolve()
    directory, discovery = discover_skill(root)
    frontmatter = _read_frontmatter(directory / "SKILL.md")
    manifest = _read_manifest(directory / "skill.json")
    entrypoint = str(manifest["entrypoint"])
    module_name, separator, attr_name = entrypoint.partition(":")
    if not separator:
        raise SkillVerificationError("entrypoint must use module:attribute form")
    old_path = list(sys.path)
    loaded = False
    capabilities: dict[str, Any] | None = None
    try:
        sys.path.insert(0, str(root))
        module = importlib.import_module(module_name)
        entrypoint_object = getattr(module, attr_name)
        loaded = callable(entrypoint_object)
        if exercise:
            adapter = entrypoint_object(root)
            capabilities = adapter.capabilities(target_platform="unspecified")
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        raise SkillVerificationError(f"entrypoint import/exercise failed: {exc}") from exc
    finally:
        sys.path[:] = old_path
    return {
        "root": str(root),
        "resource": str(directory.relative_to(root)),
        "format_valid": True,
        "project_discovered": True,
        "discovery_mode": discovery,
        "project_loader_exercised": bool(exercise and loaded),
        "entrypoint_callable": loaded,
        "capabilities": capabilities,
        # No project-skill loader observation is available through this
        # verifier.  `None` is deliberate: false would claim the host tried
        # and rejected the resource.
        "host_loaded": None,
        "host_load_status": "unverified_host_api_has_no_project_skill_loader",
        "status": "format_valid_project_discovered_host_unverified",
        "frontmatter": {"name": frontmatter["name"], "description": frontmatter["description"]},
        "manifest": {"name": manifest["name"], "entrypoint": entrypoint},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="absolute project root")
    parser.add_argument("--exercise", action="store_true", help="import entrypoint and read capabilities")
    args = parser.parse_args(argv)
    if not args.root.is_absolute():
        parser.error("--root must be absolute")
    try:
        report = verify(args.root, exercise=args.exercise)
    except (OSError, SkillVerificationError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
