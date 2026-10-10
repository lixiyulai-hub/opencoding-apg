# -*- coding: utf-8 -*-
"""通用生成执行链 v3(R17-C 检查点1 审核纠正:CP1-01/02/04)。

相对 v2 的修复(依据 01_AUDIT_REPORT_CN.md):
- **CP1-01 路径统一**:候选路径先做严格校验(非字符串/绝对/盘符/UNC/父目录段一律拒绝),
  校验产出唯一规范相对路径,落盘只用该规范路径;暂存目录独立、写前查重,杜绝
  "校验用 A、写盘用 B"。非法候选在任何 mkdir/write 前被拒。
- **CP1-02 生命周期**:目标前像在**派发 AI 请求前**冻结;每次派发(含失败/超时)都预记
  request_id/nonce 并消耗 ai_request 预算(不再只在成功后计数);取消检查贯通
  (派发前/返回后/静态检查后/暂存后/提交前);并发用 O_EXCL 原子预约锁;
  僵死判定要求锁心跳过期,标记 stale_interrupted 时显式记录"效果未知"。
- **CP1-04 独立检查**:冻结检查器字节在复制前/后核对 SHA;scratch 内检查器目录
  设只读;**重启保存断言用数据驱动步骤+文件存在+重载后精确状态**(内存实现必然失败);
  修复上下文带原候选源码。
- **CP1-03 部分**:生成任务契约来自"已确认评估"的 implementation_contract(数据驱动
  步骤协议),不再硬编码借还接口。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import grants, sandbox
from .aiadapter import AIRequestError
from .autorun import (  # 复用既有受控原语(与借还链同一实现)
    _commit_candidate,
    _stage_and_verify,
)
from .safety import canonical_json, sanitize_text, sha256_bytes

GENERIC_RUN_SCHEMA_VERSION = 3
APP_DIR = "app"
TASK_ID = "generate-app-v3"
MAX_CONTENT_BYTES = 256 * 1024
STALE_HEARTBEAT_SECONDS = 30 * 60

ALLOWED_STEP_OPS = ("call", "assert", "reload", "assert_file_exists", "assert_absent")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class GenericRunError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _runs_dir(root: Path) -> Path:
    return Path(root) / ".opencoding" / "generic_runs"


# R02:运行状态目录全集——首次 I/O 前统一校验,不再每个写入点各补局部检查
# C6-03(U08):task-budgets(同逻辑任务累计预算账本)同属运行状态,必须纳入同一
# 根/祖先保护;新状态存储一律经由 _RUNTIME_STATE_SUBDIRS + _ensure_state_dir
# 这一层访问,不允许"每加一个目录只在外层补一个名字"。
_RUNTIME_STATE_SUBDIRS = (
    "generic_runs", "frozen_checks", "grants", "transactions",
    "adoptions", "evaluations", "sessions", "dev-runs", "task-budgets",
)


def _state_dir_unsafe(target: Path) -> str | None:
    """统一判定:状态目录路径是否不安全(符号链接/重解析点/普通文件)。

    返回 None 表示安全(不存在或真实目录);返回字符串说明拒绝原因。
    validate_runtime_roots(启动全链校验)与 _ensure_state_dir(单目录写入前
    复核)共用同一判定,规则只有一份。
    """
    if target.is_symlink():
        return "符号链接"
    if target.exists() and not target.is_dir():
        return "不是目录"
    return None


def _ensure_state_dir(root: str | Path, name: str) -> Path:
    """C6-03:单个状态目录在**每次读写前**的统一安全出口。

    与运行入口的 validate_runtime_roots 同一判定规则;任何时刻(含校验之后、
    写入之前)被预置符号链接/替换为普通文件,都在实际 I/O 前被拒绝,
    不跟随、不创建、不清理。
    """
    target = Path(root) / ".opencoding" / name
    why = _state_dir_unsafe(target)
    if why:
        raise GenericRunError("runtime_root_unsafe",
                              "运行状态路径不安全(" + why + "),拒绝读写:" + str(target))
    return target


def validate_runtime_roots(root: str | Path) -> None:
    """R02 完整入口:运行状态目录的根/祖先/归属校验。

    必须在任何状态 I/O(锁预约/回执落盘/冻结检查/派发)之前调用:
    `.opencoding` 与其全部状态子目录要么不存在(随后由本进程创建),
    要么必须是真实目录;任何一级是符号链接/普通文件 → 拒绝,不跟随、
    不创建、不清理、不派发。链接目标零新增控制文件。
    """
    base = Path(root).resolve()
    state_root = base / ".opencoding"
    chain = [state_root] + [state_root / s for s in _RUNTIME_STATE_SUBDIRS]
    for d in chain:
        why = _state_dir_unsafe(d)
        if why:
            raise GenericRunError("runtime_root_unsafe",
                                  "运行状态路径" + why + ",拒绝读写:" + str(d))


def _cancel_flag(root: Path, run_id: str) -> Path:
    return _runs_dir(root) / (run_id + ".cancel")


def _lock_path(root: Path) -> Path:
    return _runs_dir(root) / ".active-lock"


def _read_doc(root: Path, name: str, limit: int = 6000) -> str:
    path = Path(root) / name
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


# W0:本线程"提交基准"——确认依据建立时的撤销计数。落盘函数内部据此复核,
# 因此撤销插入点在调用之前也一定被发现。
_COMMIT_REVOCATION_BASIS: dict[int, int] = {}


def _write_receipt(root: Path, receipt: dict[str, Any], *,
                   expect_revocation_epoch: int | None = None) -> None:
    """写回执；落盘那一刻执行**提交线性化复核**。

    正式撤销以 grants 的追加型撤销流水为线性化点(先写流水与计数,再改授权
    文件)。本函数在真正落盘前重新读取撤销计数:与建立确认依据时不同,说明
    本次提交之前已有正式撤销生效,按 ``grant_revoked`` 拒绝落盘,由上层回退
    撤销本次签发。撤销因此永远不需要等待确认临界区,也不会被提交阻塞。

    期望计数除显式入参外,也取本线程登记的提交基准
    (``_COMMIT_REVOCATION_BASIS``):复核发生在**落盘函数内部**,任何在调用
    点之前插入的正式撤销都会被看到,而不是靠调用方各自再读一次。
    """
    expected = expect_revocation_epoch
    if expected is None:
        expected = _COMMIT_REVOCATION_BASIS.get(threading.get_ident())
    if expected is not None:
        current = grants.revocation_epoch(root)
        if current != expected:
            raise GenericRunError(
                "grant_revoked",
                "确认提交前原授权已被正式撤销(撤销计数 "
                + str(expected) + " → " + str(current)
                + ");本次确认取消,原撤销记录保留")
    out = _runs_dir(root) / (receipt["run_id"] + ".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)


def load_receipt(root: str | Path, run_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"gen-[0-9a-f]{12}", run_id):
        return None
    path = _runs_dir(Path(root)) / (run_id + ".json")
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ------------------------------------------------------- 路径严格校验(CP1-01)

def canonical_candidate_path(raw: Any) -> str:
    """候选文件路径严格校验:返回唯一规范相对路径;任何非法输入拒绝。

    拒绝:非字符串、空、绝对路径、盘符、UNC、任何 .. 段、越出 app/ 前缀、非法字符。
    本函数是路径进入任何 mkdir/write 的唯一入口。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise GenericRunError("candidate_path_invalid", "候选路径必须是非空字符串")
    p = raw.replace("\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p) or p.startswith("//"):
        raise GenericRunError("candidate_path_invalid", "候选路径不允许绝对路径/盘符/UNC:" + sanitize_text(raw)[:80])
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        raise GenericRunError("candidate_path_invalid", "候选路径不允许父目录段(..):" + sanitize_text(raw)[:80])
    rel = "/".join(parts)
    if not rel.startswith(APP_DIR + "/") or len(parts) < 2:
        raise GenericRunError("candidate_path_invalid", "候选路径必须位于 app/ 之下:" + sanitize_text(raw)[:80])
    if any(ch in rel for ch in ":*?\"<>|"):
        raise GenericRunError("candidate_path_invalid", "候选路径含非法字符:" + sanitize_text(raw)[:80])
    return rel


def _static_check(files: Any) -> tuple[list[tuple[str, str]], list[str]]:
    """候选静态检查:形状/严格路径/大小/敏感(不执行候选)。返回(规范条目, 问题)。"""
    if not isinstance(files, list) or not files:
        return [], ["candidate_empty"]
    problems: list[str] = []
    canonical: list[tuple[str, str]] = []
    seen: set[str] = set()
    from .safety import inspect_sensitive  # noqa: PLC0415
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "content"}:
            return [], ["candidate_shape_invalid"]
        try:
            rel = canonical_candidate_path(entry["path"])
        except GenericRunError as exc:
            return [], [exc.code + ":" + sanitize_text(str(exc))[:120]]
        if rel in seen:
            return [], ["candidate_duplicate_path:" + rel]
        seen.add(rel)
        content = entry["content"]
        if not isinstance(content, str):
            return [], ["candidate_content_not_string:" + rel]
        if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            problems.append("candidate_oversized:" + rel)
        if inspect_sensitive(content)["sensitive"]:
            problems.append("candidate_sensitive:" + rel)
        canonical.append((rel, content))
    return canonical, problems


def _save_candidates(root: Path, run_id: str, attempt: int, canonical: list[tuple[str, str]]) -> Path:
    """把静态检查通过的候选写入专有暂存根(CP2-05:祖先链接/预存目录全检)。

    - 暂存根必须是本次新建:若 run 级目录已存在(含任何祖先为符号链接/重解析点),
      视为可疑环境,拒绝写入(不跟随、不清理、不覆盖);
    - 目标相对路径的每一级祖先都不允许是已存在的链接;
    - 写入只用 canonical_candidate_path 产出的规范相对路径。
    """
    run_dir = _runs_dir(root)
    # CP2-05/R02:从项目根向下核对其间的全部祖先(含 .opencoding/generic_runs 自身),
    # 任何一级是符号链接/预存可疑目录 → 拒绝,不跟随(Windows 重解析点未实测,不外推)
    chain = []
    cursor = Path(root).resolve()
    for seg in (".opencoding", "generic_runs"):
        cursor = cursor / seg
        chain.append(cursor)
    for dirp in chain:
        if dirp.is_symlink():
            raise GenericRunError("candidate_stage_unsafe", "祖先路径为符号链接,拒绝写入:" + str(dirp))
    if run_dir.is_dir():
        for child in run_dir.iterdir():
            if child.is_symlink():
                raise GenericRunError("candidate_stage_unsafe", "运行目录存在符号链接条目,拒绝写入:" + child.name)
    cand_dir = run_dir / (run_id + f"-candidate-attempt{attempt}")
    if cand_dir.exists() or cand_dir.is_symlink():
        raise GenericRunError("candidate_stage_conflict", "暂存根已存在(拒绝复用/可疑环境):" + str(cand_dir))
    cand_dir.mkdir(parents=True)
    for rel, content in canonical:
        target = cand_dir / rel
        # 逐级祖先:必须由本次创建,不允许既有链接
        cursor = cand_dir
        for seg in rel.split("/")[:-1]:
            cursor = cursor / seg
            if cursor.is_symlink():
                raise GenericRunError("candidate_stage_unsafe", "祖先路径为符号链接:" + str(cursor))
        if target.is_symlink() or target.exists():
            raise GenericRunError("candidate_stage_conflict", "暂存目标已存在:" + rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(content, encoding="utf-8", newline="\n")
        os.replace(tmp, target)
    return cand_dir


# ------------------------------------------------------- 原子预约(CP1-02)

def _reserve(root: Path, run_id: str) -> bool:
    """O_EXCL 原子预约;锁被占用且心跳过期时标记旧运行 stale 并接管。"""
    lp = _lock_path(root)
    lp.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"run_id": run_id, "pid": os.getpid(), "heartbeat_at": _now()})
    try:
        fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, payload.encode("utf-8"))
        os.close(fd)
        return True
    except FileExistsError:
        try:
            info = json.loads(lp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        try:
            age = time.time() - lp.stat().st_mtime
        except OSError:
            age = 0
        owner_pid = info.get("pid")
        owner_state = _pid_state(owner_pid) if isinstance(owner_pid, int) else PID_UNKNOWN
        if age > STALE_HEARTBEAT_SECONDS and owner_state == PID_DEAD:
            # 心跳过期且 owner 进程确认死亡 → 才允许接管;效果仍未知,如实标注
            _mark_stale(root, str(info.get("run_id", "")))
            lp.unlink(missing_ok=True)
            fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, payload.encode("utf-8"))
            os.close(fd)
            return True
        return False


def _release(root: Path, run_id: str) -> None:
    lp = _lock_path(root)
    try:
        if json.loads(lp.read_text(encoding="utf-8")).get("run_id") == run_id:
            lp.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def _touch_lock(root: Path, run_id: str) -> None:
    lp = _lock_path(root)
    try:
        lp.write_text(json.dumps({"run_id": run_id, "pid": os.getpid(), "heartbeat_at": _now()}), encoding="utf-8")
    except OSError:
        pass


PID_UNKNOWN = "unknown"
PID_ALIVE = "alive"
PID_DEAD = "dead"


def _pid_state(pid: int) -> str:
    """R05:owner 进程三态。OpenProcess 返回 NULL 必须区分错误原因:
    ERROR_INVALID_PARAMETER(87)=确认不存在;ERROR_ACCESS_DENIED(5)或其它=未知(保守按存活);
    成功=存活。任何核不出/平台不支持的情况都是 unknown,不得当死亡删除锁。"""
    if not isinstance(pid, int) or pid <= 0:
        return PID_UNKNOWN
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.GetLastError.restype = wintypes.DWORD
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            kernel32.CloseHandle(handle)
            return PID_ALIVE
        err = kernel32.GetLastError()
        if err == 87:  # ERROR_INVALID_PARAMETER:进程不存在
            return PID_DEAD
        return PID_UNKNOWN  # 访问拒绝等:未知,保守持锁
    except Exception:
        return PID_UNKNOWN


def _mark_stale(root: Path, run_id: str) -> None:
    if not run_id:
        return
    doc = load_receipt(root, run_id)
    if doc and doc.get("status") == "running":
        doc["status"] = "stale_interrupted"
        doc["effect_unknown"] = True
        doc["stale_note"] = "持有进程中断(锁/心跳过期);已发出的模型请求效果未知,结果按未采用材料处理"
        doc["finished_at"] = _now()
        _write_receipt(root, doc)


