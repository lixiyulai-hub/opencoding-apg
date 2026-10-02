"""有限批次授权与每步短期凭据。

批次授权由用户在自主模式开始时一次性确认，绑定项目根、业务目标、允许
路径、动作类别与预算；每一步真正执行前，由程序核对批次授权并签发短期
凭据。凭据一次性使用、绑定任务与尝试编号、过期即拒绝；撤销与预算状态
持久化，重启不复活、不重置。模型或子任务不能自行签发、续期或改写。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from contextlib import contextmanager
import os
from pathlib import Path
import re
import unicodedata
import uuid
from typing import Any, Mapping

from .safety import _relative_parts, _root_path, canonical_json, sanitize_text, sha256_bytes


GRANT_SCHEMA_VERSION = "1.0"
CREDENTIAL_SCHEMA_VERSION = "1.0"
ACTION_KINDS = ("local_write", "local_run", "ai_request")
DEFAULT_GRANT_TTL_SECONDS = 8 * 3600
DEFAULT_STEP_TTL_SECONDS = 300
_GRANT_ID = re.compile(r"^grant-[0-9a-f]{32}$")
_CREDENTIAL_ID = re.compile(r"^cred-[0-9a-f]{32}$")
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_GRANT_FIELDS = {
    "schema_version", "grant_id", "root", "goal",
    "allowed_paths", "excluded_paths", "action_kinds", "data_scope",
    "budget", "budget_used", "concurrency",
    "issued_by", "issued_at", "expires_at",
    "revoked", "revoked_at", "revoke_reason",
    "policy_fingerprint",
}
_CREDENTIAL_FIELDS = {
    "schema_version", "credential_id", "grant_id", "root",
    "task_id", "attempt", "action_kind", "targets",
    "action_digest", "input_digest",
    "issued_at", "expires_at", "used", "used_at",
    "fingerprint",
}
_IMMUTABLE_GRANT_FIELDS = (
    "schema_version", "grant_id", "root", "goal",
    "allowed_paths", "excluded_paths", "action_kinds", "data_scope",
    "budget", "concurrency", "issued_by", "issued_at", "expires_at",
)


class GrantError(ValueError):
    """A fail-closed grant/credential error with a stable machine code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must carry a timezone")
    return parsed


def _canonical(value: Any) -> bytes:
    return canonical_json(value)


def _norm_path_prefix(value: str) -> str:
    parts = _relative_parts(value)
    return "/".join(parts)


def _normalize_prefixes(paths: Any, *, field: str) -> list[str]:
    if not isinstance(paths, (list, tuple)):
        raise GrantError(field + "_invalid", field + " 必须是路径列表")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in paths:
        try:
            prefix = _norm_path_prefix(item)
        except (TypeError, ValueError) as exc:
            raise GrantError(field + "_invalid", sanitize_text(str(exc))) from exc
        key = unicodedata.normalize("NFC", prefix).casefold()
        if key in seen:
            raise GrantError(field + "_invalid", field + " 含重复路径")
        seen.add(key)
        normalized.append(prefix)
    return sorted(normalized)


def _scope_fingerprint(grant: Mapping[str, Any]) -> str:
    payload = {field: grant[field] for field in _IMMUTABLE_GRANT_FIELDS}
    return sha256_bytes(_canonical(payload))


def _credential_fingerprint(credential: Mapping[str, Any]) -> str:
    payload = {field: credential[field] for field in _CREDENTIAL_FIELDS - {"used", "used_at", "fingerprint"}}
    return sha256_bytes(_canonical(payload))


def _grants_dir(root: Path) -> Path:
    return root / ".opencoding" / "grants"


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


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise GrantError("store_unavailable", sanitize_text(str(exc))) from exc
    if not isinstance(value, dict):
        raise GrantError("store_invalid", "授权记录不是有效对象")
    return value


