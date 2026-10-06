"""Bounded TaskPlan -> Scheduler bridge; no host or arbitrary-code dispatch.

The checkpoint product_loop contract supplies the output-evidence and missing
executor semantics. This bridge uses work's exact caller confirmation and the
existing Scheduler/transaction receipts instead of adding another run ledger.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .executor import action_digest
from .planning import task_waves
from .safety import safe_target, sha256_bytes
from .scheduler import Scheduler, _validate_task
from .service import (
    ServiceError, _digest, _graph, _root_path, _service_bundle, _validate_confirmation_scope,
    build_caller_confirmation, preview_session,
)
from .sessions import read_session_snapshot, session_write_lock

_DOCUMENT = {"review_requirements", "render_document", "define_schema",
             "define_interface", "define_access", "security_review", "plan_delivery"}
_MISSING = {"implement_feature", "verify_feature"}


def _mapped_tasks(base: Mapping[str, Any]) -> list[dict[str, Any]]:
    plan = base["task_plan"]
    waves = task_waves(plan)
    entries = {entry["path"]: entry for entry in base["file_plan"]["entries"]}
    by_id = {task["id"]: task for task in plan["tasks"]}
    owned = {path for task in by_id.values() for path in task["outputs"]}
    # Context documents (e.g. memory.md) have no TaskPlan owner. Make their
    # transaction explicit rather than silently dropping approved targets.
    context_paths = sorted(set(entries) - owned)
    namespace = base["service_digest"]
    def identity(task_id):
        return "tp-" + _digest([namespace, task_id])

    result = []

    def append(task_id, source, outputs, dependencies, classification):
        if classification == "host_missing":
            action = {"type": "host_missing"}
        else:
            if not outputs or any(path not in entries for path in outputs):
                raise ServiceError("document_mapping_missing", "任务缺少同源文档输出")
            core = {"schema_version": "1.0", "root": base["root"],
                    "entries": [deepcopy(entries[path]) for path in sorted(outputs)]}
            action = {"type": "document", "plan": {**core, "plan_digest": _digest(core), "status": "preview"}}
        action_digest(action)
        result.append({
            "task_id": identity(task_id), "idempotency_key": identity(task_id),
            "input": {"plan_task_id": task_id, "source_task": deepcopy(source),
                      "plan_digest": _digest(plan), "classification": classification,
                      "activation_status": "not_activated"},
            "action": action, "depends_on": [identity(dep) for dep in dependencies],
            # Partial/unknown writes require receipt inspection, never blind retry.
            "max_attempts": 1, "timeout_seconds": 30,
        })

    context_id = "adapter-context-documents"
    if context_id in by_id:
        raise ServiceError("reserved_task_id", "任务 ID 与适配器上下文节点冲突")
    if context_paths:
        append(context_id, None, context_paths, [], "document")
    for wave in waves:
        for task_id in wave:
            task = by_id[task_id]
            kind = task["action"]["type"]
            classification = "document" if kind in _DOCUMENT else "offline_design" if kind == "integration_design" else "host_missing" if kind in _MISSING else None
            if classification is None:
                raise ServiceError("unsupported_task", "未支持的任务动作")
            dependencies = list(task["depends_on"])
            if context_paths and not dependencies:
                dependencies.append(context_id)
            append(task_id, task, task["outputs"], dependencies, classification)
    for task in result:
        _validate_task(task)
    return result


def _envelope(base: dict[str, Any]) -> dict[str, Any]:
    tasks = _mapped_tasks(base)
    value = {"schema_version": "1.0", "kind": "offline_taskplan_scheduler",
             "root": base["root"], "session": base["session"], "status": base["status"],
             "targets": base["targets"], "diff": base["diff"],
             "service_preview": base, "tasks": tasks,
             "effects": {"scheduler": "local_initialization_enqueue_and_receipts",
                         "session": "cooperative_write_lock", "external": False,
                         "rollback": "per_document_transaction"}}
    return {**value, "service_digest": _digest(value)}


def preview_task_plan(root: str | Path, session_id: str) -> dict[str, Any]:
    """Zero-write preview including mapping, dependency order and host blockers."""
    return _envelope(preview_session(root, session_id))


def build_task_plan_confirmation(preview: Mapping[str, Any], *, statement: str,
                                 actor: str = "human-caller",
                                 expires_in_seconds: int = 300) -> dict[str, Any]:
    """Record caller confirmation of the preview AND its execution deadline.

    Keep this complete value independently of the approval. It is a trusted
    caller assertion, not a signature or proof of a person's identity.
    """
    if type(expires_in_seconds) is not int or not 0 < expires_in_seconds <= 3600:
        raise ServiceError("approval_expiry_invalid", "审批期限必须为 1 至 3600 秒")
    receipt = build_caller_confirmation(preview, statement=statement, actor=actor)
    confirmed_at = datetime.fromisoformat(receipt["confirmed_at"].replace("Z", "+00:00"))
    return {"schema_version": "1.0", "kind": "offline_taskplan_confirmation",
            "receipt": receipt,
            "expires_at": (confirmed_at + timedelta(seconds=expires_in_seconds)).isoformat()}


def _validate_task_confirmation(confirmation, preview):
    if (not isinstance(confirmation, Mapping)
            or set(confirmation) != {"schema_version", "kind", "receipt", "expires_at"}
            or confirmation["schema_version"] != "1.0"
            or confirmation["kind"] != "offline_taskplan_confirmation"):
        raise ServiceError("human_confirmation_invalid", "必须提供绑定精确到期时间的调度确认")
    _validate_confirmation_scope(confirmation["receipt"], preview)
    try:
        expiry = datetime.fromisoformat(confirmation["expires_at"].replace("Z", "+00:00"))
        confirmed_at = datetime.fromisoformat(confirmation["receipt"]["confirmed_at"].replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ServiceError("approval_expiry_invalid", "调度确认到期时间无效") from exc
    if expiry.tzinfo is None or not 0 < (expiry - confirmed_at).total_seconds() <= 3600:
        raise ServiceError("approval_expiry_invalid", "调度确认期限必须为 1 至 3600 秒")
    if expiry <= datetime.now(timezone.utc):
        raise ServiceError("approval_expired", "调度确认已过期，必须重新取得调用方确认")


def approve_task_plan(preview: Mapping[str, Any], *, confirmation: Mapping[str, Any]) -> dict[str, Any]:
    """Bind the caller's exact deadline without renewing it; performs no writes."""
    expected = preview_task_plan(preview["root"], preview["session"]["id"])
    if preview != expected or preview["status"] != "ready" or preview["service_preview"]["task_plan"]["unresolved"]:
        raise ServiceError("preview_not_ready", "必须使用当前完整的精确预览")
    _validate_task_confirmation(confirmation, preview)
    return {"preview": deepcopy(dict(preview)), "confirmation": deepcopy(dict(confirmation))}


