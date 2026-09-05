#!/usr/bin/env python3
"""APG-only offline bridge from PRG progress to the adaptive Git advisor."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any, Mapping

try:
    from scripts.apg_adaptive_git_preview import simulate as simulate_git
    from scripts.apg_beginner_executor_preview import simulate as simulate_prg
    from scripts.apg_adaptive_git_ledger_preview import project as project_ledger
except ModuleNotFoundError:  # direct ``python scripts/...py`` invocation
    from apg_adaptive_git_preview import simulate as simulate_git
    from apg_beginner_executor_preview import simulate as simulate_prg
    from apg_adaptive_git_ledger_preview import project as project_ledger

SCHEMA_VERSION = "1.0"


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _next_action(status: str) -> str:
    return {
        "CHECKPOINT_RECOMMENDED": "record-checkpoint-preview",
        "ALREADY_RECOMMENDED": "continue-from-existing-checkpoint",
        "FREEZE": "freeze-and-repair-before-new-checkpoint",
        "WAIT": "continue-until-success-node-is-verified",
    }.get(status, "requeue-after-controller-state-review")


def _ledger_event(git_result: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "status": git_result.get("status"),
        "success_node": git_result.get("success_node"),
        "checkpoint_id": (git_result.get("checkpoint") or {}).get("checkpoint_id"),
        "revert_point": git_result.get("revert_point"),
        "resume_condition": git_result.get("resume_condition"),
    }
    event_id = "ledger-" + hashlib.sha256(canonical_bytes(payload)).hexdigest()[:16]
    return {"event_id": event_id, "event_type": "adaptive-git-preview", "payload": payload}


def simulate(state: Mapping[str, Any], *, request: str = "APG 离线工作项") -> dict[str, Any]:
    if not isinstance(state, Mapping):
        raise ValueError("state must be an object")
    if not isinstance(request, str) or not request.strip():
        raise ValueError("request must be non-empty text")
    failure = state.get("failure")
    failure_text = failure if isinstance(failure, str) and failure.strip() else None
    repeated = state.get("repeated") is True
    prg = simulate_prg(request, failure=failure_text)
    git_result = simulate_git(state, failure=failure_text, repeated=repeated)
    status = str(git_result["status"])
    ledger = _ledger_event(git_result)
    ledger_events = state.get("checkpoint_ledger_events", [])
    checkpoint_ledger = project_ledger(ledger_events, ledger)
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "offline-simulation",
        "controller": {
            "id": "apg.adaptive-git-controller",
            "input_valid": True,
            "source": "prg-progress-state",
            "write_policy": "PREVIEW_ONLY",
        },
        "prg": {
            "route": prg["route"],
            "loop_states": prg["loop_states"],
            "terminal_state": prg["terminal_state"],
            "resume_condition": prg["resume_condition"],
            "human_gate": prg["human_gate"],
        },
        "git_checkpoint": git_result,
        "ledger_event": ledger,
        "checkpoint_ledger": checkpoint_ledger,
        "next_action": {
            "id": _next_action(status),
            "automatic": True,
            "status": status,
            "resume_condition": git_result["resume_condition"],
        },
        "replay_digest": "",
        "execution_performed": False,
        "external_actions": {
            "git": False,
            "network": False,
            "remote": False,
            "provider": False,
            "host": False,
            "runtime": False,
            "deployment": False,
            "publication": False,
        },
    }
    result["replay_digest"] = hashlib.sha256(canonical_bytes({k: v for k, v in result.items() if k != "replay_digest"})).hexdigest()
    return result


def replay_digest(state: Mapping[str, Any], *, request: str = "APG 离线工作项") -> str:
    return simulate(state, request=request)["replay_digest"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="APG offline adaptive Git controller bridge")
    parser.add_argument("--state", required=True, help="JSON object containing progress state")
    parser.add_argument("--request", default="APG 离线工作项")
    args = parser.parse_args(argv)
    state = json.loads(args.state)
    sys.stdout.buffer.write(canonical_bytes(simulate(state, request=args.request)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