def _validate_grant(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != _GRANT_FIELDS:
        raise GrantError("grant_fields_invalid", "批次授权字段不完整或包含未知字段")
    if value["schema_version"] != GRANT_SCHEMA_VERSION:
        raise GrantError("grant_version_unsupported", "批次授权版本不受支持")
    if not isinstance(value["grant_id"], str) or not _GRANT_ID.fullmatch(value["grant_id"]):
        raise GrantError("grant_identity_invalid", "批次授权编号无效")
    _parse_time(value["issued_at"])
    _parse_time(value["expires_at"])
    if not isinstance(value["goal"], str) or not value["goal"].strip():
        raise GrantError("grant_goal_invalid", "批次业务目标无效")
    if not isinstance(value["action_kinds"], list) or not value["action_kinds"]:
        raise GrantError("grant_kinds_invalid", "批次授权必须声明动作类别")
    if any(kind not in ACTION_KINDS for kind in value["action_kinds"]):
        raise GrantError("grant_kinds_invalid", "批次授权包含未知动作类别")
    if not isinstance(value["data_scope"], str) or not value["data_scope"]:
        raise GrantError("grant_scope_invalid", "批次数据范围无效")
    if not isinstance(value["revoked"], bool):
        raise GrantError("grant_state_invalid", "批次撤销状态无效")
    if value["policy_fingerprint"] != _scope_fingerprint(value):
        raise GrantError("grant_tampered", "批次授权内容被篡改")
    return dict(value)


def _validate_credential(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != _CREDENTIAL_FIELDS:
        raise GrantError("credential_fields_invalid", "凭据字段不完整或包含未知字段")
    if value["schema_version"] != CREDENTIAL_SCHEMA_VERSION:
        raise GrantError("credential_version_unsupported", "凭据版本不受支持")
    if not isinstance(value["credential_id"], str) or not _CREDENTIAL_ID.fullmatch(value["credential_id"]):
        raise GrantError("credential_identity_invalid", "凭据编号无效")
    _parse_time(value["issued_at"])
    _parse_time(value["expires_at"])
    if not isinstance(value["used"], bool):
        raise GrantError("credential_state_invalid", "凭据使用状态无效")
    if value["fingerprint"] != _credential_fingerprint(value):
        raise GrantError("credential_tampered", "凭据内容被篡改")
    return dict(value)


@contextmanager
def authorization_order(root: Path):
    """Serialize effective confirmation commits and official revocations across processes.

    SQLite's durable transaction commit is the activation point. Receipt files may
    be staged earlier, but a pending grant cannot dispatch until this commits.
    """
    directory = _grants_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "authorization-order.sqlite3", timeout=10)
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, grant_id TEXT NOT NULL, previous_grant_id TEXT, run_id TEXT)")
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _pending_confirmation_path(root: Path, grant_id: str) -> Path:
    return _grants_dir(root) / "pending-confirmations" / (grant_id + ".json")


def _confirmation_committed(root: Path, grant_id: str) -> bool:
    database = _grants_dir(root) / "authorization-order.sqlite3"
    if not database.exists():
        return False
    try:
        with sqlite3.connect(database) as connection:
            return connection.execute("SELECT 1 FROM events WHERE kind='confirmation' AND grant_id=?", (grant_id,)).fetchone() is not None
    except sqlite3.Error as exc:
        raise GrantError("store_unavailable", "确认提交记录不可读") from exc


def issue_batch_grant(
    root: str | Path,
    *,
    goal: str,
    allowed_paths: list[str],
    action_kinds: list[str],
    issued_by: str,
    excluded_paths: list[str] | None = None,
    data_scope: str = "synthetic-local",
    budget: Mapping[str, int] | None = None,
    ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
    pending_confirmation: bool = False,
) -> dict[str, Any]:
    """登记一次有限、有期、可撤销的批次授权；只有程序可调用。"""

    project_root = _root_path(root)
    if not isinstance(goal, str) or not goal.strip():
        raise GrantError("grant_goal_invalid", "批次业务目标不能为空")
    if not isinstance(issued_by, str) or not issued_by.strip():
        raise GrantError("grant_source_invalid", "批次授权必须绑定用户确认来源")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or ttl_seconds <= 0:
        raise GrantError("grant_ttl_invalid", "批次有效期无效")
    normalized_allowed = _normalize_prefixes(allowed_paths, field="allowed_paths")
    if not normalized_allowed:
        raise GrantError("allowed_paths_invalid", "批次授权必须声明允许路径")
    normalized_excluded = _normalize_prefixes(excluded_paths or [], field="excluded_paths")
    for kind in action_kinds:
        if kind not in ACTION_KINDS:
            raise GrantError("grant_kinds_invalid", "未知动作类别：" + sanitize_text(str(kind)))
    budget_value = {
        "max_ai_requests": 20,
        "max_repair_rounds": 6,
        "max_no_progress_rounds": 4,
    }
    if budget:
        for key in budget_value:
            candidate = budget.get(key)
            if candidate is not None:
                if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 0:
                    raise GrantError("grant_budget_invalid", "预算必须是未启用的或正整数")
                budget_value[key] = candidate
    issued = datetime.now(timezone.utc)
    grant: dict[str, Any] = {
        "schema_version": GRANT_SCHEMA_VERSION,
        "grant_id": "grant-" + uuid.uuid4().hex,
        "root": str(project_root),
        "goal": sanitize_text(goal.strip()),
        "allowed_paths": normalized_allowed,
        "excluded_paths": normalized_excluded,
        "action_kinds": sorted(set(action_kinds)),
        "data_scope": sanitize_text(data_scope),
        "budget": budget_value,
        "budget_used": {"ai_requests": 0, "repair_rounds": 0, "no_progress_rounds": 0},
        "concurrency": 1,
        "issued_by": sanitize_text(issued_by.strip()),
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(seconds=float(ttl_seconds))).isoformat().replace("+00:00", "Z"),
        "revoked": False,
        "revoked_at": None,
        "revoke_reason": None,
    }
    grant["policy_fingerprint"] = _scope_fingerprint(grant)
    if pending_confirmation:
        _atomic_write_json(_pending_confirmation_path(project_root, grant["grant_id"]), {"pending": True})
    _atomic_write_json(_grants_dir(project_root) / f"{grant['grant_id']}.json", grant)
    return dict(grant)