def execute_task_plan(root: str | Path, approval: Mapping[str, Any], *,
                      authorization_context: Mapping[str, Any]) -> dict[str, Any]:
    """Execute only this approved graph; repeats retain original runs/evidence.

    Completed hashes are checked before continuation. A rolled-back or modified
    output blocks replay. Each document task has its own reversible transaction;
    earlier successes remain on a later failure. Roll back via service.rollback.
    """
    project_root = _root_path(root)
    if set(approval) != {"preview", "confirmation"}:
        raise ServiceError("approval_fields_invalid", "审批字段无效")
    preview = deepcopy(approval["preview"])
    if preview != _envelope(preview["service_preview"]) or preview["root"] != str(project_root):
        raise ServiceError("preview_content_mismatch", "预览内容或根目录不匹配")
    if authorization_context != approval["confirmation"]:
        raise ServiceError("human_confirmation_scope_mismatch", "需要同一份调用方确认收据")
    _validate_task_confirmation(approval["confirmation"], preview)
    base = preview["service_preview"]
    file_plan = base["file_plan"]
    core = {key: file_plan[key] for key in ("schema_version", "root", "entries")}
    if (file_plan["plan_digest"] != _digest(core)
            or file_plan["root"] != str(project_root)
            or base["targets"] != [entry["path"] for entry in file_plan["entries"]]
            or len(base["targets"]) != len(set(base["targets"]))
            or {entry["path"]: entry["content"] for entry in file_plan["entries"]} != base["documents"]
            or base["service_digest"] != _digest(_service_bundle(project_root, base["session"], base["recommendation"], base["task_plan"], base["documents"], file_plan))):
        raise ServiceError("preview_content_mismatch", "文件计划必须来自同源文档和预览")
    if base["status"] != "ready" or base["task_plan"]["unresolved"]:
        raise ServiceError("preview_not_ready", "未完成澄清的计划不能执行")
    with session_write_lock(project_root):
        current = read_session_snapshot(project_root, base["session"]["id"])
        recommendation, plan, documents = _graph(current)
        if current != base["session"] or (recommendation, plan, documents) != (base["recommendation"], base["task_plan"], base["documents"]):
            return {"status": "stale", "reason": "session_or_plan_drift"}
        scheduler = Scheduler(project_root)
        for task in preview["tasks"]:
            scheduler.enqueue(task)
        # Check all prior successes before allowing any new effects.
        for task in preview["tasks"]:
            state = scheduler.get_task(task["task_id"])
            if state["state"] == "succeeded":
                for entry in task["action"]["plan"]["entries"]:
                    target = safe_target(project_root, entry["path"])
                    if not target.is_file() or sha256_bytes(target.read_bytes()) != entry["after_sha256"]:
                        return {"status": "blocked", "reason": "completed_output_drift", "task_id": task["task_id"]}
        for task in preview["tasks"]:
            scheduler.run_next(task["task_id"])
        states = [scheduler.get_task(task["task_id"]) for task in preview["tasks"]]
        runs = [run for task in preview["tasks"] for run in scheduler.list_runs(task["task_id"])]
        return {"status": "succeeded" if all(task["state"] == "succeeded" for task in states) else "blocked",
                "scope": "offline_documents_only", "live_verified": False,
                "preview_digest": preview["service_digest"], "tasks": states, "runs": runs}


__all__ = ["preview_task_plan", "build_task_plan_confirmation", "approve_task_plan", "execute_task_plan"]
