"""Persisted acceptance gates that remain stable across restart and resume."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import uuid
from typing import Any

from .safety import _is_reparse, _reject_linked_ancestors, canonical_json, sha256_bytes


ACCEPTANCE_STATE_SCHEMA_VERSION = "opencoding-acceptance-state-v1"
_STATUSES = {"observed", "unverified", "blocked"}


class AcceptanceStateError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _root_path(root: str | os.PathLike[str] | Path) -> Path:
    path = Path(root)
    if not path.is_absolute() or path.is_symlink() or _is_reparse(path) or not path.is_dir():
        raise AcceptanceStateError("root_invalid", "验收状态 root 必须是绝对且未链接的目录")
    return path.resolve()


def _state_dir(root: Path) -> Path:
    metadata = root / ".opencoding"
    acceptance = metadata / "acceptance"
    _reject_linked_ancestors(metadata)
    for directory in (metadata, acceptance):
        if directory.exists() or directory.is_symlink():
            if directory.is_symlink() or _is_reparse(directory) or not directory.is_dir():
                raise AcceptanceStateError("state_path_unsafe", "验收状态目录不安全")
        else:
            try:
                directory.mkdir()
            except FileExistsError:
                pass
    _reject_linked_ancestors(acceptance)
    return acceptance


def _state_path(root: Path, state_id: str) -> Path:
    if not isinstance(state_id, str) or not state_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for ch in state_id):
        raise AcceptanceStateError("state_id_invalid", "验收状态 id 无效")
    path = _state_dir(root) / f"{state_id}.json"
    if path.is_symlink() or _is_reparse(path):
        raise AcceptanceStateError("state_path_unsafe", "验收状态文件是链接")
    return path


def _checks(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(report, dict) or not isinstance(report.get("checks"), list):
        raise AcceptanceStateError("report_invalid", "验收报告缺少 checks")
    values: dict[str, dict[str, Any]] = {}
    for item in report["checks"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in values:
            raise AcceptanceStateError("report_invalid", "验收报告 check id 无效或重复")
        status = item.get("status")
        if status not in _STATUSES or not isinstance(item.get("reason"), str) or not item["reason"]:
            raise AcceptanceStateError("report_invalid", "验收报告状态或原因无效")
        if status == "blocked" and item.get("human_gate", {}).get("required") is not True:
            raise AcceptanceStateError("report_invalid", "blocked check 必须有 human gate")
        values[item["id"]] = {"status": status, "reason": item["reason"], "human_gate": item.get("human_gate")}
    return values


def _status_digest(checks: dict[str, dict[str, Any]]) -> str:
    return sha256_bytes(canonical_json(checks))


def _write_state(path: Path, state: dict[str, Any]) -> None:
    payload = canonical_json(state) + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def initialize_acceptance_state(root: str | os.PathLike[str] | Path, report: dict[str, Any], *, state_id: str | None = None) -> dict[str, Any]:
    project_root = _root_path(root)
    checks = _checks(report)
    state_id = state_id or f"acceptance-{uuid.uuid4().hex}"
    state = {
        "schema_version": ACCEPTANCE_STATE_SCHEMA_VERSION,
        "state_id": state_id,
        "report_digest": sha256_bytes(canonical_json(report)),
        "checks": checks,
        "status_digest": _status_digest(checks),
        "resume_count": 0,
        "history": [{"event": "initialized", "status_digest": _status_digest(checks)}],
    }
    _write_state(_state_path(project_root, state_id), state)
    return state


def load_acceptance_state(root: str | os.PathLike[str] | Path, state_id: str) -> dict[str, Any]:
    project_root = _root_path(root)
    path = _state_path(project_root, state_id)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AcceptanceStateError("state_unreadable", "验收状态无法读取") from error
    if not isinstance(state, dict) or state.get("schema_version") != ACCEPTANCE_STATE_SCHEMA_VERSION or state.get("state_id") != state_id:
        raise AcceptanceStateError("state_invalid", "验收状态 schema 或 id 不匹配")
    checks = state.get("checks")
    if not isinstance(checks, dict) or state.get("status_digest") != _status_digest(checks):
        raise AcceptanceStateError("state_drifted", "验收状态摘要漂移")
    if any(item.get("status") not in _STATUSES for item in checks.values() if isinstance(item, dict)):
        raise AcceptanceStateError("state_invalid", "验收状态包含未知 status")
    return state


def resume_acceptance_state(root: str | os.PathLike[str] | Path, state_id: str, *, report: dict[str, Any] | None = None) -> dict[str, Any]:
    project_root = _root_path(root)
    state = load_acceptance_state(project_root, state_id)
    if report is not None and state["report_digest"] != sha256_bytes(canonical_json(report)):
        raise AcceptanceStateError("report_drifted", "恢复时验收报告摘要已改变")
    state["resume_count"] = int(state.get("resume_count", 0)) + 1
    state.setdefault("history", []).append({"event": "resumed", "resume_count": state["resume_count"], "status_digest": state["status_digest"]})
    _write_state(_state_path(project_root, state_id), state)
    return state


def require_observed(state: dict[str, Any], check_id: str) -> dict[str, Any]:
    checks = state.get("checks") if isinstance(state, dict) else None
    check = checks.get(check_id) if isinstance(checks, dict) else None
    if not isinstance(check, dict):
        raise AcceptanceStateError("check_unknown", f"验收 check 不存在: {check_id}")
    status = check.get("status")
    if status == "observed":
        return check
    if status == "blocked":
        raise AcceptanceStateError("acceptance_gate_blocked", f"{check_id} 被人工 Gate 阻断；不会消费授权或写入文件")
    raise AcceptanceStateError("acceptance_status_unverified", f"{check_id} 尚未验证；不会消费授权或写入文件")


__all__ = [
    "ACCEPTANCE_STATE_SCHEMA_VERSION",
    "AcceptanceStateError",
    "initialize_acceptance_state",
    "load_acceptance_state",
    "resume_acceptance_state",
    "require_observed",
]