def load_grant(root: str | Path, grant_id: str, *, _allow_pending: bool = False) -> dict[str, Any]:
    project_root = _root_path(root)
    if not isinstance(grant_id, str) or not _GRANT_ID.fullmatch(grant_id):
        raise GrantError("grant_identity_invalid", "批次授权编号无效")
    path = _grants_dir(project_root) / f"{grant_id}.json"
    if not path.exists():
        raise GrantError("grant_unknown", "批次授权不存在")
    grant = _validate_grant(_read_json(path))
    # F05/R10：授权与项目根强绑定；原字节复制到其他根目录一律拒绝。
    if grant["root"] != str(project_root):
        raise GrantError("grant_root_mismatch", "批次授权绑定于其他项目根，拒绝在本根使用")
    # W0：撤销流水已生效但授权文件还没改写(撤销写入被打断)——按失败关闭判定为
    # 已撤销，并尽力补写授权文件，绝不允许"流水已撤销、文件仍可用"的分裂状态。
    if not grant["revoked"]:
        pending = _pending_revocation(project_root, grant["grant_id"])
        if pending is not None:
            grant["revoked"] = True
            grant["revoked_at"] = str(pending.get("at") or _now())
            grant["revoke_reason"] = sanitize_text(str(pending.get("reason") or ""))
            try:
                _save_grant(project_root, grant)
            except OSError:
                pass
    if (not _allow_pending and not grant["revoked"]
            and _pending_confirmation_path(project_root, grant_id).exists()
            and not _confirmation_committed(project_root, grant_id)):
        raise GrantError("confirmation_pending", "新批次确认尚未有效提交，不可使用")
    _check_budget_against_journal(project_root, grant)
    return grant


def _journal_path(root: Path, grant_id: str) -> Path:
    return _grants_dir(root) / "journal" / f"{grant_id}.jsonl"


