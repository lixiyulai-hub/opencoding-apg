"""Root-explicit W2 entry service built on the accepted W1 product primitives."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import difflib
import json
from pathlib import Path
from typing import Any, Mapping

from .decisions import build_recommendation, derive_fact_constraints, evaluate_fact_constraints
from .documents import render_documents, validate_recommendation
from .facts import FactError, build_decision_record, list_facts
from .intake import QUESTION_DEFINITIONS, answer_question, new_session
from . import plansource  # CP5 §4.2：唯一的 AI 派生计划来源
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
    "targets",
    "action",
    "expires_at",
    "approved",
}
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


def plan_digest(task_plan: Mapping[str, Any]) -> str:
    """CP5 §4.2：对外公开的计划摘要算法（规则计划与 AI 派生计划必须可比）。"""

    return _digest(task_plan)


def _adoption_record(project_root: Path, session_id: str) -> Mapping[str, Any] | None:
    """只读读取采用记录；缺失、损坏、非对象一律返回 None（由调用方决定动作）。"""

    target = project_root / ".opencoding" / "adoptions" / (session_id + ".json")
    if not target.is_file():
        return None
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return record if isinstance(record, Mapping) else None


def _plan_override_from_adoption(project_root: Path, session_id: str,
                                 adoption: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """CP5 §4.2：已确认 AI 决策成为正式计划后，任何重算都必须回到同一计划来源。

    采用记录里带 ``plan_source.kind == confirmed_ai_evaluation`` 时，展示、漂移核对、
    恢复后重算都按 ``plansource.plan_input_for_choice`` 重建 override——与采用时
    逐字节同源；否则一律退回规则计划（不得凭空假设 AI 决策）。
    """

    if adoption is None:
        adoption = _adoption_record(project_root, session_id)
    if not isinstance(adoption, Mapping):
        return None
    source = adoption.get("plan_source")
    if not isinstance(source, Mapping) or source.get("kind") != "confirmed_ai_evaluation":
        return None
    choice = str(source.get("ai_choice") or "").strip()
    if not choice:
        return None
    view = source.get("ai_view") if isinstance(source.get("ai_view"), Mapping) else {}
    session = read_session_snapshot(project_root, session_id)
    facts = list_facts(project_root, session_id)
    rule_recommendation = build_recommendation(session, constraints=derive_fact_constraints(facts))
    return plansource.plan_input_for_choice(rule_recommendation, choice, view)


def current_plan_source(root: str | Path, session_id: str) -> dict[str, Any]:
    """对外说明当前计划来源：规则 / 已确认 AI 决策，并给出是否仍可用的结论。"""

    project_root = _root_path(root)
    adoption = _adoption_record(project_root, session_id)
    if not isinstance(adoption, Mapping):
        return {"kind": "rule", "adopted": False, "usable": True,
                "note": "尚未采用计划，当前展示的是规则草案"}
    source = adoption.get("plan_source") if isinstance(adoption.get("plan_source"), Mapping) else {}
    kind = str(source.get("kind") or "rule")
    if kind != "confirmed_ai_evaluation":
        return {"kind": "rule", "adopted": True, "usable": True,
                "note": "已采用规则草案计划"}
    result = {"kind": kind, "adopted": True,
              "evaluation_id": source.get("evaluation_id"),
              "ai_choice": source.get("ai_choice")}
    override = _plan_override_from_adoption(project_root, session_id, adoption)
    if override is None:
        result.update(usable=False, reason="采用记录缺少可复现的 AI 决策来源")
        return result
    facts = list_facts(project_root, session_id)
    conflict = plansource.hard_constraint_conflict(derive_fact_constraints(facts), result["ai_choice"])
    if conflict:
        result.update(usable=False, conflicting_constraint=conflict[0], effect=conflict[1],
                      reason="已登记硬约束禁止该 AI 首选，须重新评估")
        return result
    result.update(usable=True, note="正式计划由已确认 AI 决策派生")
    return _json_value(result)


def _executor_mapping(evaluated: Mapping[str, Any]) -> dict[str, Any]:
    """Create the explicit producer-side mapping for the supported offline executor."""

    recommendation = evaluated["recommendation"]
    project = recommendation.get("project", {})
    text = " ".join(
        str(project.get(key) or "")
        for key in ("goal", "outcome")
    )
    platform = (recommendation.get("platforms") or {}).get("primary")
    plan = evaluated["task_plan"]
    plan_ids = {task.get("id") for task in plan.get("tasks", []) if isinstance(task, Mapping)}
    if platform != "cli" or not ("借" in text and "还" in text):
        return {
            "schema_version": "1.0",
            "executor_id": None,
            "supported": False,
            "reason": "当前计划没有已支持的单机借还登记离线执行器。",
        }
    # F02：硬约束参与采用判断；禁止独立命令行应用或要求在已有网站内增量修改时，
    # 单机借还离线执行器不再被当作已支持，避免用旧计划继续错误派发。
    binding_constraints = [
        item for item in (evaluated.get("fact_consideration") or [])
        if isinstance(item, Mapping) and item.get("applied") is True
        and item.get("kind") in {"no_standalone_cli", "web_only_incremental"}
    ]
    if binding_constraints:
        return {
            "schema_version": "1.0",
            "executor_id": None,
            "supported": False,
            "reason": "已确认硬约束禁止独立命令行应用或要求在已有网站内增量修改，当前没有已支持的离线执行器。",
        }
    implement_id = "implement-scenario-1"
    verify_id = "verify-scenario-1"
    if implement_id not in plan_ids or verify_id not in plan_ids:
        return {
            "schema_version": "1.0",
            "executor_id": None,
            "supported": False,
            "reason": "借还计划尚未形成可执行的实现与验证节点。",
        }
    from .autorun import LENDREG_SCENARIO

    scenario_digest = _digest({
        "goal": LENDREG_SCENARIO["goal"],
        "tasks": [dict(item) for item in LENDREG_SCENARIO["tasks"]],
    })
    implementation_tasks = {
        "d01-models", "d02-storage", "d03-add", "d04-lend",
        "d06-return", "d07-list", "d08-chinese-entry",
    }
    bindings = {
        task["task_id"]: {
            "plan_task_id": implement_id if task["task_id"] in implementation_tasks else verify_id,
            "depends_on": list(task["depends_on"]),
            "acceptance": list(task["acceptance"]),
            "allowed_outputs": list(task["outputs"]),
        }
        for task in LENDREG_SCENARIO["tasks"]
    }
    return {
        "schema_version": "1.0",
        "executor_id": "lendreg",
        "supported": True,
        "scenario_id": "lendreg",
        "scenario_digest": scenario_digest,
        "plan_task_ids": [implement_id, verify_id],
        "task_bindings": bindings,
    }


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

    by_id = {item["id"]: item for item in QUESTION_DEFINITIONS}
    ids: list[str] = []
    if not _settled(session, "audience"):
        ids.append("audience")
    if not _settled(session, "platform"):
        ids.append("platform")
    if _settled(session, "audience") and not _settled(session, "outcome"):
        ids.append("outcome")
    if _settled(session, "audience") and _settled(session, "outcome") and _settled(session, "platform"):
        ids.extend(item_id for item_id in _CAPABILITY_IDS if not _settled(session, item_id))
    result: list[dict[str, Any]] = []
    for question_id in ids:
        item = deepcopy(by_id[question_id])
        item["status"] = _question_status(session, question_id)
        result.append(item)
    return result


def _graph(session: dict[str, Any], facts: list[Mapping[str, Any]] | None = None,
           recommendation_override: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    """F02：事实先于判断参与图生成；返回方案、任务图、文档与事实处置痕迹。

    CP5 §4.2：``recommendation_override`` 传入**已确认 AI 决策**时，它与单纯规则判断
    走同一条路线生成任务图/文档/快照，保证计划来源唯一；规则版本只作为约束与对照，
    不再被当作正式执行目标。
    """

    constraints = derive_fact_constraints(facts)
    rule_recommendation = build_recommendation(session, constraints=constraints)
    recommendation = dict(recommendation_override) if recommendation_override is not None \
        else rule_recommendation
    task_plan = build_task_plan(recommendation)
    documents = render_documents(recommendation, task_plan)
    _platform, consideration = evaluate_fact_constraints(session, constraints)
    return recommendation, task_plan, documents, consideration


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


def _view(root: Path, session: dict[str, Any], *, include_preview: bool,
          recommendation_override: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """CP5 §4.2：展示层与执行层必须同源。

    会话一旦采用过**已确认 AI 决策**派生的正式计划，此后所有 view/文档/计划摘要
    都按同一来源重算；否则展示规则草案、执行 AI 计划的自相矛盾会再次出现。
    """

    if recommendation_override is None:
        recommendation_override = _plan_override_from_adoption(root, session["id"])
    recommendation, task_plan, documents, _consideration = _graph(
        session, list_facts(root, session["id"]),
        recommendation_override=recommendation_override)
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


def session_view(root: str | Path, session_id: str, *, include_preview: bool = False,
                 recommendation_override: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """读取会话视图。

    CP5 §4.2：``recommendation_override`` 传入时按它生成 view（AI 计划同源展示）；
    不传时自动沿用本会话已采用的计划来源，避免展示与执行各说一套。
    """

    project_root = _root_path(root)
    try:
        session = read_session_snapshot(project_root, session_id)
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise ServiceError("session_read_failed", sanitize_text(str(exc))) from exc
    try:
        return _view(project_root, session, include_preview=include_preview,
                     recommendation_override=recommendation_override)
    except (TypeError, ValueError) as exc:
        raise ServiceError("graph_invalid", sanitize_text(str(exc))) from exc


def _input_snapshot(session: Mapping[str, Any], recommendation: Mapping[str, Any], decision: Mapping[str, Any], task_plan: Mapping[str, Any], consideration: list[Mapping[str, Any]]) -> dict[str, Any]:
    """F02：固定本次采用所依据的有效输入快照（会话、需求、事实、方案、计划、痕迹）。"""

    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": session.get("id"),
        "revision": session.get("revision"),
        "requirement_digest": _digest({
            "answers": session.get("answers", {}),
            "requirements": session.get("requirements", {}),
        }),
        "facts_digest": _digest(decision.get("facts", [])),
        "recommendation_digest": _digest(recommendation),
        "plan_digest": _digest(task_plan),
        "fact_consideration_digest": _digest([dict(item) for item in consideration]),
    }


def evaluate_session(root: str | Path, session_id: str, *,
                     recommendation_override: Mapping[str, Any] | None = None,
                     use_plan_source: bool = True) -> dict[str, Any]:
    """Evaluate recorded answers and facts, then return the validated plan to adopt.

    F02：相关用户事实与授权仓库硬事实在判断与任务图生成之前读取并参与，
    不再只在决策展示里附加。
    CP5 §4.2：``recommendation_override`` 为**用户已确认的 AI 决策**时，任务图、
    文档、快照与 plan_digest 全部由它派生（唯一计划来源），规则判断只作约束/兜底。
    ``use_plan_source`` 为真（默认）时，本会话若已采用过 AI 决策派生的正式计划，
    本次评估按同一来源重算——展示、执行、漂移核对必须是同一个计划；需要纯规则
    对照（如计算 AI 与规则的差异）时必须显式传 ``use_plan_source=False``。
    """

    project_root = _root_path(root)
    try:
        override = recommendation_override
        if override is None and use_plan_source:
            override = _plan_override_from_adoption(project_root, session_id)
        session = read_session_snapshot(project_root, session_id)
        facts = list_facts(project_root, session_id)
        recommendation, task_plan, _documents, consideration = _graph(
            session, facts, recommendation_override=override)
        decision = build_decision_record(recommendation, facts, consideration)
        plan_result = validate_task_plan(task_plan)
        if not plan_result["valid"]:
            raise ServiceError("evaluation_plan_invalid", "评估生成的任务图未通过结构校验")
        snapshot = _input_snapshot(session, recommendation, decision, task_plan, consideration)
        return _json_value({
            "schema_version": SCHEMA_VERSION,
            "root": str(project_root),
            "session_id": session_id,
            "revision": session["revision"],
            "recommendation": recommendation,
            "decision_record": decision,
            "task_plan": task_plan,
            "adopted_plan_digest": _digest(task_plan),
            "plan_validation": plan_result,
            "fact_consideration": [dict(item) for item in consideration],
            "input_snapshot": snapshot,
            "input_digest": _digest(snapshot),
        })
    except FactError as exc:
        raise ServiceError("evaluation_facts_" + exc.code, sanitize_text(str(exc))) from exc
    except (FileNotFoundError, TypeError, ValueError) as exc:
        if isinstance(exc, ServiceError):
            raise
        raise ServiceError("evaluation_rejected", sanitize_text(str(exc))) from exc


def adopt_evaluation_plan(root: str | Path, session_id: str, *,
                          recommendation_override: Mapping[str, Any] | None = None,
                          extras: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Persist an explicit, revalidated adoption of the current evaluated plan.

    ``recommendation_override`` 传入已确认 AI 决策时，落盘的 recommendation/
    task_plan/plan_digest/executor_mapping 全部来自它（同一计划来源）；
    ``extras`` 用于把计划来源与对照材料一并冻结（不删改原规则事实）。
    """

    project_root = _root_path(root)
    evaluated = evaluate_session(project_root, session_id,
                                 recommendation_override=recommendation_override)
    adoption_dir = project_root / ".opencoding" / "adoptions"
    adoption_dir.mkdir(parents=True, exist_ok=True)
    mapping = _executor_mapping(evaluated)
    record = {
        "schema_version": SCHEMA_VERSION,
        "session_id": evaluated["session_id"],
        "revision": evaluated["revision"],
        "plan_digest": evaluated["adopted_plan_digest"],
        "facts_digest": _digest(evaluated["decision_record"].get("facts", [])),
        "recommendation": evaluated["recommendation"],
        "decision_input_digest": _digest({
            "recommendation": evaluated["recommendation"],
            "decision_record": evaluated["decision_record"],
        }),
        "task_plan": evaluated["task_plan"],
        "decision_record": evaluated["decision_record"],
        "executor_mapping": mapping,
        "plan_validation": evaluated["plan_validation"],
        "validated": bool(evaluated["plan_validation"].get("valid")),
        # F02：明确采用时的有效输入快照（会话、需求摘要、所用事实摘要、计划内容、
        # 事实处置痕迹与映射依据），供运行前与接续边界核对是否发生漂移。
        "input_snapshot": evaluated["input_snapshot"],
        "input_digest": evaluated["input_digest"],
        "mapping_digest": _digest(mapping),
        "adopted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if extras:
        record.update({str(key): value for key, value in extras.items()})
    if not record["validated"]:
        raise ServiceError("evaluation_plan_invalid", "评估计划未通过校验，不能采用")
    target = adoption_dir / (session_id + ".json")
    temporary = target.with_name("." + target.name + ".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)
    return _json_value({"status": "adopted", "adoption": record, "evaluation": evaluated})


_DRIFT_FIELDS = (
    "revision",
    "requirement_digest",
    "facts_digest",
    "recommendation_digest",
    "plan_digest",
    "fact_consideration_digest",
)


def adoption_input_status(root: str | Path, session_id: str) -> dict[str, Any]:
    """F02：只读核对采用记录中的有效输入与当前输入是否仍然一致。

    采用记录内部自洽不是充分条件；相关输入漂移时返回 drift 字段，调用方应
    暂停旧计划派发并要求重新评估/明确采用，不得静默覆盖旧计划。
    """

    project_root = _root_path(root)
    adoption_path = project_root / ".opencoding" / "adoptions" / (session_id + ".json")
    adoption = _adoption_record(project_root, session_id)
    plan_source = adoption.get("plan_source") if isinstance(adoption, Mapping) else None
    # CP5 §4.2：AI 决策派生计划必须与采用时同源重算，否则"AI 计划 vs 规则核对"
    # 会永远漂移（这是 §6 假通过的另一种形态：把规则结果当 AI 结果核对）。
    override = _plan_override_from_adoption(project_root, session_id, adoption)
    plan_source_conflict: dict[str, Any] | None = None
    if isinstance(plan_source, Mapping) and plan_source.get("kind") == "confirmed_ai_evaluation":
        facts = list_facts(project_root, session_id)
        conflict = plansource.hard_constraint_conflict(
            derive_fact_constraints(facts), str(plan_source.get("ai_choice") or ""))
        if conflict:
            plan_source_conflict = {"constraint": conflict[0], "effect": conflict[1]}
    if adoption is None and adoption_path.is_file():
        # 文件存在但不可读/不是对象：必须失败可见，不能当作"未采用"放过去
        raise ServiceError("adoption_read_failed", "采用记录不可读或不是对象")
    if not isinstance(adoption, Mapping):
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "adopted": False,
            "matches": False,
            "drift": [],
            "input_snapshot_missing": True,
            "current": None,
            "adopted_snapshot": None,
            "required_action": "尚未采用计划；请先评估并明确采用。",
        }
    current = evaluate_session(project_root, session_id, recommendation_override=override)
    adopted_snapshot = adoption.get("input_snapshot")
    if not isinstance(adopted_snapshot, Mapping):
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "adopted": True,
            "matches": False,
            "drift": [],
            "input_snapshot_missing": True,
            "current": current["input_snapshot"],
            "adopted_snapshot": None,
            "required_action": "采用记录缺少有效输入快照；请重新评估并明确采用。",
        }
    drift = [name for name in _DRIFT_FIELDS if current["input_snapshot"].get(name) != adopted_snapshot.get(name)]
    mapping_drift = (adoption.get("mapping_digest") != _digest(adoption.get("executor_mapping")))
    if mapping_drift:
        drift.append("executor_mapping")
    required_action = "" if not drift else "相关有效输入已变化：暂停旧计划派发，需重新评估并明确采用后继续。"
    if plan_source_conflict:
        drift.append("plan_source_conflict")
        required_action = ("已登记硬约束(" + plan_source_conflict["constraint"] + ")禁止该已确认 AI 首选："
                           + "旧计划不可继承，须重新评估并明确采用。")
    status = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "adopted": True,
        "matches": not drift,
        "drift": drift,
        "input_snapshot_missing": False,
        "current": current["input_snapshot"],
        "adopted_snapshot": dict(adopted_snapshot),
        "current_mapping": _executor_mapping(current),
        "adopted_mapping": adoption.get("executor_mapping"),
        "plan_source": _json_value(dict(plan_source)) if isinstance(plan_source, Mapping) else {"kind": "rule"},
        "plan_source_conflict": plan_source_conflict,
        "required_action": required_action,
    }
    return _json_value(status)


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
    """Return scheduler and autonomous-run status through one zero-write path."""

    project_root = _root_path(root)
    from . import autorun

    try:
        result = _json_value(read_snapshot(project_root, task_id))
        autonomous = autorun.read_status_snapshot(project_root, task_id)
        if autonomous["runs"]:
            result["status"] = "ready"
            result["tasks"] = list(result.get("tasks", [])) + autonomous["tasks"]
            result["runs"] = list(result.get("runs", [])) + autonomous["runs"]
            result["autonomous_runs"] = autonomous["runs"]
            result["events"] = autonomous["events"]
        return _json_value(result)
    except autorun.AutorunError as exc:
        raise ServiceError("execution_status_" + exc.code, sanitize_text(str(exc))) from exc
    except SchedulerSnapshotError as exc:
        raise ServiceError("execution_status_" + exc.code, sanitize_text(str(exc))) from exc
    except (TypeError, ValueError) as exc:
        raise ServiceError("execution_status_rejected", sanitize_text(str(exc))) from exc


