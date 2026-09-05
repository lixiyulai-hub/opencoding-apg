from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

BLOCKER_FIELDS = (("release_approval", "missing_release_approval"), ("rollback_evidence", "missing_rollback_evidence"))
PROHIBITED_REQUIREMENTS = (
    ("provider_required", "provider_required"),
    ("network_required", "network_required"),
    ("credentials_required", "credentials_required"),
    ("real_data_required", "real_data_required"),
    ("runtime_requested", "runtime_requested"),
    ("deployment_requested", "deployment_requested"),
    ("publication_requested", "publication_requested"),
    ("pilot_requested", "pilot_requested"),
)


def evaluate_preview(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate local evidence only; never contacts providers or deployment targets."""
    blockers: list[str] = []
    for field, code in BLOCKER_FIELDS:
        if evidence.get(field) is not True:
            blockers.append(code)
    for field, code in PROHIBITED_REQUIREMENTS:
        if evidence.get(field) is True:
            blockers.append(code)
    return {
        "schema_version": "1.0",
        "status": "ready-for-preview" if not blockers else "BLOCK",
        "blocker_codes": blockers,
        "preview_only": True,
        "release_action_executed": False,
        "publication_action_executed": False,
        "deployment_action_executed": False,
        "external_actions": [],
        "evidence": {field: evidence.get(field) is True for field, _ in BLOCKER_FIELDS + PROHIBITED_REQUIREMENTS},
    }


def rollback_copy(pre_state: Path, target: Path) -> str:
    """Restore a disposable target from a pre-state copy and return its text."""
    pre_state = Path(pre_state)
    target = Path(target)
    if not pre_state.is_file():
        raise FileNotFoundError(f"pre-state not found: {pre_state}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pre_state, target)
    return target.read_text(encoding="utf-8")


def _load_input(path: str | None) -> Mapping[str, Any]:
    raw = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline APG deployment preview simulator")
    parser.add_argument("--input", help="local evidence JSON file; stdin when omitted")
    parser.add_argument("--rollback-source", help="pre-state file for disposable rollback")
    parser.add_argument("--rollback-target", help="target file for disposable rollback")
    args = parser.parse_args(argv)
    if bool(args.rollback_source) != bool(args.rollback_target):
        parser.error("--rollback-source and --rollback-target must be supplied together")
    if args.rollback_source:
        restored = rollback_copy(Path(args.rollback_source), Path(args.rollback_target))
        print(json.dumps({"status": "rollback-restored", "restored_text": restored}, ensure_ascii=False, sort_keys=True))
        return 0
    try:
        result = evaluate_preview(_load_input(args.input))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "BLOCK", "blocker_codes": ["invalid_local_evidence"], "error": str(error)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "ready-for-preview" else 3


if __name__ == "__main__":
    raise SystemExit(main())