def _journal_append(root: Path, grant_id: str, entry: Mapping[str, Any]) -> None:
    """追加型消费日志：签发、消费与预算计数都留独立痕迹，防静默重置（F05/R15）。"""
    path = _journal_path(root, grant_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": _now(), **entry}
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _journal_entries(root: Path, grant_id: str) -> list[dict[str, Any]]:
    path = _journal_path(root, grant_id)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                entries.append(item)
    return entries


def grant_consumption_journal(root: str | Path, grant_id: str) -> list[dict[str, Any]]:
    """读取授权消费日志（追加型）；用于核对使用标记与预算未被静默重置。"""
    project_root = _root_path(root)
    if not isinstance(grant_id, str) or not _GRANT_ID.fullmatch(grant_id):
        raise GrantError("grant_identity_invalid", "批次授权编号无效")
    return _journal_entries(project_root, grant_id)


BUDGET_COUNTERS = ("ai_requests", "repair_rounds", "no_progress_rounds")


def _journal_counter_totals(root: Path, grant_id: str) -> dict[str, int]:
    """按追加型消费日志还原三类计数的实际消耗；用于与授权文件交叉核对（S03/R15）。"""

    totals = {"ai_requests": 0, "repair_rounds": 0, "no_progress_rounds": 0}
    for item in _journal_entries(root, grant_id):
        kind = item.get("kind")
        if kind == "ai_request_issue":
            totals["ai_requests"] += 1
        elif kind == "counter_consumed":
            counter = item.get("counter")
            amount = item.get("amount", 0)
            if counter in totals and isinstance(amount, int) and not isinstance(amount, bool) and amount > 0:
                totals[counter] += amount
    return totals


def _check_budget_against_journal(root: Path, grant: Mapping[str, Any]) -> None:
    totals = _journal_counter_totals(root, grant["grant_id"])
    for counter in BUDGET_COUNTERS:
        used = grant["budget_used"].get(counter)
        if not isinstance(used, int) or isinstance(used, bool) or used < 0:
            raise GrantError("grant_budget_tampered", "已用预算字段无效：" + counter)
        if used < totals[counter]:
            raise GrantError(
                "grant_budget_tampered",
                "已用预算低于消费日志记录（" + counter + "：" + str(used) + " < " + str(totals[counter])
                + "），疑似被重置；拒绝使用",
            )
        limit = grant["budget"].get("max_" + counter)
        if isinstance(limit, int) and used > limit:
            raise GrantError("grant_budget_tampered", "已用预算超过批次上限：" + counter)


def _save_grant(root: Path, grant: Mapping[str, Any]) -> None:
    _atomic_write_json(_grants_dir(root) / f"{grant['grant_id']}.json", grant)


# --------------------------------------------------------------- 撤销线性化点
# W0(2026-09-30 R01-B 收口):正式撤销必须与"新批次确认的最终提交"处于可协调
# 次序。撤销以**追加型撤销流水**为线性化点:先把撤销记录与单调递增的撤销
# 计数写盘,再改授权文件。任何确认提交在写回执前复核该计数即可判定"本次
# 提交之前是否已有正式撤销生效",不需要让撤销去抢确认临界区的锁(撤销永远
# 不被提交阻塞),也不靠"再读一次授权文件"这种仍有窗口的方式。

def _revocation_journal_path(root: Path) -> Path:
    return _grants_dir(root) / "revocations" / "revocations.jsonl"


def _revocation_epoch_path(root: Path) -> Path:
    return _grants_dir(root) / "revocations" / "revocation-epoch.json"


def revocation_entries(root: str | Path) -> list[dict[str, Any]]:
    """读取追加型撤销流水(原始顺序)；用于核对撤销与提交的先后。"""
    project_root = _root_path(root)
    path = _revocation_journal_path(project_root)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    entries.append(item)
    except OSError:
        return entries
    return entries


def revocation_epoch(root: str | Path) -> int:
    """当前撤销计数：每次正式撤销单调 +1，写盘先于授权文件改写。"""

    project_root = _root_path(root)
    try:
        value = _read_json(_revocation_epoch_path(project_root))
        epoch = value.get("epoch")
        if isinstance(epoch, int) and not isinstance(epoch, bool) and epoch >= 0:
            return epoch
    except GrantError:
        pass
    return len(revocation_entries(project_root))


def _append_revocation(root: Path, grant_id: str, reason: str) -> int:
    """先立撤销点(流水 + 计数写盘)，返回撤销后的计数。"""

    path = _revocation_journal_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": _now(), "grant_id": grant_id, "reason": sanitize_text(reason)}
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    epoch = len(revocation_entries(root))
    _atomic_write_json(_revocation_epoch_path(root), {"epoch": epoch, "last": record})
    return epoch


def _pending_revocation(root: Path, grant_id: str) -> dict[str, Any] | None:
    found: dict[str, Any] | None = None
    for item in revocation_entries(root):
        if item.get("grant_id") == grant_id:
            found = item
    return found


def revoke_batch_grant(root: str | Path, grant_id: str, *, reason: str) -> dict[str, Any]:
    """撤销批次授权；撤销状态持久化，不删除任何记录。

    次序约定：撤销流水与计数**先**写盘（线性化点），再改写授权文件。因此
    即使授权文件这一步被中断/失败，后续任何 ``load_grant`` 仍按流水判定为
    已撤销（失败关闭），撤销不会被静默丢失。
    """

    project_root = _root_path(root)
    if not isinstance(reason, str) or not reason.strip():
        raise GrantError("revoke_reason_invalid", "撤销必须说明原因")
    with authorization_order(project_root) as order:
        grant = load_grant(project_root, grant_id, _allow_pending=True)
        _append_revocation(project_root, grant_id, reason.strip())
        order.execute("INSERT INTO events(kind, grant_id) VALUES ('revocation', ?)", (grant_id,))
        if not grant["revoked"]:
            grant["revoked"] = True
            grant["revoked_at"] = _now()
            grant["revoke_reason"] = sanitize_text(reason.strip())
        _save_grant(project_root, grant)
    return dict(grant)


def grant_valid(grant: Mapping[str, Any]) -> tuple[bool, str]:
    """返回 (是否可派发, 机器原因码)；只做判定，不做 I/O。"""

    try:
        _validate_grant(grant)
    except GrantError as exc:
        return False, exc.code
    now = datetime.now(timezone.utc)
    if grant["revoked"]:
        return False, "grant_revoked"
    if _parse_time(grant["expires_at"]) <= now:
        return False, "grant_expired"
    return True, "grant_active"


def check_grant_scope(grant: Mapping[str, Any], *, action_kind: str, targets: list[str]) -> str | None:
    """核对动作类别与精确目标是否完全落在批次范围内；越界返回原因码。"""

    if action_kind not in grant["action_kinds"]:
        return "step_kind_not_allowed"
    excluded = [unicodedata.normalize("NFC", item).casefold() for item in grant["excluded_paths"]]
    allowed = [unicodedata.normalize("NFC", item).casefold() for item in grant["allowed_paths"]]
    for target in targets:
        try:
            parts = _relative_parts(target)
        except (TypeError, ValueError):
            return "step_target_invalid"
        normalized = unicodedata.normalize("NFC", "/".join(parts)).casefold()
        if any(normalized == item or normalized.startswith(item + "/") for item in excluded):
            return "step_target_excluded"
        if not any(normalized == item or normalized.startswith(item + "/") for item in allowed):
            return "step_target_out_of_scope"
    return None


def _credential_path(root: Path, credential_id: str) -> Path:
    return _grants_dir(root) / "credentials" / f"{credential_id}.json"


def issue_step_credential(
    root: str | Path,
    grant_id: str,
    *,
    task_id: str,
    attempt: int,
    action_kind: str,
    targets: list[str],
    action_digest: str,
    input_digest: str,
    ttl_seconds: int = DEFAULT_STEP_TTL_SECONDS,
) -> dict[str, Any]:
    """在有效批次内为单步签发短期凭据；预算随签发登记，越界即拒绝。"""

    project_root = _root_path(root)
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise GrantError("step_task_invalid", "任务编号无效")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise GrantError("step_attempt_invalid", "尝试编号无效")
    if not isinstance(action_digest, str) or not action_digest:
        raise GrantError("step_digest_invalid", "动作摘要无效")
    if not isinstance(input_digest, str) or not input_digest:
        raise GrantError("step_digest_invalid", "输入摘要无效")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or ttl_seconds <= 0:
        raise GrantError("step_ttl_invalid", "凭据有效期无效")
    grant = load_grant(project_root, grant_id)
    valid, reason = grant_valid(grant)
    if not valid:
        raise GrantError(reason, "批次授权不可用：" + reason)
    if action_kind not in ACTION_KINDS:
        raise GrantError("step_kind_not_allowed", "未知动作类别")
    normalized_targets = sorted({_norm_path_prefix(item) for item in targets})
    scope_error = check_grant_scope(grant, action_kind=action_kind, targets=normalized_targets)
    if scope_error:
        raise GrantError(scope_error, "本步超出批次授权范围：" + scope_error)
    if action_kind == "ai_request":
        used = grant["budget_used"]["ai_requests"]
        if used >= grant["budget"]["max_ai_requests"]:
            raise GrantError("step_budget_exhausted", "本批次的 AI 请求预算已用完")
        grant["budget_used"]["ai_requests"] = used + 1
        _save_grant(project_root, grant)
    issued = datetime.now(timezone.utc)
    credential: dict[str, Any] = {
        "schema_version": CREDENTIAL_SCHEMA_VERSION,
        "credential_id": "cred-" + uuid.uuid4().hex,
        "grant_id": grant_id,
        "root": str(project_root),
        "task_id": task_id,
        "attempt": attempt,
        "action_kind": action_kind,
        "targets": normalized_targets,
        "action_digest": action_digest,
        "input_digest": input_digest,
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(seconds=float(ttl_seconds))).isoformat().replace("+00:00", "Z"),
        "used": False,
        "used_at": None,
    }
    credential["fingerprint"] = _credential_fingerprint(credential)
    _atomic_write_json(_credential_path(project_root, credential["credential_id"]), credential)
    if action_kind == "ai_request":
        _journal_append(project_root, grant_id, {
            "kind": "ai_request_issue",
            "credential_id": credential["credential_id"],
            "task_id": task_id,
            "attempt": attempt,
        })
    return dict(credential)


