"""统一中文出口：稳定错误码映射、中断来源诊断与标准进度文案。

本模块只做展示与事件记录，不参与控制流判断；程序内部仍读取结构化
错误码与状态值，不解析中文文案。中断记录仅写入真实发生的暂停点，
只读查询路径（--status / --preview）不会触发任何写入。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .safety import _root_path, sanitize_text


SCHEMA_VERSION = "1.0"

SOURCE_LAYERS = ("product", "execution_ai", "tool", "os", "organization", "unknown")
SOURCE_LAYER_LABELS = {
    "product": "产品自身",
    "execution_ai": "执行 AI 工作要求",
    "tool": "执行工具",
    "os": "操作系统",
    "organization": "组织策略",
    "unknown": "未知来源",
}

INTERRUPTION_CATEGORIES = (
    "business_question",
    "new_authorization",
    "repeat_question",
    "tool_forced",
    "user_pause",
    "user_rejected",
    "system_error",
)
CATEGORY_LABELS = {
    "business_question": "必要业务问题",
    "new_authorization": "必要新增授权",
    "repeat_question": "不必要重复询问",
    "tool_forced": "工具或系统强制确认",
    "user_pause": "用户主动暂停",
    "user_rejected": "用户拒绝",
    "system_error": "异常暂停",
}

ERROR_MESSAGES = {
    "invalid_root": "无法读取执行状态：项目根目录无效。",
    "execution_status_invalid_task_id": "无法读取执行状态：任务编号无效。",
    "execution_status_database_busy": "读取执行状态失败：本地状态正在被占用，请稍后重试。",
    "execution_status_unsafe_journal_state": "无法安全读取执行状态：本地状态正在更新。",
    "execution_status_unsupported_platform": "当前平台不支持只读执行状态查询。",
    "execution_status_unsupported_schema": "无法读取执行状态：本地状态版本不受支持。",
    "execution_status_database_unavailable": "无法读取执行状态：本地状态不可用。",
    "execution_status_autorun_status_store_unsafe": "无法安全读取自主运行状态：状态文件身份不安全。",
    "execution_status_autorun_status_store_invalid": "无法读取自主运行状态：状态账本损坏或版本不受支持。",
    "status_read_failed": "读取执行状态失败，请检查本地状态后重试。",
    "session_read_failed": "无法读取本地会话，会话可能不存在或已损坏。",
    "session_save_failed": "保存会话失败，本次修改未写入。",
    "session_lock_busy": "本地会话正在被其他操作占用，请稍后重试。",
    "session_revision_stale": "会话版本已变化，本次修改未写入，请重新查看会话。",
    "answer_rejected": "回答被拒绝：内容或会话状态无效。",
    "approval_expired": "本次批准的执行凭据已过期，需要重新预览并确认。",
    "approval_not_granted": "本次操作没有获得明确批准，已阻止。",
    "approval_safety_blocked": "本次写入未通过本地安全校验，已阻止。",
    "preview_content_mismatch": "方案内容与当前本地状态不一致，需要重新生成预览。",
    "apply_rejected": "应用变更失败：状态或文件已变化。",
    "rollback_rejected": "回滚失败：事务不存在或证据不完整。",
    "non_json_state": "服务状态不可序列化。",
}

_PRODUCT_CODE_PREFIXES = (
    "execution_status_",
    "session_",
    "approval_",
    "preview_",
    "answer_",
    "apply_",
    "rollback_",
    "graph_invalid",
    "non_json_state",
)
_TOOL_CODE_MARKERS = ("lock", "busy", "database")


def error_message(code: str) -> str:
    """把稳定错误码映射为中文说明；未知码返回通用中文兜底。"""

    if not isinstance(code, str) or not code:
        return "操作失败，请检查本地状态后重试。"
    return ERROR_MESSAGES.get(code, "操作失败（" + sanitize_text(code) + "），请检查本地状态后重试。")


def classify_source(reason_code: Any, exc: BaseException | None = None) -> str:
    """按可核实的来源把暂停归层，不把未知来源编造为已查明的限制。"""

    code = reason_code if isinstance(reason_code, str) else ""
    if code:
        if any(code.startswith(prefix) for prefix in _PRODUCT_CODE_PREFIXES):
            return "product"
        if any(marker in code for marker in _TOOL_CODE_MARKERS):
            return "tool"
        if code in {"invalid_root", "os_error", "permission_denied"}:
            return "os"
        return "unknown"
    if isinstance(exc, OSError):
        return "os"
    if exc is not None:
        return "tool"
    return "unknown"


def render_progress(judgment: str, basis: str, completed: str, continuing: str, needs_user: str = "暂无") -> str:
    """标准中文进度块：当前判断 / 依据 / 已完成 / 正在继续 / 需要你处理。"""

    lines = (
        "当前判断：" + sanitize_text(judgment),
        "依据：" + sanitize_text(basis),
        "已完成：" + sanitize_text(completed),
        "正在继续：" + sanitize_text(continuing),
        "需要你处理：" + sanitize_text(needs_user),
    )
    return "\n".join(lines)


def render_pause(reason: str, suggestion: str, decision_needed: str) -> str:
    """真正暂停时的中文文案：原因 / 建议 / 需要用户决定。"""

    lines = (
        "当前暂停原因：" + sanitize_text(reason),
        "我的建议：" + sanitize_text(suggestion),
        "需要你决定：" + sanitize_text(decision_needed),
    )
    return "\n".join(lines)


def _ledger_path(root: Path) -> Path:
    return root / ".opencoding" / "interruptions.jsonl"


def record_interruption(
    root: str | Path,
    *,
    category: str,
    action: str,
    reason: str,
    reason_code: str = "",
    source_layer: str | None = None,
    authorization: str | None = None,
    auto_path: str | None = None,
    new_risk: str | None = None,
) -> dict[str, Any]:
    """在真实暂停点追加一条脱敏中断记录；只读路径不会调用本函数。"""

    project_root = _root_path(root)
    if category not in INTERRUPTION_CATEGORIES:
        raise ValueError("unknown interruption category")
    if source_layer is not None and source_layer not in SOURCE_LAYERS:
        raise ValueError("unknown interruption source layer")
    layer = source_layer or classify_source(reason_code)
    event = {
        "schema_version": SCHEMA_VERSION,
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "category": category,
        "category_label": CATEGORY_LABELS[category],
        "source_layer": layer,
        "source_label": SOURCE_LAYER_LABELS[layer],
        "action": sanitize_text(action),
        "reason_code": sanitize_text(reason_code),
        "reason": sanitize_text(reason),
        "authorization": sanitize_text(authorization) if authorization else None,
        "auto_path": sanitize_text(auto_path) if auto_path else None,
        "new_risk": sanitize_text(new_risk) if new_risk else None,
    }
    metadata = project_root / ".opencoding"
    if metadata.exists() and not metadata.is_dir():
        raise ValueError(".opencoding must be a directory")
    metadata.mkdir(exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    handle = open(_ledger_path(project_root), "a", encoding="utf-8", newline="\n")
    try:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    return event


def read_interruptions(root: str | Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    """读取中断记录；没有记录时返回空表，绝不创建目录或文件。"""

    project_root = _root_path(root)
    ledger = _ledger_path(project_root)
    if not ledger.exists():
        return []
    events: list[dict[str, Any]] = []
    with open(ledger, "r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                events.append(json.loads(stripped))
    if limit is not None:
        return events[-limit:]
    return events


def interruption_stats(root: str | Path) -> dict[str, Any]:
    """按类别与来源层统计中断，供“不必要询问为零”等指标核对。"""

    events = read_interruptions(root)
    by_category = {category: 0 for category in INTERRUPTION_CATEGORIES}
    by_layer = {layer: 0 for layer in SOURCE_LAYERS}
    for event in events:
        category = event.get("category")
        layer = event.get("source_layer")
        if category in by_category:
            by_category[category] += 1
        if layer in by_layer:
            by_layer[layer] += 1
    return {
        "total": len(events),
        "by_category": by_category,
        "by_layer": by_layer,
        "unnecessary_questions": by_category["repeat_question"],
        "necessary_business_questions": by_category["business_question"],
        "necessary_new_authorizations": by_category["new_authorization"],
        "tool_forced_confirmations": by_category["tool_forced"],
    }


__all__ = [
    "CATEGORY_LABELS",
    "ERROR_MESSAGES",
    "INTERRUPTION_CATEGORIES",
    "SCHEMA_VERSION",
    "SOURCE_LAYERS",
    "SOURCE_LAYER_LABELS",
    "classify_source",
    "error_message",
    "interruption_stats",
    "read_interruptions",
    "record_interruption",
    "render_pause",
    "render_progress",
]
