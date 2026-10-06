"""Pure, bounded consistency projection of caller-supplied TaskPlan captures.

This is neither a collector nor an authenticator. Transaction bytes are opaque;
there is deliberately no verified-document path without the core's complete
strict transaction validator. Private pure helpers are tied to the C contract.
"""
from __future__ import annotations

import math
from pathlib import PurePosixPath, PureWindowsPath
import re

from .documents import validate_recommendation
from .executor import action_digest
from .intake import _validate_session_shape
from .planning import validate_task_plan
from .safety import _portable_key, _relative_parts, canonical_json, sha256_bytes
from .scheduler import TASK_STATES, _receipt_digest
from .service import _digest, _service_bundle
from .taskplan_scheduler import _envelope
from .transactions import _TRANSACTION_ID as _TX


MAX_DEPTH = 64
MAX_NODES = 100_000
MAX_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 4096
MAX_TASKS = 256
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DEFINITION = {"task_id", "idempotency_key", "input", "action", "depends_on", "max_attempts", "timeout_seconds"}
_TASK_FIELDS = _DEFINITION | {"schema_version", "state", "attempt", "last_run_id", "last_error"}
_RECEIPT_FIELDS = {"schema_version", "run_id", "task_id", "attempt", "input_sha256", "action_digest", "status",
                   "exit_code", "timed_out", "cancelled", "stdout_summary", "stderr_summary", "artifacts", "reason", "receipt_digest"}
_RUN_FIELDS = _RECEIPT_FIELDS | {"started_at", "finished_at", "receipt"}
_BASE_FIELDS = {"schema_version", "root", "session", "frontier", "recommendation", "task_plan", "documents",
                "status", "service_digest", "file_plan", "targets", "diff"}
_STRICT_GAP = "strict_transaction_validator_unavailable"


class _Rejected(ValueError):
    pass


def _require(condition, code="invalid_input"):
    if not condition:
        raise _Rejected(code)


def _keys(value, fields):
    _require(type(value) is dict and set(value) == fields)


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _id(value):
    return type(value) is str and _ID.fullmatch(value) is not None


def _plain(value, budget, *, bytes_allowed=False, depth=0):
    """Copy only builtin values; reject cycles by depth and never invoke objects."""
    budget[0] += 1
    _require(depth <= MAX_DEPTH and budget[0] <= MAX_NODES, "resource_limit")
    kind = type(value)
    if kind is str or (kind is bytes and bytes_allowed):
        _require(len(value) <= MAX_BYTES, "resource_limit")
        budget[1] += len(value.encode("utf-8")) if kind is str else len(value)
        _require(budget[1] <= MAX_BYTES, "resource_limit")
        return value
    if kind in (dict, list):
        _require(len(value) <= MAX_ITEMS, "resource_limit")
        if kind is list:
            return [_plain(item, budget, bytes_allowed=bytes_allowed, depth=depth + 1) for item in value]
        result = {}
        for key, item in value.items():
            _require(type(key) is str)
            _plain(key, budget, depth=depth + 1)
            result[key] = _plain(item, budget, bytes_allowed=bytes_allowed, depth=depth + 1)
        return result
    _require(kind in (int, float, bool, type(None)))
    if kind is float:
        _require(math.isfinite(value))
    if kind is int:
        _require(-(2**63) <= value < 2**63)
    return value


def _path(value):
    _require(type(value) is str and "/".join(_relative_parts(value)) == value)
    return value


