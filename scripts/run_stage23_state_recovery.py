#!/usr/bin/env python3
"""Exercise acceptance-state restart, blocked gates and repeated resume."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from opencoding.acceptance_report import build_acceptance_report
from opencoding.acceptance_state import (
    AcceptanceStateError,
    initialize_acceptance_state,
    load_acceptance_state,
    require_observed,
    resume_acceptance_state,
)
from opencoding.agent_adapter import LocalAgentAdapter
from scripts.run_stage21_project_matrix import SCENARIOS, THIRD_SCENARIO, _run_scenario


def _statuses(state: dict) -> dict[str, str]:
    return {key: value["status"] for key, value in sorted(state["checks"].items())}


def run() -> dict:
    report = build_acceptance_report()
    with tempfile.TemporaryDirectory(prefix="opencoding-stage23-state-") as directory:
        root = Path(directory).resolve()
        state = initialize_acceptance_state(root, report, state_id="stage23-acceptance")
        initial_statuses = _statuses(state)
        adapter = LocalAgentAdapter(root)

        observed_action = {"type": "write_text", "path": "observed.txt", "content": "local\n"}
        require_observed(state, "local_structured_actions")
        observed_auth = adapter.authorization_for(observed_action, scope="stage23-observed")
        observed_result = adapter.execute(observed_action, authorization=observed_auth, run_id="stage23-observed")

        blocked_root = root / "blocked"
        blocked_root.mkdir()
        blocked = {"status": "not_run", "code": None, "file_exists": False, "claim_dir_exists": False}
        try:
            require_observed(state, "provider_or_model")
        except AcceptanceStateError as error:
            blocked = {"status": "blocked_before_action", "code": error.code, "file_exists": (blocked_root / "provider.txt").exists(), "claim_dir_exists": (blocked_root / ".opencoding" / "authorizations").exists()}

        unverified_root = root / "unverified"
        unverified_root.mkdir()
        unverified = {"status": "not_run", "code": None, "file_exists": False}
        try:
            require_observed(state, "windows_target_execution")
        except AcceptanceStateError as error:
            unverified = {"status": "unverified_before_action", "code": error.code, "file_exists": (unverified_root / "windows.txt").exists()}

        restarted = load_acceptance_state(root, "stage23-acceptance")
        resumed_once = resume_acceptance_state(root, "stage23-acceptance", report=report)
        resumed_twice = resume_acceptance_state(root, "stage23-acceptance", report=report)
        final_statuses = _statuses(resumed_twice)

        project_results = [_run_scenario(spec, index) for index, spec in enumerate(list(SCENARIOS) + [THIRD_SCENARIO], 1)]
        return {
            "schema": "opencoding-stage23-state-recovery-v1",
            "state": {
                "state_id": state["state_id"],
                "initial_statuses": initial_statuses,
                "restarted_statuses": _statuses(restarted),
                "resumed_once_statuses": _statuses(resumed_once),
                "resumed_twice_statuses": final_statuses,
                "status_stable_across_restart_and_resume": initial_statuses == _statuses(restarted) == _statuses(resumed_once) == final_statuses,
                "resume_count": resumed_twice["resume_count"],
            },
            "observed_action": {"status": observed_result.get("status"), "file_exists": (root / "observed.txt").exists(), "claim_dir_exists": (root / ".opencoding" / "authorizations").exists()},
            "blocked_gate": blocked,
            "unverified_gate": unverified,
            "projects": project_results,
            "summary": {
                "state_stable": initial_statuses == _statuses(restarted) == _statuses(resumed_once) == final_statuses,
                "observed_action_succeeded": observed_result.get("status") == "succeeded",
                "blocked_no_file": blocked["status"] == "blocked_before_action" and not blocked["file_exists"],
                "blocked_no_claim": blocked["status"] == "blocked_before_action" and not blocked["claim_dir_exists"],
                "unverified_no_file": unverified["status"] == "unverified_before_action" and not unverified["file_exists"],
                "projects_fail_repair_rollback": all(p["failure_repair"]["failure_observed"] and p["failure_repair"]["repair_passed"] and p["rollback"]["rollback_status"] == "rolled_back" and not p["rollback"]["residual_paths"] for p in project_results),
            },
            "boundary": "Restart/recovery evidence is Linux-local and synthetic; blocked gates never call the adapter.",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.is_absolute():
        parser.error("--output must be absolute")
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "summary": report["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
