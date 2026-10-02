"""事实来源分层与会话级决策记录。

任何影响方案的事实都带来源类型：用户明确输入、授权仓库事实、AI 推断、
临时假设。AI 补出的答案不会被记成用户事实；假设可撤销且带影响说明。
事实簿按会话独立保存，缺失时等价于空表；旧会话数据不被改写。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid
from typing import Any, Mapping

from .documents import validate_recommendation
from .safety import _root_path, sanitize_text


FACT_SCHEMA_VERSION = "1.0"
DECISION_SCHEMA_VERSION = "1.0"
SOURCE_TYPES = ("user", "repository", "ai", "assumption")
SOURCE_TYPE_LABELS = {
    "user": "用户明确输入",
    "repository": "仓库或授权文档事实",
    "ai": "AI 推断",
    "assumption": "临时假设",
}
_FACT_FIELDS = {"fact_id", "content", "source_type", "source_ref", "confirmed", "scope", "updated_at"}


class FactError(ValueError):
    """A fail-closed fact-book error with a stable machine code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _facts_path(root: Path, session_id: str) -> Path:
    if not isinstance(session_id, str) or not session_id.strip():
        raise FactError("fact_session_invalid", "会话编号无效")
    safe = session_id.replace("\\", "/").split("/")[-1]
    if not safe or safe in {".", ".."}:
        raise FactError("fact_session_invalid", "会话编号无效")
    return root / ".opencoding" / "facts" / f"{safe}.json"


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _validate_fact(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _FACT_FIELDS:
        raise FactError("fact_fields_invalid", "事实字段不完整或包含未知字段")
    if value["source_type"] not in SOURCE_TYPES:
        raise FactError("fact_source_invalid", "事实来源类型无效")
    if not isinstance(value["content"], str) or not value["content"].strip():
        raise FactError("fact_content_invalid", "事实内容不能为空")
    if not isinstance(value["source_ref"], str):
        raise FactError("fact_source_invalid", "事实来源引用必须是字符串")
    if type(value["confirmed"]) is not bool:
        raise FactError("fact_state_invalid", "事实确认状态必须是布尔值")
    if not isinstance(value["scope"], str) or not value["scope"]:
        raise FactError("fact_scope_invalid", "事实适用范围无效")
    datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00"))
    return dict(value)


def _load_book(root: Path, session_id: str) -> dict[str, Any]:
    path = _facts_path(root, session_id)
    if not path.exists():
        return {"schema_version": FACT_SCHEMA_VERSION, "session_id": session_id, "facts": []}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise FactError("fact_store_invalid", sanitize_text(str(exc))) from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "session_id", "facts"}:
        raise FactError("fact_store_invalid", "事实簿结构无效")
    if value["schema_version"] != FACT_SCHEMA_VERSION:
        raise FactError("fact_version_unsupported", "事实簿版本不受支持")
    if value["session_id"] != session_id:
        raise FactError("fact_session_mismatch", "事实簿会话不匹配")
    facts = value["facts"]
    if not isinstance(facts, list):
        raise FactError("fact_store_invalid", "事实列表无效")
    return {"schema_version": value["schema_version"], "session_id": session_id, "facts": [_validate_fact(item) for item in facts]}


def add_fact(
    root: str | Path,
    session_id: str,
    *,
    content: str,
    source_type: str,
    source_ref: str = "",
    confirmed: bool | None = None,
    scope: str = "project",
) -> dict[str, Any]:
    """追加一条带来源的事实；AI 推断默认未确认，用户事实默认已确认。"""

    project_root = _root_path(root)
    if source_type not in SOURCE_TYPES:
        raise FactError("fact_source_invalid", "事实来源类型无效")
    if not isinstance(content, str) or not content.strip():
        raise FactError("fact_content_invalid", "事实内容不能为空")
    if not isinstance(scope, str) or not scope.strip():
        raise FactError("fact_scope_invalid", "事实适用范围无效")
    if confirmed is None:
        confirmed = source_type in {"user", "repository"}
    if source_type in {"ai", "assumption"} and confirmed:
        raise FactError("fact_confirmation_invalid", "AI 推断或假设不能默认记为已确认事实")
    book = _load_book(project_root, session_id)
    clean = sanitize_text(content.strip())
    for existing in book["facts"]:
        if existing["content"] == clean and existing["source_type"] == source_type:
            return dict(existing)
    fact = {
        "fact_id": "fact-" + uuid.uuid4().hex,
        "content": clean,
        "source_type": source_type,
        "source_ref": sanitize_text(source_ref),
        "confirmed": bool(confirmed),
        "scope": sanitize_text(scope.strip()),
        "updated_at": _now(),
    }
    book["facts"].append(fact)
    _atomic_write_json(_facts_path(project_root, session_id), book)
    return dict(fact)