def _binding(root, digest, preview):
    _require(type(root) is str and bool(root) and not any(ord(c) < 32 for c in root))
    _require(PurePosixPath(root).is_absolute() or PureWindowsPath(root).is_absolute())
    _require(not any(part in {".", ".."} for part in re.split(r"[/\\]", root)))
    _require(_hash(digest) and type(preview) is dict)
    _require(preview.get("root") == root and preview.get("service_digest") == digest)
    _require(_digest({key: value for key, value in preview.items() if key != "service_digest"}) == digest)
    base = preview["service_preview"]
    _keys(base, _BASE_FIELDS)
    _require(base["schema_version"] == "1.0" and base["root"] == root)
    _validate_session_shape(base["session"])
    validate_recommendation(base["recommendation"])
    recommendation = base["recommendation"]
    _require(recommendation["session_id"] == base["session"]["id"] and recommendation["revision"] == base["session"]["revision"])
    _require(base["status"] == recommendation["status"] and type(base["diff"]) is str)
    _require(type(base["frontier"]) is list and type(base["task_plan"]) is dict)
    _require(type(base["task_plan"].get("tasks")) is list and len(base["task_plan"]["tasks"]) <= MAX_TASKS, "resource_limit")
    _require(validate_task_plan(base["task_plan"])["valid"])
    _require(base["task_plan"]["session_id"] == base["session"]["id"] and base["task_plan"]["revision"] == base["session"]["revision"])
    plan = base["file_plan"]
    _keys(plan, {"schema_version", "root", "entries", "plan_digest", "status"})
    _require(plan["schema_version"] == "1.0" and plan["root"] == root and plan["status"] == "preview")
    _require(type(plan["entries"]) is list and bool(plan["entries"]))
    paths, documents, aliases = [], {}, set()
    for entry in plan["entries"]:
        _keys(entry, {"path", "operation", "before_sha256", "after_sha256", "content"})
        path = _path(entry["path"])
        _require(_portable_key(path) not in aliases)
        aliases.add(_portable_key(path))
        _require(type(entry["content"]) is str and sha256_bytes(entry["content"].encode("utf-8")) == entry["after_sha256"])
        _require((entry["operation"] == "create" and entry["before_sha256"] is None) or
                 (entry["operation"] == "update" and _hash(entry["before_sha256"])))
        paths.append(path)
        documents[path] = entry["content"]
    _require(paths == base["targets"] and documents == base["documents"])
    core = {key: plan[key] for key in ("schema_version", "root", "entries")}
    _require(_hash(plan["plan_digest"]) and _digest(core) == plan["plan_digest"])
    _require(_digest(_service_bundle(root, base["session"], recommendation, base["task_plan"], documents, plan)) == base["service_digest"])
    _require(_digest(_envelope(base)) == _digest(preview))
    return base


def _captures(root, targets, transactions, files):
    _require(type(transactions) is dict and type(files) is dict)
    for key, value in transactions.items():
        _keys(value, {"root", "transaction_id", "raw_files", "acquisition_error"})
        _require(_TX.fullmatch(key) is not None and value["transaction_id"] == key and value["root"] == root)
        _require(value["acquisition_error"] is None or type(value["acquisition_error"]) is str)
        _require(type(value["raw_files"]) is dict)
        aliases = set()
        for name, raw in value["raw_files"].items():
            _path(name)
            _require(_portable_key(name) not in aliases and type(raw) is bytes)
            aliases.add(_portable_key(name))
    for key, value in files.items():
        _keys(value, {"root", "path", "content_bytes", "acquisition_error"})
        _path(key)
        _require(key in targets and value["path"] == key and value["root"] == root)
        _require(value["acquisition_error"] is None or type(value["acquisition_error"]) is str)
        _require(value["content_bytes"] is None or type(value["content_bytes"]) is bytes)


def _index(items, key):
    _require(type(items) is list)
    result, duplicate = {}, False
    for item in items:
        _require(type(item) is dict and _id(item.get(key)))
        duplicate |= item[key] in result
        result[item[key]] = item
    return result, duplicate