def cancel_autonomous_run(root: str | Path, run_id: str, *, reason: str) -> dict[str, Any]:
    project_root = _root_path(root)
    from . import autorun

    try:
        return _json_value(autorun.cancel_run(project_root, run_id, reason=reason))
    except (autorun.AutorunError, OSError, TypeError, ValueError) as exc:
        raise ServiceError(getattr(exc, "code", "autorun_cancel_rejected"), sanitize_text(str(exc))) from exc


def rollback_autonomous_run(
    root: str | Path, run_id: str, task_id: str | None = None
) -> dict[str, Any]:
    project_root = _root_path(root)
    from . import autorun

    try:
        return _json_value(autorun.rollback_task_files(project_root, run_id, task_id))
    except (autorun.AutorunError, OSError, TypeError, ValueError) as exc:
        raise ServiceError(getattr(exc, "code", "autorun_rollback_rejected"), sanitize_text(str(exc))) from exc


def query_autonomous_run(root: str | Path, run_id: str) -> dict[str, Any]:
    project_root = _root_path(root)
    from . import autorun

    try:
        return _json_value(autorun.query_saved_results(project_root, run_id))
    except (autorun.AutorunError, OSError, TypeError, ValueError) as exc:
        raise ServiceError(getattr(exc, "code", "autorun_query_rejected"), sanitize_text(str(exc))) from exc


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