def _load_credential(root: Path, credential_id: str) -> dict[str, Any]:
    if not isinstance(credential_id, str) or not _CREDENTIAL_ID.fullmatch(credential_id):
        raise GrantError("credential_identity_invalid", "凭据编号无效")
    path = _credential_path(root, credential_id)
    if not path.exists():
        raise GrantError("credential_unknown", "凭据不存在")
    return _validate_credential(_read_json(path))


def check_step_credential(
    root: str | Path,
    credential_id: str,
    *,
    expect_task_id: str | None = None,
    expect_action_digest: str | None = None,
    consume: bool = True,
) -> dict[str, Any]:
    """应用前校验凭据：一次性使用、绑定任务、过期与撤销均拒绝。"""

    project_root = _root_path(root)
    credential = _load_credential(project_root, credential_id)
    if credential["root"] != str(project_root):
        raise GrantError("credential_root_mismatch", "凭据根目录不匹配")
    grant = load_grant(project_root, credential["grant_id"])
    valid, reason = grant_valid(grant)
    if not valid:
        raise GrantError(reason, "凭据所属批次不可用：" + reason)
    if credential["used"]:
        raise GrantError("credential_replay", "凭据已被使用，拒绝重放")
    if _parse_time(credential["expires_at"]) <= datetime.now(timezone.utc):
        raise GrantError("credential_expired", "凭据已过期")
    # F05/R15：使用标记与消费日志交叉核对；重置 used 字段无法绕过。
    if not credential["used"]:
        for item in _journal_entries(project_root, credential["grant_id"]):
            if item.get("kind") == "credential_consumed" and item.get("credential_id") == credential_id:
                raise GrantError("credential_journal_mismatch", "凭据使用标记与消费日志不一致，疑似被重置；拒绝使用")
    if expect_task_id is not None and credential["task_id"] != expect_task_id:
        raise GrantError("credential_task_mismatch", "凭据绑定的任务不匹配")
    if expect_action_digest is not None and credential["action_digest"] != expect_action_digest:
        raise GrantError("credential_action_mismatch", "凭据动作摘要不匹配")
    if consume:
        _journal_append(project_root, credential["grant_id"], {
            "kind": "credential_consumed",
            "credential_id": credential_id,
            "grant_id": credential["grant_id"],
            "task_id": credential["task_id"],
            "attempt": credential["attempt"],
            "action_kind": credential["action_kind"],
        })
        credential["used"] = True
        credential["used_at"] = _now()
        _atomic_write_json(_credential_path(project_root, credential_id), credential)
    return {"credential": credential, "grant": grant}