def _snapshot(root, snapshot):
    _keys(snapshot, {"schema_version", "root", "status", "tasks", "runs"})
    _require(snapshot["schema_version"] == "1.0")
    tasks, duplicate_tasks = _index(snapshot["tasks"], "task_id")
    runs, duplicate_runs = _index(snapshot["runs"], "run_id")
    reasons = []
    if snapshot["root"] != root:
        reasons.append("snapshot_root_mismatch")
    if snapshot["status"] != "ready":
        reasons.append("snapshot_not_ready")
    if duplicate_tasks or duplicate_runs:
        reasons.append("duplicate_scheduler_id")
    return tasks, runs, reasons


def _run_reasons(task, original, run):
    reasons = []
    try:
        _keys(run, _RUN_FIELDS)
        _require(run["schema_version"] == "1.0" and _id(run["run_id"]))
        _require(type(run["attempt"]) is int and run["attempt"] == task["attempt"] == 1)
        _require(run["task_id"] == original["task_id"] and run["run_id"] == task["last_run_id"])
        _require(run["input_sha256"] == _digest(original["input"]) and run["action_digest"] == action_digest(original["action"]))
        _require(type(run["exit_code"]) is int or run["exit_code"] is None)
        _require(type(run["timed_out"]) is bool and type(run["cancelled"]) is bool)
        _require(type(run["started_at"]) is str and bool(run["started_at"]))
        _require(type(run["stdout_summary"]) is str and type(run["stderr_summary"]) is str)
        _require(run["reason"] is None or type(run["reason"]) is str)
        _require(type(run["artifacts"]) is list)
        _require(run["status"] in {"running", "succeeded", "failed", "cancelled", "timed_out"} and run["status"] == task["state"])
        if run["status"] == "running":
            return ["run_not_terminal"]
        _require(type(run["finished_at"]) is str and bool(run["finished_at"]))
        receipt = run["receipt"]
        _keys(receipt, _RECEIPT_FIELDS)
        _require(_hash(run["receipt_digest"]) and _receipt_digest(receipt) == run["receipt_digest"])
        _require(canonical_json(receipt) == canonical_json({key: run[key] for key in _RECEIPT_FIELDS}))
        if run["status"] != "succeeded" or run["exit_code"] != 0 or run["timed_out"] or run["cancelled"]:
            reasons.append("run_not_successful")
    except (ValueError, TypeError, KeyError):
        reasons.append("run_receipt_inconsistent")
    return reasons


def _artifacts(original, run):
    expected = {entry["path"]: entry["after_sha256"] for entry in original["action"]["plan"]["entries"]}
    artifacts, seen, transactions = run["artifacts"], set(), set()
    _require(type(artifacts) is list and len(artifacts) == len(expected))
    for artifact in artifacts:
        _keys(artifact, {"path", "sha256", "sha256_kind", "transaction_id"})
        path = _path(artifact["path"])
        _require(path in expected and path not in seen and artifact["sha256"] == expected[path])
        _require(artifact["sha256_kind"] == "file_bytes" and type(artifact["transaction_id"]) is str)
        _require(_TX.fullmatch(artifact["transaction_id"]) is not None)
        seen.add(path)
        transactions.add(artifact["transaction_id"])
    _require(len(transactions) == 1)
    return next(iter(transactions))


