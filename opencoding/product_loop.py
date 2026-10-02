"""小白产品任务闭环的离线执行账本。

This module checks project documents and accepts explicitly confirmed local
Agent tasks.  Capability tasks that would activate payment, notifications,
deployment, or another external system stop at an explicit human gate.  It
records per-task evidence, hashes, rollback instructions, and resumable state;
it never calls a provider or changes a remote system.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from typing import Any, Mapping
from functools import wraps
from .sessions import session_write_lock
from .safety import _root_path, safe_target, _reject_linked_ancestors
from .agent_tasks import LocalAgentTaskExecutor

from .safety import inspect_sensitive, sanitize_text
from .service import adoption_input_status
from .planning import validate_task_plan

PRODUCT_RUN_SCHEMA_VERSION = "1.0"
_RUN_ID = re.compile(r"^product-[0-9a-f]{12}$")


class ProductLoopError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# The product loop deliberately does not manufacture implementation actions.
# A skill/Agent caller may inject a reviewed executor for one run; the object is
# never persisted in the run ledger and must be supplied again on resume.
ProjectExecutor = LocalAgentTaskExecutor


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _root(root: str | os.PathLike[str] | Path) -> Path:
    path = Path(root)
    if not path.is_absolute():
        raise ProductLoopError("root_invalid", "项目根目录必须是绝对路径")
    return _root_path(path)


def _runs_dir(root: Path) -> Path:
    return root / ".opencoding" / "product_runs"


def _safe_runs_dir(root: Path, *, create: bool = False) -> Path:
    target = _runs_dir(root)
    _reject_linked_ancestors(target)
    for part in (root / ".opencoding", target):
        if part.is_symlink() or (part.exists() and not part.is_dir()):
            raise ProductLoopError("runtime_root_unsafe", "产品运行账本路径不安全：" + str(part))
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _reject_linked_ancestors(path)
    if isinstance(value, dict):
        value["record_digest"] = _digest({k: v for k, v in value.items() if k != "record_digest"})
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_run(root: Path, run_id: str) -> dict[str, Any]:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ProductLoopError("run_id_invalid", "产品运行编号无效")
    path = _safe_runs_dir(root) / (run_id + ".json")
    _reject_linked_ancestors(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProductLoopError("run_not_found", "找不到产品运行：" + run_id) from exc
    except (OSError, ValueError) as exc:
        raise ProductLoopError("run_unreadable", "产品运行账本不可读") from exc
    if not isinstance(value, dict) or value.get("schema_version") != PRODUCT_RUN_SCHEMA_VERSION:
        raise ProductLoopError("run_invalid", "产品运行账本版本或结构无效")
    if value.get("root", str(root)) != str(root):
        raise ProductLoopError("run_root_mismatch", "运行账本属于其他根目录")
    if "record_digest" in value and value["record_digest"] != _digest({k: v for k, v in value.items() if k != "record_digest"}):
        raise ProductLoopError("run_drifted", "运行账本摘要已变化")
    return value


def _locked_operation(function):
    @wraps(function)
    def wrapped(root, *args, **kwargs):
        project_root = _root(root)
        with session_write_lock(project_root):
            return function(project_root, *args, **kwargs)
    return wrapped


def _validate_executor(executor, root, session_id, plan_digest):
    if executor is not None:
        if type(executor) is not LocalAgentTaskExecutor:
            raise ProductLoopError("executor_invalid", "只接受显式确认的 LocalAgentTaskExecutor")
        executor.validate(root, session_id, plan_digest)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_evidence(root: Path, task: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    evidence: list[dict[str, Any]] = []
    errors: list[str] = []
    for raw in task.get("outputs", []):
        if not isinstance(raw, str) or not raw or raw.startswith("/") or ".." in Path(raw).parts or "\\" in raw:
            errors.append("output_path_invalid")
            continue
        target = (root / raw).resolve()
        if root not in target.parents and target != root:
            errors.append("output_path_escape")
            continue
        if not target.is_file():
            errors.append("missing_output:" + raw)
            continue
        payload = target.read_bytes()
        evidence.append({"path": raw, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    return evidence, errors


def _execute_task(
    root: Path,
    task: Mapping[str, Any],
    *,
    human_confirmed: bool,
    project_executor: ProjectExecutor | None = None,
    run_id: str | None = None,
    attempt: int = 1,
    checkpoint=None,
) -> dict[str, Any]:
    gate = task.get("activation_gate") if isinstance(task.get("activation_gate"), Mapping) else {}
    required = gate.get("required") is True
    action = task.get("action") if isinstance(task.get("action"), Mapping) else {}
    action_type = str(action.get("type") or "unknown")
    capability = str(action.get("capability") or "")
    if action_type == "integration_design" and capability in {"payment", "notifications", "deployment"}:
        # These tasks are harmless design documents, but continuing beyond
        # their boundary would imply activating an external service.
        required = True
        gate = {**dict(gate), "reason": str(gate.get("reason") or "外部服务激活前需要人工确认")}
    base = {
        "task_id": task.get("id"),
        "action": action_type,
        "started_at": _now(),
        "rollback": str(task.get("rollback") or "保留证据并按对应事务回滚。"),
        "test": "offline_contract_check",
    }
    if required and not human_confirmed:
        return {**base, "status": "blocked_human_gate", "human_gate": {"required": True, "recorded": False, "reason": str(gate.get("reason") or "需要人工确认")}, "errors": []}
    if action_type in {"implement_feature", "verify_feature"}:
        # A plan can describe real product code, but it must come from an
        # explicitly injected Agent/skill executor.  The executor is never
        # inferred from a target label and is not persisted in the ledger.
        if project_executor is not None:
            try:
                outcome = project_executor(
                    root,
                    dict(task),
                    run_id=run_id,
                    attempt=attempt,
                    checkpoint=checkpoint,
                )
            except Exception as exc:  # noqa: BLE001 - task failure is ledger evidence
                return {
                    **base,
                    "status": "failed",
                    "executor_id": getattr(project_executor, "executor_id", None),
                    "errors": ["project_executor_error:" + type(exc).__name__],
                    "executor_error": sanitize_text(str(exc))[:240],
                    "finished_at": _now(),
                }
            if not isinstance(outcome, Mapping):
                return {
                    **base,
                    "status": "failed",
                    "executor_id": getattr(project_executor, "executor_id", None),
                    "errors": ["project_executor_result_invalid"],
                    "finished_at": _now(),
                }
            result = dict(outcome)
            result.setdefault("executor_id", getattr(project_executor, "executor_id", None))
            status = result.get("status")
            if status not in {"succeeded", "failed", "blocked_human_gate", "blocked_capability"}:
                result.update(status="failed", errors=["project_executor_status_invalid"])
            result.setdefault("errors", [])
            result.setdefault("finished_at", _now())
            return {**base, **result}
        # Keep the fail-closed behavior when no reviewed executor was supplied.
        return {
            **base,
            "status": "blocked_capability",
            "human_gate": {
                "required": True,
                "recorded": False,
                "reason": "当前未提供已确认的本地 Agent 动作；请先预览并绑定 LocalAgentTaskExecutor。",
            },
            "errors": ["project_executor_unavailable"],
            "finished_at": _now(),
        }
    files, errors = _file_evidence(root, task)
    if action_type == "security_review":
        for item in files:
            text = (root / item["path"]).read_text(encoding="utf-8", errors="replace")
            if inspect_sensitive(text).get("sensitive"):
                errors.append("sensitive_output:" + item["path"])
    result: dict[str, Any] = {**base, "status": "succeeded" if not errors else "failed", "evidence": files, "errors": sorted(set(errors))}
    if required:
        result["human_gate"] = {"required": True, "recorded": True, "confirmed_at": _now(), "scope": "offline-product-plan-gate"}
    result["finished_at"] = _now()
    return result


def _continue(
    root: Path,
    record: dict[str, Any],
    *,
    human_confirmed: bool,
    project_executor: ProjectExecutor | None = None,
) -> dict[str, Any]:
    _validate_executor(project_executor, root, record['session_id'], record['plan_digest'])
    if record.get("pending_action") is not None or any(t.get('status') == 'executing' for t in record.get('tasks', [])):
        raise ProductLoopError("execution_unknown", "上次动作/任务终态未持久化；保留现场，禁止自动重放")
    if record.get("status") in {"succeeded", "rolled_back"}:
        return record
    if human_confirmed and type(human_confirmed) is not bool:
        raise ProductLoopError("confirmation_invalid", "人工确认必须是严格布尔值")
    # A resumed run may have been blocked in an earlier wave; clear that
    # transient status before replaying the remaining dependency frontier.
    record["status"] = "running"
    record.pop("failure", None)
    record.pop("acceptance", None)
    record.pop("human_gate", None)
    tasks = record.get("tasks")
    by_id = {item.get("id"): item for item in tasks if isinstance(item, Mapping)}
    for wave in record.get("waves", []):
        for task_id in wave:
            current = by_id.get(task_id)
            if not isinstance(current, dict) or current.get("status") == "succeeded":
                continue
            if current.get("status") == "blocked_capability" and project_executor is None:
                record["status"] = "blocked_capability"
                record["human_gate"] = current.get("human_gate")
                break
            if current.get("status") == "blocked_human_gate" and not human_confirmed:
                record["status"] = "blocked_human_gate"
                record["updated_at"] = _now()
                _write_json(_safe_runs_dir(root, create=True) / (record["run_id"] + ".json"), record)
                return record
            if current["task"]["action"]["type"] in {"implement_feature", "verify_feature"} and int(current.get("attempts", 0)) >= int(current["task"].get("retry", {}).get("max_attempts", 2)):
                record["status"] = "failed"
                record["failure"] = {"task_id": task_id, "errors": ["attempt_budget_exhausted"]}
                break
            previous = {k: v for k, v in current.items() if k not in {"task", "history"}}
            if previous.get("attempts", 0):
                current.setdefault("history", []).append(previous)
            current["attempts"] = int(current.get("attempts", 0)) + 1
            def checkpoint(event):
                current["status"] = "executing"
                entry = {**event, "task_id": task_id, "at": _now()}
                record.setdefault("action_events", []).append(entry)
                record["pending_action"] = entry if event["phase"] == "dispatching" else None
                _write_json(_safe_runs_dir(root, create=True) / (record["run_id"] + ".json"), record)
            result = _execute_task(
                root,
                current["task"],
                human_confirmed=human_confirmed,
                project_executor=project_executor,
                run_id=str(record.get("run_id")),
                attempt=int(current.get("attempts", 1)),
                checkpoint=checkpoint,
            )
            current.update(result)
            if result["status"] in {"blocked_human_gate", "blocked_capability"}:
                current["attempts"] -= 1
            if result["status"] in {"blocked_human_gate", "blocked_capability"}:
                record["status"] = result["status"]
                record["human_gate"] = result.get("human_gate")
                break
            if result["status"] == "failed":
                record["status"] = "failed"
                record["failure"] = {"task_id": task_id, "errors": result["errors"]}
                break
        if record.get("status") in {"blocked_human_gate", "blocked_capability", "failed"}:
            break
    else:
        record["status"] = "succeeded"
        record["acceptance"] = {"status": "passed", "checks": ["task_dependencies", "output_hashes", "offline_contracts", "human_gates"]}
    record["updated_at"] = _now()
    _write_json(_safe_runs_dir(root, create=True) / (record["run_id"] + ".json"), record)
    return record


@_locked_operation
def start_product_run(
    root: str | os.PathLike[str] | Path,
    session_id: str,
    *,
    human_confirmed: bool = False,
    run_id: str | None = None,
    project_executor: ProjectExecutor | None = None,
) -> dict[str, Any]:
    project_root = _root(root)
    if type(human_confirmed) is not bool:
        raise ProductLoopError("confirmation_invalid", "人工确认必须是严格布尔值")
    status = adoption_input_status(project_root, session_id)
    if not status.get("adopted"):
        raise ProductLoopError("adoption_required", "必须先明确采用已校验方案")
    if not status.get("matches"):
        raise ProductLoopError("input_drift", sanitize_text(str(status.get("required_action") or "采用输入已漂移")))
    adoption_path = project_root / ".opencoding" / "adoptions" / (session_id + ".json")
    try:
        adoption = json.loads(adoption_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProductLoopError("adoption_unreadable", "采用记录不可读") from exc
    plan = adoption.get("task_plan")
    validation = validate_task_plan(plan)
    if not validation["valid"]:
        raise ProductLoopError("plan_invalid", "采用任务图未通过校验")
    _validate_executor(project_executor, project_root, session_id, adoption["plan_digest"])
    selected = run_id or ("product-" + uuid.uuid4().hex[:12])
    if not _RUN_ID.fullmatch(selected):
        raise ProductLoopError("run_id_invalid", "产品运行编号无效")
    run_path = _safe_runs_dir(project_root, create=True) / (selected + ".json")
    if run_path.exists():
        raise ProductLoopError("run_exists", "产品运行已存在")
    tasks = [{"id": task["id"], "task": dict(task), "status": "pending", "attempts": 0} for task in plan["tasks"]]
    record = {
        "schema_version": PRODUCT_RUN_SCHEMA_VERSION,
        "run_id": selected,
        "root": str(project_root),
        "session_id": session_id,
        "revision": adoption.get("revision"),
        "plan_digest": adoption.get("plan_digest"),
        "waves": [list(wave) for wave in plan["waves"]],
        "tasks": tasks,
        "status": "running",
        "started_at": _now(),
        "updated_at": _now(),
        "external_actions": [],
        "source": "offline_product_loop",
    }
    _write_json(run_path, record)
    return _continue(project_root, record, human_confirmed=human_confirmed, project_executor=project_executor)


@_locked_operation
def resume_product_run(
    root: str | os.PathLike[str] | Path,
    run_id: str,
    *,
    human_confirmed: bool = False,
    project_executor: ProjectExecutor | None = None,
) -> dict[str, Any]:
    project_root = _root(root)
    if type(human_confirmed) is not bool:
        raise ProductLoopError("confirmation_invalid", "人工确认必须是严格布尔值")
    record = _load_run(project_root, run_id)
    status = adoption_input_status(project_root, str(record.get("session_id")))
    if not status.get("matches"):
        raise ProductLoopError("input_drift", "接续前发现采用输入漂移，必须重新评估")
    latest = {}
    for event in record.get("action_events", []):
        if event.get("phase") == "completed":
            for artifact in event.get("receipt", {}).get("artifacts", []):
                latest[artifact["path"]] = artifact["sha256"]
    for path, expected in latest.items():
        target = safe_target(project_root, path, allow_missing=False)
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise ProductLoopError("output_drifted", "接续前已完成产物发生变化：" + path)
    if record.get('file_rollbacks'):
        raise ProductLoopError('run_rolled_back', '本运行已有文件回滚，请重新预览采用后创建新运行')
    return _continue(project_root, record, human_confirmed=human_confirmed, project_executor=project_executor)


@_locked_operation
def rollback_product_run(root: str | os.PathLike[str] | Path, run_id: str, *, reason: str, transaction_id: str | None = None) -> dict[str, Any]:
    project_root = _root(root)
    if not isinstance(reason, str) or not reason.strip():
        raise ProductLoopError("rollback_reason_required", "回滚必须说明原因")
    record = _load_run(project_root, run_id)
    if record.get("pending_action") is not None:
        raise ProductLoopError("execution_unknown", "未知动作效果需人工检查，禁止自动回滚")
    transactions = list(dict.fromkeys(
        event["receipt"]["transaction"]["transaction_id"]
        for event in record.get("action_events", [])
        if event.get("phase") == "completed" and event.get("receipt", {}).get("transaction", {}).get("transaction_id")
    ))
    if transactions:
        if transaction_id is not None and transaction_id not in transactions:
            raise ProductLoopError("rollback_receipt_mismatch", "回滚事务不属于本运行")
        # Receipts are inode-bound; an older receipt cannot safely undo a
        # later repair replacement. Restore the first reviewed preimage for
        # each path, using a fresh transaction for existing files.
        preimages: dict[str, Mapping[str, Any]] = {}
        latest: dict[str, str] = {}
        for event in record.get("action_events", []):
            if event.get("phase") == "dispatching" and event.get("action", {}).get("type") == "write_text":
                preimages.setdefault(event["action"]["path"], event)
        for event in record.get("action_events", []):
            if event.get("phase") != "completed" or event.get("receipt", {}).get("status") != "succeeded":
                continue
            receipt = event.get("receipt", {})
            tx = receipt.get("transaction", {}).get("transaction_id")
            if tx and (transaction_id is None or tx == transaction_id):
                for artifact in receipt.get("artifacts", []):
                    latest[artifact["path"]] = artifact["sha256"]
        outcomes = []
        for path, expected in latest.items():
            target = safe_target(project_root, path, allow_missing=True)
            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                outcomes.append({"path": path, "status": "blocked", "reason": "postimage_drift"})
                continue
            event = preimages.get(path)
            if event is None:
                outcomes.append({"path": path, "status": "blocked", "reason": "preimage_missing"})
                continue
            if event.get("before_exists"):
                # Legacy executors without a persisted preimage remain
                # receipt-only and fail closed instead of guessing content.
                outcomes.append({"path": path, "status": "blocked", "reason": "preimage_not_recorded"})
                continue
            if target.exists():
                target.unlink()
            outcomes.append({"path": path, "status": "rolled_back", "removed": True})
            parent = target.parent
            while parent != project_root and parent.is_dir():
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        record.setdefault("file_rollbacks", []).extend(outcomes)
        record["status"] = "files_rolled_back" if all(item["status"] == "rolled_back" for item in outcomes) else "rollback_blocked"
        record["rollback"] = {"requested_at": _now(), "reason": sanitize_text(reason),
            "scope": "run_preimages" if transaction_id is None else "selected_receipt",
            "effects": "仅删除首次确认时不存在且仍匹配最新回执的文件；已有文件/任意 Python 副作用不自动恢复。"}
        record.pop("acceptance", None)
        _write_json(_safe_runs_dir(project_root, create=True) / (run_id + ".json"), record)
        return record
    record["status"] = "rolled_back"
    record["rollback"] = {"requested_at": _now(), "reason": sanitize_text(reason.strip()), "effects": "本账本不删除用户文档；如需撤销文档，使用对应 document transaction 回滚。"}
    record["updated_at"] = _now()
    _write_json(_safe_runs_dir(project_root, create=True) / (run_id + ".json"), record)
    return record


def read_product_run(root: str | os.PathLike[str] | Path, run_id: str) -> dict[str, Any]:
    return _load_run(_root(root), run_id)


__all__ = ["ProductLoopError", "ProjectExecutor", "read_product_run", "resume_product_run", "rollback_product_run", "start_product_run"]