def approve_preview(preview: Mapping[str, Any], *, expires_in_seconds: int = 300) -> dict[str, Any]:
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
            "targets": targets,
            "action": action,
            "expires_at": expiry.isoformat().replace("+00:00", "Z"),
            "approved": True,
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


def _validate_approval(root: Path, approval: Mapping[str, Any]) -> None:
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


def apply_approved(root: str | Path, approval: Mapping[str, Any]) -> dict[str, Any]:
    project_root = _root_path(root)
    try:
        _validate_approval(project_root, approval)
        with session_write_lock(project_root):
            current = read_session_snapshot(project_root, approval["session_id"])
            if current["revision"] != approval["revision"]:
                return {"status": "stale", "reason_codes": ["session_revision_stale"], "revision": current["revision"]}
            recommendation, task_plan, documents, _consideration = _graph(current, list_facts(project_root, current["id"]))
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
    "SCHEMA_VERSION",
    "ServiceError",
    "apply_approved",
    "adopt_evaluation_plan",
    "as_json",
    "approve_preview",
    "cancel_autonomous_run",
    "create_session",
    "derive_frontier",
    "execution_status",
    "evaluate_session",
    "list_sessions",
    "preview_session",
    "query_autonomous_run",
    "rollback",
    "rollback_autonomous_run",
    "session_view",
    "submit_answer",
]
