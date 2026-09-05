#!/usr/bin/env python3
"""APG-only local Ledger persistence adapter with atomic, idempotent writes."""
from __future__ import annotations
import argparse, hashlib, json, os, tempfile
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "1.0"
TARGET_PATH = ".governance/progress/apg-adaptive-git-ledger.json"


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_path(path: Path) -> str | None:
    return sha256_bytes(path.read_bytes()) if path.is_file() else None

def _validate_snapshot(snapshot: Mapping[str, Any]) -> None:
    if not isinstance(snapshot, Mapping) or snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_version must be 1.0")
    if snapshot.get("target_path") != TARGET_PATH:
        raise ValueError("target_path mismatch")
    records = snapshot.get("records")
    if not isinstance(records, list):
        raise ValueError("records must be a list")
    ids: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping) or not isinstance(record.get("event_id"), str):
            raise ValueError("invalid ledger record")
        if record["event_id"] in ids:
            raise ValueError("duplicate event_id")
        ids.add(record["event_id"])

def build_plan(root: Path, snapshot: Mapping[str, Any], *, source_snapshot_digest: str | None = None, idempotency_key: str | None = None, preimage_sha256: str | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    _validate_snapshot(snapshot)
    expected_digest = hashlib.sha256(canonical_bytes(snapshot)).hexdigest()
    if source_snapshot_digest is not None and source_snapshot_digest != expected_digest:
        return {"status":"FREEZE", "freeze_reason":"snapshot_digest_mismatch", "write_action":"PREVIEW_ONLY", "external_actions": {"filesystem_write":False}}
    target = (root / TARGET_PATH).resolve()
    if root not in target.parents:
        return {"status":"FREEZE", "freeze_reason":"target_outside_root", "write_action":"PREVIEW_ONLY", "external_actions": {"filesystem_write":False}}
    current = target.read_bytes() if target.is_file() else b""
    current_hash = sha256_bytes(current)
    if preimage_sha256 is not None and preimage_sha256 != current_hash:
        return {"status":"FREEZE", "freeze_reason":"preimage_hash_mismatch", "write_action":"PREVIEW_ONLY", "external_actions": {"filesystem_write":False}}
    key = idempotency_key or ("ledger-write-" + hashlib.sha256((expected_digest + current_hash).encode()).hexdigest()[:20])
    postimage = canonical_bytes(snapshot)
    post_hash = sha256_bytes(postimage)
    plan_payload = {"target_path": TARGET_PATH, "preimage_sha256": current_hash, "postimage_sha256": post_hash, "idempotency_key": key, "atomic_replace": True}
    plan_digest = sha256_bytes(canonical_bytes(plan_payload))
    return {"status":"WRITE_CANDIDATE", "write_action":"APPLY" ,"target_path":TARGET_PATH, "preimage_sha256":current_hash, "postimage_sha256":post_hash, "source_snapshot_digest":expected_digest, "idempotency_key":key, "atomic_replace":True, "write_plan_digest":plan_digest, "rollback_command":"sh artifacts/apg-adaptive-git-ledger-persistence-preview/ROLLBACK.sh <target-root> <preimage-root>", "snapshot":snapshot, "external_actions": {"filesystem_write":False}}

def persist(root: Path, plan: Mapping[str, Any], *, apply: bool = False, fail_atomic: bool = False) -> dict[str, Any]:
    if plan.get("status") == "FREEZE": return dict(plan)
    if not apply:
        result = dict(plan); result["write_action"] = "PREVIEW_ONLY"; return result
    root = Path(root).resolve(); target = (root / TARGET_PATH).resolve(); target.parent.mkdir(parents=True, exist_ok=True)
    marker = target.with_suffix(target.suffix + ".idempotency.json")
    if marker.is_file():
        try:
            prior = json.loads(marker.read_text(encoding="utf-8"))
            if prior.get("idempotency_key") == plan.get("idempotency_key") and prior.get("postimage_sha256") == plan.get("postimage_sha256"):
                out = dict(plan); out.update(status="ALREADY_PERSISTED", write_action="APPLIED", external_actions={"filesystem_write":False}); return out
        except Exception:
            pass
    if fail_atomic:
        out = dict(plan); out.update(status="FREEZE", freeze_reason="atomic_replace_failed", write_action="APPLIED", external_actions={"filesystem_write":False}); return out
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(target.parent), prefix=".apg-ledger-") as handle:
            temp_name = handle.name; handle.write(canonical_bytes(plan["snapshot"])); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp_name, target)
        marker.write_text(json.dumps({"idempotency_key":plan["idempotency_key"],"postimage_sha256":plan["postimage_sha256"]}, sort_keys=True)+"\n", encoding="utf-8")
    except Exception:
        if temp_name: Path(temp_name).unlink(missing_ok=True)
        out = dict(plan); out.update(status="FREEZE", freeze_reason="atomic_replace_failed", write_action="APPLIED", external_actions={"filesystem_write":False}); return out
    out = dict(plan); out.update(status="PERSISTED", write_action="APPLIED", external_actions={"filesystem_write":True}); return out

def simulate(root: Path, snapshot: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    return persist(root, build_plan(root, snapshot, **{k:v for k,v in kwargs.items() if k in {"source_snapshot_digest","idempotency_key","preimage_sha256"}}), apply=kwargs.get("apply", False), fail_atomic=kwargs.get("fail_atomic", False))

def main(argv: list[str] | None = None) -> int:
    p=argparse.ArgumentParser(); p.add_argument("--root", default="."); p.add_argument("--snapshot", required=True); p.add_argument("--source-snapshot-digest"); p.add_argument("--idempotency-key"); p.add_argument("--preimage-sha256"); p.add_argument("--apply", action="store_true"); p.add_argument("--fail-atomic", action="store_true"); a=p.parse_args(argv)
    result=simulate(Path(a.root), json.loads(a.snapshot), source_snapshot_digest=a.source_snapshot_digest, idempotency_key=a.idempotency_key, preimage_sha256=a.preimage_sha256, apply=a.apply, fail_atomic=a.fail_atomic)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("status") not in {"FREEZE"} else 3
if __name__ == "__main__": raise SystemExit(main())