def _task(original, tasks, runs, global_reasons):
    classification = original["input"]["classification"]
    result = {"plan_task_id": original["input"]["plan_task_id"], "task_id": original["task_id"],
              "classification": classification, "scheduler_state": None, "evidence_status": "unverified",
              "activation_status": "not_activated", "run_id": None, "receipt_digest": None,
              "transaction_id": None, "transaction_capture": "not_supplied", "outputs": [], "reason_codes": list(global_reasons)}
    reasons = result["reason_codes"]
    task = tasks.get(original["task_id"])
    if task is None:
        reasons.append("task_missing")
        return result
    if type(task.get("state")) is str and task["state"] in TASK_STATES:
        result["scheduler_state"] = task["state"]
    try:
        _keys(task, _TASK_FIELDS)
        _require(task["schema_version"] == "1.0" and result["scheduler_state"] is not None)
        _require(type(task["attempt"]) is int and task["attempt"] in (0, 1))
        _require(type(task["max_attempts"]) is int and task["max_attempts"] == 1)
        _require(type(task["timeout_seconds"]) in (int, float) and task["timeout_seconds"] == original["timeout_seconds"])
        _require(_digest({key: task[key] for key in _DEFINITION - {"timeout_seconds"}}) ==
                 _digest({key: original[key] for key in _DEFINITION - {"timeout_seconds"}}))
        _require(task["last_error"] is None or type(task["last_error"]) is str)
        _require(task["last_run_id"] is None or _id(task["last_run_id"]))
    except (ValueError, TypeError, KeyError):
        reasons.append("task_definition_inconsistent")
        return result
    if task["state"] == "succeeded" and any(tasks.get(dep, {}).get("state") != "succeeded" for dep in original["depends_on"]):
        reasons.append("dependency_state_inconsistent")
    related = [run for run in runs.values() if run.get("task_id") == original["task_id"]]
    if len(related) != task["attempt"]:
        reasons.append("task_run_count_inconsistent")
    if not task["last_run_id"] or task["last_run_id"] not in runs:
        reasons.append("run_missing")
        return result
    run = runs[task["last_run_id"]]
    result["run_id"] = task["last_run_id"]
    reasons.extend(_run_reasons(task, original, run))
    if not reasons:
        result["receipt_digest"] = run["receipt_digest"]
        if classification != "host_missing":
            try:
                result["transaction_id"] = _artifacts(original, run)
            except (ValueError, TypeError, KeyError):
                reasons.append("artifacts_inconsistent")
    return result


def _capabilities(original_tasks):
    kinds = {task["input"]["source_task"]["action"]["type"] for task in original_tasks if task["input"]["source_task"]}
    result = {name: {"status": "unverified", "reason": "not_probed"} for name in ("real_host", "managed_loader")}
    for name, kind in (("implementation", "implement_feature"), ("tests", "verify_feature")):
        result[name] = {"status": "blocked", "reason": "host_missing"} if kind in kinds else {"status": "unverified", "reason": "not_in_evidence_scope"}
    result["external_services"] = {"status": "unverified", "reason": "not_activated"}
    return result