def consume_budget_counter(root: str | Path, grant_id: str, *, counter: str, amount: int = 1) -> dict[str, Any]:
    """登记修复或无进展预算消耗；超出上限返回 budget_exhausted 异常。"""

    if counter not in {"repair_rounds", "no_progress_rounds"}:
        raise GrantError("budget_counter_invalid", "未知预算计数器")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
        raise GrantError("budget_counter_invalid", "预算消耗量无效")
    project_root = _root_path(root)
    grant = load_grant(project_root, grant_id)
    valid, reason = grant_valid(grant)
    if not valid:
        raise GrantError(reason, "批次授权不可用：" + reason)
    limit = grant["budget"]["max_" + counter]
    if grant["budget_used"][counter] + amount > limit:
        raise GrantError("budget_exhausted", "预算计数已达上限：" + counter)
    grant["budget_used"][counter] += amount
    _save_grant(project_root, grant)
    _journal_append(project_root, grant_id, {"kind": "counter_consumed", "counter": counter, "amount": amount})
    return dict(grant)


__all__ = [
    "ACTION_KINDS",
    "BUDGET_COUNTERS",
    "CREDENTIAL_SCHEMA_VERSION",
    "DEFAULT_GRANT_TTL_SECONDS",
    "DEFAULT_STEP_TTL_SECONDS",
    "GRANT_SCHEMA_VERSION",
    "GrantError",
    "check_grant_scope",
    "check_step_credential",
    "consume_budget_counter",
    "grant_consumption_journal",
    "grant_valid",
    "issue_batch_grant",
    "issue_step_credential",
    "load_grant",
    "revocation_entries",
    "revocation_epoch",
    "revoke_batch_grant",
]
