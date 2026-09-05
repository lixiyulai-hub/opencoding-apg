#!/usr/bin/env python3
"""Deterministic APG checkpoint-ledger projection; no persistence is performed."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "1.0"
TARGET_PATH = ".governance/progress/apg-adaptive-git-ledger.json"
_RECORDABLE_STATUSES = {"CHECKPOINT_RECOMMENDED"}


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _event(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("ledger event must be an object")
    event_id = value.get("event_id")
    event_type = value.get("event_type")
    payload = value.get("payload")
    if not isinstance(event_id, str) or not event_id.startswith("ledger-"):
        raise ValueError("ledger event_id must start with ledger-")
    if event_type != "adaptive-git-preview" or not isinstance(payload, Mapping):
        raise ValueError("ledger event shape is invalid")
    status = payload.get("status")
    checkpoint_id = payload.get("checkpoint_id")
    if status not in {"CHECKPOINT_RECOMMENDED", "ALREADY_RECOMMENDED", "FREEZE", "WAIT"}:
        raise ValueError("ledger event status is invalid")
    if status in _RECORDABLE_STATUSES and (not isinstance(checkpoint_id, str) or not checkpoint_id):
        raise ValueError("recordable ledger event requires checkpoint_id")
    return {
        "event_id": event_id,
        "event_type": event_type,
        "payload": {
            "status": status,
            "success_node": payload.get("success_node"),
            "checkpoint_id": checkpoint_id,
            "revert_point": payload.get("revert_point"),
            "resume_condition": payload.get("resume_condition"),
        },
    }


def project(events: Sequence[Mapping[str, Any]], candidate: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(events, (str, bytes)) or not isinstance(events, Sequence):
        raise ValueError("ledger events must be a sequence")
    records = [_event(item) for item in events]
    if len({item["event_id"] for item in records}) != len(records):
        raise ValueError("ledger events must have unique event_id values")
    event = _event(candidate)
    status = event["payload"]["status"]
    checkpoint_id = event["payload"]["checkpoint_id"]
    existing_ids = {item["event_id"] for item in records}
    existing_checkpoints = {
        item["payload"]["checkpoint_id"]
        for item in records
        if item["payload"]["status"] == "CHECKPOINT_RECOMMENDED"
    }
    duplicate = event["event_id"] in existing_ids or (checkpoint_id is not None and checkpoint_id in existing_checkpoints)
    append = status in _RECORDABLE_STATUSES and not duplicate
    projected_records = records + ([event] if append else [])
    last = next((item for item in reversed(projected_records) if item["payload"]["status"] == "CHECKPOINT_RECOMMENDED"), None)
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "ledger_id": "apg-adaptive-git-ledger-v1",
        "target_path": TARGET_PATH,
        "records": projected_records,
    }
    snapshot_digest = hashlib.sha256(canonical_bytes(snapshot)).hexdigest()
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "offline-simulation",
        "target_path": TARGET_PATH,
        "write_action": "PREVIEW_ONLY",
        "status": "APPEND_CANDIDATE" if append else "REUSE_EXISTING" if duplicate else "NO_CHECKPOINT_APPEND",
        "append_candidate": append,
        "deduplicated": duplicate,
        "candidate_event_id": event["event_id"],
        "last_checkpoint": None if last is None else {
            "event_id": last["event_id"],
            "checkpoint_id": last["payload"]["checkpoint_id"],
            "success_node": last["payload"]["success_node"],
        },
        "snapshot": snapshot,
        "snapshot_digest": snapshot_digest,
        "replay_digest": "",
        "execution_performed": False,
        "external_actions": {"git": False, "network": False, "remote": False, "provider": False, "host": False, "runtime": False, "deployment": False, "publication": False},
    }
    result["replay_digest"] = hashlib.sha256(canonical_bytes({key: value for key, value in result.items() if key != "replay_digest"})).hexdigest()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="APG offline checkpoint ledger projection")
    parser.add_argument("--events", default="[]", help="JSON array of existing events")
    parser.add_argument("--candidate", required=True, help="JSON checkpoint event")
    args = parser.parse_args(argv)
    sys.stdout.buffer.write(canonical_bytes(project(json.loads(args.events), json.loads(args.candidate))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