def project_taskplan_evidence(*, expected_root, expected_preview_digest, original_preview,
                              scheduler_snapshot, transaction_evidence, file_evidence):
    """Project six already-captured inputs, without IO, execution or clock reads.

    Expected values must have been retained independently by the caller. Builtin
    dict/list JSON only; bytes are confined to the two documented capture fields.
    Limits apply to the combined input, with no silent truncation. Byte matches
    refer only to supplied bytes, never to authenticated current filesystem data.
    """
    report = {"schema_version": "1.0", "kind": "offline_taskplan_evidence", "root_sha256": None,
              "preview_digest": None, "session": None, "activation_status": "not_activated", "live_verified": False,
              "binding": {"status": "rejected", "reason_codes": []}, "scheduler": {"status": None, "reason_codes": []},
              "capture_reason_codes": [], "tasks": [], "capabilities": _capabilities([])}
    try:
        budget = [0, 0]
        root, digest, preview = [_plain(value, budget) for value in
                                (expected_root, expected_preview_digest, original_preview)]
        base = _binding(root, digest, preview)
    except (ValueError, TypeError, KeyError, IndexError, RecursionError) as error:
        report["binding"]["reason_codes"] = [str(error) if type(error) is _Rejected and str(error) == "resource_limit" else "preview_binding_invalid"]
        return report
    report.update(root_sha256=sha256_bytes(root.encode("utf-8")), preview_digest=digest,
                  session={"id": base["session"]["id"], "revision": base["session"]["revision"]},
                  binding={"status": "matched", "reason_codes": []}, capabilities=_capabilities(preview["tasks"]))
    try:
        transactions, files = [_plain(value, budget, bytes_allowed=True) for value in (transaction_evidence, file_evidence)]
        _captures(root, base["targets"], transactions, files)
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        report["capture_reason_codes"].append("capture_invalid")
        if type(error) is _Rejected and str(error) == "resource_limit":
            report["capture_reason_codes"].append("resource_limit")
        transactions, files = {}, {}
    try:
        snapshot = _plain(scheduler_snapshot, budget)
        tasks, runs, reasons = _snapshot(root, snapshot)
        report["scheduler"]["status"] = snapshot["status"] if snapshot["status"] in ("ready", "not_initialized", "not_found") else None
        report["scheduler"]["reason_codes"] = reasons
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        tasks, runs = {}, {}
        report["scheduler"]["reason_codes"] = ["snapshot_invalid"]
        if type(error) is _Rejected and str(error) == "resource_limit":
            report["scheduler"]["reason_codes"].append("resource_limit")
    original_ids = {task["task_id"] for task in preview["tasks"]}
    attempted = any((type(task.get("attempt")) is int and task["attempt"] > 0) or
                    (type(task.get("state")) is str and task["state"] in TASK_STATES - {"queued"})
                    for task_id, task in tasks.items() if task_id in original_ids)
    related_runs = any(type(run.get("task_id")) is str and run["task_id"] in original_ids for run in runs.values())
    if (base["status"] != "ready" or base["task_plan"]["unresolved"]) and (attempted or related_runs):
        report["scheduler"]["reason_codes"].append("plan_not_executable")
    global_reasons = report["scheduler"]["reason_codes"] + report["capture_reason_codes"]
    projected = [_task(task, tasks, runs, global_reasons) for task in preview["tasks"]]
    # The derived original mapping is topological. Propagate only the known
    # impossible Host success, never the universal transaction-validation gap.
    impossible = set()
    for original, item in zip(preview["tasks"], projected):
        if item["scheduler_state"] == "succeeded":
            if any(dependency in impossible for dependency in original["depends_on"]):
                item["reason_codes"].append("dependency_evidence_inconsistent")
                impossible.add(item["task_id"])
            if item["classification"] == "host_missing":
                impossible.add(item["task_id"])
    claims = {}
    for item in projected:
        if item["transaction_id"]:
            claims.setdefault(item["transaction_id"], []).append(item)
    for owners in claims.values():
        if len(owners) > 1:
            for item in owners:
                item["reason_codes"].append("transaction_reused")
    for original, item in zip(preview["tasks"], projected):
        reasons = item["reason_codes"]
        if item["classification"] == "host_missing":
            item["evidence_status"] = "blocked"
            reasons.append("host_missing")
            if item["scheduler_state"] == "succeeded":
                reasons.append("host_missing_claimed_success")
        else:
            eligible = not reasons
            capture = transactions.get(item["transaction_id"])
            if capture is None:
                reasons.append("transaction_capture_missing")
            elif capture["acquisition_error"] is not None:
                reasons.append("transaction_acquisition_error")
                eligible = False
            else:
                item["transaction_capture"] = "opaque_supplied"
            for entry in original["action"]["plan"]["entries"]:
                output = {"path": entry["path"], "expected_sha256": entry["after_sha256"], "captured_sha256": None, "status": "unverified"}
                capture = files.get(entry["path"])
                if capture is None or capture["content_bytes"] is None:
                    reasons.append("file_capture_missing")
                elif capture["acquisition_error"] is not None:
                    reasons.append("file_acquisition_error")
                elif eligible:
                    output["captured_sha256"] = sha256_bytes(capture["content_bytes"])
                    output["status"] = "byte_match" if output["captured_sha256"] == entry["after_sha256"] else "byte_mismatch"
                    if output["status"] == "byte_mismatch":
                        reasons.append("file_bytes_mismatch")
                item["outputs"].append(output)
            reasons.append(_STRICT_GAP)
        item["reason_codes"] = sorted(set(reasons))
    report["tasks"] = projected
    return report


__all__ = ["project_taskplan_evidence"]
