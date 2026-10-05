"""Root-explicit W2 entry service built on the accepted W1 product primitives."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import difflib
import json
from pathlib import Path
import re
from typing import Any, Mapping
import uuid

from .decisions import build_recommendation
from .documents import render_documents, validate_recommendation
from .intake import QUESTION_DEFINITIONS, answer_question, new_session
from .planning import build_task_plan, validate_task_plan
from .scheduler import SchedulerSnapshotError, read_snapshot
from .safety import action_digest, canonical_json, evaluate_action, sanitize_text, sha256_bytes
from .sessions import (
    SessionConflictError,
    SessionLockError,
    _root,
    read_session_snapshot,
    save_session,
    session_write_lock,
)
from .transactions import apply_changes, preview_changes, rollback_changes


SCHEMA_VERSION = "1.0"
APPROVAL_SCHEMA_VERSION = "1.0"
CONFIRMATION_SCHEMA_VERSION = "1.0"
_APPROVAL_FIELDS = {
    "schema_version",
    "root",
    "session_id",
    "revision",
    "recommendation",
    "task_plan",
    "documents",
    "file_plan",
    "service_digest",
    "recommendation_digest",
    "task_plan_digest",
    "documents_digest",
    "file_plan_digest",
    "diff_digest",
    "targets",
    "action",
    "expires_at",
    "approved",
    "authorization_context",
}
_CONFIRMATION_FIELDS = {
    "schema_version",
    "kind",
    "root",
    "session_id",
    "revision",
    "service_digest",
    "targets",
    "diff_digest",
    "statement",
    "actor",
    "confirmed_at",
    "receipt_id",
}
_RECEIPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_CAPABILITY_IDS = tuple(item["id"] for item in QUESTION_DEFINITIONS if item["id"] not in {"audience", "outcome", "platform"})


class ServiceError(ValueError):
    """A fail-closed, user-mappable service error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _json_value(value: Any) -> Any:
    """Return a JSON round-trippable copy, rejecting accidental runtime objects."""

    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ServiceError("non_json_state", "服务状态不可序列化") from exc


def _root_path(root: str | Path) -> Path:
    if not isinstance(root, (str, Path)):
        raise ServiceError("invalid_root", "必须显式提供项目根目录")
    path = Path(root)
    if not path.is_absolute():
        raise ServiceError("invalid_root", "项目根目录必须是绝对路径")
    try:
        return _root(path).resolve(strict=True)
    except (TypeError, ValueError) as exc:
        raise ServiceError("invalid_root", sanitize_text(str(exc))) from exc