def unknown_effect_run(root: str | Path) -> dict[str, Any] | None:
    """最近一次结果未知(传输失败/中断)的运行;新派发必须先显式确认或核实。"""
    newest = None
    for path in sorted(_runs_dir(Path(root)).glob("gen-*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "unknown_acknowledged" in doc:
            continue  # 已显式确认接受不确定性的旧请求不再阻塞
        if (doc.get("current_request") or {}).get("effect") == "unknown" \
                or doc.get("effect_unknown") is True:
            if newest is None or str(doc.get("finished_at", "")) > str(newest.get("finished_at", "")):
                newest = doc
    return newest


def task_lineage(root: str | Path, session_id: str | None,
                 exclude_run_id: str | None = None) -> dict[str, Any]:
    """R03-B5:同一逻辑任务(同 session_id)的历史请求与预算事实。

    新尝试必须关联并如实累计旧请求消费;风险确认不等于预算重置。
    只统计已终态、同 session_id 的回执;无 session_id 的旧版回执不计入。
    """
    if not session_id:
        return {"inherited_runs": [], "inherited_ai_requests": 0}
    inherited: list[dict[str, Any]] = []
    total = 0
    for path in sorted(_runs_dir(Path(root)).glob("gen-*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("run_id") == exclude_run_id:
            continue
        if doc.get("session_id") != session_id:
            continue
        used = int(doc.get("ai_requests_dispatched", 0) or 0)
        if used <= 0 and doc.get("status") in ("not_started", "cancelled"):
            continue
        total += used
        inherited.append({
            "run_id": doc.get("run_id"),
            "status": doc.get("status"),
            "ai_requests_dispatched": used,
            "request_ids": [a.get("request_id") for a in (doc.get("attempts") or [])
                            if isinstance(a, dict) and a.get("request_id")],
        })
    return {"inherited_runs": inherited, "inherited_ai_requests": total}


def acknowledge_unknown(root: str | Path, run_id: str, note: str) -> dict[str, Any]:
    """显式接受旧请求效果未知后的新尝试:记录确认事件(不宣称远端已停止/未计费)。

    返回旧请求身份、可能已发生的消费事实与累计预算,供 UI 如实展示;
    确认只解除"效果未知"阻断,不重置、不扩大预算授权。
    """
    doc = load_receipt(root, run_id)
    if doc is None:
        raise GenericRunError("run_not_found", "运行不存在:" + run_id)
    if not ((doc.get("current_request") or {}).get("effect") == "unknown"
            or doc.get("effect_unknown") is True):
        raise GenericRunError("run_not_unknown",
                              "该运行不是结果未知状态,无需确认:" + run_id)
    lineage = task_lineage(root, doc.get("session_id"))
    doc["unknown_acknowledged"] = {
        "at": _now(), "note": sanitize_text(note)[:200],
        "semantics": "用户/执行者知悉旧请求效果未知;新尝试继承同一逻辑任务的请求历史与预算事实;"
                     "远端是否已停止/是否已计费本机无法核实",
        "old_request_id": (doc.get("current_request") or {}).get("request_id"),
    }
    _write_receipt(Path(root), doc)
    return {
        "run_id": run_id,
        "status": doc.get("status"),
        "acknowledged": doc["unknown_acknowledged"],
        "old_request": doc.get("current_request"),
        "old_failure": doc.get("failure"),
        "budget_lineage": lineage,
        "note_to_user": "已记录你接受旧请求结果的不确定性;新尝试将继续消耗同一任务的 AI 请求预算,"
                        "累计消费按事实计入(不因确认而重置)。",
    }


def active_run(root: str | Path) -> dict[str, Any] | None:
    """存在未完成(锁在、心跳新鲜)的运行时返回其回执;僵死锁标记 stale 并放行。"""
    lp = _lock_path(Path(root))
    if not lp.exists():
        return None
    try:
        info = json.loads(lp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        lp.unlink(missing_ok=True)
        return None
    try:
        age = time.time() - lp.stat().st_mtime
    except OSError:
        age = 0
    owner_pid = info.get("pid")
    owner_state = _pid_state(owner_pid) if isinstance(owner_pid, int) else PID_UNKNOWN
    if age > STALE_HEARTBEAT_SECONDS and owner_state == PID_DEAD:
        _mark_stale(Path(root), str(info.get("run_id", "")))
        lp.unlink(missing_ok=True)
        return None
    return load_receipt(root, str(info.get("run_id", "")))


def request_cancel(root: str | Path, run_id: str) -> dict[str, Any]:
    doc = load_receipt(root, run_id)
    if doc is None:
        raise GenericRunError("run_not_found", "运行不存在:" + run_id)
    if doc.get("status") != "running":
        return {"run_id": run_id, "status": doc["status"], "cancel_requested": False}
    _cancel_flag(Path(root), run_id).write_text(_now() + "\n", encoding="utf-8")
    return {"run_id": run_id, "status": "running", "cancel_requested": True}


def _cancelled(root: Path, run_id: str) -> bool:
    return _cancel_flag(root, run_id).is_file()


# ------------------------------------------------------- 共用控制闸(C6-01)

def _control_gate(project: Path, *, run_id: str, stage: str,
                  binding: dict[str, Any] | None,
                  expected: Mapping[str, Any] | None = None,
                  grant: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """C6-01:初次生成/修复/接续共用的验证/提交控制入口。

    每个关键边界(响应后、隔离执行前、提交前,以及接续的对应边界)调用同一
    函数,按同一顺序复查同一套条件;**不允许为接续单独复制第二套弱化判断**:

    1. 取消登记——同一取消域(generic_runs/<run_id>.cancel),任何边界已登记
       即停止后继;
    2. 采用绑定——evaluation_id/contract_sha256/plan_digest 与派发(或原采用)
       时一致;采用记录缺失/不可读同样拒绝(看不清 ≠ 放行);
    3. 当前事实——事实/会话答案/有效输入快照任一变化 → 拒绝;
    4. 授权有效性——传入 grant 时核对仍有效(撤销/过期/损坏都拒绝);
    5. 目标前像——提供 expected 时逐文件核对仍为冻结值(用户改动不覆盖)。

    返回 None 表示放行;返回 {"reason", "code", "stage", "detail"[, "paths"]}
    表示拒绝,detail 不含阶段前缀(由调用方按各自文案组合),调用方按各自
    状态字段终态化。本函数只判定、不写状态。
    """
    if _cancelled(project, run_id):
        return {"reason": "cancel", "code": "cancel_requested", "stage": stage,
                "detail": "已有取消登记;停止后继边界"}
    try:
        _verify_adoption_binding(project, binding)
    except GenericRunError as exc:
        return {"reason": "binding_drift", "code": exc.code, "stage": stage,
                "detail": sanitize_text(str(exc))[:200]}
    drift = _input_drift_during_request(project, binding)
    if drift:
        return {"reason": "input_drift", "code": "adoption_input_drift", "stage": stage,
                "detail": drift}
    if grant is not None:
        # 复核**持久化**的授权本体而不是调用方手里的内存副本:
        # 请求进行中被撤销时,内存副本看不到,落盘状态才是权威。
        gid = grant.get("grant_id") if isinstance(grant, Mapping) else None
        try:
            if not isinstance(gid, str) or not gid:
                ok, why = False, "grant_unverifiable:missing_grant_id"
            else:
                ok, why = grants.grant_valid(grants.load_grant(project, gid))
        except grants.GrantError as exc:
            ok, why = False, "grant_unverifiable:" + sanitize_text(str(exc))[:120]
        if not ok:
            return {"reason": "grant_invalid", "code": why, "stage": stage,
                    "detail": "批次授权已失效(" + why + ")"}
    if expected:
        drifted = [rel for rel, want in expected.items()
                   if (sha256_bytes((project / rel).read_bytes())
                       if (project / rel).exists() else None) != want]
        if drifted:
            return {"reason": "preimage_drift", "code": "preimage_drift", "stage": stage,
                    "detail": "目标前像已变化,拒绝覆盖:" + ",".join(sorted(drifted)),
                    "paths": sorted(drifted)}
    return None


# ------------------------------------------------------- 冻结检查(数据驱动契约)

def _frozen_check_spec(run_id: str, goal: str, contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "run_id": run_id,
        "goal": goal,
        "frozen_at": _now(),
        "contract": {
            "entry_module": contract.get("entry_module", "app.main"),
            "files": contract.get("files", []),
            "functions": contract.get("functions", {}),
            "data_dir": contract.get("data_dir", "app/data"),
        },
        "steps": contract.get("steps", []),
        "restart_empty_expectation": contract.get("restart_empty_expectation"),
        "note": "检查器为控制器固定字节;步骤为派发前冻结的数据(候选不可写)。",
    }


IDENTITY_FIELD_DEFAULTS = ("id", "name", "code", "key", "title")
# 弱标量不得充当业务记录标识:版本号/数量/布尔只证明数据形状,不证明"用户要的东西还在"。
_WEAK_PATTERN = re.compile(r"^[-+]?\d+(\.\d+)*$")


def _is_identity_value(value: Any) -> bool:
    """是否可作为业务记录标识:必须是非纯数字的实质字符串。"""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if len(text) < 2:
        return False
    return not _WEAK_PATTERN.match(text)


def _main_call_constants(steps: list[dict[str, Any]]) -> set[Any]:
    """保留旧行为:main 阶段 call 的全部实参常量(用于提示/兼容显示)。"""
    values: set[Any] = set()
    for s in steps:
        if s.get("op") != "call" or s.get("phase", "main") != "main":
            continue
        for a in (s.get("args") or []):
            if isinstance(a, str) and a.startswith("$"):
                continue
            if isinstance(a, (str, int, float, bool)) or a is None:
                values.add(a)
    return values


def _main_call_identities(steps: list[dict[str, Any]],
                          contract: Mapping[str, Any] | None = None) -> set[str]:
    """CP5 §6/T06:抽取 main 阶段建立的**业务记录标识**。

    只允许字符串形态的实质标识;`identity_fields` 未声明时用默认业务标识字段。
    纯数字/版本号/布尔不作为标识——它们无法把"用户要保留的那条记录"与
    "恰好同值的另一个标量"区分开。
    """
    fields_raw = (contract or {}).get("identity_fields")
    fields = [str(f) for f in fields_raw] if isinstance(fields_raw, list) and fields_raw \
        else list(IDENTITY_FIELD_DEFAULTS)
    identities: set[str] = set()
    for s in steps:
        if s.get("op") != "call" or s.get("phase", "main") != "main":
            continue
        for arg in (s.get("args") or []):
            if isinstance(arg, Mapping):
                for key, value in arg.items():
                    if str(key) in fields and _is_identity_value(value):
                        identities.add(str(value))
            elif _is_identity_value(arg):
                identities.add(str(arg))
    declared = (contract or {}).get("business_records")
    if isinstance(declared, list):
        for rec in declared:
            if not isinstance(rec, Mapping):
                continue
            for holder in (rec.get("identity"), rec.get("record_id")):
                if _is_identity_value(holder):
                    identities.add(str(holder))
            values = rec.get("identity_values")
            if isinstance(values, list):
                for v in values:
                    if _is_identity_value(v):
                        identities.add(str(v))
    return identities


def _criteria_linked(step: dict[str, Any], main_values: set[Any]) -> bool:
    """兼容入口:旧形态(任意共有标量)仍然返回 True,供展示使用。

    真正的挂钩判定由 :func:`_criteria_linked_record` 承担。
    """
    blobs: list[Any] = []
    blobs.extend(step.get("contains") or [])
    if step.get("equals") is not None:
        blobs.append(step["equals"])
    return any(v in main_values for v in _flatten_scalars(blobs) if _scalar_hashable(v))


def _scalar_hashable(value: Any) -> bool:
    try:
        hash(value)
        return True
    except TypeError:
        return False


def _flatten_scalars(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        out: list[Any] = []
        for v in value.values():
            out.extend(_flatten_scalars(v))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            out.extend(_flatten_scalars(v))
        return out
    return [value]


def _criteria_linked_record(step: dict[str, Any], identities: set[str]) -> str | None:
    """restart assert 是否明确断言了某条业务记录仍在(返回被断言的标识)。

    仅当判定值里出现 main 阶段建立的具体记录标识才算关联;数量/版本号/布尔同值
    不再被视为"业务结果已核对"。
    """
    blobs: list[Any] = []
    blobs.extend(step.get("contains") or [])
    if step.get("equals") is not None:
        blobs.append(step["equals"])
    for value in _flatten_scalars(blobs):
        if isinstance(value, str) and value in identities:
            return value
    return None


def _validate_contract(contract: Any) -> dict[str, Any]:
    """宽松结构校验:步骤只允许已知算子;文件全部位于 app/ 下;runtime 显式声明。"""
    if not isinstance(contract, dict):
        raise GenericRunError("contract_invalid", "implementation_contract 必须是对象")
    runtime = str(contract.get("runtime", "python-stdlib")).strip() or "python-stdlib"
    # W2:第二技术上下文——除 Python 家族外,明确支持 Node/JavaScript(本机有真实
    # node 可执行文件时)。除此之外的路线仍然如实拒绝并要求重评,不靠"看起来像
    # 支持"放行(例如出现过 HTML 字符串就算第二 runtime)。
    if runtime not in _RUNTIME_CHECKERS:
        raise GenericRunError("runtime_unsupported",
                              "契约要求的技术路线当前无执行适配:" + sanitize_text(runtime)[:60]
                              + ";请重新评估选择当前能力支持的实现路线("
                              + "、".join(sorted(_RUNTIME_CHECKERS)) + ")")
    files = contract.get("files")
    if not isinstance(files, list) or not files:
        raise GenericRunError("contract_invalid", "契约缺少 files")
    canon_files = []
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str):
            raise GenericRunError("contract_invalid", "契约 files 元素需含 path")
        canon_files.append(canonical_candidate_path(f["path"]))
    steps = contract.get("steps")
    if not isinstance(steps, list) or not steps:
        raise GenericRunError("contract_invalid", "契约缺少 steps(冻结检查步骤)")
    for s in steps:
        if not isinstance(s, dict) or s.get("op") not in ALLOWED_STEP_OPS:
            raise GenericRunError("contract_invalid", "未知检查步骤算子:" + sanitize_text(str(s.get("op")))[:40])
        if s.get("op") == "assert":
            has_criteria = any(isinstance(s.get(k), list) and s[k] for k in ("contains", "not_contains")) \
                or s.get("equals") is not None
            if not has_criteria:
                raise GenericRunError("contract_invalid",
                                      "assert 步骤缺少判定依据(contains/not_contains/equals),空断言不能作为独立验收")
    restart_steps = [s for s in steps if s.get("phase") == "restart"]
    has_call = any(s.get("op") == "call" for s in restart_steps)
    has_state_assert = any(
        s.get("op") == "assert" and (s.get("contains") or s.get("equals") is not None)
        for s in restart_steps)
    if not restart_steps or not has_call or not has_state_assert:
        raise GenericRunError("contract_invalid",
                              "契约 restart 阶段必须重新调用读取函数并断言先前业务数据"
                              "(call+assert 结构化判定);仅 assert_file_exists 不能证明用户数据还在")
    # CP5 §6/T06:关联判定不再是"任意一个共有标量";必须指明业务记录标识,
    # 且该标识要在 restart 阶段对"读取动作的结果"被明确断言。
    # 不再允许用数量/版本号/布尔同值冒充"用户业务结果已核对"。
    identities = _main_call_identities(steps, contract)
    if not identities:
        raise GenericRunError(
            "record_identity_missing",
            "契约缺少业务记录标识:main 阶段的写入调用必须包含字符串形态的记录标识"
            "(可用 identity_fields 声明标识字段,或用 business_records 显式声明),"
            "纯数字/数量/版本号不可作为标识")
    linked: list[tuple[dict[str, Any], str]] = []
    for s in restart_steps:
        if s.get("op") != "assert":
            continue
        hit = _criteria_linked_record(s, identities)
        if hit:
            linked.append((s, hit))
    if not linked:
        empty = contract.get("restart_empty_expectation")
        if not (isinstance(empty, dict) and str(empty.get("justification", "")).strip()
                and str(empty.get("requirement_ref", "")).strip()):
            raise GenericRunError(
                "restart_assert_unlinked",
                "restart 断言没有明确断言任何一条已在 main 建立的业务记录("
                + "、".join(sorted(identities)[:3])
                + ");数量/版本号/布尔同值不足以证明用户要的数据仍在。"
                  "若业务确为'重启后为空',请提供 restart_empty_expectation 的 "
                  "requirement_ref(指向已采用需求)与 justification")
    elif contract.get("restart_empty_expectation") is not None:
        empty = contract.get("restart_empty_expectation")
        if not (isinstance(empty, dict) and str(empty.get("justification", "")).strip()
                and str(empty.get("requirement_ref", "")).strip()):
            raise GenericRunError(
                "restart_empty_expectation_invalid",
                "restart_empty_expectation 必须同时给出 requirement_ref(已采用需求依据)"
                "与 justification;非空字符串本身不能把持久化需求改成'启动清空'")
    # CP5 §3.3:业务写区不得覆盖检查区/控制区/代码区;越早在契约层拒绝越好。
    data_dir = str(contract.get("data_dir", "app/data"))
    from .docker_provider import validate_data_dir  # noqa: PLC0415

    probe_root = Path(tempfile.gettempdir()) / "opencoding-write-area-probe"
    probe_root.mkdir(parents=True, exist_ok=True)
    _, refusal = validate_data_dir(probe_root, data_dir)
    if refusal:
        raise GenericRunError("contract_invalid",
                              "契约声明的业务写区不允许(" + refusal + ")")
    return {
        "entry_module": str(contract.get("entry_module", "app.main")),
        "files": canon_files,
        "functions": contract.get("functions") if isinstance(contract.get("functions"), dict) else {},
        "data_dir": data_dir,
        "steps": steps,
        "runtime": runtime,
        "business_identities": sorted(identities),
        "restart_empty_expectation": contract.get("restart_empty_expectation"),
    }


FROZEN_CHECKER_SOURCE = '''# -*- coding: utf-8 -*-
"""冻结契约检查器 v3(控制器固定字节):分进程阶段执行数据驱动步骤。

用法:python app_contract_v2.py <phase>   phase ∈ {main, restart}
- main:    主业务步骤(创建/借出/断言等);
- restart: **独立新进程**重新导入入口模块,验证数据真实落盘(内存实现必然失败)。
断言为结构化匹配:对列表取"至少一条记录、指定键精确相等";不做全文关键词搜索。
任何步骤失败立即整体失败。
"""
import importlib
import json
import sys
from pathlib import Path

RESULTS = []


def _rec(rid, ok, detail=""):
    RESULTS.append({"id": rid, "ok": bool(ok), "detail": str(detail)[:200]})
    print(("PASS" if ok else "FAIL"), rid, detail)


def _matches_record(record, item):
    """结构化匹配:record 是 dict 时,item 的每个指定键都必须精确相等。"""
    if isinstance(record, dict) and isinstance(item, dict):
        return all(record.get(k) == v for k, v in item.items())
    return record == item


def _contains(saved, items):
    if not isinstance(saved, list):
        saved = [saved]
    return any(all(_matches_record(rec, item) for item in items)
               for rec in saved)


def _not_contains(saved, items):
    if not isinstance(saved, list):
        saved = [saved]
    return not any(_matches_record(rec, item) for item in items for rec in saved)


def main():
    fdir = Path(__file__).resolve().parent
    root = fdir.parent
    sys.path.insert(0, str(root))
    phase = sys.argv[1] if len(sys.argv) > 1 else "main"
    spec = json.loads((fdir / "spec.json").read_text(encoding="utf-8"))
    contract = spec["contract"]
    entry = contract["entry_module"]
    rel_entry = entry.replace(".", "/")
    if not (root / (rel_entry + ".py")).exists() and not (root / rel_entry).exists():
        print("FAIL F00 入口缺失:" + entry)
        return 1
    mod = importlib.import_module(entry)
    saved = {}
    idx = 0
    for step in spec["steps"]:
        if step.get("phase", "main") != phase:
            continue
        idx += 1
        rid = "%s-S%02d_%s" % (phase, idx, step.get("op"))
        op = step.get("op")
        try:
            if op == "reload":
                mod = importlib.reload(mod)
                _rec(rid, True)
            elif op == "assert_file_exists":
                rel = str(step["path"]).replace("\\\\", "/").lstrip("/")
                target = root / "app" / rel
                _rec(rid, target.is_file() and target.stat().st_size > 0, str(target))
            elif op == "assert_absent":
                rel = str(step["path"]).replace("\\\\", "/").lstrip("/")
                _rec(rid, not (root / "app" / rel).exists(), rel)
            elif op == "call":
                fn = getattr(mod, step["function"], None)
                if not callable(fn):
                    _rec(rid, False, "缺少函数 " + step["function"])
                    return _finish()
                args = [saved.get(a[1:], a) if isinstance(a, str) and a.startswith("$") else a
                        for a in step.get("args", [])]
                if step.get("expect_exception"):
                    try:
                        fn(*args)
                    except Exception as exc:
                        _rec(rid, True, type(exc).__name__)
                    else:
                        _rec(rid, False, "预期异常未发生")
                else:
                    out = fn(*args)
                    if step.get("save_as"):
                        saved[step["save_as"]] = out
                    _rec(rid, True, str(out)[:100])
            elif op == "assert":
                val = saved.get(step.get("saved"))
                blob = json.dumps(val, ensure_ascii=False, default=str, sort_keys=True)
                ok = True
                if step.get("contains"):
                    ok = ok and _contains(val, step["contains"])
                if step.get("not_contains"):
                    ok = ok and _not_contains(val, step["not_contains"])
                if step.get("equals") is not None:
                    ok = ok and (json.dumps(val, ensure_ascii=False, default=str, sort_keys=True)
                                 == json.dumps(step["equals"], ensure_ascii=False, default=str, sort_keys=True))
                _rec(rid, ok, blob[:150])
            else:
                _rec(rid, False, "未知算子 " + str(op))
        except Exception as exc:
            _rec(rid, False, type(exc).__name__ + ":" + repr(exc)[:150])
    return _finish()


def _finish():
    failed = [r for r in RESULTS if not r["ok"]]
    print(json.dumps({"passed": len(RESULTS) - len(failed), "failed": len(failed)}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


# W2(2026-09-30):第二技术上下文——Node/JavaScript 运行时的冻结检查器。
# 与 Python 检查器同一语义:main 与 restart 在**两个独立进程**中执行,内存
# 实现必然在 restart 失败;断言为结构化匹配,不做全文关键词搜索。
FROZEN_CHECKER_NODE_SOURCE = '''// 冻结契约检查器(node 运行时,控制器固定字节):分进程阶段执行数据驱动步骤。
// 用法:node app_contract_node.js <phase>   phase ∈ {main, restart}
const fs = require('fs');
const path = require('path');

const fdir = __dirname;
const root = path.resolve(fdir, '..');
const phase = process.argv[2] || 'main';
const spec = JSON.parse(fs.readFileSync(path.join(fdir, 'spec.json'), 'utf8'));
const contract = spec.contract || {};
const RESULTS = [];

function rec(id, ok, detail) {
  const text = (detail === undefined || detail === null) ? '' : String(detail).slice(0, 200);
  RESULTS.push({ id: id, ok: !!ok, detail: text });
  console.log((ok ? 'PASS' : 'FAIL'), id, text);
}

function matches(record, item) {
  if (record && typeof record === 'object' && !Array.isArray(record) &&
      item && typeof item === 'object' && !Array.isArray(item)) {
    return Object.keys(item).every(function (k) { return record[k] === item[k]; });
  }
  return JSON.stringify(record) === JSON.stringify(item);
}

function containsAny(saved, items) {
  const list = Array.isArray(saved) ? saved : [saved];
  return list.some(function (rec_) {
    return items.every(function (item) { return matches(rec_, item); });
  });
}

function containsNone(saved, items) {
  const list = Array.isArray(saved) ? saved : [saved];
  return !list.some(function (rec_) {
    return items.some(function (item) { return matches(rec_, item); });
  });
}

function resolveEntry() {
  const rel = String(contract.entry_module || 'app.main').replace(/\\./g, '/');
  const candidates = [rel + '.js', rel + '.mjs', path.join(rel, 'index.js'),
                      path.join(rel, 'package.json')];
  for (const cand of candidates) {
    const abs = path.join(root, cand);
    if (fs.existsSync(abs)) { return abs; }
  }
  return null;
}

function main() {
  const entryPath = resolveEntry();
  if (!entryPath) { rec('F00', false, '入口缺失:' + String(contract.entry_module)); return finish(); }
  let mod;
  try { mod = require(entryPath); } catch (exc) {
    rec('F01', false, '入口加载失败:' + (exc && exc.message)); return finish();
  }
  if (mod && typeof mod === 'object' && mod.default && typeof mod.default === 'object') {
    mod = mod.default;
  }
  const saved = {};
  let idx = 0;
  for (const step of (spec.steps || [])) {
    if ((step.phase || 'main') !== phase) { continue; }
    idx += 1;
    const rid = phase + '-S' + String(idx).padStart(2, '0') + '_' + step.op;
    try {
      if (step.op === 'reload') {
        delete require.cache[require.resolve(entryPath)];
        mod = require(entryPath);
        rec(rid, true, 'reloaded');
      } else if (step.op === 'assert_file_exists') {
        const rel = String(step.path).replace(/\\\\/g, '/').replace(/^\\/+/, '');
        const target = path.join(root, 'app', rel);
        let ok = false;
        try { ok = fs.statSync(target).isFile() && fs.statSync(target).size > 0; } catch (e) { ok = false; }
        rec(rid, ok, target);
      } else if (step.op === 'assert_absent') {
        const rel = String(step.path).replace(/\\\\/g, '/').replace(/^\\/+/, '');
        rec(rid, !fs.existsSync(path.join(root, 'app', rel)), rel);
      } else if (step.op === 'call') {
        const fn = mod ? mod[step.function] : undefined;
        if (typeof fn !== 'function') { rec(rid, false, '缺少函数 ' + step.function); return finish(); }
        const args = (step.args || []).map(function (a) {
          return (typeof a === 'string' && a.charAt(0) === '$') ? saved[a.slice(1)] : a;
        });
        if (step.expect_exception) {
          try { fn.apply(null, args); rec(rid, false, '预期异常未发生'); }
          catch (exc) { rec(rid, true, exc && exc.name ? exc.name : 'Error'); }
        } else {
          let out;
          try { out = fn.apply(null, args); }
          catch (exc) { rec(rid, false, '调用抛出异常:' + (exc && exc.message)); return finish(); }
          if (step.save_as) { saved[step.save_as] = out; }
          rec(rid, true, JSON.stringify(out) === undefined ? '' : String(JSON.stringify(out)).slice(0, 100));
        }
      } else if (step.op === 'assert') {
        const val = saved[step.saved];
        let ok = true;
        if (step.contains) { ok = ok && containsAny(val, step.contains); }
        if (step.not_contains) { ok = ok && containsNone(val, step.not_contains); }
        if (step.equals !== undefined && step.equals !== null) {
          ok = ok && JSON.stringify(val) === JSON.stringify(step.equals);
        }
        rec(rid, ok, String(JSON.stringify(val)).slice(0, 150));
      } else {
        rec(rid, false, '未知算子 ' + String(step.op));
      }
    } catch (exc) {
      rec(rid, false, (exc && exc.name ? exc.name : 'Error') + ':' + String(exc && exc.message).slice(0, 150));
    }
  }
  return finish();
}

function finish() {
  const failed = RESULTS.filter(function (r) { return !r.ok; });
  console.log(JSON.stringify({ passed: RESULTS.length - failed.length, failed: failed.length }));
  return failed.length ? 1 : 0;
}

process.exit(main());
'''

# 契约运行时 → 冻结检查器文件名与执行参数(W2: 第二技术上下文)
_RUNTIME_CHECKERS = {
    "python": ("app_contract_v2.py", "python"),
    "python-stdlib": ("app_contract_v2.py", "python"),
    "node": ("app_contract_node.js", "node"),
    "node-stdlib": ("app_contract_node.js", "node"),
}


def _node_executable() -> str | None:
    """本机 Node 可执行文件(仅查找,不安装、不改环境)。"""
    import shutil

    for name in ("node", "nodejs"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _runtime_command(runtime: str, script_rel: str, *args: str) -> list[str]:
    """按运行时给出真实 argv;Node 缺失时明确报"无执行适配",不伪装可用。"""
    if runtime in ("node", "node-stdlib"):
        node = _node_executable()
        if not node:
            raise GenericRunError("runtime_unavailable",
                                  "契约要求 Node/JavaScript 运行时,但本机未找到 node 可执行文件;"
                                  "请重新评估选择当前能力支持的实现路线,或先安装 Node 后重试")
        return [node, script_rel, *args]
    return [sys.executable, "-X", "utf8", script_rel, *args]


def _write_frozen_checks(root: Path, run_id: str, goal: str, contract: dict[str, Any]) -> dict[str, Any]:
    spec = _frozen_check_spec(run_id, goal, contract)
    fdir = Path(root) / ".opencoding" / "frozen_checks" / run_id
    fdir.mkdir(parents=True, exist_ok=True)
    spec_path = fdir / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    runtime = str(contract.get("runtime", "python-stdlib"))
    checker_name, _kind = _RUNTIME_CHECKERS.get(runtime, ("app_contract_v2.py", "python"))
    checker = fdir / checker_name
    source = FROZEN_CHECKER_NODE_SOURCE if _kind == "node" else FROZEN_CHECKER_SOURCE
    checker.write_text(source, encoding="utf-8", newline="\n")
    record = {
        "run_id": run_id,
        "frozen_at": spec["frozen_at"],
        "spec_sha256": sha256_bytes(spec_path.read_bytes()),
        "checker_sha256": sha256_bytes(checker.read_bytes()),
        "checker_name": checker_name,
        "runtime": runtime,
        "controller_note": "冻结于候选生成之前;候选白名单=契约 files;检查区不可写。",
    }
    (fdir / "frozen_record.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return record


# ------------------------------------------------------- 实现消息

def _runtime_requirements(contract: Mapping[str, Any]) -> list[str]:
    """按契约运行时给出硬性要求(W2:第二技术上下文的中文约束)。"""
    runtime = str(contract.get("runtime", "python-stdlib"))
    data_dir = str(contract.get("data_dir", "app/data"))
    common = [
        "不得联网;不得读写 app/ 目录之外的任何路径",
        "状态必须真实落盘在 " + data_dir + " 下的数据文件(独立新进程重新打开后数据仍在,内存变量不算)",
        "界面与提示全部使用中文",
    ]
    if runtime in ("node", "node-stdlib"):
        return [
            "只使用 Node.js 内置模块(fs/path 等),不安装任何第三方依赖",
            "入口以 CommonJS 导出(module.exports)可被调用的函数;不得依赖浏览器 DOM",
            *common,
        ]
    return [
        "只使用 Python 标准库(当前执行适配为 Python 家族)",
        "核心逻辑必须拆成可导入的函数",
        *common,
    ]


def _implementation_messages(root: Path, goal: str, contract: dict[str, Any]) -> list[dict[str, str]]:
    files_spec = {rel: "本文件完整内容" for rel in contract["files"]}
    user = json.dumps({
        "项目目标": goal,
        "PRG_md_节选": _read_doc(root, "PRG.md"),
        "plan_md_节选": _read_doc(root, "plan.md"),
        "必须交付的文件": files_spec,
        "必须实现的函数契约": contract["functions"],
        "技术路线": str(contract.get("runtime", "python-stdlib")),
        "硬性要求": _runtime_requirements(contract),
        "输出格式": {"summary": "非空字符串",
                     "files": [{"path": p, "content": "完整文件内容"} for p in contract["files"]]},
    }, ensure_ascii=False, indent=1)
    return [
        {"role": "system", "content": "你是受控执行环境中的编码助手。只输出一个 JSON 对象,"
         "包含 summary 与 files 字段;必须原样回传系统消息里 OpenCoding 请求绑定中的 nonce。"},
        {"role": "user", "content": user},
    ]


def _repair_messages(goal: str, contract: dict[str, Any], failures: list[str],
                     previous_files: list[tuple[str, str]], frozen_steps: list[dict[str, Any]] | None = None,
                     remaining_budget: dict[str, int] | None = None) -> list[dict[str, str]]:
    prev_src = {rel: content[:4000] for rel, content in previous_files}
    user = json.dumps({
        "说明": "上一次候选在隔离验收中失败。基于原候选源码修复,重新输出完整文件。",
        "项目目标": goal,
        "函数契约": contract["functions"],
        "冻结验收步骤": frozen_steps or contract.get("steps", []),
        "原候选源码节选": prev_src,
        "源码截断说明": "每个文件最多展示前 4000 字符;完整文件以你上一轮输出为准",
        "失败证据": failures[-8:],
        "剩余预算": remaining_budget or {},
        "预算说明": "修复轮次有限(数值见剩余预算);若无法修复请如实说明",
        "输出格式": {"summary": "修复说明", "fix": "改动要点",
                     "files": [{"path": p, "content": "完整文件内容"} for p in contract["files"]]},
    }, ensure_ascii=False, indent=1)
    return [
        {"role": "system", "content": "你是受控执行环境中的编码助手。只输出一个 JSON 对象,"
         "包含 summary/fix/files;必须原样回传系统消息里 OpenCoding 请求绑定中的 nonce。"},
        {"role": "user", "content": user},
    ]


# ------------------------------------------------------- 预算与派发(CP1-02)

def _consume_ai_budget(project: Path, grant: dict[str, Any], attempt: int,
                       input_digest: str) -> None:
    """派发前预记并消耗一次 ai_request 预算(失败/超时同样计入)。"""
    credential = grants.issue_step_credential(
        project, grant["grant_id"], task_id=TASK_ID, attempt=attempt,
        action_kind="ai_request", targets=[],
        action_digest=sha256_bytes(canonical_json(
            {"type": "ai_request", "task_id": TASK_ID, "attempt": attempt})),
        input_digest=input_digest or sha256_bytes(canonical_json({"attempt": attempt})),
    )
    grants.check_step_credential(project, credential["credential_id"],
                                 expect_task_id=TASK_ID, consume=True)


# ------------------------------------------------------- 验证器(CP1-04)

def _make_verifier(project: Path, run_id: str,
                   data_dir: str = "app/data") -> Callable[[Path], tuple[bool, list[str]]]:
    """验证器:复制前/后核对冻结字节;先跑冻结检查器(数据驱动),后跑辅助 selftest。

    data_dir 传给执行后端(Docker 模式下唯一可写业务区)。
    """

    def verifier(scratch: Path) -> tuple[bool, list[str]]:
        evidence: list[str] = []
        fdir_src = project / ".opencoding" / "frozen_checks" / run_id
        record = json.loads((fdir_src / "frozen_record.json").read_text(encoding="utf-8"))
        runtime = str(record.get("runtime") or "python-stdlib")
        checker_name = str(record.get("checker_name")
                           or _RUNTIME_CHECKERS.get(runtime, ("app_contract_v2.py", "python"))[0])
        selftest_rel = "app/selftest.js" if runtime in ("node", "node-stdlib") else "app/selftest.py"
        checker_rel = "frozen_checks/" + checker_name
        dst = scratch / "frozen_checks"
        dst.mkdir(parents=True, exist_ok=True)
        for name, want in (("spec.json", record["spec_sha256"]),
                           (checker_name, record["checker_sha256"])):
            src = fdir_src / name
            if sha256_bytes(src.read_bytes()) != want:
                return False, ["frozen_source_modified:" + name]
            (dst / name).write_bytes(src.read_bytes())
        if sha256_bytes((dst / checker_name).read_bytes()) != record["checker_sha256"]:
            return False, ["frozen_copy_mismatch:checker"]
        for f in dst.iterdir():
            try:
                os.chmod(f, 0o500)
            except OSError:
                pass
        # 冻结检查 = 两个独立进程:main(主业务) + restart(新进程重开数据,内存实现必败)
        # 候选 selftest 仅作辅助。运行前后核对 spec 与 checker 字节。
        for label, phase, rel, script in (
                ("frozen-main", "main", checker_rel, checker_rel),
                ("frozen-restart", "restart", checker_rel, checker_rel),
                ("selftest", None, selftest_rel, selftest_rel)):
            if not (scratch / rel).is_file():
                evidence.append(label + "_missing")
                if label == "frozen":
                    return False, evidence
                continue
            argv = _runtime_command(runtime, script) if phase is None \
                else _runtime_command(runtime, script, phase)
            try:
                result = sandbox.run(argv, cwd=scratch, guard_dir=_guard_for(scratch), timeout=90,
                                     backend_options={"data_dir": data_dir})
            except sandbox.SandboxError as exc:
                evidence.append(f"{label}_sandbox_refused:" + str(exc)[:200])
                return False, evidence
            evidence.append(f"{label}_exit={result.returncode}")
            evidence.append(f"{label}_tail=" + sanitize_text(result.stdout)[-300:])
            if result.returncode != 0:
                return False, evidence
            if label.startswith("frozen"):
                after_checker = sha256_bytes((scratch / "frozen_checks" / checker_name).read_bytes())
                after_spec = sha256_bytes((scratch / "frozen_checks" / "spec.json").read_bytes())
                if after_checker != record["checker_sha256"] or after_spec != record["spec_sha256"]:
                    evidence.append("frozen_modified_during_run:" + label)
                    return False, evidence
        return True, evidence

    return verifier


def _guard_for(scratch: Path) -> Path:
    from .autorun import _guard_for as _g  # noqa: PLC0415

    return _g(scratch)


# ------------------------------------------------------- 主链

_TASK_BUDGET_SCHEMA_VERSION = "1.0"


def _budget_dir(project: Path) -> Path:
    """C6-03:预算状态目录经统一安全出口访问(符号链接/替换文件在任何 I/O 前拒绝)。"""
    return _ensure_state_dir(project, "task-budgets")


def _ledger_path(project: Path, task_id: str, session_id: str) -> Path:
    slug = sha256_bytes((str(task_id) + "|" + str(session_id)).encode("utf-8"))[:24]
    return _budget_dir(project) / (slug + ".json")


def _validate_ledger_structure(doc: Any) -> str | None:
    """FIX-03:既有账本的**结构契约**校验——返回 None 表示合法,否则返回原因。

    与"初次不存在"严格区分:文件存在就必须符合既定 schema,**任何字段都不
    得被静默归零/重建**(runs 变数组、dispatched 变非数字、ceiling 缺失或
    变 bool/字符串等一律按损坏处理,保守停止)。读取、展示、派发记账、
    扩额共用同一校验入口。

    必需字段(产品写出器 ``_normalize_ledger`` 一直写出的形状):
    - ``schema_version`` == "1.0";
    - ``task_id`` / ``session_id``:非空字符串(消费归属身份);
    - ``ceiling``:非 bool 的 int,0 ≤ ceiling ≤ 1_000_000(防溢出上限);
    - ``runs``:dict;键为非空 run 标识;每条记录为 dict 且带**非 bool int、
      ≥0** 的 ``dispatched``(缺失消费信息不得默认 0);
    可选字段(兼容依据:产品写出器可能带):``created_at`` / ``updated_at``
    (字符串)、``extensions``(dict 列表)、记录内 ``grant_id`` / ``started_at``。
    """
    if not isinstance(doc, dict):
        return "not_an_object"
    version = doc.get("schema_version")
    if version != _TASK_BUDGET_SCHEMA_VERSION:
        return "schema_version_invalid:" + sanitize_text(str(version))[:40]
    for field in ("task_id", "session_id"):
        value = doc.get(field)
        if not isinstance(value, str) or not value:
            return field + "_missing_or_invalid"
    ceiling = doc.get("ceiling")
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) \
            or not 0 <= ceiling <= 1_000_000:
        return "ceiling_invalid:" + sanitize_text(str(ceiling))[:40]
    runs = doc.get("runs")
    if not isinstance(runs, dict):
        return "runs_not_a_dict:" + type(runs).__name__
    for key, item in runs.items():
        if not isinstance(key, str) or not key:
            return "run_key_invalid"
        if not isinstance(item, dict):
            return "run_entry_invalid:" + sanitize_text(str(key))[:60]
        dispatched = item.get("dispatched")
        if isinstance(dispatched, bool) or not isinstance(dispatched, int) or dispatched < 0:
            return "dispatched_invalid:" + sanitize_text(str(key))[:60]
    extensions = doc.get("extensions")
    if extensions is not None:
        if not isinstance(extensions, list) or any(not isinstance(i, dict) for i in extensions):
            return "extensions_invalid"
    return None


def _load_ledger(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """读取账本,返回 (账本, 拒绝原因)。

    C6-03/FIX-03:文件不存在 → (None, None)(首次运行,允许建立);**存在但
    不可解析/结构不符 → (None, 原因)**——调用方必须保守停止,不得把损坏
    账本静默当成空账本(那会把已消费额度重置回零,等于绕过累计上限)。
    结构校验与身份校验共用 :func:`_validate_ledger_structure`。
    """
    if not path.exists():
        return None, None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, type(exc).__name__ + ":" + sanitize_text(str(exc))[:120]
    reason = _validate_ledger_structure(doc)
    if reason:
        return None, "ledger_shape_invalid:" + reason
    return doc, None


def _require_readable_ledger(project: Path, task_id: str, session_id: str) -> dict[str, Any]:
    """C6-03/FIX-03:账本存在但损坏(语法或结构)或身份不符时保守失败——
    不重建、不重置、不继续消费、不覆盖原字节。"""
    doc, err = _load_ledger(_ledger_path(project, task_id, session_id))
    if err:
        raise GenericRunError(
            "task_budget_ledger_unreadable",
            "累计预算账本存在但不可读(" + err + ");保守停止:不新建空账本、"
            "不派发、不扩额。请人工核对 .opencoding/task-budgets/ 后按原授权规则恢复")
    if doc is not None:
        # FIX-03:既有账本的消费归属身份必须与本次请求一致;换名读取不得
        # 把别人/别的任务的账本当成自己的空账本。
        mismatch = _ledger_identity_mismatch(doc, task_id, session_id)
        if mismatch:
            raise GenericRunError(
                "task_budget_ledger_unreadable",
                "累计预算账本身份不符(" + mismatch + ");保守停止:"
                "不派发、不扩额、不改写。请人工核对 .opencoding/task-budgets/")
    return doc or {}


def _write_ledger(path: Path, ledger: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(dict(ledger), ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _normalize_ledger(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """整理账本形状——仅接受**空对象**(首次建立)或已通过
    :func:`_validate_ledger_structure` 的账本。

    FIX-03:不再把坏字段静默归零/重建。旧实现把非 dict 的 runs 换成 {}、
    把非数字的 dispatched 换成 0,等于把"已消费"改写成"未消费"再放行派发;
    现在非法形状在校验层已被拒绝,这里再遇到属于纵深防御,直接抛错而不
    覆盖原字节。
    """
    if not ledger:
        return {
            "schema_version": _TASK_BUDGET_SCHEMA_VERSION,
            "task_id": None, "session_id": None, "ceiling": 0,
            "runs": {}, "extensions": [], "updated_at": None,
        }
    reason = _validate_ledger_structure(ledger)
    if reason:
        raise GenericRunError(
            "task_budget_ledger_unreadable",
            "账本结构校验未通过(" + reason + ");拒绝整理与改写,原字节保持原样")
    clean: dict[str, Any] = {}
    for key, item in ledger["runs"].items():
        clean[str(key)] = {
            "grant_id": item.get("grant_id"),
            "dispatched": int(item["dispatched"]),
            "started_at": item.get("started_at"),
        }
    return {
        "schema_version": _TASK_BUDGET_SCHEMA_VERSION,
        "task_id": ledger["task_id"],
        "session_id": ledger["session_id"],
        "ceiling": int(ledger["ceiling"]),
        "runs": clean,
        "extensions": [dict(item) for item in (ledger.get("extensions") or [])],
        "updated_at": ledger.get("updated_at"),
    }


def open_task_ledger(project: Path, task_id: str, session_id: str, *, ceiling: int,
                     run_id: str) -> dict[str, Any]:
    """同一逻辑任务(同 session_id + task_id)的累计 AI 请求账本。

    首次运行时以本次基数建立额度上限;后续运行不再各自签发全额新额度,而是
    继承**剩余额度**。风险确认不续期、不扩额;追加额度必须由
    :func:`extend_task_budget` 产生一条明确的授权记录。
    """
    path = _ledger_path(project, task_id, session_id)
    doc = _require_readable_ledger(project, task_id, session_id)
    ledger = _normalize_ledger(doc)
    ledger["task_id"] = task_id
    ledger["session_id"] = session_id
    if not doc:
        ledger["ceiling"] = max(0, int(ceiling))
        ledger["created_at"] = _now()
    entry = ledger["runs"].setdefault(run_id, {"grant_id": None, "dispatched": 0})
    entry.setdefault("started_at", _now())
    _write_ledger(path, ledger)
    return ledger


def record_dispatch(project: Path, task_id: str, session_id: str, run_id: str, *,
                    dispatched: int, grant_id: str | None = None) -> dict[str, Any]:
    """把本次运行已派发的请求数写回同一逻辑任务账本(每次派发都计数)。

    C6-03:账本损坏时保守失败(在派发记账前拒绝),不带病记账、不静默重建。
    """
    path = _ledger_path(project, task_id, session_id)
    ledger = _normalize_ledger(_require_readable_ledger(project, task_id, session_id))
    ledger["task_id"] = task_id
    ledger["session_id"] = session_id
    entry = ledger["runs"].setdefault(run_id, {"grant_id": grant_id, "dispatched": 0})
    entry["dispatched"] = max(int(entry.get("dispatched", 0) or 0), max(0, int(dispatched)))
    if grant_id:
        entry["grant_id"] = grant_id
    ledger["updated_at"] = _now()
    _write_ledger(path, ledger)
    return ledger


def _ledger_identity_mismatch(doc: Mapping[str, Any], task_id: str,
                              session_id: str) -> str | None:
    """FIX-03:账本消费归属身份与请求是否一致(读取/展示/派发/扩额共用)。"""
    if str(doc.get("task_id")) != str(task_id) or str(doc.get("session_id")) != str(session_id):
        return ("identity_mismatch:账本 task/session(" + sanitize_text(str(doc.get("task_id")))
                + "|" + sanitize_text(str(doc.get("session_id")))[:40]
                + ")与请求(" + sanitize_text(str(task_id))[:40] + "|" + sanitize_text(str(session_id))[:40]
                + ")不一致")
    return None


def task_budget_state(project: Path, task_id: str, session_id: str) -> dict[str, Any]:
    if not session_id:
        return {"ceiling": None, "dispatched": 0, "remaining": None, "runs": {}}
    doc, err = _load_ledger(_ledger_path(project, task_id, session_id))
    if err:
        # C6-03:只读视图对损坏账本也保守处理——remaining=0 阻止后续派发,
        # 并如实标注不可读原因,不把损坏当成"未消费"。
        return {"ceiling": None, "dispatched": 0, "remaining": 0, "runs": {},
                "ledger_unreadable": err}
    if doc is not None:
        # FIX-03:身份不符的账本不得被展示为本任务的可用额度。
        mismatch = _ledger_identity_mismatch(doc, task_id, session_id)
        if mismatch:
            return {"ceiling": None, "dispatched": 0, "remaining": 0, "runs": {},
                    "ledger_unreadable": mismatch}
    ledger = _normalize_ledger(doc or {})
    if not ledger.get("task_id"):
        return {"ceiling": None, "dispatched": 0, "remaining": None, "runs": {}}
    dispatched = sum(item["dispatched"] for item in ledger["runs"].values())
    return {"ceiling": ledger["ceiling"], "dispatched": dispatched,
            "remaining": max(0, ledger["ceiling"] - dispatched),
            "runs": ledger["runs"], "extensions": ledger["extensions"]}


def extend_task_budget(root: str | Path, task_id: str, session_id: str, *,
                       amount: int, reason: str, issued_by: str) -> dict[str, Any]:
    """追加同一逻辑任务的 AI 请求额度:必须是**新的明确授权**,不是风险确认。

    每次追加都留痕(数量、理由、授权人、时间),累计额度只能按此增长。
    """
    if not isinstance(session_id, str) or not session_id:
        raise GenericRunError("task_budget_invalid", "缺少会话标识,无法归属逻辑任务")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1 or amount > 100:
        raise GenericRunError("task_budget_invalid", "追加额度必须是 1-100 的整数")
    why = sanitize_text(str(reason or "")).strip()
    if len(why) < 4:
        raise GenericRunError("task_budget_reason_required", "追加额度必须写明理由(≥4 字)")
    project = Path(root)
    path = _ledger_path(project, task_id, session_id)
    # C6-03:账本损坏时拒绝扩额——不知道真实已消费量,就不允许在任何基数上累加。
    ledger = _normalize_ledger(_require_readable_ledger(project, task_id, session_id))
    ledger["task_id"] = task_id
    ledger["session_id"] = session_id
    if not ledger.get("created_at"):
        ledger["created_at"] = _now()
    ledger.setdefault("runs", {})
    ledger["ceiling"] = int(ledger.get("ceiling", 0) or 0) + amount
    ledger["extensions"].append({
        "amount": amount, "reason": why[:300],
        "issued_by": sanitize_text(str(issued_by))[:120], "at": _now(),
    })
    ledger["updated_at"] = _now()
    _write_ledger(path, ledger)
    return task_budget_state(project, task_id, session_id)


def _persist_received_material(project: Path, run_id: str, attempt: int, request_id: str,
                               result: Any) -> dict[str, Any]:
    """CP5 §5.3:响应本体、摘要与请求身份先落盘,再允许标记 received。

    顺序不可颠倒:没有可恢复本体时不得出现"已收到"状态。写入失败视为未收到,
    由调用方的异常处理终态化,不静默继续。
    """
    structured = result.get("structured") if isinstance(result, Mapping) else None
    if not isinstance(structured, Mapping):
        structured = {}
    body = {
        "schema_version": GENERIC_RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "attempt": attempt,
        "request_id": request_id,
        "received_at": _now(),
        "adapter": {"real": bool(getattr(result, "real", False)),
                    "provider": str(getattr(result, "provider", "?"))[:80],
                    "model": str(getattr(result, "model", "?"))[:80]},
        "structured": dict(structured),
    }
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True)
    run_dir = _runs_dir(project)
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / (run_id + "-received-attempt" + str(attempt) + ".json")
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(target)
    return {"path": str(target.relative_to(project)), "sha256": sha256_bytes(payload.encode("utf-8")),
            "attempt": attempt, "request_id": request_id, "bytes": len(payload.encode("utf-8"))}


def _material_path(project: Path, run_id: str, attempt: int) -> Path:
    return _runs_dir(project) / (run_id + "-received-attempt" + str(attempt) + ".json")


def load_received_material(root: str | Path, run_id: str, attempt: int) -> dict[str, Any] | None:
    """读取某次尝试已落盘的响应本体(用于接续,不重新调用模型)。"""
    path = _material_path(Path(root), run_id, attempt)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_CANDIDATE_REJECT_TEXT = {
    "received_material_missing":
        "候选缺少可核对的原始响应本体;拒绝把暂存文件当作原候选接续,"
        "请通过正常入口重新评估并生成",
    "candidate_unreadable":
        "暂存候选存在但不可读;拒绝接续,候选保持原样",
    "candidate_identity_mismatch":
        "暂存候选与原响应本体不一致(路径集合或文件内容与原请求产物不符);"
        "拒绝以'继续原候选'名义使用被改动的字节;如确需使用改动内容,"
        "请走重新生成形成明确的新候选版本(受累计额度约束)",
}


def _pending_candidate(project: Path, run_id: str, receipt: Mapping[str, Any],
                       ) -> tuple[dict[str, Any] | None, str | None]:
    """找出本次运行中**已保存但未提交**的候选(last attempt 优先)。

    C6-01(U04):暂存候选必须与**原响应落盘本体**逐字节一致才算"同一个候选"——
    核对完整路径集合与每个文件的内容;任何不一致(或本体缺失,无法核对)都
    返回拒绝原因。不得把目录里恰好扫到的任意新字节当成"继续原候选"。
    返回 (候选, 拒绝原因):候选为 None 时原因非 None 即为精确拒绝码。
    """
    for item in reversed(list(receipt.get("attempts") or [])):
        if not isinstance(item, Mapping):
            continue
        preserved = item.get("candidate_preserved")
        if not preserved:
            continue
        cand_dir = Path(str(preserved))
        if not cand_dir.is_dir():
            cand_dir = _runs_dir(project) / (run_id + "-candidate-attempt" + str(item.get("attempt")))
        if not cand_dir.is_dir():
            continue
        attempt_no = int(item.get("attempt") or 0)
        material = load_received_material(project, run_id, attempt_no)
        if not isinstance(material, Mapping):
            return None, "received_material_missing"
        structured = material.get("structured") if isinstance(material.get("structured"), Mapping) else {}
        raw_files = structured.get("files") if isinstance(structured.get("files"), list) else None
        if not raw_files:
            return None, "received_material_missing"
        expected: dict[str, str] = {}
        for entry in raw_files:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("content"), str):
                return None, "received_material_missing"
            try:
                # 与保存路径同一规范入口:响应里的原始写法不参与比对
                rel = canonical_candidate_path(entry.get("path"))
            except GenericRunError:
                return None, "received_material_missing"
            expected[rel] = entry["content"]
        staged: dict[str, str] = {}
        for file_path in sorted(cand_dir.rglob("*")):
            if not file_path.is_file():
                continue
            rel = file_path.relative_to(cand_dir).as_posix()
            if not rel.startswith("app/") or file_path.stat().st_size > MAX_CONTENT_BYTES:
                continue
            try:
                staged[rel] = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                return None, "candidate_unreadable"
        if set(staged) != set(expected) or any(staged[rel] != want for rel, want in expected.items()):
            return None, "candidate_identity_mismatch"
        return {"attempt": attempt_no, "candidate_dir": str(cand_dir),
                "request_id": item.get("request_id") or material.get("request_id"),
                "files": [{"path": rel, "content": staged[rel]} for rel in sorted(staged)]}, None
    return None, None


def resumable_candidate(root: str | Path, session_id: str | None) -> dict[str, Any] | None:
    """CP5 §5.3:同会话是否存在**可接续**的已收候选(未提交、输入未漂移、绑定仍在)。

    存在时不应被迫重新调用模型:先给接续入口。用户明确要求重新生成时,
    由调用方显式选择 regenerate(那是新尝试,照样受累计额度约束)。
    """
    if not session_id:
        return None
    project = Path(root)
    best: dict[str, Any] | None = None
    for path in sorted(_runs_dir(project).glob("gen-*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("session_id") != session_id or doc.get("transaction_id"):
            continue
        candidate, reject = _pending_candidate(project, str(doc.get("run_id")), doc)
        if candidate is None:
            # C6-01:身份核对未通过的候选不进接续预览;精确原因在真正接续时报出。
            continue
        binding = doc.get("ai_binding") if isinstance(doc.get("ai_binding"), dict) else None
        drift = _input_drift_during_request(project, binding)
        current = {"run_id": doc.get("run_id"), "attempt": candidate["attempt"],
                   "status": doc.get("status"), "candidate_dir": candidate["candidate_dir"],
                   "files": [item["path"] for item in candidate["files"]],
                   "request_id": candidate.get("request_id"),
                   "adopted_plan_digest": doc.get("adopted_plan_digest"),
                   "input_drift": drift, "resumable": drift is None}
        best = current if best is None else max(best, current, key=lambda d: d["attempt"])
    return best


def resume_saved_candidate(root: str | Path, run_id: str) -> dict[str, Any]:
    """CP5 §5.3:用已保存的候选继续同一次任务——**零新 AI 请求**地执行检查并提交。

    C6-01:接续与初次生成共用同一验证/提交控制(授权域、取消域、当前事实、
    候选身份、提交保护),不再是一条弱化支路:

    - 入口边界:候选必须与原响应落盘本体逐字节一致(身份核对),采用绑定与
      当前事实复核;任何一项不过 → 拒绝,候选准确保留;
    - 运行边界:接续正式开始即持久化 running 阶段(cancel 可登记),隔离执行前、
      提交前经 _control_gate 与普通生成走同一套取消/绑定/事实/授权/前像复查;
    - 授权边界:撤销/篡改/不可读/缺失的 grant 不自动换发;**当前活跃授权**
      被撤销不得由过期祖先续签顶替(FIX-01);批次到期需用户正常入口的
      新批次确认(见 _resume_grant_for_resume)。

    后端仍不可用时如实返回 blocked_execution,候选继续保持可接续。
    """
    project = Path(root)
    validate_runtime_roots(project)
    receipt = load_receipt(project, run_id)
    if receipt is None:
        raise GenericRunError("run_not_found", "运行不存在:" + str(run_id))
    if receipt.get("transaction_id"):
        raise GenericRunError("candidate_already_committed", "该运行的候选已提交,不可重复接续")
    candidate, reject = _pending_candidate(project, run_id, receipt)
    if candidate is None:
        if reject:
            raise GenericRunError(reject, _CANDIDATE_REJECT_TEXT.get(
                reject, "候选本体校验未通过:" + reject))
        raise GenericRunError("resume_material_missing", "没有可接续的候选本体:该运行未保存候选")
    binding = receipt.get("ai_binding") if isinstance(receipt.get("ai_binding"), dict) else None
    _verify_adoption_binding(project, binding)
    drift = _input_drift_during_request(project, binding)
    if drift:
        raise GenericRunError("adoption_input_drift", "接" + drift + ";旧候选不可接续,请重新评估采用")
    existing = active_run(project)
    if existing is not None:
        return {"status": "already_running", "run_id": existing["run_id"],
                "note": "已有进行中的生成运行;接续会与之冲突"}
    if not _reserve(project, run_id):
        return {"status": "already_running", "run_id": run_id, "note": "原子预约失败:另一运行持锁"}
    try:
        # 本次显式接续入口是用户对同一候选的新的延续动作:此前为旧运行登记、
        # 尚未被任何边界消费的取消标记在此清除并留痕,否则旧标记会让接续
        # 在第一道闸就被无声取消(用户永远无法再次接续)。
        stale = _cancel_flag(project, run_id)
        if stale.is_file():
            stale.unlink(missing_ok=True)
            receipt["resume_cleared_cancel_flag_at"] = _now()
        return _resume_inner(project, receipt, candidate, binding)
    finally:
        _release(project, run_id)


# ------------------------------------------------- R01:新批次确认正式入口

# 接续复用既有候选,零新 AI 请求;新批次预算固定全零,不自动扩额。
_RESUME_CONFIRM_BUDGET = {"max_ai_requests": 0, "max_repair_rounds": 0,
                          "max_no_progress_rounds": 0}


def _evaluated_grant_state(project: Path, receipt: Mapping[str, Any],
                           ) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """R01:评估回执当前授权(活跃位优先,回退最初)的加载与有效性。

    返回 (评估的 grant_id, 已加载授权, 状态码):状态码为 ``grant_active``
    (有效)、grants.grant_valid 的失效原因(grant_expired/grant_revoked/
    grant_tampered)、``grant_id_missing`` 或 ``grant_unverifiable:<原因>``
    (不可读/不存在)。判定对象与接续链路一致:活跃位优先,不回溯祖先。

    R01-C:已到期(grant_expired)的授权仍是 load_grant 结构校验通过的完整
    对象(指纹/根绑定/字段全部核对过)——预览展示与实际签发必须以这同一
    来源为准,不得在预览侧回退到默认范围。仅不可信状态(撤销/篡改/不可读)
    置 None;这些状态 confirmable=False,不得借展示放行。
    """
    original_id = receipt.get("grant_id")
    active_id = receipt.get("grant_id_active")
    target_id = active_id if isinstance(active_id, str) and active_id else \
        (original_id if isinstance(original_id, str) else None)
    if not isinstance(target_id, str) or not target_id:
        return None, None, "grant_id_missing"
    try:
        current = grants.load_grant(project, target_id)
    except grants.GrantError as exc:
        return target_id, None, "grant_unverifiable:" + sanitize_text(str(exc))[:120]
    ok, why = grants.grant_valid(current)
    trusted = bool(ok) or why == "grant_expired"
    return target_id, (dict(current) if trusted else None), why


def describe_resume_confirmation(root: str | Path, run_id: str, *,
                                  session_id: str | None = None) -> dict[str, Any]:
    """R01:新批次确认前的中文预览——用户先看清接续要素,再决定是否确认。

    只读不写:不签发授权、不改回执、不产生任何状态 I/O。候选身份、采用
    绑定与当前事实任何一项不成立 → 直接拒绝(确认一个接不上的候选没有意义)。
    预览展示:继续哪次任务、使用哪份既有候选、允许的动作/写入范围、
    零新 AI 请求与既有消费限制。
    """
    project = Path(root)
    validate_runtime_roots(project)
    receipt = load_receipt(project, run_id)
    if receipt is None:
        raise GenericRunError("run_not_found", "运行不存在:" + str(run_id))
    if receipt.get("transaction_id"):
        raise GenericRunError("candidate_already_committed",
                              "该运行的候选已提交,无需新批次确认")
    if session_id is not None and str(receipt.get("session_id") or "") != str(session_id):
        raise GenericRunError("confirmation_identity_mismatch",
                              "运行不属于当前会话;拒绝跨会话的新批次确认")
    candidate, reject = _pending_candidate(project, run_id, receipt)
    if candidate is None:
        if reject:
            raise GenericRunError(reject, _CANDIDATE_REJECT_TEXT.get(
                reject, "候选本体校验未通过:" + reject))
        raise GenericRunError("resume_material_missing",
                              "没有可接续的候选本体:该运行未保存候选")
    binding = receipt.get("ai_binding") if isinstance(receipt.get("ai_binding"), dict) else None
    _verify_adoption_binding(project, binding)
    drift = _input_drift_during_request(project, binding)
    if drift:
        raise GenericRunError("adoption_input_drift",
                              "接" + drift + ";旧候选不可接续,新批次确认无意义")
    if active_run(project) is not None:
        raise GenericRunError("run_active",
                              "已有进行中的生成运行;请等它结束后再确认新批次")
    evaluated_id, grant, state = _evaluated_grant_state(project, receipt)
    files = [item["path"] for item in candidate["files"]]
    digest = sha256_bytes(canonical_json(
        [{"path": item["path"], "content": item["content"]}
         for item in candidate["files"]]))
    original_id = receipt.get("grant_id")
    if state == "grant_active":
        state_note = "当前批次授权仍有效;直接接续即可,无需新批次确认"
    elif state == "grant_expired":
        state_note = ("当前批次授权已到期;确认后将签发零 AI 请求的新批次并绑定本次接续,"
                      "原候选与既有消费记录保持不变")
    elif state == "grant_revoked":
        state_note = "当前活跃授权已被正式撤销;已撤销的授权不能由新批次确认顶替"
    elif state == "grant_tampered":
        state_note = "当前授权内容被篡改;拒绝接续,请人工核对授权文件"
    elif isinstance(state, str) and state.startswith("grant_unverifiable"):
        state_note = "当前授权不可核对;拒绝接续,请人工核对授权文件"
    else:
        state_note = "接续回执缺少可核对的授权记录;请通过正常入口重新授权"
    # R01-C:待确认范围与实际签发同源——到期授权经 load_grant 结构校验,
    # 预览按其真实字段只读展示;不可信状态不给默认假象,如实标注不可核对。
    if grant is not None and state == "grant_expired":
        scope_source = "原授权(已到期;以下为经核对的原始范围,确认后按此逐项签发,不扩大)"
    elif grant is not None:
        scope_source = "当前授权(仍有效;直接接续即可)"
    else:
        scope_source = "当前授权不可核对(" + str(state) + ");不展示待确认范围,已拒绝确认"
    # R01-C:既有逻辑任务的累计预算只读展示——同一台账、不清零、不自动续期、
    # 不建第二台账;账本损坏时如实标注(接续派发仍按原规则保守拒绝)。
    budget_view = task_budget_state(project, receipt.get("task_id") or TASK_ID,
                                    str(receipt.get("session_id") or ""))
    existing_budget: dict[str, Any] = {
        "ceiling": budget_view.get("ceiling"),
        "dispatched": budget_view.get("dispatched"),
        "remaining": budget_view.get("remaining"),
    }
    if budget_view.get("ledger_unreadable"):
        existing_budget["unreadable"] = budget_view["ledger_unreadable"]
    return {
        "run_id": run_id,
        "session_id": receipt.get("session_id"),
        "task_id": receipt.get("task_id") or TASK_ID,
        "status": receipt.get("status"),
        "candidate": {"attempt": candidate["attempt"],
                      "request_id": candidate.get("request_id"),
                      "files": files, "content_sha256": digest},
        "original_grant_id": original_id if isinstance(original_id, str) else None,
        "previous_grant": {"grant_id": evaluated_id, "state": state},
        "new_batch_scope": {
            "allowed_paths": list(grant["allowed_paths"]) if grant else [APP_DIR],
            "excluded_paths": list(grant.get("excluded_paths") or []) if grant else [],
            "action_kinds": list(grant["action_kinds"]) if grant else ["local_write", "local_run"],
            "data_scope": str(grant.get("data_scope") or "synthetic-local") if grant else "synthetic-local",
        },
        "new_batch_scope_source": scope_source,
        "new_batch_budget": dict(_RESUME_CONFIRM_BUDGET),
        "existing_task_budget": existing_budget,
        "confirmable": state == "grant_expired",
        "state_note": state_note,
        "zero_ai_requests": True,
    }


def _revert_issued_grant(project: Path, grant_id: str, reason: str) -> tuple[bool, str]:
    """R01-B:回退本次确认刚签发、尚未绑定回执的新授权。

    返回 (是否已确认撤销, 如实说明)。撤销失败时**绝不声称"已撤销"**——
    未绑定授权不占回执活跃位、不能经正常接续使用,但回收未完成必须如实上报,
    不得用文案掩盖状态未知。
    """
    try:
        grants.revoke_batch_grant(project, grant_id, reason=reason)
        return True, "已回退撤销"
    except grants.GrantError as exc:
        return False, ("回退撤销未完成(" + sanitize_text(str(exc))[:80]
                       + ");该授权未绑定任何回执、不占活跃位,不会被正常接续使用,"
                         "请人工核对")


def _reconfirm_confirmation_basis(project: Path, run_id: str, session_id: str | None,
                                  preview: Mapping[str, Any],
                                  ) -> tuple[dict[str, Any], dict[str, Any]]:
    """R01-B:确认依据复验——锁外预检与锁内最终复验共用同一套核对。

    核对:原授权加载并**仍为到期**(撤销/篡改/恢复/其他一律拒绝)、回执存在且
    未提交、会话归属一致、回执当前授权关系与预览一致、候选本体逐字节一致、
    采用绑定与当前事实无漂移、无进行中运行。任何一项不成立即抛对应错误。
    返回 (当前回执, 复核通过的原授权)。
    """
    previous_id = preview["previous_grant"]["grant_id"]
    try:
        original = grants.load_grant(project, str(previous_id))
    except grants.GrantError as exc:
        # R01-B:确认边界上原授权已不可按原样核对(篡改/丢失等)——如实拒绝,
        # 不把结构不可信的对象当作签发范围来源。
        code = exc.code if isinstance(getattr(exc, "code", None), str) and exc.code \
            else "grant_unverifiable"
        raise GenericRunError(
            code, "确认过程中原授权不可核对(grant " + str(previous_id) + "):"
            + sanitize_text(str(exc))[:120] + ";本次确认取消,未签发任何新授权") from exc
    # 复验①——原授权在预览之后可能已被正式撤销/篡改/恢复;以核对瞬间为准,
    # 只有"仍为到期"可继续。任何偏离都不得签发。
    still_ok, still_why = grants.grant_valid(original)
    if still_why == "grant_revoked":
        raise GenericRunError("grant_revoked",
                              "确认过程中原授权已被正式撤销(grant " + str(previous_id)
                              + ");已撤销的授权不能由新批次确认顶替,本次确认取消,"
                                "未签发任何新授权")
    if still_why == "grant_tampered":
        raise GenericRunError("grant_tampered",
                              "确认过程中原授权被篡改(grant " + str(previous_id)
                              + ");拒绝通过新批次确认接续,请人工核对授权文件")
    if still_ok:
        raise GenericRunError("batch_still_active",
                              "确认过程中原授权恢复有效(grant " + str(previous_id)
                              + ");直接接续即可,无需新批次确认")
    if still_why != "grant_expired":
        raise GenericRunError("grant_state_invalid",
                              "确认过程中原授权状态变为 " + str(still_why)
                              + "(grant " + str(previous_id) + ");不能通过新批次确认接续")
    # 复验②——确认依据(回执/会话/候选身份/采用绑定/当前事实/运行状态)与预览
    # 时仍一致;期间任何变化都使本次确认过期。
    recheck = load_receipt(project, run_id)
    if recheck is None:
        raise GenericRunError("run_not_found", "运行不存在:" + str(run_id))
    if recheck.get("transaction_id"):
        raise GenericRunError("candidate_already_committed",
                              "确认过程中该运行的候选已提交;本次确认取消")
    if session_id is not None and str(recheck.get("session_id") or "") != str(session_id):
        raise GenericRunError("confirmation_identity_mismatch",
                              "确认过程中运行归属发生变化;拒绝跨会话的新批次确认")
    recheck_id, recheck_grant, recheck_state = _evaluated_grant_state(project, recheck)
    if recheck_id != previous_id or recheck_state != "grant_expired":
        raise GenericRunError("confirmation_state_changed",
                              "确认过程中回执的当前授权关系已变化(评估对象 "
                              + str(recheck_id) + ",状态 " + str(recheck_state)
                              + ");本次确认取消,请刷新预览后重新确认")
    if recheck_grant is None or recheck_grant.get("grant_id") != original.get("grant_id"):
        raise GenericRunError("confirmation_state_changed",
                              "确认过程中原授权文件已不可按原样核对;本次确认取消,"
                              "未签发任何新授权")
    recheck_candidate, recheck_reject = _pending_candidate(project, run_id, recheck)
    if recheck_candidate is None or recheck_reject:
        raise GenericRunError(recheck_reject or "resume_material_missing",
                              "确认过程中候选本体校验未通过;本次确认取消")
    if (recheck_candidate["attempt"] != preview["candidate"]["attempt"]
            or recheck_candidate.get("request_id") != preview["candidate"]["request_id"]
            or sha256_bytes(canonical_json(
                [{"path": item["path"], "content": item["content"]}
                 for item in recheck_candidate["files"]]))
            != preview["candidate"]["content_sha256"]):
        raise GenericRunError("confirmation_state_changed",
                              "确认过程中候选已变化;本次确认取消,请刷新预览后重新确认")
    recheck_binding = (recheck.get("ai_binding")
                       if isinstance(recheck.get("ai_binding"), dict) else None)
    _verify_adoption_binding(project, recheck_binding)
    recheck_drift = _input_drift_during_request(project, recheck_binding)
    if recheck_drift:
        raise GenericRunError("adoption_input_drift",
                              "确" + recheck_drift + ";确认过程中当前事实已变化,本次确认取消")
    if active_run(project) is not None:
        raise GenericRunError("run_active",
                              "确认过程中出现进行中的生成运行;本次确认取消,请稍后重试")
    return recheck, original


# R01-B:确认提交临界区的有界等待上限(临界区为毫秒级,超时仅见于异常占用)。
_CONFIRM_LOCK_WAIT_TIMEOUT = 10.0
# 确认串行化锁的崩溃残留接管阈值(秒):仅用于"持有者进程已确认退出"后的
# 残留判定,**不再**单独作为抢占存活持有者的依据。
_CONFIRM_LOCK_STALE_SECONDS = 60.0
# 持锁期间的心跳:活着的慢持有者(慢 I/O/被调度挂起)靠心跳证明自己仍在工作,
# 不被年龄阈值误判为崩溃残留。
_CONFIRM_LOCK_HEARTBEAT_INTERVAL = 1.0
_CONFIRM_LOCK_HEARTBEAT_TIMEOUT = 6.0

_PROCESS_TOKEN = uuid.uuid4().hex
_CONFIRM_HEARTBEATS: dict[str, threading.Event] = {}
_CONFIRM_OWNED: dict[int, list[str]] = {}
_CONFIRM_LAST_REASON = ""


def _pid_alive(pid: Any) -> bool | None:
    """进程存活探测:True/False,无法确定返回 None(调用方按保守处理)。

    Windows 上 ``os.kill(pid, 0)`` 对已退出进程仍可能成功,因此改用
    OpenProcess + GetExitCodeProcess(STILL_ACTIVE=259)判定。
    """

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                                    ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok) and int(code.value) == 259
            finally:
                kernel32.CloseHandle(handle)
        except (OSError, AttributeError, ImportError, ValueError):
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _confirm_lock_path(root: Path) -> Path:
    return _runs_dir(root) / ".confirm-serialization.lock"


def _confirm_lock_payload(generation: str) -> dict[str, Any]:
    return {
        "kind": "confirmation",
        "pid": os.getpid(),
        "process_token": _PROCESS_TOKEN,
        "thread_id": threading.get_native_id(),
        "generation": generation,
        "acquired_at": _now(),
        "heartbeat_at": time.time(),
    }


def _confirm_lock_write(path: Path, payload: Mapping[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp-" + str(payload.get("generation") or "x"))
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, path)


def _confirm_heartbeat_loop(path: Path, generation: str, stop: threading.Event) -> None:
    while not stop.wait(_CONFIRM_LOCK_HEARTBEAT_INTERVAL):
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(info, dict) or info.get("generation") != generation:
            return  # 锁已易主:不再触碰继任者的锁
        info["heartbeat_at"] = time.time()
        try:
            _confirm_lock_write(path, info)
        except OSError:
            return


def _confirm_start_heartbeat(root: Path, generation: str) -> None:
    stop = threading.Event()
    worker = threading.Thread(target=_confirm_heartbeat_loop,
                              args=(_confirm_lock_path(root), generation, stop),
                              name="confirm-lock-heartbeat", daemon=True)
    worker.start()
    _CONFIRM_HEARTBEATS[generation] = stop


def _confirm_owner_state(info: Mapping[str, Any]) -> tuple[str, bool]:
    """判定既有锁的持有者状态：(原因码, 可否接管)。

    只在**能确认持有者进程已退出**时才接管;存活、存活但心跳停止、存活
    性未知三种情况一律不抢占,由调用方按有界等待后明确拒绝。
    """

    if not isinstance(info, dict):
        return "owner_record_unreadable", False
    pid = info.get("pid")
    alive = _pid_alive(pid)
    if alive is None:
        return "owner_liveness_unknown", False
    if alive and pid == os.getpid() and info.get("process_token") != _PROCESS_TOKEN:
        # 同一 pid 被本进程的新实例复用:上一实例已不存在,可接管
        return "owner_process_replaced", True
    if alive:
        heartbeat = info.get("heartbeat_at")
        if isinstance(heartbeat, (int, float)) and \
                time.time() - heartbeat > _CONFIRM_LOCK_HEARTBEAT_TIMEOUT:
            return "owner_alive_silent", False
        return "owner_busy", False
    return "owner_process_exited", True


def _confirm_reserve(root: Path) -> bool:
    """R01-B:确认提交临界区的原子预约(O_EXCL)——与生成运行锁相互独立。

    收口后的所有者语义(W0, 2026-09-30):
    - 同线程重入立即拒绝(嵌套确认永远等不到本栈释放);
    - 持有者**存活**即不接管:不因锁年龄达到阈值删掉活持有者或存活未知的
      持有者,慢 I/O 的活线程靠心跳自证;
    - 仅在能确认持有者进程已退出(或 pid 已被新进程实例复用)时接管残留,
      并记录接管原因;
    - 每次预约持有唯一 ``generation``,释放只删除本代际的锁,旧持有者不会
      误删继任者的锁。
    """
    global _CONFIRM_LAST_REASON
    lp = _confirm_lock_path(root)
    lp.parent.mkdir(parents=True, exist_ok=True)
    for _round in range(4):
        generation = uuid.uuid4().hex
        payload = _confirm_lock_payload(generation)
        try:
            fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, json.dumps(payload, ensure_ascii=False,
                                        sort_keys=True).encode("utf-8"))
            finally:
                os.close(fd)
        except FileExistsError:
            pass
        else:
            _CONFIRM_OWNED.setdefault(threading.get_ident(), []).append(generation)
            _confirm_start_heartbeat(root, generation)
            _CONFIRM_LAST_REASON = ""
            return True
        try:
            info = json.loads(lp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        if isinstance(info, dict) \
                and info.get("thread_id") == threading.get_native_id() \
                and info.get("pid") == os.getpid() \
                and info.get("process_token") == _PROCESS_TOKEN:
            _CONFIRM_LAST_REASON = "reentrant"
            return False  # 同线程重入:立即拒绝,不等待也不伪装成功
        reason, takeover = _confirm_owner_state(info)
        if not takeover:
            _CONFIRM_LAST_REASON = reason
            return False
        try:
            lp.unlink()
        except OSError:
            _CONFIRM_LAST_REASON = "stale_lock_unremovable"
            return False
        _CONFIRM_LAST_REASON = "takeover:" + reason
    _CONFIRM_LAST_REASON = "takeover_retry_exhausted"
    return False


def _confirm_release(root: Path, generation: str | None = None) -> bool:
    """只释放本代际(或本进程本线程最近一次预约)的锁;继任者锁绝不删除。"""

    lp = _confirm_lock_path(root)
    if generation is None:
        owned = _CONFIRM_OWNED.get(threading.get_ident()) or []
        generation = owned.pop() if owned else None
    try:
        info = json.loads(lp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(info, dict):
        return False
    current = str(info.get("generation") or "")
    if generation is not None:
        if current != generation:
            return False
    elif info.get("pid") != os.getpid() \
            or info.get("process_token") != _PROCESS_TOKEN:
        return False
    try:
        lp.unlink()
    except OSError:
        return False
    stop = _CONFIRM_HEARTBEATS.pop(current, None)
    if stop is not None:
        stop.set()
    return True


def _confirm_reentrant(root: Path) -> bool:
    """R01-B:当前锁持有人是否为同进程同一线程(嵌套确认场景,等待无意义)。"""
    try:
        info = json.loads(_confirm_lock_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (info.get("thread_id") == threading.get_native_id()
            and info.get("pid") == os.getpid()
            and info.get("process_token") == _PROCESS_TOKEN)


def confirm_new_batch_for_resume(root: str | Path, run_id: str, *,
                                  session_id: str | None = None,
                                  issued_by: str = "工作台用户",
                                  note: str = "") -> dict[str, Any]:
    """R01:批次到期后的**正式新批次确认**——服务签发新批次并绑定回执。

    与"非法祖先续签"的区分:新批次由本入口在用户明确确认后签发,签发事件
    (回执 ``resume_confirmations``)绑定当前项目/会话/任务/run/原候选身份/
    当前采用与前像保护/旧授权与新活跃授权的关系;新授权的 ``issued_by``
    记录确认来源。用户不手填 grant/hash/JSON,不通过改私有回执文件完成操作。

    边界(与 FIX-01 一致):
    - 只有**到期**的当前授权可被用户确认的新批次接替;仍有效 → 提示直接
      接续;已撤销/被篡改/不可核对 → 拒绝,不得由新批次顶替;
    - 新批次范围(allowed_paths/excluded_paths/action_kinds/data_scope)逐项
      承接原授权,不扩大;预算固定零 AI 请求,不自动扩额;
    - 回执的 ``grant_id``(最初授权)永不改写;``grant_id_active`` 记录本次
      确认签发的新活跃授权;确认后任何撤销/事实/候选变化仍由原共享闸拒绝。

    R01-B(一致提交边界):预览与签发之间存在真实时间窗——原授权可能已被正式
    撤销、篡改或恢复,回执/候选/采用关系也可能变化,同一运行还可能出现重叠的
    两次确认。为此整个提交段在 per-run 原子预约锁的**有界临界区**内完成:
    锁外预检(同一套核对快速失败)→ 拿锁 → 锁内最终复验 → 签发 → 绑定前复查
    (原授权状态 + 回执活跃位)→ 写回执。据此:
    - 锁内复验发现活跃关系已被另一确认更新 → 后到者在**签发前**直接拒绝,
      不产生第二份签发(重叠确认只允许一份成功,较早快照不覆盖较新绑定);
    - 签发调用内部/紧前发生的正式撤销(较晚撤销)在绑定前复查中被捕获 →
      立即回退撤销本次签发并拒绝,原撤销记录保留;
    - 回执落盘失败 → 立即回退撤销本次签发;撤销结果如实陈述(未核实不得写
      "已撤销"),错误码 confirmation_commit_failed;
    - 锁获取失败(异常占用)按 confirmation_busy 明确拒绝,不伪装业务结论。
    """
    project = Path(root)
    preview = describe_resume_confirmation(project, run_id, session_id=session_id)
    previous_id = preview["previous_grant"]["grant_id"]
    state = preview["previous_grant"]["state"]
    if state != "grant_expired":
        if state == "grant_active":
            raise GenericRunError("batch_still_active",
                                  "当前批次授权仍有效(grant " + str(previous_id)
                                  + ");直接接续即可,无需新批次确认")
        if state == "grant_revoked":
            raise GenericRunError("grant_revoked",
                                  "当前活跃授权已被正式撤销(grant " + str(previous_id)
                                  + ");已撤销的授权不能由新批次确认顶替——如需继续该需求,"
                                    "请重新评估、采用并生成新候选(受同一逻辑任务累计额度约束)")
        if isinstance(state, str) and state.startswith("grant_unverifiable"):
            raise GenericRunError("grant_unverifiable",
                                  "当前授权不可核对(grant " + str(previous_id)
                                  + ");拒绝通过新批次确认接续,请人工核对授权文件")
        raise GenericRunError("grant_state_invalid",
                              "当前授权状态为 " + str(state) + ";不能通过新批次确认接续")
    confirmed_by = sanitize_text(str(issued_by or "").strip() or "工作台用户")
    # 锁外预检:与锁内最终复验同一套核对,先快速失败(提示语义保持);
    # 真正的提交判定在下方临界区内进行。
    _reconfirm_confirmation_basis(project, run_id, session_id, preview)
    # ---- R01-B:一致提交临界区(有界等待 per-run 原子预约锁) ----
    # 确认、接续与另一确认在同一临界区串行;锁内完成"最终复验 → 签发 →
    # 绑定前复查(原授权状态 + 活跃位) → 写回执",使授权依据、活跃关系、
    # 确认事件与新授权可用性相互匹配。锁获取失败按明确业务码拒绝,
    # 不把等待当作确认成功,也不以超时伪装业务结论。
    if not _confirm_reserve(project):
        if _confirm_reentrant(project):
            # 同线程嵌套确认:锁在本请求栈内不可能释放,立即按明确业务码拒绝,
            # 不做无谓等待;已签发的部分由下方统一回退。
            raise GenericRunError("confirmation_busy",
                                  "同一请求内嵌套的确认被拒绝;确认临界区正在处理中"
                                  "(本次未签发任何新授权)")
        deadline = time.monotonic() + _CONFIRM_LOCK_WAIT_TIMEOUT
        acquired = False
        while time.monotonic() < deadline and not acquired:
            time.sleep(0.05)
            acquired = _confirm_reserve(project)
        if not acquired:
            raise GenericRunError(
                "confirmation_busy",
                "同一运行的确认/接续正在处理中(持有者状态:"
                + (_CONFIRM_LAST_REASON or "unknown") + ");请稍后重试"
                "(本次未签发任何新授权;锁不会按年龄抢占仍存活的持有者)")
    try:
        # 锁内最终复验:等待/竞争锁期间依据可能已变化(含另一确认已完成提交)
        # ——后到者在**签发之前**就看到已更新的活跃关系,直接拒绝,不产生第二份签发。
        receipt, original = _reconfirm_confirmation_basis(project, run_id, session_id,
                                                          preview)
        new_grant = grants.issue_batch_grant(
            project,
            goal="接续原候选的新批次确认(运行 " + run_id + ";零新 AI 请求)",
            allowed_paths=list(original["allowed_paths"]),
            excluded_paths=list(original.get("excluded_paths") or []),
            action_kinds=list(original["action_kinds"]),
            data_scope=str(original.get("data_scope") or "synthetic-local"),
            issued_by="新批次确认(接续原候选):" + confirmed_by,
            budget=dict(_RESUME_CONFIRM_BUDGET),
        )
        new_gid = str(new_grant["grant_id"])
        try:
            # 绑定前复查①:回执仍存在、活跃位未被签发窗口内的其他提交更新。
            bind_check = load_receipt(project, run_id)
            if bind_check is None:
                raise GenericRunError("run_not_found", "运行不存在:" + str(run_id))
            if str(bind_check.get("grant_id_active") or "") != \
                    str(receipt.get("grant_id_active") or ""):
                raise GenericRunError("confirmation_state_changed",
                                      "签发过程中回执活跃授权已被更新;"
                                      "请刷新预览后重新确认")
            # 绑定前复查②:原授权仍为到期——正式撤销可能发生在签发调用内部/
            # 紧前(审核 q05 较晚撤销形态);若原授权已变,本次签发不得绑定。
            try:
                original_now = grants.load_grant(project, str(previous_id))
                still_valid, still_state = grants.grant_valid(original_now)
            except grants.GrantError as exc:
                still_valid = False
                still_state = exc.code if isinstance(getattr(exc, "code", None), str) \
                    and exc.code else "grant_tampered"
            if still_state == "grant_revoked":
                raise GenericRunError("grant_revoked",
                                      "确认提交过程中原授权已被正式撤销(grant "
                                      + str(previous_id) + ");原撤销记录保留")
            if still_state == "grant_tampered":
                raise GenericRunError("grant_tampered",
                                      "确认提交过程中原授权被篡改(grant "
                                      + str(previous_id) + ");请人工核对授权文件")
            if still_valid:
                raise GenericRunError("batch_still_active",
                                      "确认提交过程中原授权恢复有效(grant "
                                      + str(previous_id) + ");直接接续即可")
            if still_state != "grant_expired":
                raise GenericRunError("grant_state_invalid",
                                      "确认提交过程中原授权状态变为 " + str(still_state)
                                      + "(grant " + str(previous_id) + ")")
            # 绑定:确认事件与新活跃授权一起写入回执。
            event = {
                "at": _now(),
                "run_id": run_id,
                "session_id": bind_check.get("session_id"),
                "task_id": bind_check.get("task_id") or TASK_ID,
                "original_grant_id": bind_check.get("grant_id"),
                "previous_active_grant_id": bind_check.get("grant_id_active"),
                "previous_grant_state": state,
                "new_grant_id": new_grant["grant_id"],
                "issued_by": confirmed_by,
                "note": sanitize_text(str(note or ""))[:200],
                "candidate_attempt": preview["candidate"]["attempt"],
                "candidate_request_id": preview["candidate"]["request_id"],
                "candidate_content_sha256": preview["candidate"]["content_sha256"],
                "candidate_files": list(preview["candidate"]["files"]),
                "adopted_plan_digest": bind_check.get("adopted_plan_digest"),
                "preimage_paths": sorted((bind_check.get("preimage_at_dispatch") or {}).keys()),
                "zero_ai_requests": True,
                "new_batch_budget": dict(new_grant["budget"]),
            }
            bind_check.setdefault("resume_confirmations", []).append(event)
            bind_check["grant_id_active"] = new_grant["grant_id"]
            # W0 协调点:以撤销计数为线性化点——建立依据后、落盘前若发生正式
            # 撤销,计数必然变化,落盘即被拒绝;撤销完成在落盘之后的真实次序
            # 也如实记录,不伪造撤销顺序。
            revocation_point = grants.revocation_epoch(project)
            event["revocation_epoch_at_commit"] = revocation_point
            _COMMIT_REVOCATION_BASIS[threading.get_ident()] = revocation_point
            try:
                _write_receipt(project, bind_check,
                               expect_revocation_epoch=revocation_point)
            finally:
                _COMMIT_REVOCATION_BASIS.pop(threading.get_ident(), None)
        except BaseException as exc:
            # R01-B:签发之后任何未完成绑定(复查拒绝、回执落盘失败、调度侧
            # 注入、非预期异常/中断)都必须回退撤销本次签发——不留"有效但
            # 无事件"的授权;撤销结果如实附加,未核实不得写"已撤销"。
            revert_ok, revert_detail = _revert_issued_grant(
                project, new_gid,
                "确认未完成绑定;回退撤销本次签发的授权以防未绑定孤儿授权")
            suffix = "本次签发的新授权(grant " + new_gid + ")" + revert_detail
            if isinstance(exc, GenericRunError):
                raise GenericRunError(exc.code, str(exc.args[0]) + ";" + suffix) from exc
            if isinstance(exc, OSError):
                raise GenericRunError("confirmation_commit_failed",
                                      "确认回执写入失败(" + sanitize_text(str(exc))[:120]
                                      + ");" + suffix) from exc
            raise
    finally:
        _confirm_release(project)
    later_epoch = grants.revocation_epoch(project)
    commit_order = "commit_then_revoke" if later_epoch != event.get(
        "revocation_epoch_at_commit") else "no_revocation_during_commit"
    return {
        "status": "confirmed",
        "run_id": run_id,
        "task_id": event["task_id"],
        "new_grant_id": new_grant["grant_id"],
        "previous_grant": {"grant_id": previous_id, "state": state},
        "commit_order": commit_order,
        "revocation_epoch_at_commit": event.get("revocation_epoch_at_commit"),
        "revocation_epoch_after_commit": later_epoch,
        "candidate": {"attempt": preview["candidate"]["attempt"],
                      "request_id": preview["candidate"]["request_id"],
                      "files": list(preview["candidate"]["files"]),
                      "content_sha256": preview["candidate"]["content_sha256"]},
        "allowed_paths": list(new_grant["allowed_paths"]),
        "action_kinds": list(new_grant["action_kinds"]),
        "budget": dict(new_grant["budget"]),
        "confirmation_event": dict(event),
        "note": "新批次已确认并绑定本次接续;候选与采用关系保持不变,接续将复用"
                "既有候选且零新 AI 请求;后续任何撤销/事实/候选变化仍按原控制闸拒绝",
        "resume_hint": "POST /api/project/<name>/session/<sid>/resume {\"run_id\": \""
                       + run_id + "\"}",
    }


def _resume_grant_for_resume(project: Path, receipt: Mapping[str, Any],
                             attempt_record: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """C6-01(U03)/FIX-01:接续的授权判定——核对**当前活跃授权**及其父链。

    - 判定对象是当前活跃授权(回执 ``grant_id_active`` 优先,回退到最初
      ``grant_id``);两者都留痕在 ``attempt_record["grant_chain"]``;
    - 活跃授权仍有效 → 原样复用(零新 AI 请求的正常接续不退化);
    - 活跃授权已被**撤销**(明确的主权动作)、被篡改、不可读、不存在 → **拒绝**;
      不得回退到已过期的最初授权再"续签"顶替——过期祖先不能复活为授权链;
    - 批次**到期** → **不自动签发新批次**(FIX-01:接续按钮本身不构成新的
      批次确认;原"到期自动续发"路径废除,授权层级的短期凭据签发只发生在
      仍有效的批次内部,见 grants.issue_step_credential)。
      需用户通过正常授权入口完成新的批次确认后再接续,候选保持保留;
    - 回执的 ``grant_id``(最初授权)永不改写;``grant_id_active`` 只记录
      实际复核通过的授权。
    """
    original_id = receipt.get("grant_id")
    active_id = receipt.get("grant_id_active")
    target_id = active_id if isinstance(active_id, str) and active_id else \
        (original_id if isinstance(original_id, str) else None)
    attempt_record["grant_chain"] = {
        "original": original_id if isinstance(original_id, str) else None,
        "active_before_resume": active_id if isinstance(active_id, str) else None,
        "evaluated": target_id,
    }
    if not isinstance(target_id, str) or not target_id:
        attempt_record["grant_rejected"] = {"grant_id": None, "reason": "grant_id_missing"}
        return None, ("接续回执缺少可核对的授权记录;不自动签发新授权,"
                      "候选保持保留,请通过正常入口重新授权")
    try:
        current = grants.load_grant(project, target_id)
    except grants.GrantError as exc:
        # 不可读/不存在:保守拒绝,不回退到链上其他授权
        attempt_record["grant_rejected"] = {
            "grant_id": target_id,
            "reason": "grant_unverifiable:" + sanitize_text(str(exc))[:120]}
        return None, ("接续授权不可核对(grant " + target_id + " 读取失败:"
                      + sanitize_text(str(exc))[:80] + ");不自动换发新授权,候选保持保留")
    ok, why = grants.grant_valid(current)
    if ok:
        attempt_record["grant_used"] = target_id
        attempt_record["grant_source"] = ("reused_active" if active_id == target_id
                                          and original_id != target_id else "reused_original")
        return dict(current), None
    if why == "grant_expired":
        # FIX-01:整个批次到期 ≠ 自动新签。到期授权的"续发"需要一个真实的
        # 新批次确认(正常授权入口),接续入口不代签——否则任何过期链都能
        # 通过反复点击 resume 无限续命。
        attempt_record["grant_rejected"] = {
            "grant_id": target_id, "reason": "grant_expired",
            "note": "batch_expired_needs_fresh_confirmation"}
        return None, ("当前批次授权已到期(grant " + target_id
                      + ");接续不自动签发新批次——请通过正常授权入口完成新的批次确认后再接续,"
                        "候选保持保留")
    # grant_revoked / grant_tampered 及其他任何失效:一律不自动换发,
    # 也不回退到最初授权续签顶替(V02 反例:被撤销的活跃授权 G2 不得被
    # 从过期 G1 续出的 G3 顶替)。
    attempt_record["grant_rejected"] = {"grant_id": target_id, "reason": why}
    if why == "grant_revoked":
        return None, ("当前批次授权已被撤销(grant_revoked,grant " + target_id
                      + ");不自动换发新授权——已撤销的活跃授权不能由接续自动顶替,"
                        "候选保持保留,请通过正常入口重新授权后再接续")
    return None, ("接续授权已失效(" + why + ",grant " + target_id
                  + ");不自动换发新授权,候选保持保留,请通过正常入口重新授权后再接续")


def _resume_inner(project: Path, receipt: dict[str, Any], candidate: Mapping[str, Any],
                  binding: dict[str, Any] | None) -> dict[str, Any]:
    run_id = str(receipt["run_id"])
    outputs = sorted({item["path"] for item in candidate["files"]}) or \
        list((receipt.get("preimage_at_dispatch") or {}).keys())
    task = {"task_id": receipt.get("task_id") or TASK_ID, "outputs": outputs}
    attempt = int(receipt.get("ai_requests_dispatched", 0) or 0) + 1
    attempt_record: dict[str, Any] = {"attempt": attempt, "kind": "resume",
                                      "request_id": candidate.get("request_id"),
                                      "resumed_from_attempt": candidate["attempt"],
                                      "ai_request_dispatched": False}
    receipt["attempts"].append(attempt_record)
    expected = dict(receipt.get("preimage_at_dispatch") or {})

    # C6-01(U03-取消):接续正式开始先持久化真实运行阶段——
    # request_cancel 必须能在接续进行中登记并中断后继,不能仍显示旧终态。
    receipt["status"] = "running"
    # 接续会覆盖旧的阻塞/授权失败终态;若本次仍失败,由本次闸门重新写入
    # 精确原因,避免交付回执残留此前的 grant_expired 等历史文案。
    receipt.pop("failure", None)
    receipt["cancel_requested"] = False
    receipt["current_request"] = {"request_id": candidate.get("request_id"),
                                  "kind": "resume", "attempt": attempt,
                                  "effect": "resuming"}
    _write_receipt(project, receipt)

    def _terminalize(status: str, failure: str | None, effect: str) -> dict[str, Any]:
        receipt["status"] = status
        if failure:
            receipt["failure"] = failure
        receipt["current_request"]["effect"] = effect
        receipt["finished_at"] = _now()
        _write_receipt(project, receipt)
        return receipt

    # C6-01(U03)/FIX-01:复核**当前活跃授权**及其父链;撤销/篡改/不可读/
    # 到期一律不换发(到期需用户正常入口的新批次确认),正常零 AI 接续不退化。
    grant, grant_reject = _resume_grant_for_resume(project, receipt, attempt_record)
    if grant is None:
        return _terminalize("failed", grant_reject, "rejected")
    # 接续 receipt 如实记录**实际使用**的授权(原 receipt.grant_id 不改写)
    receipt["grant_id_active"] = attempt_record.get("grant_used")

    capability = _capability()
    # 后端能力可能在候选保存后恢复;接续回执必须记录本次实际复核结果,
    # 不能继续沿用首次生成时的 unavailable 快照。
    receipt["execution_capability"] = capability
    if not capability["available"]:
        attempt_record["executed"] = False
        attempt_record["note"] = ("实际受限后端仍不可用(" + str(capability["kind"])
                                  + ");候选保持可接续,未执行、未提交")
        return _terminalize("blocked_execution", None, "blocked_execution")

    # 边界:隔离执行前——与普通生成共用同一控制闸(取消/绑定/事实/授权)
    gate = _control_gate(project, run_id=run_id, stage="接续隔离执行前",
                         binding=binding, grant=grant)
    if gate:
        attempt_record["gate_reject"] = dict(gate)
        if gate["reason"] == "cancel":
            receipt["cancel_requested"] = True
            return _terminalize("cancelled", None, "cancelled")
        attempt_record["executed"] = False
        return _terminalize("failed", "接续" + gate["detail"] + ";未执行、未提交", "rejected")

    ok, evidence, _digests = _stage_and_verify(
        project, grant, task, candidate["files"], run_id, attempt,
        _make_verifier(project, run_id, str((receipt.get("frozen_checks") or {}).get(
            "data_dir", "app/data"))))
    attempt_record["executed"] = True
    attempt_record["verified"] = ok
    attempt_record["evidence"] = evidence
    if not ok:
        return _terminalize("failed",
                            "接续候选未通过隔离验收:" + ";".join(str(e)[:120] for e in evidence[:4]),
                            "rejected")

    # 边界:提交前——与普通生成共用同一控制闸(取消/绑定/事实/授权/前像)
    gate = _control_gate(project, run_id=run_id, stage="接续提交前",
                         binding=binding, grant=grant, expected=expected)
    if gate:
        attempt_record["gate_reject"] = dict(gate)
        if gate["reason"] == "cancel":
            receipt["cancel_requested"] = True
            return _terminalize("cancelled", None, "cancelled")
        if gate["reason"] == "preimage_drift":
            attempt_record["preimage_drift"] = gate.get("paths") or []
        return _terminalize("failed", gate["detail"] + ";拒绝提交旧候选", "rejected")
    try:
        tx_before = {p.name for p in (project / ".opencoding" / "transactions").glob("*")} \
            if (project / ".opencoding" / "transactions").is_dir() else set()
        committed = _commit_candidate(project, grant, task, candidate["files"], run_id,
                                      task["task_id"], attempt, expected)
        tx_after = {p.name for p in (project / ".opencoding" / "transactions").glob("*")}
        receipt["status"] = "delivered"
        receipt["committed_file_digests"] = committed
        receipt["transaction_id"] = sorted(tx_after - tx_before)[-1] if (tx_after - tx_before) else None
        receipt["current_request"]["effect"] = "applied"
        receipt["finished_at"] = _now()
        _write_receipt(project, receipt)
        return receipt
    except Exception as exc:  # noqa: BLE001 - 提交链异常必须终态化,不留 running 假状态
        attempt_record["error_code"] = type(exc).__name__
        attempt_record["error"] = sanitize_text(str(exc))[:300]
        return _terminalize("failed",
                            "接续提交失败(" + type(exc).__name__ + ");候选保持保留,"
                            "事务层状态以 .opencoding/transactions 记录为准",
                            "rejected")


def _input_drift_during_request(project: Path, binding: dict[str, Any] | None) -> str | None:
    """CP5：请求进行中相关输入是否发生变化；返回漂移说明或 None。

    只在采用链存在时核对（`binding` 来自工作台采用记录）。事实、会话答案、
    采用的有效输入快照任一变化，旧候选即不再代表当前需求，不可验证、不可提交。
    核对失败（读不到/不可解析）同样按漂移处理，不放过看不清的情况。
    """
    if not binding:
        return None  # 非采用链路不强制此项
    sid = str(binding.get("session_id") or "")
    if not sid:
        return None
    from . import service as service_module

    try:
        status = service_module.adoption_input_status(project, sid)
    except Exception as exc:  # noqa: BLE001 - 服务层异常不得放行:看不清等同于已变化
        return "采用输入不可核对(" + type(exc).__name__ + ":" + str(exc)[:120] + ")"
    if not status.get("adopted"):
        return "采用记录已不存在(可能被撤销或重建)"
    if status.get("input_snapshot_missing"):
        return "采用记录缺少有效输入快照"
    drift = [str(item) for item in (status.get("drift") or [])]
    if drift:
        return "相关有效输入已变化(" + ",".join(drift) + ")"
    return None


def _verify_adoption_binding(project: Path, binding: dict[str, Any] | None) -> None:
    """R07:采用绑定复核——生成开始与提交前各一次。

    要求当前采用记录仍存在,且 ai_binding(evaluation_id/contract_sha256/plan_digest)
    与派发时传入完全一致;确认后的会话修订变化、重新评估、重新采用都会导致不一致 → 拒绝。
    """
    if not binding:
        return  # 非工作台链路(如 CLI 直调)不强制;工作台路由必须传
    sid = str(binding.get("session_id") or "")
    if not sid or any(ch in sid for ch in "/\\:*?\"<>|"):
        raise GenericRunError("adoption_binding_invalid", "采用绑定的会话标识无效")
    path = project / ".opencoding" / "adoptions" / (sid + ".json")
    if not path.is_file():
        raise GenericRunError("adoption_binding_missing", "采用记录已不存在;请重新评估并采用")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GenericRunError("adoption_binding_missing", "采用记录不可读:" + str(exc)) from exc
    ai = doc.get("ai_binding") or {}
    for key in ("evaluation_id", "contract_sha256"):
        if str(ai.get(key) or "") != str(binding.get(key) or ""):
            raise GenericRunError(
                "adoption_binding_drifted",
                "采用绑定已变化(" + key + ");请基于当前已确认评估重新采用后再生成")
    if str(doc.get("plan_digest") or "") != str(binding.get("plan_digest") or ""):
        raise GenericRunError("adoption_binding_drifted",
                              "采用计划摘要已变化;请基于当前评估重新采用")


def run_generic_app(
    root: str | Path,
    goal: str | None,
    adapter: Any,
    *,
    contract: dict[str, Any],
    adopted_plan_digest: str | None = None,
    acknowledged_unknown: bool = False,
    session_id: str | None = None,
    adoption_binding: dict[str, Any] | None = None,
    run_id: str | None = None,
    max_repair_rounds: int = 2,
) -> dict[str, Any]:
    """完整链:根校验→冻结契约检查→授权→真实 AI 生成→静态检查→(受限后端可用时)隔离验收→事务提交。

    contract: 来自已确认评估的 implementation_contract(经 _validate_contract 规范化)。
    acknowledged_unknown: 项目存在效果未知旧请求时,须显式 True 才允许新派发。
    session_id: 关联工作台会话(权威采用链与累计预算的归属键)。
    adoption_binding: R07 采用绑定 {session_id, evaluation_id, plan_digest, contract_sha256};
        运行开始与提交前各复核一次,任何漂移拒绝继续。
    """
    project = Path(root)
    if not project.is_dir():
        raise GenericRunError("invalid_root", "项目根不存在")
    # R02:全部运行状态 I/O 之前完成根/祖先校验——拒绝时零锁、零回执、零派发
    validate_runtime_roots(project)
    if adapter is None:
        raise GenericRunError("adapter_required", "必须提供已配置的真实 AI 适配器")
    if not isinstance(contract, dict) or "files" not in contract:
        raise GenericRunError("contract_required", "必须提供已确认评估的实现契约")
    existing = active_run(project)
    if existing is not None:
        return {"status": "already_running", "run_id": existing["run_id"],
                "note": "已有进行中的生成运行;可先取消它再重新派发"}
    # R03:存在效果未知的旧请求时,新派发必须先显式确认(acknowledge_unknown)
    if not acknowledged_unknown:
        pending = unknown_effect_run(project)
        if pending is not None:
            return {"status": "pending_verification", "run_id": pending["run_id"],
                    "blocked_by": {"run_id": pending["run_id"],
                                   "request_id": (pending.get("current_request") or {}).get("request_id"),
                                   "failure": pending.get("failure"),
                                   "status": pending.get("status")},
                    "note": "此前有已派发但结果未知的请求(传输失败/中断);"
                            "需先核实或显式确认接受不确定性,才能发起新派发"}
    run_id = run_id or ("gen-" + uuid.uuid4().hex[:12])
    if not re.fullmatch(r"gen-[0-9a-f]{12}", run_id):
        run_id = "gen-" + hashlib.sha256(run_id.encode()).hexdigest()[:12]
    goal = (goal or _read_doc(project, "PRG.md", 300) or "未提供目标").strip()[:300]
    if not _reserve(project, run_id):
        return {"status": "already_running", "run_id": run_id, "note": "原子预约失败:另一运行持锁"}
    try:
        return _run_inner(project, goal, adapter, contract, adopted_plan_digest,
                          run_id, max_repair_rounds,
                          session_id=session_id, adoption_binding=adoption_binding)
    finally:
        _release(project, run_id)


def _run_inner(project: Path, goal: str, adapter: Any, contract: dict[str, Any],
               adopted_plan_digest: str | None, run_id: str, max_repair_rounds: int,
               session_id: str | None = None,
               adoption_binding: dict[str, Any] | None = None) -> dict[str, Any]:
    # R07/CP5/C6-01:派发前最后一道复核(路由校验与真实派发之间的时间窗)——
    # 采用绑定与当前事实经共用控制闸(与接续同一入口);取消检查在循环起点。
    gate = _control_gate(project, run_id=run_id, stage="派发前", binding=adoption_binding)
    if gate:
        if gate["reason"] == "input_drift":
            raise GenericRunError("adoption_input_drift", "派发前采用输入已漂移:" + gate["detail"])
        raise GenericRunError(gate["code"], "派发前" + gate["detail"])
    capability = _capability()
    frozen = _write_frozen_checks(project, run_id, goal, contract)
    outputs = list(contract["files"])

    # CP5:同一逻辑任务的累计额度真正限制派发——新 run 继承剩余额度,不再各自发全额
    base = 4 + 2 * max_repair_rounds
    ledger = open_task_ledger(project, TASK_ID, session_id or "", ceiling=base,
                              run_id=run_id) if session_id else None
    budget_view = task_budget_state(project, TASK_ID, session_id or "") if session_id else {
        "ceiling": None, "dispatched": 0, "remaining": None}
    if session_id and budget_view.get("ledger_unreadable"):
        # C6-03:账本存在但损坏/不可读 → 保守停止,不派发、不重置(真实消费未知)。
        raise GenericRunError(
            "task_budget_ledger_unreadable",
            "累计预算账本不可读(" + str(budget_view["ledger_unreadable"]) + ");"
            "保守停止:不派发、不新建空账本,请人工核对后按原授权规则恢复")
    cap = base
    if session_id:
        remaining = int(budget_view["remaining"])
        if remaining <= 0:
            raise GenericRunError(
                "task_budget_exhausted",
                "同一逻辑任务的累计 AI 请求额度已用尽(上限 " + str(budget_view["ceiling"])
                + ",已派发 " + str(budget_view["dispatched"])
                + ");风险确认不续期、不扩额,追加额度需要新的明确授权")
        cap = min(base, remaining)
    grant = grants.issue_batch_grant(
        project,
        goal="生成可运行产物:" + goal,
        allowed_paths=[APP_DIR],
        action_kinds=["ai_request", "local_write", "local_run"],
        issued_by="工作台用户发起生成(已确认评估+已采用计划);受限后端=" + str(capability["kind"]),
        data_scope="synthetic-local",
        budget={"max_ai_requests": cap, "max_repair_rounds": max_repair_rounds},
        ttl_seconds=3600,
    )
    if session_id:
        # 账本里补上本次运行的授权编号:金额与次数双向可核
        record_dispatch(project, TASK_ID, session_id, run_id,
                        dispatched=0, grant_id=grant["grant_id"])
    # R03-B5:同一逻辑任务的历史请求事实(累计显示,不因新尝试重置)
    lineage = task_lineage(project, session_id, exclude_run_id=run_id)
    # CP1-02:派发前冻结目标前像(用户后改不得成为新前像)
    expected = {rel: (sha256_bytes((project / rel).read_bytes())
                      if (project / rel).exists() else None)
                for rel in outputs}
    receipt: dict[str, Any] = {
        "schema_version": GENERIC_RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "task_id": TASK_ID,
        "session_id": session_id,
        "goal": goal,
        "grant_id": grant["grant_id"],
        "adopted_plan_digest": adopted_plan_digest,
        "ai_binding": dict(adoption_binding) if adoption_binding else None,
        "adapter": {"real": bool(adapter.real), "provider": getattr(adapter, "provider", "?"),
                    "model": getattr(adapter, "model", "?")},
        "execution_capability": capability,
        "frozen_checks": frozen,
        "preimage_at_dispatch": expected,
        "budget_lineage": {"inherited_ai_requests": lineage["inherited_ai_requests"],
                           "inherited_runs": lineage["inherited_runs"],
                           "this_run_limit": cap},
        # CP5:同一逻辑任务的累计额度(跨 run 共享),本次运行只能用完剩余部分
        "task_budget": {"ceiling": budget_view.get("ceiling"),
                        "dispatched_before_run": int(budget_view.get("dispatched", 0) or 0),
                        "remaining_before_run": budget_view.get("remaining"),
                        "extensions": ledger.get("extensions", []) if ledger else []},
        "started_at": _now(),
        "attempts": [],
        "status": "running",
        "cancel_requested": False,
        "ai_requests_dispatched": 0,
    }
    _write_receipt(project, receipt)

    task = {"task_id": TASK_ID, "outputs": outputs}
    attempt = 1
    previous_files: list[tuple[str, str]] = []
    last_failures: list[str] = []
    while attempt <= 1 + max_repair_rounds:
        receipt["heartbeat_at"] = _now()
        _touch_lock(project, run_id)
        _write_receipt(project, receipt)
        if _cancelled(project, run_id):
            receipt["status"] = "cancelled"
            receipt["cancel_requested"] = True
            break
        kind = "implement" if attempt == 1 else "repair"
        nonce = "nonce-" + os.urandom(16).hex()
        request_id = "req-" + os.urandom(16).hex()
        attempt_record: dict[str, Any] = {"attempt": attempt, "kind": kind,
                                          "request_id": request_id, "nonce_prefix": nonce[:14]}
        if session_id:
            state = task_budget_state(project, TASK_ID, session_id)
            if int(state["remaining"]) <= 0:
                receipt["status"] = "failed"
                receipt["failure"] = ("同一逻辑任务累计额度已用尽(上限 " + str(state["ceiling"])
                                      + ",已派发 " + str(state["dispatched"])
                                      + ");不再派发新请求,追加额度需要新的明确授权")
                attempt_record["budget_blocked"] = {
                    "ceiling": state["ceiling"], "dispatched": state["dispatched"]}
                receipt["attempts"].append(attempt_record)
                break
        try:
            inherited_used = int((receipt.get("budget_lineage") or {}).get("inherited_ai_requests", 0))
            remaining = {"max_ai_requests_this_run": cap,
                         "used_this_run": receipt.get("ai_requests_dispatched", 0),
                         "inherited_from_task": inherited_used,
                         "cumulative_task_requests": inherited_used + receipt.get("ai_requests_dispatched", 0),
                         "repair_rounds_left": max(0, 1 + max_repair_rounds - attempt)}
            messages = _implementation_messages(project, goal, contract) if attempt == 1 else \
                _repair_messages(goal, contract, last_failures, previous_files,
                                 frozen_steps=contract.get("steps", []),
                                 remaining_budget=remaining)
            # CP2-03:派发前把请求身份可靠落盘(磁盘先于网络;保存失败则不发送)
            receipt["attempts"].append(attempt_record)
            receipt["ai_requests_dispatched"] = receipt.get("ai_requests_dispatched", 0) + 1
            if session_id:
                # 累计账本按真实派发次数递增(新 run 不再自带全额新额度)
                record_dispatch(project, TASK_ID, session_id, run_id,
                                dispatched=int(receipt["ai_requests_dispatched"]),
                                grant_id=grant["grant_id"])
            receipt["current_request"] = {"request_id": request_id, "nonce_prefix": nonce[:14],
                                          "kind": kind, "attempt": attempt,
                                          "effect": "dispatching"}
            _write_receipt(project, receipt)
            # 预算消耗(失败/超时同样计入真实账本)
            _consume_ai_budget(project, grant, attempt, "")
            result = adapter.complete(
                messages, request_kind=kind, run_id=run_id, task_id=TASK_ID,
                attempt=attempt, allowed_outputs=list(outputs),
                request_id=request_id, nonce=nonce)
            # R03-B1/CP5:先把响应本体可靠落盘(本体先于状态),再标记"已收到"。
            # 否则中断会留下"已收到"却没有可恢复材料的假状态。
            material = _persist_received_material(project, run_id, attempt, request_id, result)
            attempt_record["received_material"] = material
            receipt["current_request"]["effect"] = "received"
            receipt["current_request"]["received_material"] = material
            _write_receipt(project, receipt)
            # CP5/C6-01:响应后边界经共用控制闸——事实/会话变化、绑定漂移、
            # 授权被撤销、取消登记,任一发生旧候选都不进入验证、不提交
            gate = _control_gate(project, run_id=run_id, stage="请求期间",
                                 binding=adoption_binding, grant=grant)
            if gate:
                attempt_record["gate_reject"] = dict(gate)
                if gate["reason"] == "cancel":
                    receipt["status"] = "cancelled"
                    receipt["cancel_requested"] = True
                    attempt_record["executed"] = False
                    break
                receipt["status"] = "failed"
                receipt["failure"] = "请求期间" + gate["detail"] + ";旧候选不可作为当前需求的结果提交"
                if gate["reason"] == "input_drift":
                    attempt_record["input_drift"] = gate["detail"]
                attempt_record["executed"] = False
                break
            attempt_record["latency_ms"] = result.get("latency_ms")
            attempt_record["stream_timing"] = result.get("stream_timing")
            attempt_record["binding_fill"] = result.get("binding_fill")
            structured = result.get("structured") or {}
            files = structured.get("files")
            if _cancelled(project, run_id):
                receipt["status"] = "cancelled"
                receipt["cancel_requested"] = True
                break
            canonical, problems = _static_check(files)
            attempt_record["static_ok"] = not problems
            attempt_record["static_problems"] = problems
            if problems:
                attempt_record["executed"] = False
                last_failures = problems
                previous_files = canonical
            else:
                cand_dir = _save_candidates(project, run_id, attempt, canonical)
                attempt_record["candidate_preserved"] = str(cand_dir)
                if _cancelled(project, run_id):
                    receipt["status"] = "cancelled"
                    receipt["cancel_requested"] = True
                    break
                if not capability["available"]:
                    attempt_record["executed"] = False
                    attempt_record["note"] = ("实际受限后端不可用(" + str(capability["kind"])
                                              + ");候选已保留为未采用材料,未执行、未提交(REAL-14 受阻)")
                    receipt["status"] = "blocked_execution"
                    break
                # CP5/C6-01:隔离执行前再经共用控制闸核一次(等待期间事实/绑定/
                # 授权可能又变了),不隔离执行、不验证、不提交
                gate = _control_gate(project, run_id=run_id, stage="隔离执行前",
                                     binding=adoption_binding, grant=grant)
                if gate:
                    attempt_record["gate_reject"] = dict(gate)
                    if gate["reason"] == "cancel":
                        receipt["status"] = "cancelled"
                        receipt["cancel_requested"] = True
                    else:
                        receipt["status"] = "failed"
                        receipt["failure"] = "执行前" + gate["detail"] + ";未隔离执行、未验证、未提交"
                        if gate["reason"] == "input_drift":
                            attempt_record["input_drift"] = gate["detail"]
                    attempt_record["executed"] = False
                    break
                ok, evidence, digests = _stage_and_verify(
                    project, grant, task, files, run_id, attempt,
                    _make_verifier(project, run_id, str(contract.get("data_dir", "app/data"))))
                attempt_record["executed"] = True
                attempt_record["verified"] = ok
                attempt_record["evidence"] = evidence
                if _cancelled(project, run_id):
                    receipt["status"] = "cancelled"
                    receipt["cancel_requested"] = True
                    break
                if ok:
                    # C6-01:提交前经共用控制闸一次复核——采用绑定、当前事实、
                    # 授权有效性、取消登记、前像仍为派发前冻结值(与接续同入口)
                    gate = _control_gate(project, run_id=run_id, stage="提交前",
                                         binding=adoption_binding, grant=grant,
                                         expected=expected)
                    if gate:
                        attempt_record["gate_reject"] = dict(gate)
                        if gate["reason"] == "cancel":
                            receipt["status"] = "cancelled"
                            receipt["cancel_requested"] = True
                            break
                        if gate["reason"] == "input_drift":
                            attempt_record["input_drift"] = gate["detail"]
                            receipt["failure"] = "提交前" + gate["detail"] + ";拒绝提交旧候选"
                        elif gate["reason"] == "preimage_drift":
                            attempt_record["preimage_drift"] = gate.get("paths") or []
                            receipt["failure"] = "提交前前像漂移(用户在请求期间修改了目标文件),拒绝覆盖:" + ",".join(gate.get("paths") or [])
                        else:
                            receipt["failure"] = "提交前" + gate["detail"] + ";拒绝提交旧候选"
                        receipt["status"] = "failed"
                        break
                    tx_before = {p.name for p in (project / ".opencoding" / "transactions").glob("*")} \
                        if (project / ".opencoding" / "transactions").is_dir() else set()
                    committed = _commit_candidate(project, grant, task, files, run_id, TASK_ID,
                                                  attempt, expected)
                    tx_after = {p.name for p in (project / ".opencoding" / "transactions").glob("*")}
                    receipt["status"] = "delivered"
                    receipt["current_request"]["effect"] = "applied"
                    receipt["committed_file_digests"] = committed
                    receipt["transaction_id"] = sorted(tx_after - tx_before)[-1] if (tx_after - tx_before) else None
                    break
                last_failures = evidence
                previous_files = canonical
        except AIRequestError as exc:
            attempt_record["error_code"] = exc.code
            attempt_record["error"] = sanitize_text(str(exc))[:300]
            if exc.code in ("read_idle_timeout", "network_unavailable", "deadline_exceeded",
                            "gateway_stream_interrupted", "http_5xx", "http_4xx"):
                # CP2-03:传输失败 ≠ 有错误候选;效果未知,不自动伪装 repair
                receipt["current_request"]["effect"] = "unknown"
                receipt["status"] = "failed_transport"
                receipt["failure"] = ("传输失败(" + exc.code + "),已派发请求效果未知;"
                                      "保留请求身份待核实,不自动重发或转为修复")
                break
            last_failures = [exc.code + ": " + sanitize_text(str(exc))[:200]]
            if exc.code in ("config_invalid", "binding_invalid"):
                # 配置/绑定错误发生在网络发送之前:请求未发出,不构成"效果未知"
                receipt["current_request"]["effect"] = "not_dispatched"
                receipt["status"] = "failed"
                receipt["failure"] = "适配层配置错误,停止重试:" + sanitize_text(str(exc))[:200]
                break
        except GenericRunError as exc:
            # C6-01/C6-03:控制层(账本/授权/绑定)拒绝也必须精确终态化,
            # 不与未预期异常混同;请求效果只在尚未派发时改标
            attempt_record["error_code"] = exc.code
            attempt_record["error"] = sanitize_text(str(exc))[:300]
            cr = receipt.setdefault("current_request", {})
            if not cr.get("effect") or cr.get("effect") == "dispatching":
                cr["effect"] = "not_dispatched"
            receipt["status"] = "failed"
            receipt["failure"] = "控制层拒绝,运行终态化:" + exc.code + ":" + sanitize_text(str(exc))[:200]
            break
        except Exception as exc:  # noqa: BLE001 - CP2-03/Q06:一般异常也必须终态化,不留 running
            attempt_record["error_code"] = type(exc).__name__
            attempt_record["error"] = sanitize_text(str(exc))[:300]
            receipt["status"] = "failed"
            receipt["failure"] = "未预期异常,运行终态化:" + type(exc).__name__
            break
        attempt += 1
    else:
        if receipt["status"] == "running":
            receipt["status"] = "exhausted"
    if receipt["status"] == "running":
        receipt["status"] = "failed"
    if receipt.get("current_request", {}).get("effect") == "dispatching":
        receipt["current_request"]["effect"] = "unknown"
    receipt["finished_at"] = _now()
    _write_receipt(project, receipt)
    return receipt


def _capability() -> dict[str, Any]:
    """实际受限后端能力(以 sandbox 实测为准,不做自报)。"""
    return sandbox.execution_capability(guard_verified=False)


def write_worker_outcome(root: str | Path, run_id: str, outcome: Any) -> dict[str, Any] | None:
    """R03-B4/S09:后台 worker 的早期返回必须落到可查询的权威状态。

    run_generic_app 在 already_running/pending_verification 等早期分支**不写回执**;
    若工作台已向用户返回 running(run_id 已对外),worker 必须为该 run_id 落一份
    终态回执,保证 gen-status 查到真实原因,而不是永远 unknown/假 running。
    已有真实回执时不覆盖。
    """
    if not isinstance(outcome, dict):
        return None
    status = str(outcome.get("status", ""))
    if status == "running":
        return None  # 真实运行回执由 _run_inner 落盘
    existing = load_receipt(root, run_id)
    if existing is not None:
        return existing
    if status in ("already_running", "pending_verification"):
        doc_status = "not_started"
    else:
        doc_status = status or "failed"
    doc: dict[str, Any] = {
        "schema_version": GENERIC_RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "task_id": TASK_ID,
        "session_id": outcome.get("session_id"),
        "goal": "",
        "grant_id": None,
        "started_at": _now(),
        "finished_at": _now(),
        "attempts": [],
        "ai_requests_dispatched": 0,
        "status": doc_status,
        "blocked_by": outcome.get("blocked_by"),
        "note": str(outcome.get("note", ""))[:300],
        "failure": (str(outcome.get("failure") or outcome.get("note") or ("运行未启动:" + status)))[:300],
        "worker_outcome": outcome,
    }
    _write_receipt(Path(root), doc)
    return doc


__all__ = ["GENERIC_RUN_SCHEMA_VERSION", "GenericRunError", "PID_ALIVE", "PID_DEAD",
           "PID_UNKNOWN", "active_run", "canonical_candidate_path", "extend_task_budget",
           "load_receipt", "load_received_material", "request_cancel", "resumable_candidate",
           "resume_saved_candidate", "run_generic_app", "task_budget_state", "task_lineage",
           "validate_runtime_roots", "write_worker_outcome"]