def list_facts(root: str | Path, session_id: str, *, source_type: str | None = None) -> list[dict[str, Any]]:
    """列出事实；没有事实簿时返回空表，绝不创建文件。"""

    project_root = _root_path(root)
    book = _load_book(project_root, session_id)
    facts = book["facts"]
    if source_type is not None:
        if source_type not in SOURCE_TYPES:
            raise FactError("fact_source_invalid", "事实来源类型无效")
        facts = [item for item in facts if item["source_type"] == source_type]
    return [dict(item) for item in facts]


def mark_fact_confirmed(root: str | Path, session_id: str, fact_id: str) -> dict[str, Any]:
    """用户确认一条事实（例如把合理假设升级为已确认事实）。"""

    project_root = _root_path(root)
    book = _load_book(project_root, session_id)
    for fact in book["facts"]:
        if fact["fact_id"] == fact_id:
            fact["confirmed"] = True
            fact["updated_at"] = _now()
            _atomic_write_json(_facts_path(project_root, session_id), book)
            return dict(fact)
    raise FactError("fact_unknown", "事实不存在")


def build_decision_record(
    recommendation: Mapping[str, Any],
    facts: list[Mapping[str, Any]] | None = None,
    consideration: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """从已校验的方案与事实簿派生可核查的决策记录（纯函数，不做 I/O）。

    F02：`consideration` 记录每条相关硬约束是否参与判断、产生了什么影响或
    为什么没有改变方案，便于复核“事实已采用”而不是只被展示。
    """

    validate_recommendation(recommendation)
    fact_list = [dict(item) for item in (facts or [])]
    for fact in fact_list:
        _validate_fact(fact)
    consideration_rows = []
    for item in consideration or []:
        if not isinstance(item, Mapping):
            raise FactError("fact_consideration_invalid", "事实处置痕迹必须是对象")
        consideration_rows.append({
            "fact_id": item.get("fact_id"),
            "kind": item.get("kind"),
            "source_type": item.get("source_type"),
            "applied": bool(item.get("applied")),
            "effect": str(item.get("effect") or ""),
            "explanation": str(item.get("explanation") or ""),
        })
    stack = recommendation["stack"]
    capabilities = {item["id"]: item for item in recommendation["capabilities"]}
    unresolved = list(recommendation["unresolved"]) + list(recommendation["platforms"]["unresolved"])
    contradictions: list[str] = []
    if not recommendation["project"]["outcome"] and recommendation["status"] == "ready":
        contradictions.append("ready 状态缺少业务结果，需重新评估。")
    assumptions = [item for item in fact_list if item["source_type"] == "assumption"]
    ai_inferences = [item for item in fact_list if item["source_type"] == "ai" and not item["confirmed"]]
    return {
        "schema_version": DECISION_SCHEMA_VERSION,
        "session_id": recommendation["session_id"],
        "revision": recommendation["revision"],
        # F02：事实本体进入决策记录，事实摘要才能随事实变化（此前摘要恒为空表）。
        "facts": fact_list,
        "business_goal": recommendation["project"]["goal"],
        "hard_constraints": [
            "不连接未授权的外部服务、凭据或真实生产数据。",
            "外部激活、部署、发布与付款必须单独确认。",
        ],
        "chosen": {
            "platform": recommendation["platforms"]["primary"],
            "stack": {key: stack[key]["technology"] for key in stack},
        },
        "reasoning": [
            {
                "item": "platform",
                "choice": recommendation["platforms"]["primary"],
                "reason": recommendation["platforms"]["reason"],
                "evidence": "answers.platform",
                "confidence": recommendation["platforms"]["confidence"],
            },
            *[
                {
                    "item": capability_id,
                    "choice": entry["need"],
                    "reason": entry["reason"],
                    "evidence": "answers." + entry["source"],
                }
                for capability_id, entry in sorted(capabilities.items())
            ],
        ],
        "alternatives": {
            key: stack[key]["alternatives"] for key in stack if stack[key]["alternatives"]
        },
        "assumptions": [item["content"] for item in assumptions],
        "ai_inferences_unconfirmed": [item["content"] for item in ai_inferences],
        "unknowns": unresolved,
        "contradiction_check": {
            "consistent": not contradictions and not unresolved,
            "contradictions": contradictions,
        },
        "validation_plan": [
            "用离线单元测试与任务图校验验证结构。",
            "用真实运行证据验证业务结果；模型自评不算验收。",
        ],
        "reevaluation_triggers": [
            "平台、数据范围或预算答案发生变化。",
            "出现 unresolved 未解决问题或新的硬约束。",
            "验证失败且原因不属于当前假设范围。",
        ],
        "fact_consideration": consideration_rows,
        "user_decisions_required": unresolved,
        "auto_continuable": not unresolved and not contradictions,
    }


__all__ = [
    "DECISION_SCHEMA_VERSION",
    "FACT_SCHEMA_VERSION",
    "SOURCE_TYPES",
    "SOURCE_TYPE_LABELS",
    "FactError",
    "add_fact",
    "build_decision_record",
    "list_facts",
    "mark_fact_confirmed",
]