def _digest(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def _settled(session: Mapping[str, Any], question_id: str) -> bool:
    requirement = session.get("requirements", {}).get(question_id)
    return bool(requirement and requirement.get("kind") in {"known", "affirmative", "negative"} and not requirement.get("conflict"))


def _question_status(session: Mapping[str, Any], question_id: str) -> str:
    if question_id not in session["answers"]:
        return "unanswered"
    requirement = session["requirements"].get(question_id, {})
    if requirement.get("conflict"):
        return "needs_confirmation"
    if requirement.get("kind") in {"unknown", "ambiguous"}:
        return "needs_clarification"
    return "answered"


def derive_frontier(session: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Derive the current dependency frontier without persisting a new schema."""

    questions = session.get("questions")
    if not isinstance(questions, list):
        questions = list(QUESTION_DEFINITIONS)
    by_id = {item["id"]: item for item in questions}
    ids: list[str] = []
    if not _settled(session, "audience"):
        ids.append("audience")
    if not _settled(session, "platform"):
        ids.append("platform")
    if _settled(session, "audience") and not _settled(session, "outcome"):
        ids.append("outcome")
    if _settled(session, "audience") and _settled(session, "outcome") and _settled(session, "platform"):
        ids.extend(item["id"] for item in questions if item["id"] not in {"audience", "platform", "outcome"} and not _settled(session, item["id"]))
    result: list[dict[str, Any]] = []
    for question_id in ids:
        item = deepcopy(by_id[question_id])
        item["status"] = _question_status(session, question_id)
        result.append(item)
    return result


def _graph(session: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    recommendation = build_recommendation(session)
    task_plan = build_task_plan(recommendation)
    documents = render_documents(recommendation, task_plan)
    return recommendation, task_plan, documents


def _diff_for_plan(root: Path, file_plan: Mapping[str, Any]) -> str:
    chunks: list[str] = []
    for entry in file_plan["entries"]:
        target = root / entry["path"]
        if target.exists():
            try:
                old = sanitize_text(target.read_text(encoding="utf-8"))
            except (OSError, UnicodeError) as exc:
                raise ServiceError("diff_read_failed", sanitize_text(str(exc))) from exc
        else:
            old = ""
        new = sanitize_text(entry["content"])
        lines = list(
            difflib.unified_diff(
                old.splitlines(),
                new.splitlines(),
                fromfile=entry["path"],
                tofile=entry["path"],
                lineterm="",
            )
        )
        if lines:
            chunks.append("\n".join(lines))
    return "\n".join(chunks)


def _service_bundle(root: Path, session: Mapping[str, Any], recommendation: Mapping[str, Any], task_plan: Mapping[str, Any], documents: Mapping[str, str], file_plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "root": str(root),
        "session_id": session["id"],
        "revision": session["revision"],
        "recommendation": recommendation,
        "task_plan": task_plan,
        "documents": documents,
        "file_plan": file_plan,
    }


def _view(root: Path, session: dict[str, Any], *, include_preview: bool) -> dict[str, Any]:
    recommendation, task_plan, documents = _graph(session)
    file_plan = preview_changes(root, documents) if include_preview else None
    bundle = _service_bundle(root, session, recommendation, task_plan, documents, file_plan or {})
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "session": _json_value(session),
        "frontier": derive_frontier(session),
        "recommendation": _json_value(recommendation),
        "task_plan": _json_value(task_plan),
        "documents": _json_value(documents),
        "status": recommendation["status"],
        "service_digest": _digest(bundle),
    }
    if include_preview:
        result["file_plan"] = _json_value(file_plan)
        result["targets"] = [item["path"] for item in file_plan["entries"]]
        result["diff"] = _diff_for_plan(root, file_plan)
    return result


def create_session(root: str | Path, goal: str) -> dict[str, Any]:
    project_root = _root_path(root)
    try:
        session = new_session(goal)
        saved = save_session(project_root, session)
    except (SessionConflictError, SessionLockError, TypeError, ValueError) as exc:
        raise ServiceError(getattr(exc, "code", "session_save_failed"), sanitize_text(str(exc))) from exc
    return {**session_view(project_root, session["id"], include_preview=False), "status": saved["status"]}


def session_view(root: str | Path, session_id: str, *, include_preview: bool = False) -> dict[str, Any]:
    project_root = _root_path(root)
    try:
        session = read_session_snapshot(project_root, session_id)
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise ServiceError("session_read_failed", sanitize_text(str(exc))) from exc
    try:
        return _view(project_root, session, include_preview=include_preview)
    except (TypeError, ValueError) as exc:
        raise ServiceError("graph_invalid", sanitize_text(str(exc))) from exc


def list_sessions(root: str | Path) -> list[dict[str, Any]]:
    project_root = _root_path(root)
    sessions_dir = project_root / ".opencoding" / "sessions"
    if not sessions_dir.exists():
        return []
    if not sessions_dir.is_dir():
        raise ServiceError("session_store_invalid", "会话目录不是目录")
    result: list[dict[str, Any]] = []
    for path in sorted(sessions_dir.glob("*.json"), key=lambda item: item.name):
        try:
            session = read_session_snapshot(project_root, path.stem)
        except (FileNotFoundError, TypeError, ValueError) as exc:
            raise ServiceError("session_read_failed", sanitize_text(str(exc))) from exc
        result.append({"id": session["id"], "revision": session["revision"], "goal": session["goal"], "state": session["state"]})
    return result


def execution_status(root: str | Path, task_id: str | None = None) -> dict[str, Any]:
    """Return the scheduler's zero-write task/run status for one local root."""

    project_root = _root_path(root)
    try:
        return _json_value(read_snapshot(project_root, task_id))
    except SchedulerSnapshotError as exc:
        raise ServiceError("execution_status_" + exc.code, sanitize_text(str(exc))) from exc
    except (TypeError, ValueError) as exc:
        raise ServiceError("execution_status_rejected", sanitize_text(str(exc))) from exc


def submit_answer(root: str | Path, session_id: str, expected_revision: int, question_id: str, answer: str) -> dict[str, Any]:
    project_root = _root_path(root)
    try:
        current = read_session_snapshot(project_root, session_id)
        if current["revision"] != expected_revision:
            return {"status": "stale", "reason_codes": ["session_revision_stale"], "revision": current["revision"]}
        updated = answer_question(current, question_id, answer)
        saved = save_session(project_root, updated)
    except SessionLockError as exc:
        return {"status": "busy", "reason_codes": [SessionLockError.code], "message": sanitize_text(str(exc))}
    except SessionConflictError as exc:
        return {"status": "stale", "reason_codes": [SessionConflictError.code], "message": sanitize_text(str(exc))}
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise ServiceError("answer_rejected", sanitize_text(str(exc))) from exc
    return {**session_view(project_root, session_id, include_preview=False), "status": saved["status"]}


def preview_session(root: str | Path, session_id: str) -> dict[str, Any]:
    return session_view(root, session_id, include_preview=True)


def build_caller_confirmation(
    preview: Mapping[str, Any],
    *,
    statement: str,
    actor: str = "human-caller",
    receipt_id: str | None = None,
    confirmed_at: str | None = None,
) -> dict[str, Any]:
    """Build a caller-owned receipt after a person confirms the exact preview.

    This helper only records the caller's assertion.  It does not approve or
    apply anything; :func:`approve_preview` and :func:`apply_approved` still
    bind and revalidate the receipt against the exact preview scope.
    """

    if not isinstance(preview, Mapping):
        raise ServiceError("preview_required", "人类确认必须绑定完整的只读预览")
    if not isinstance(statement, str) or not statement.strip():
        raise ServiceError("human_confirmation_invalid", "人类确认必须包含可追溯的确认语句")
    if not isinstance(actor, str) or not actor.strip():
        raise ServiceError("human_confirmation_invalid", "人类确认必须标记调用方")
    if receipt_id is None:
        receipt_id = "receipt-" + uuid.uuid4().hex
    if confirmed_at is None:
        confirmed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    confirmation = {
        "schema_version": CONFIRMATION_SCHEMA_VERSION,
        "kind": "human_confirmation",
        "root": preview.get("root"),
        "session_id": preview.get("session", {}).get("id") if isinstance(preview.get("session"), Mapping) else None,
        "revision": preview.get("session", {}).get("revision") if isinstance(preview.get("session"), Mapping) else None,
        "service_digest": preview.get("service_digest"),
        "targets": preview.get("targets"),
        "diff_digest": _digest(preview.get("diff")),
        "statement": sanitize_text(statement.strip()),
        "actor": sanitize_text(actor.strip()),
        "confirmed_at": confirmed_at,
        "receipt_id": receipt_id,
    }
    _validate_confirmation_scope(confirmation, preview)
    return _json_value(confirmation)


def _validate_confirmation_scope(confirmation: Mapping[str, Any], preview: Mapping[str, Any]) -> None:
    if not isinstance(confirmation, Mapping) or set(confirmation) != _CONFIRMATION_FIELDS:
        raise ServiceError("human_confirmation_invalid", "人类确认收据字段不完整或包含未知字段")
    if confirmation["schema_version"] != CONFIRMATION_SCHEMA_VERSION or confirmation["kind"] != "human_confirmation":
        raise ServiceError("human_confirmation_invalid", "人类确认收据版本或类型无效")
    if not isinstance(confirmation["root"], str) or confirmation["root"] != preview.get("root"):
        raise ServiceError("human_confirmation_scope_mismatch", "人类确认未绑定预览根目录")
    session = preview.get("session")
    if not isinstance(session, Mapping) or confirmation["session_id"] != session.get("id") or confirmation["revision"] != session.get("revision"):
        raise ServiceError("human_confirmation_scope_mismatch", "人类确认未绑定预览会话版本")
    if confirmation["service_digest"] != preview.get("service_digest"):
        raise ServiceError("human_confirmation_scope_mismatch", "人类确认未绑定预览摘要")
    if confirmation["targets"] != preview.get("targets") or not isinstance(confirmation["targets"], list) or any(not isinstance(item, str) for item in confirmation["targets"]):
        raise ServiceError("human_confirmation_scope_mismatch", "人类确认未绑定精确目标范围")
    if confirmation["diff_digest"] != _digest(preview.get("diff")):
        raise ServiceError("human_confirmation_scope_mismatch", "人类确认未绑定精确差异")
    if not isinstance(confirmation["statement"], str) or not confirmation["statement"].strip() or not isinstance(confirmation["actor"], str) or not confirmation["actor"].strip():
        raise ServiceError("human_confirmation_invalid", "人类确认语句和调用方不能为空")
    if not isinstance(confirmation["receipt_id"], str) or not _RECEIPT_ID.fullmatch(confirmation["receipt_id"]):
        raise ServiceError("human_confirmation_invalid", "人类确认 receipt_id 无效")
    try:
        confirmed_at = datetime.fromisoformat(str(confirmation["confirmed_at"]).replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ServiceError("human_confirmation_invalid", "人类确认时间无效") from exc
    if confirmed_at.tzinfo is None or confirmed_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ServiceError("human_confirmation_invalid", "人类确认时间无效")


def approve_preview(
    preview: Mapping[str, Any],
    *,
    confirmation: Mapping[str, Any] | None = None,
    authorization_context: Mapping[str, Any] | None = None,
    expires_in_seconds: int = 300,
) -> dict[str, Any]:
    """Validate a preview and bind a caller-issued human confirmation receipt."""

    if confirmation is not None and authorization_context is not None and _json_value(confirmation) != _json_value(authorization_context):
        raise ServiceError("human_confirmation_invalid", "confirmation 与 authorization_context 不一致")
    confirmation = confirmation if confirmation is not None else authorization_context
    if confirmation is None:
        raise ServiceError("human_confirmation_required", "必须先取得调用方对精确预览的人工确认")
    if not isinstance(preview, Mapping) or not isinstance(preview.get("file_plan"), Mapping):
        raise ServiceError("preview_required", "审批必须来自完整的只读预览")
    try:
        required_preview_fields = {
            "schema_version",
            "root",
            "session",
            "frontier",
            "recommendation",
            "task_plan",
            "documents",
            "status",
            "service_digest",
            "file_plan",
            "targets",
            "diff",
        }
        if set(preview) != required_preview_fields:
            raise ServiceError("preview_fields_invalid", "预览字段不完整或包含未知字段")
        if preview["schema_version"] != SCHEMA_VERSION or preview["status"] != "ready":
            raise ServiceError("preview_not_ready", "只有已确认的完整方案才能申请写入")
        root = _root_path(preview["root"])
        if str(root) != preview["root"]:
            raise ServiceError("preview_root_invalid", "预览根目录不是规范的本地项目根")
        session = preview["session"]
        recommendation = preview["recommendation"]
        task_plan = preview["task_plan"]
        documents = preview["documents"]
        file_plan = preview["file_plan"]
        if not isinstance(preview["targets"], list) or not all(isinstance(item, str) for item in preview["targets"]):
            raise ServiceError("preview_targets_invalid", "预览目标范围无效")
        if not isinstance(preview["diff"], str):
            raise ServiceError("preview_diff_invalid", "预览差异不是文本")
        if not all(isinstance(value, dict) for value in (session, recommendation, task_plan, documents)):
            raise ServiceError("preview_content_invalid", "预览内容结构无效")
        expected = _view(root, session, include_preview=True)
        if _json_value(dict(preview)) != _json_value(expected):
            raise ServiceError("preview_content_mismatch", "预览内容与当前本地状态不一致")
        _validate_confirmation_scope(confirmation, preview)
        validate_recommendation(recommendation)
        plan_result = validate_task_plan(task_plan)
        if not plan_result["valid"]:
            raise ServiceError("preview_task_plan_invalid", "预览任务图无效")
        targets = [item["path"] for item in file_plan["entries"]]
        bundle = _service_bundle(root, session, recommendation, task_plan, documents, file_plan)
        service_digest = _digest(bundle)
        if preview.get("service_digest") != service_digest:
            raise ServiceError("preview_digest_mismatch", "预览摘要不匹配")
        if preview["targets"] != targets:
            raise ServiceError("preview_targets_mismatch", "预览目标与文件计划不一致")
        action = {
            "kind": "local_write",
            "root": str(root),
            "plan_digest": file_plan["plan_digest"],
            "targets": targets,
            "external": False,
            "cost_limit": 0,
            "data_scope": "synthetic-local-documents",
            "irreversible": False,
        }
        expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
        approval = {
            "schema_version": APPROVAL_SCHEMA_VERSION,
            "root": str(root),
            "session_id": session["id"],
            "revision": session["revision"],
            "recommendation": _json_value(recommendation),
            "task_plan": _json_value(task_plan),
            "documents": _json_value(documents),
            "file_plan": _json_value(file_plan),
            "service_digest": service_digest,
            "recommendation_digest": _digest(recommendation),
            "task_plan_digest": _digest(task_plan),
            "documents_digest": _digest(documents),
            "file_plan_digest": _digest(file_plan),
            "diff_digest": _digest(preview["diff"]),
            "targets": targets,
            "action": action,
            "expires_at": expiry.isoformat().replace("+00:00", "Z"),
            "approved": True,
            "authorization_context": _json_value(confirmation),
        }
        _validate_approval(root, approval)
        return _json_value(approval)
    except ServiceError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ServiceError("approval_invalid", sanitize_text(str(exc))) from exc


def _expected_action(root: Path, file_plan: Mapping[str, Any], targets: list[str]) -> dict[str, Any]:
    return {
        "kind": "local_write",
        "root": str(root),
        "plan_digest": file_plan["plan_digest"],
        "targets": targets,
        "external": False,
        "cost_limit": 0,
        "data_scope": "synthetic-local-documents",
        "irreversible": False,
    }


def _validate_approval(
    root: Path,
    approval: Mapping[str, Any],
    *,
    authorization_context: Mapping[str, Any] | None = None,
    require_external_authorization: bool = False,
) -> None:
    if set(approval) != _APPROVAL_FIELDS:
        raise ServiceError("approval_fields_invalid", "审批字段不完整或包含未知字段")
    if approval["schema_version"] != APPROVAL_SCHEMA_VERSION or approval["root"] != str(root):
        raise ServiceError("approval_identity_invalid", "审批根目录或版本不匹配")
    if not isinstance(approval["session_id"], str) or isinstance(approval["revision"], bool) or not isinstance(approval["revision"], int) or approval["revision"] < 0:
        raise ServiceError("approval_identity_invalid", "审批会话绑定无效")
    if type(approval["approved"]) is not bool or not approval["approved"]:
        raise ServiceError("approval_not_granted", "审批未明确同意")
    try:
        expiry = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ServiceError("approval_expiry_invalid", "审批有效期无效") from exc
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise ServiceError("approval_expired", "审批已过期")
    if not isinstance(approval["targets"], list) or any(not isinstance(item, str) for item in approval["targets"]):
        raise ServiceError("approval_targets_invalid", "审批目标范围无效")
    if not isinstance(approval.get("diff_digest"), str) or not approval["diff_digest"]:
        raise ServiceError("approval_diff_invalid", "审批差异摘要无效")
    embedded_context = approval.get("authorization_context")
    if not isinstance(embedded_context, Mapping):
        raise ServiceError("human_confirmation_required", "审批缺少调用方人工确认收据")
    expected_context = {
        "schema_version": CONFIRMATION_SCHEMA_VERSION,
        "kind": "human_confirmation",
        "root": approval["root"],
        "session_id": approval["session_id"],
        "revision": approval["revision"],
        "service_digest": approval["service_digest"],
        "targets": approval["targets"],
        "diff_digest": approval["diff_digest"],
    }
    for key, expected in expected_context.items():
        if embedded_context.get(key) != expected:
            raise ServiceError("human_confirmation_scope_mismatch", "审批人工确认未绑定精确范围")
    if set(embedded_context) != _CONFIRMATION_FIELDS:
        raise ServiceError("human_confirmation_invalid", "审批人工确认收据字段不完整或包含未知字段")
    if not isinstance(embedded_context.get("statement"), str) or not embedded_context["statement"].strip() or not isinstance(embedded_context.get("actor"), str) or not embedded_context["actor"].strip():
        raise ServiceError("human_confirmation_invalid", "审批人工确认语句和调用方不能为空")
    if not isinstance(embedded_context.get("receipt_id"), str) or not _RECEIPT_ID.fullmatch(embedded_context["receipt_id"]):
        raise ServiceError("human_confirmation_invalid", "审批人工确认 receipt_id 无效")
    try:
        confirmed_at = datetime.fromisoformat(str(embedded_context.get("confirmed_at")).replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ServiceError("human_confirmation_invalid", "审批人工确认时间无效") from exc
    if confirmed_at.tzinfo is None or confirmed_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ServiceError("human_confirmation_invalid", "审批人工确认时间无效")
    if require_external_authorization:
        if authorization_context is None:
            raise ServiceError("human_confirmation_required", "apply 必须收到调用方人工确认 authorization_context")
        if _json_value(dict(authorization_context)) != _json_value(dict(embedded_context)):
            raise ServiceError("human_confirmation_scope_mismatch", "apply 的 authorization_context 与审批收据不一致")
    if not isinstance(approval["recommendation"], dict) or not isinstance(approval["task_plan"], dict) or not isinstance(approval["documents"], dict):
        raise ServiceError("approval_content_invalid", "审批内容结构无效")
    if not isinstance(approval["file_plan"], dict) or not isinstance(approval["action"], dict):
        raise ServiceError("approval_content_invalid", "审批文件计划或动作结构无效")
    try:
        validate_recommendation(approval["recommendation"])
        plan_result = validate_task_plan(approval["task_plan"])
    except (TypeError, ValueError) as exc:
        raise ServiceError("approval_content_invalid", "审批方案或任务图无效") from exc
    if not plan_result["valid"]:
        raise ServiceError("approval_content_invalid", "审批任务图无效")
    if approval["recommendation"].get("status") != "ready":
        raise ServiceError("approval_not_ready", "审批方案尚未达到可交付状态")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in approval["documents"].items()):
        raise ServiceError("approval_content_invalid", "审批文档必须是字符串映射")
    file_plan = approval["file_plan"]
    if set(file_plan) != {"schema_version", "root", "entries", "plan_digest", "status"} or not isinstance(file_plan.get("entries"), list):
        raise ServiceError("approval_file_plan_invalid", "审批没有有效文件计划")
    if file_plan["schema_version"] != "1.0" or file_plan["root"] != str(root) or file_plan["status"] != "preview":
        raise ServiceError("approval_file_plan_invalid", "审批文件计划版本或根目录无效")
    for entry in file_plan["entries"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "operation", "before_sha256", "after_sha256", "content"}:
            raise ServiceError("approval_file_plan_invalid", "审批文件计划条目无效")
        if not isinstance(entry["path"], str) or not isinstance(entry["content"], str):
            raise ServiceError("approval_file_plan_invalid", "审批文件计划条目类型无效")
    expected_targets = [item["path"] for item in file_plan["entries"]]
    if approval["targets"] != expected_targets:
        raise ServiceError("approval_targets_mismatch", "审批目标与文件计划不一致")
    for value, expected in (
        (approval["recommendation_digest"], _digest(approval["recommendation"])),
        (approval["task_plan_digest"], _digest(approval["task_plan"])),
        (approval["documents_digest"], _digest(approval["documents"])),
        (approval["file_plan_digest"], _digest(file_plan)),
    ):
        if value != expected:
            raise ServiceError("approval_digest_mismatch", "审批内容摘要不匹配")
    bundle = _service_bundle(root, {"id": approval["session_id"], "revision": approval["revision"]}, approval["recommendation"], approval["task_plan"], approval["documents"], file_plan)
    if approval["service_digest"] != _digest(bundle):
        raise ServiceError("approval_service_digest_mismatch", "审批服务摘要不匹配")
    action = _expected_action(root, file_plan, approval["targets"])
    if approval["action"] != action:
        raise ServiceError("approval_action_mismatch", "审批动作或精确目标不匹配")
    safety = evaluate_action(action, {
        "action_digest": action_digest(action),
        "root": str(root),
        "expires_at": approval["expires_at"],
        "approved": True,
    })
    if safety.get("decision") != "allow":
        raise ServiceError("approval_safety_blocked", "审批未通过本地写入安全校验")


def apply_approved(
    root: str | Path,
    approval: Mapping[str, Any],
    authorization_context: Mapping[str, Any] | None = None,
    *,
    confirmation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if authorization_context is not None and confirmation is not None and _json_value(authorization_context) != _json_value(confirmation):
        raise ServiceError("human_confirmation_invalid", "authorization_context 与 confirmation 不一致")
    authorization_context = authorization_context if authorization_context is not None else confirmation
    project_root = _root_path(root)
    try:
        _validate_approval(project_root, approval, authorization_context=authorization_context, require_external_authorization=True)
        with session_write_lock(project_root):
            current = read_session_snapshot(project_root, approval["session_id"])
            if current["revision"] != approval["revision"]:
                return {"status": "stale", "reason_codes": ["session_revision_stale"], "revision": current["revision"]}
            recommendation, task_plan, documents = _graph(current)
            current_plan = preview_changes(project_root, documents)
            current_bundle = _service_bundle(project_root, current, recommendation, task_plan, documents, current_plan)
            if (
                recommendation != approval["recommendation"]
                or task_plan != approval["task_plan"]
                or documents != approval["documents"]
                or current_plan != approval["file_plan"]
                or _digest(current_bundle) != approval["service_digest"]
            ):
                return {"status": "stale", "reason_codes": ["business_or_file_plan_drift"]}
            result = apply_changes(project_root, dict(approval["file_plan"]), approved_digest=approval["file_plan"]["plan_digest"])
            return {"status": result.get("status"), "approval": "accepted", "transaction": _json_value(result)}
    except SessionLockError as exc:
        return {"status": "busy", "reason_codes": [SessionLockError.code], "message": sanitize_text(str(exc))}
    except ServiceError:
        raise
    except (FileNotFoundError, TypeError, ValueError, AttributeError) as exc:
        raise ServiceError("apply_rejected", sanitize_text(str(exc))) from exc


def rollback(root: str | Path, transaction_id: str) -> dict[str, Any]:
    project_root = _root_path(root)
    try:
        result = rollback_changes(project_root, transaction_id)
    except (TypeError, ValueError, OSError) as exc:
        raise ServiceError("rollback_rejected", sanitize_text(str(exc))) from exc
    return _json_value(result)


def as_json(value: Any) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, indent=2)


__all__ = [
    "APPROVAL_SCHEMA_VERSION",
    "CONFIRMATION_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "ServiceError",
    "apply_approved",
    "as_json",
    "approve_preview",
    "build_caller_confirmation",
    "create_session",
    "derive_frontier",
    "execution_status",
    "list_sessions",
    "preview_session",
    "rollback",
    "session_view",
    "submit_answer",
]
