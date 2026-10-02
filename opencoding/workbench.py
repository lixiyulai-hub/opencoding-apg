# -*- coding: utf-8 -*-
"""OpenCoding 本地可视化工作台:小白主入口(仅绑定 127.0.0.1,一次性令牌鉴权)。

启动:python -m opencoding.workbench --workspace <目录> [--port 0]
- 启动后打印形如 http://127.0.0.1:<port>/?t=<token> 的入口,浏览器打开即用;
- 所有 API 请求必须带令牌(首访 URL 携带,服务端种 HttpOnly Cookie);
- API 只调用 service/advisor/aiconfig 的既有公开函数,共享同一条状态链;
- 密钥只进用户级配置文件,任何响应/日志不回显。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
import secrets
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import advisor, aiconfig, service

MAX_BODY_BYTES = 512 * 1024


def _load_adoption(root: Path, session_id: str) -> dict[str, Any] | None:
    """读取当前会话的采用记录(文件名与会话标识都核对)。"""
    path = root / ".opencoding" / "adoptions" / (session_id + ".json")
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("session_id") != session_id:
        return None
    return doc


def sanitize_worker_error(exc: BaseException) -> str:
    """worker 异常的脱敏摘要(不含本地路径细节)。"""
    from .safety import sanitize_text

    return sanitize_text(str(exc))[:300]


class WorkbenchError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _project_root(workspace: Path, name: str) -> Path:
    if not name or any(ch in name for ch in "\\/:*?\"<>|") or name.startswith("."):
        raise WorkbenchError(400, "invalid_project_name", "项目名不能为空且不能包含路径字符")
    root = workspace / name
    if not root.is_dir():
        raise WorkbenchError(404, "project_not_found", "项目不存在:" + name)
    return root


PROJECT_IDENTITY_SCHEMA = "1.0"


def _project_identity_path(root: Path) -> Path:
    return root / ".opencoding" / "project.json"


def _write_project_identity(root: Path, name: str, origin: str) -> dict[str, Any]:
    """W1:建立项目身份——只在**显式创建/导入动作**时写,列举接口保持只读。

    不枚举时初始化每个用户目录;不对符号链接/重解析点建身份。
    """
    if root.is_symlink():
        raise WorkbenchError(400, "unsafe_project_path", "该路径是链接,不能作为项目")
    marker_dir = root / ".opencoding"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "sessions").mkdir(exist_ok=True)
    marker = _project_identity_path(root)
    if marker.is_file():
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if isinstance(existing, dict) and existing.get("name"):
            return existing
    doc = {"schema_version": PROJECT_IDENTITY_SCHEMA, "name": name,
           "origin": origin, "created_at": _utc_now()}
    marker.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _latest_session(root: Path) -> str | None:
    try:
        sessions = service.list_sessions(root)
    except Exception:
        return None
    items = sessions.get("sessions") if isinstance(sessions, dict) else sessions
    if not items:
        return None
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict):
            return first.get("id") or first.get("session_id")
        return str(first)
    return None


class Workbench:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(24)
        self._authed: set[str] = set()

    # ---------- 视图辅助(W1:服务状态 → 可操作页面) ----------

    @staticmethod
    def _resume_view(root: Path, session_id: str) -> dict[str, Any]:
        """可接续候选与批次状态的中文视图(只读)。"""
        from . import generic_run

        out: dict[str, Any] = {"available": False}
        try:
            pending = generic_run.resumable_candidate(root, session_id)
        except Exception:  # noqa: BLE001 - 只读视图不得让整个页面失败
            pending = None
        if isinstance(pending, dict):
            out = {
                "available": bool(pending.get("resumable")),
                "run_id": pending.get("run_id"),
                "attempt": pending.get("attempt"),
                "files": list(pending.get("files") or []),
                "input_drift": pending.get("input_drift"),
                "note": ("已有等待后端的候选:继续它不会新增 AI 请求"
                         if pending.get("resumable")
                         else "已有候选,但当前事实已变化,需要重新评估后再继续"),
            }
        try:
            budget = None
            if hasattr(generic_run, "task_budget_state"):
                task_id = (pending or {}).get("task_id") or getattr(
                    generic_run, "TASK_ID", "generic-run")
                budget = generic_run.task_budget_state(root, task_id, session_id)
        except Exception:  # noqa: BLE001
            budget = None
        if isinstance(budget, dict):
            out["task_budget"] = budget
        return out

    @staticmethod
    def _result_view(root: Path, name: str, run_id: str) -> dict[str, Any]:
        """W1:某次运行的实际产物位置与可打开链接(不要求用户猜启动命令)。"""
        from .generic_run import load_receipt

        if not re.fullmatch(r"gen-[0-9a-f]{12}", run_id):
            raise WorkbenchError(400, "invalid_run_id", "运行编号无效")
        doc = load_receipt(root, run_id)
        if doc is None:
            raise WorkbenchError(404, "run_not_found", "找不到这次运行")
        files: list[dict[str, Any]] = []
        delivered = doc.get("delivered_files")
        if isinstance(delivered, list):
            candidates = [str(item.get("path")) for item in delivered
                          if isinstance(item, dict) and item.get("path")]
        else:
            candidates = []
        if not candidates:
            app_dir = root / "app"
            if app_dir.is_dir():
                candidates = sorted(p.relative_to(root).as_posix()
                                    for p in app_dir.rglob("*") if p.is_file())
        for rel in candidates:
            files.append({
                "path": rel,
                "absolute": str((root / rel).resolve()),
                "exists": (root / rel).is_file(),
                "raw_url": f"/api/raw/{name}/{rel}",
            })
        return {"run_id": run_id, "status": doc.get("status"),
                "project_root": str(root.resolve()), "files": files,
                "committed": bool(doc.get("transaction_id")),
                "note": "产物以安全下载方式提供,不会在工作台内执行;可用本地编辑器查看"}

    @staticmethod
    def _runs_view(root: Path, session_id: str) -> list[dict[str, Any]]:
        """本会话的运行历史(只读,按更新时间倒序)。"""
        runs_dir = root / ".opencoding" / "generic_runs"
        if not runs_dir.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(runs_dir.glob("gen-*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if doc.get("session_id") != session_id:
                continue
            items.append({
                "run_id": doc.get("run_id"),
                "status": doc.get("status"),
                "attempt": len(doc.get("attempts") or []),
                "updated_at": doc.get("updated_at") or doc.get("finished_at"),
                "committed": bool(doc.get("transaction_id")),
                "failure": doc.get("failure"),
                "files": [item.get("path") for item in
                          ((doc.get("delivered_files") or [])
                           if isinstance(doc.get("delivered_files"), list) else [])],
            })
        return items

    # ---------- API ----------

    def api(self, method: str, path: str, query: dict, body: dict) -> dict[str, Any]:
        if path == "/api/bootstrap" and method == "GET":
            projects = []
            uninitialized: list[str] = []
            for child in sorted(self.workspace.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                if not (child / ".opencoding").is_dir():
                    # W1:只列举名字,不在列举时写入任何东西(A23 只读原则)
                    if not child.is_symlink():
                        uninitialized.append(child.name)
                    continue
                sessions = service.list_sessions(child)
                items = sessions.get("sessions") if isinstance(sessions, dict) else sessions
                projects.append({
                    "name": child.name,
                    "sessions": len(items) if isinstance(items, list) else 0,
                })
            return {
                "workspace": str(self.workspace),
                "projects": projects,
                "uninitialized": uninitialized,
                "ai": aiconfig.describe_status(),
                "test": aiconfig.load_last_test(),
                "capability": advisor.capability_view(),
            }
        if path == "/api/project/import" and method == "POST":
            # W1:显式导入工作区里已存在但还没有项目身份的目录(不破坏用户前像)
            name = str(body.get("name", "")).strip()
            if not name or any(ch in name for ch in "\\/:*?\"<>|") or name.startswith("."):
                raise WorkbenchError(400, "invalid_project_name", "项目名不能为空且不能包含路径字符")
            root = self.workspace / name
            if not root.is_dir():
                raise WorkbenchError(404, "project_not_found", "工作区里没有这个目录:" + name)
            if root.is_symlink():
                raise WorkbenchError(400, "unsafe_project_path", "该目录是链接,不能作为项目")
            if (root / ".opencoding").is_dir():
                raise WorkbenchError(409, "project_exists", "该目录已经是项目,直接打开即可")
            if type(body.get("confirm")) is not bool or body.get("confirm") is not True:
                raise WorkbenchError(400, "import_unconfirmed",
                                     "把已有目录作为项目需要先确认(confirm=true):"
                                     "仅写入项目身份文件,不会移动或删除你的文件")
            _write_project_identity(root, name, "imported-existing-directory")
            return {"ok": True, "name": name, "imported": True,
                    "note": "已为这个已有目录建立项目身份;原有文件未改动"}
        if path == "/api/ai/config" and method == "POST":
            ok, err = aiconfig.save_config(body)
            if not ok:
                raise WorkbenchError(400, "ai_config_invalid", err or "配置无效")
            # W1:保存只等于"已保存",不等于"已连通";绝不因为按钮叫"保存并测试"
            # 就偷偷发起外部请求。真实连通性测试走独立的 /api/ai/test。
            aiconfig.save_last_test({"state": "not_tested", "at": _utc_now(),
                                     "note": "配置已保存,尚未做连通性测试"})
            return {"ok": True, "ai": aiconfig.describe_status(),
                    "test": aiconfig.load_last_test(),
                    "note": "已保存(未测试):点击“测试连接”才会真正发起一次外部请求"}
        if path == "/api/ai/status" and method == "GET":
            return {"ai": aiconfig.describe_status(), "test": aiconfig.load_last_test()}
        if path == "/api/ai/test" and method == "POST":
            # W1:真实连通性测试——只有用户显式点击才发起一次外部请求。
            # 只检查地址可达性;认证与生成能力需要各自证据。
            if type(body.get("confirm")) is not bool or body.get("confirm") is not True:
                raise WorkbenchError(400, "ai_test_unconfirmed",
                                     "测试连接会向外部服务发起一次真实请求;需要显式确认")
            config, err = aiconfig.load_config()
            if config is None:
                raise WorkbenchError(400, "ai_not_configured", err or "请先完成 AI 接入向导")
            from . import aiadapter

            result = aiadapter.probe_connection(config)
            result["at"] = _utc_now()
            result["scope"] = "仅检查地址可达性;认证与生成能力需要各自证据,本探测不发生成请求"
            aiconfig.save_last_test(result)
            return {"ok": result.get("state") == "verified", "test": result}

        if path == "/api/project" and method == "POST":
            name = str(body.get("name", "")).strip()
            if not name:
                raise WorkbenchError(400, "invalid_project_name", "请填写项目名")
            if any(ch in name for ch in "\\/:*?\"<>|") or name.startswith("."):
                raise WorkbenchError(400, "invalid_project_name", "项目名不能包含路径字符")
            root = self.workspace / name
            existed = root.is_dir()
            if root.is_symlink():
                raise WorkbenchError(400, "unsafe_project_path", "同名链接已存在,换个名字")
            root.mkdir(parents=True, exist_ok=True)
            # W1:创建即建立项目身份,使新项目在列表里立即可见、重开后仍在。
            # 写入只发生在这个显式创建动作里;GET /api/bootstrap 仍然只读。
            identity = _write_project_identity(root, name,
                                               "created-empty" if not existed
                                               else "created-over-existing-directory")
            items = service.list_sessions(root)
            count = len(items) if isinstance(items, list) else 0
            return {"ok": True, "name": name, "created": not existed,
                    "sessions": count, "identity": identity}

        parts = [p for p in path.split("/") if p]  # api/project/<name>/...
        if not parts or parts[0] != "api":
            raise WorkbenchError(404, "not_found", "未知接口")
        parts = parts[1:]
        if not parts or parts[0] != "project":
            raise WorkbenchError(404, "not_found", "未知接口")
        name = parts[1]
        root = _project_root(self.workspace, name)
        rest = parts[2:]

        if rest == ["result"] and method == "GET":
            return self._result_view(root, name, str((query or {}).get("run_id", "")))
        if not rest and method == "GET":
            sessions = service.list_sessions(root)
            items = sessions.get("sessions") if isinstance(sessions, dict) else sessions
            return {"name": name, "sessions": items}
        if rest == ["session"] and method == "POST":
            goal = str(body.get("goal", "")).strip()
            if not goal:
                raise WorkbenchError(400, "goal_required", "请先用一句话描述你想做什么")
            created = service.create_session(root, goal)
            return created
        if rest[:1] == ["session"] and len(rest) >= 2 and method == "GET":
            sid = rest[1]
            if len(rest) >= 3 and rest[2] == "preview":
                return service.preview_session(root, sid)
            if len(rest) >= 4 and rest[2] == "gen-status":
                from .generic_run import load_receipt

                doc = load_receipt(root, rest[3])
                return doc if doc is not None else {"run_id": rest[3], "status": "unknown"}
            if len(rest) >= 3 and rest[2] == "confirm-new-batch":
                # R01:新批次确认的中文预览(只读)——看清接续要素再决定是否确认;
                # 不签发授权、不改回执、不产生任何状态 I/O。
                from .generic_run import (GenericRunError, describe_resume_confirmation,
                                          validate_runtime_roots)

                validate_runtime_roots(root)
                run_id = str((query or {}).get("run_id", ""))
                if not re.fullmatch(r"gen-[0-9a-f]{12}", run_id):
                    raise WorkbenchError(400, "invalid_run_id", "运行编号无效")
                try:
                    return {"preview": describe_resume_confirmation(root, run_id,
                                                                    session_id=sid)}
                except GenericRunError as exc:
                    raise WorkbenchError(400, exc.code, str(exc)) from exc
            view = service.session_view(root, sid)
            view["evaluations"] = advisor.list_evaluations(root, sid)
            view["ai"] = aiconfig.describe_status()
            # W1:把已有能力接成页面可直接操作的中文流程——可接续候选、批次
            # 到期确认预览、历史运行一览,不再让用户自己拼 API。
            view["resume"] = self._resume_view(root, sid)
            view["runs"] = self._runs_view(root, sid)
            return view
        if len(rest) >= 2 and rest[0] == "session" and method == "POST":
            sid = rest[1]
            action = rest[2] if len(rest) > 2 else ""
            if action == "answer":
                result = service.submit_answer(
                    root, sid, int(body.get("revision", 0)),
                    str(body.get("question_id", "")), str(body.get("answer", "")))
                return result
            if action == "evaluate":
                adapter = adapter_from_config(config)
                record = advisor.run_ai_evaluation(root, sid, adapter, run_id="workbench")
                return record
            if action == "confirm-evaluation":
                record = advisor.confirm_evaluation(
                    root, sid, str(body.get("evaluation_id", "")),
                    expected_revision=int(body.get("revision", 0)),
                    accepted=bool(body.get("accepted")))
                view = service.session_view(root, sid)
                return {"evaluation": record, "view": view}
            if action == "adopt":
                from .advisor import adopt_confirmed_evaluation

                return adopt_confirmed_evaluation(root, sid)
            if action == "generate":
                from .generic_run import (_validate_contract, active_run, run_generic_app,
                                          unknown_effect_run, validate_runtime_roots,
                                          write_worker_outcome)

                # R02:工作台路由同步做根校验(拒绝时不产生任何状态 I/O 与线程)
                validate_runtime_roots(root)
                existing = active_run(root)
                if existing is not None:
                    return {"status": "already_running", "run_id": existing["run_id"],
                            "note": "已有进行中的生成运行;可先取消它再重新派发"}
                # R03-B4/S09:效果未知旧请求在路由同步拦截——返回旧 run 与真实原因,
                # 不启动线程、不预发新 run_id,杜绝"查不到的假 running"
                pending = unknown_effect_run(root)
                if pending is not None:
                    return {"status": "pending_verification", "run_id": pending["run_id"],
                            "blocked_by": {"run_id": pending["run_id"],
                                           "request_id": (pending.get("current_request") or {}).get("request_id"),
                                           "status": pending.get("status"),
                                           "failure": pending.get("failure")},
                            "note": "此前有已派发但结果未知的请求;请在页面确认后再发起新尝试"}
                # 派发前置:必须存在已确认评估(含实现契约)
                confirmed = next((e for e in advisor.list_evaluations(root, sid)
                                  if e.get("status") == "confirmed"), None)
                if confirmed is None:
                    raise WorkbenchError(400, "evaluation_not_confirmed",
                                         "请先完成 AI 评估并确认后,再生成产物")
                raw_contract = (confirmed.get("structured") or {}).get("implementation_contract") or {}
                if not raw_contract.get("files") or not raw_contract.get("steps"):
                    raise WorkbenchError(400, "contract_missing",
                                         "该评估缺少实现契约;请在最新版本下重新评估")
                contract = _validate_contract(raw_contract)
                # R07:只认可权威判定成立的当前采用——adopted/matches/input_snapshot_missing
                # 三项必须全部成立;drift=[] 不代表通过,缺快照/格式坏一律拒绝。
                status = service.adoption_input_status(root, sid)
                if not status.get("adopted"):
                    raise WorkbenchError(400, "plan_not_adopted",
                                         "当前会话尚未采用计划;请先采用已确认评估的计划")
                if status.get("input_snapshot_missing"):
                    raise WorkbenchError(400, "adoption_snapshot_missing",
                                         "采用记录缺少有效输入快照,不能作为生成依据;请重新采用当前评估的计划")
                if not status.get("matches") or status.get("drift"):
                    raise WorkbenchError(400, "adoption_drifted",
                                         "采用计划与当前会话事实已漂移;请基于最新评估重新采用计划")
                adoption_doc = _load_adoption(root, sid)
                if adoption_doc is None:
                    raise WorkbenchError(400, "adoption_invalid",
                                         "采用记录不可读;请重新采用当前评估的计划")
                adopted_plan_digest = adoption_doc.get("plan_digest")
                if not adopted_plan_digest:
                    raise WorkbenchError(400, "adoption_invalid",
                                         "采用记录缺少计划摘要,不能作为生成依据;请重新采用当前评估的计划")
                # R07-A2:采用必须绑定当前已确认的 AI 评估(新契约+旧计划一律拒绝)
                ai_binding = adoption_doc.get("ai_binding") or {}
                contract_sha = hashlib.sha256(json.dumps(
                    raw_contract, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
                if not ai_binding.get("evaluation_id"):
                    raise WorkbenchError(400, "adoption_unbound",
                                         "采用记录未绑定已确认的 AI 评估;请基于当前评估重新采用计划")
                if ai_binding.get("evaluation_id") != confirmed.get("evaluation_id"):
                    raise WorkbenchError(400, "adoption_stale_evaluation",
                                         "采用记录绑定的是旧评估(当前评估 %s);请重新采用当前评估的计划"
                                         % str(confirmed.get("evaluation_id"))[:20])
                if str(ai_binding.get("contract_sha256") or "") != contract_sha:
                    raise WorkbenchError(400, "contract_changed_after_adopt",
                                         "采用后的实现契约已变化;请重新采用当前评估的计划")
                # 确认后的会话修订必须与当前一致(确认后条件又改了 → 旧绑定失效)
                view2 = service.session_view(root, sid)
                current_revision = (view2["session"] if "session" in view2 else view2).get("revision")
                if confirmed.get("session_revision_at_eval") != current_revision:
                    raise WorkbenchError(400, "evaluation_stale",
                                         "已确认评估基于旧会话版本;请在当前版本下重新评估")
                binding = {"session_id": sid,
                           "evaluation_id": confirmed.get("evaluation_id"),
                           "plan_digest": adopted_plan_digest,
                           "contract_sha256": contract_sha}
                goal = (view2["session"] if "session" in view2 else view2).get("goal", "")
                config, err = aiconfig.load_config()
                if config is None:
                    raise WorkbenchError(400, "ai_not_configured", err or "请先完成 AI 接入向导")
                from .aiadapter import adapter_from_config

                # CP5 §5.3:已有可接续候选时不强迫重新请求模型——先把接续入口交给用户。
                # 明确要求重新生成时(body.mode=regenerate 或 regenerate=true)才是新尝试。
                from .generic_run import resumable_candidate

                force_new = (str(body.get("mode", "")).lower() == "regenerate"
                             or bool(body.get("regenerate")))
                if not force_new:
                    pending_resume = resumable_candidate(root, sid)
                    if pending_resume is not None and pending_resume.get("resumable"):
                        return {
                            "status": "candidate_resumable",
                            "run_id": pending_resume["run_id"],
                            "attempt": pending_resume["attempt"],
                            "files": pending_resume["files"],
                            "note": "已存在等待后端的候选;可继续它(零新请求)或明确选择重新生成",
                            "resume_path": f"/api/project/{name}/session/{sid}/resume",
                            "how_to_regenerate": {"mode": "regenerate"},
                        }
                run_id = "gen-" + uuid.uuid4().hex[:12]
                adapter = adapter_from_config(config)

                def _worker():
                    try:
                        outcome = run_generic_app(root, goal, adapter, contract=contract,
                                                  adopted_plan_digest=adopted_plan_digest,
                                                  session_id=sid, adoption_binding=binding,
                                                  run_id=run_id)
                        write_worker_outcome(root, run_id, outcome)
                    except Exception as exc:  # noqa: BLE001 - worker 异常必须落到权威状态
                        import traceback
                        traceback.print_exc()
                        write_worker_outcome(root, run_id, {
                            "status": "failed", "session_id": sid,
                            "note": "生成线程异常终止:" + type(exc).__name__,
                            "failure": sanitize_worker_error(exc)})

                threading.Thread(target=_worker, daemon=True).start()
                return {"run_id": run_id, "status": "running"}
            if action == "resume" and method == "POST":
                # CP5 §5.3:继续已保存候选——零新 AI 请求;不是"重新生成"。
                from .generic_run import GenericRunError, resume_saved_candidate, validate_runtime_roots

                validate_runtime_roots(root)
                run_id = str(body.get("run_id", ""))
                if not re.fullmatch(r"gen-[0-9a-f]{12}", run_id):
                    raise WorkbenchError(400, "invalid_run_id", "运行编号无效")
                try:
                    doc = resume_saved_candidate(root, run_id)
                except GenericRunError as exc:
                    raise WorkbenchError(400, exc.code, str(exc)) from exc
                return {"run_id": run_id, "status": doc.get("status"), "receipt": doc}
            if action == "confirm-new-batch" and method == "POST":
                # R01:批次到期后的正式新批次确认——显式确认后由服务签发新批次
                # 并绑定回执;用户不手填 grant/hash/JSON,不改私有回执文件。
                # 只有到期的当前授权可被确认;撤销/篡改/不可核对一律拒绝。
                # R01-A:confirm 必须是 JSON 布尔 true 本身(合同定义类型)——
                # 缺失/null/false/字符串"false"/数字/列表/字典一律拒绝;
                # 不做真值推断,拒绝路径不签发、不写 active、不加确认事件。
                from .generic_run import (GenericRunError, confirm_new_batch_for_resume,
                                          validate_runtime_roots)

                validate_runtime_roots(root)
                run_id = str(body.get("run_id", ""))
                if not re.fullmatch(r"gen-[0-9a-f]{12}", run_id):
                    raise WorkbenchError(400, "invalid_run_id", "运行编号无效")
                if body.get("confirm") is not True:
                    raise WorkbenchError(
                        400, "new_batch_unconfirmed",
                        "新批次确认需要显式 confirm=true(JSON 布尔值;字符串、数字等"
                        "一律不算确认);确认前请核对:继续哪次任务、使用哪份既有候选、"
                        "允许的动作与写入范围、不新增 AI 请求及既有消费限制"
                        "(可先 GET confirm-new-batch?run_id=... 查看中文预览)")
                try:
                    result = confirm_new_batch_for_resume(
                        root, run_id, session_id=sid,
                        issued_by=str(body.get("issued_by", "工作台用户")),
                        note=str(body.get("note", "")))
                except GenericRunError as exc:
                    raise WorkbenchError(400, exc.code, str(exc)) from exc
                return result
            if action == "extend-budget" and method == "POST":
                # CP5 §5.2:追加同一逻辑任务额度必须是**新的明确授权**,不是风险确认
                from .generic_run import TASK_ID, extend_task_budget, validate_runtime_roots

                validate_runtime_roots(root)
                reason = str(body.get("reason", "")).strip()
                if len(reason) < 4:
                    raise WorkbenchError(400, "budget_reason_required", "追加额度必须写明理由")
                if not bool(body.get("confirm")):
                    raise WorkbenchError(400, "budget_extension_unconfirmed",
                                         "追加额度需要显式确认(confirm=true)并写明理由")
                state = extend_task_budget(root, TASK_ID, sid, amount=int(body.get("amount", 1)),
                                           reason=reason, issued_by=str(body.get("issued_by", "工作台用户")))
                return {"task_budget": state, "note": "已记录追加授权;继续生成时按新上限结算剩余额度"}
            if action == "gen-status" and len(rest) >= 4:
                from .generic_run import load_receipt

                run_id = rest[3]
                doc = load_receipt(root, run_id)
                if doc is None:
                    return {"run_id": run_id, "status": "unknown"}
                return doc
            if action == "cancel-generation" and len(rest) >= 4:
                from .generic_run import request_cancel

                return request_cancel(root, rest[3])
            if action == "ack-unknown" and method == "POST":
                from .generic_run import acknowledge_unknown

                run_id = str(body.get("run_id", ""))
                if not re.fullmatch(r"gen-[0-9a-f]{12}", run_id):
                    raise WorkbenchError(400, "invalid_run_id", "运行编号无效")
                return acknowledge_unknown(root, run_id, str(body.get("note", "用户知悉旧请求结果未知,发起新尝试")))
            if action == "preview":
                return service.preview_session(root, sid)
            if action == "approve":
                preview = body.get("preview")
                if not isinstance(preview, dict):
                    raise WorkbenchError(400, "preview_required", "缺少预览内容")
                return service.approve_preview(preview)
            if action == "apply":
                approval = body.get("approval")
                if not isinstance(approval, dict):
                    raise WorkbenchError(400, "approval_required", "缺少审批凭据")
                return service.apply_approved(root, approval)
            if action == "status":
                return service.execution_status(root, sid)
            if action == "cancel":
                return service.cancel(root, sid)
        raise WorkbenchError(404, "not_found", "未知接口:" + path)


def _html(token: str) -> str:
    return UI_TEMPLATE.replace("__TOKEN__", token)


UI_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenCoding 工作台</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2330;--mut:#68738a;--acc:#2563eb;--ok:#0a7d33;--warn:#b45309;--err:#b91c1c;--line:#e4e8ef}
*{box-sizing:border-box}body{margin:0;font-family:"Microsoft YaHei",system-ui,sans-serif;background:var(--bg);color:var(--ink)}
header{background:var(--card);border-bottom:1px solid var(--line);padding:12px 20px;display:flex;gap:14px;align-items:center}
header h1{font-size:18px;margin:0}
main{max-width:960px;margin:18px auto;padding:0 14px;display:flex;flex-direction:column;gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.card h2{font-size:15px;margin:0 0 10px}
.mut{color:var(--mut);font-size:13px}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:8px 16px;font-size:14px;cursor:pointer}
button.sec{background:#eef2ff;color:var(--acc)}
button:disabled{opacity:.5;cursor:default}
input,textarea,select{width:100%;border:1px solid var(--line);border-radius:8px;padding:8px;font-size:14px;font-family:inherit}
label{display:block;font-size:13px;margin:10px 0 4px;color:var(--mut)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.q{border-top:1px dashed var(--line);padding-top:10px;margin-top:10px}
.q .why{color:var(--mut);font-size:12px;margin:2px 0 6px}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;background:#eef2ff;color:var(--acc)}
.badge.ok{background:#e7f6ec;color:var(--ok)}.badge.warn{background:#fef3e2;color:var(--warn)}.badge.err{background:#fdeaea;color:var(--err)}
pre{white-space:pre-wrap;word-break:break-all;background:#0f172a;color:#dbe4ff;padding:10px;border-radius:8px;font-size:12px;max-height:320px;overflow:auto}
.hide{display:none}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:#111827;color:#fff;padding:10px 18px;border-radius:8px;font-size:14px;opacity:0;transition:.3s}
#toast.show{opacity:.95}
ul.history{margin:4px 0;padding-left:18px;font-size:13px}
</style></head><body>
<header><h1>🧭 OpenCoding 工作台</h1><span class="mut">把你的想法变成能执行的项目方案</span>
<span id="aiBadge" class="badge err" style="margin-left:auto">AI 未接入</span></header>
<main>

<div id="view-home" class="card">
 <h2>我的项目</h2>
 <div class="row"><input id="np-name" placeholder="新项目名称,如:家庭借还登记" style="flex:1">
 <button onclick="createProject()">创建项目</button></div>
 <p class="mut">创建后会立刻出现在下面列表里,关掉浏览器再打开也还在。</p>
 <div id="proj-list" style="margin-top:12px"></div>
 <div id="import-area" class="hide" style="margin-top:10px">
  <h2 style="font-size:14px">工作区里还没成为项目的文件夹</h2>
  <p class="mut">这些是你工作区里已有的文件夹。导入只会写一个项目身份文件,不会移动、改名或删除你的任何文件。</p>
  <div id="import-list"></div>
 </div>
</div>

<div id="view-project" class="hide">
 <div class="card"><div class="row">
  <button class="sec" onclick="showHome()">← 返回项目列表</button>
  <h2 id="proj-title" style="margin:0"></h2>
  <button onclick="newSession()">＋ 新需求会话</button></div>
  <div id="sess-list" style="margin-top:10px"></div>
 </div>

 <div id="view-session" class="hide">
  <div class="card">
   <h2 id="sess-goal"></h2><div class="mut" id="sess-status"></div>
   <div id="questions"></div>
   <div class="row" style="margin-top:12px">
    <button id="btn-eval" onclick="runEvaluation()">🤖 让 AI 评估方案</button>
    <span class="mut" id="eval-hint">回答完问题后,让真实 AI 给出有依据的首选方案</span>
   </div>
   <div id="resume-area" style="margin-top:12px"></div>
  </div>
  <div id="eval-card" class="card hide">
   <h2>AI 评估结果 <span id="eval-meta" class="mut"></span></h2>
   <div id="eval-body"></div>
   <div class="row" style="margin-top:10px">
    <button onclick="confirmEval(true)">✅ 采纳并继续</button>
    <button class="sec" onclick="confirmEval(false)">不接受,我要补充信息</button>
   </div>
  </div>
  <div class="card">
   <h2>方案与文档</h2>
   <div class="row"><button class="sec" onclick="doAdopt()">采用此评估进入计划</button>
   <button class="sec" onclick="doPreview()">生成文档预览</button>
   <button class="sec" onclick="doApproveApply()">确认并写入项目</button>
   <button onclick="doGenerate()">🚀 生成可运行产物(AI 编码)</button></div>
   <p class="mut">生成产物会让真实 AI 编写 app/main.py 与 app/selftest.py,在隔离沙箱中实测通过后才写入项目,约需 1-5 分钟。</p>
   <div id="plan-area" style="margin-top:10px"></div>
  </div>
  <div class="card">
   <h2>历史记录</h2>
   <div id="runs-area"><p class="mut">还没有运行记录。</p></div>
  </div>
 </div>
</div>

<div id="view-wizard" class="card hide">
 <h2>AI 接入向导</h2>
 <p class="mut">OpenCoding 需要一条真实 AI 通道来评估方案。推荐使用 WorkBuddy 云网关(无需自己找密钥),
 也可以填写任何 OpenAI 兼容服务(如 DashScope / DeepSeek / 本地 Ollama)。密钥只保存在你电脑的用户配置里,
 不会进入项目文件或日志。</p>
 <label>提供方</label><select id="wz-provider" onchange="wzSwitch()">
  <option value="workbuddy_gateway">WorkBuddy 云网关(推荐)</option>
  <option value="openai_compatible">OpenAI 兼容接口(自己的密钥)</option></select>
 <div id="wz-gw">
  <label>网关地址</label><input id="wz-endpoint" placeholder="https://…workbuddy.host">
  <label>应用访问密钥(publishable key)</label><input id="wz-pk" placeholder="wbpk_…">
  <label>模型</label><input id="wz-model" value="glm-5.3">
 </div>
 <div id="wz-oc" class="hide">
  <label>接口地址(base_url,以 /v1 结尾)</label><input id="wz-base" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1">
  <label>API Key</label><input id="wz-key" type="password">
  <label>模型</label><input id="wz-model2" placeholder="qwen-plus">
 </div>
 <div id="wz-state" class="mut"></div>
 <div class="row" style="margin-top:12px">
  <button onclick="saveWizard()">保存配置</button>
  <button class="sec" onclick="testConnection()">测试连接(会发起一次真实请求)</button>
  <button class="sec" onclick="showView('view-home')">返回</button></div>
 <p class="mut"><b>保存</b>只把配置写到本机,不等于能连上。<b>测试连接</b>才会真正访问外部服务一次,
  并且只检查地址可达性,不能仅凭响应证明凭据有效;生成能力要等第一次真实评估才算验证过。</p>
</div>
</main>
<div id="toast"></div>
<script>
const $=s=>document.querySelector(s);let ST={project:null,session:null,revision:0,preview:null,approval:null,evaluation:null};
function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2600)}
async function api(path,opt){opt=opt||{};opt.headers=Object.assign({'Content-Type':'application/json'},opt.headers||{});
 if(opt.body&&typeof opt.body!=='string')opt.body=JSON.stringify(opt.body);
 const r=await fetch(path,opt);const d=await r.json().catch(()=>({}));
 if(!r.ok){toast((d&&d.message)||('请求失败 '+r.status));throw new Error(d&&d.code||r.status)}return d}
function showView(id){['view-home','view-project','view-session','view-wizard'].forEach(v=>$('#'+v).classList.toggle('hide',v!==id))}
function showHome(){showView('view-home');loadBoot()}
function aiBadge(ai,test){const badge=$('#aiBadge');badge.onclick=()=>showWizard();
 const st=(test&&test.state)||'not_tested';
 if(!ai.configured){badge.textContent='AI 未接入(点击设置)';badge.className='badge err';return}
 const kind=ai.provider==='workbuddy_gateway'?'云网关':'自定义接口';
 if(st==='reachable_unverified'){badge.textContent='AI 地址可达 · 认证与生成尚未核实';badge.className='badge warn';return}
 if(st==='verified'){badge.textContent='AI 已接入('+kind+') · 连通性已验证';badge.className='badge ok';return}
 if(st==='blocked'||st==='unreachable'){badge.textContent='AI 已接入('+kind+') · 连接受阻,点击查看';badge.className='badge warn';return}
 badge.textContent='AI 已接入('+kind+') · 已保存未测试';badge.className='badge'}
async function loadBoot(){const b=await api('/api/bootstrap');ST.ai=b.ai||{};ST.aiTest=b.test||{};
 aiBadge(ST.ai,ST.aiTest);
 const list=$('#proj-list');list.innerHTML='';
 (b.projects||[]).forEach(p=>{const d=document.createElement('div');d.className='row';
  d.innerHTML=`<button class="sec" onclick="openProject('${esc(p.name)}')">📁 ${esc(p.name)}</button><span class="mut">${p.sessions} 个会话</span>`;
  list.appendChild(d)});
 if(!(b.projects||[]).length)list.innerHTML='<p class="mut">还没有项目。在上面填个名字点“创建项目”就行。</p>';
 const imp=$('#import-list');imp.innerHTML='';const un=b.uninitialized||[];
 $('#import-area').classList.toggle('hide',!un.length);
 un.forEach(n=>{const d=document.createElement('div');d.className='row';
  d.innerHTML=`<span>📂 ${esc(n)}</span><button class="sec" onclick="importExisting('${esc(n)}')">作为项目打开</button>`;
  imp.appendChild(d)})}
async function createProject(){const name=$('#np-name').value.trim();if(!name)return toast('请填写项目名');
 const r=await api('/api/project',{method:'POST',body:{name}});$('#np-name').value='';
 toast('项目已创建,正在打开');await loadBoot();openProject(r.name)}
async function importExisting(name){
 if(!confirm('把“'+name+'”作为项目打开?\n只会写入一个项目身份文件 .opencoding/project.json,不会移动、改名或删除你的文件。'))return;
 try{await api('/api/project/import',{method:'POST',body:{name,confirm:true}});toast('已导入');await loadBoot();openProject(name)}
 catch(e){toast('导入失败:'+e.message)}}
async function openProject(name){ST.project=name;$('#proj-title').textContent='📁 '+name;
 const s=await api('/api/project/'+encodeURIComponent(name));
 const list=$('#sess-list');list.innerHTML='';const items=(s.sessions&&s.sessions.items)||s.sessions||[];
 (Array.isArray(items)?items:[]).forEach(x=>{const id=x.id||x.session_id||x;const goal=x.goal||'';
  const d=document.createElement('div');d.className='row';
  d.innerHTML=`<button class="sec" onclick="openSession('${id}')">会话 ${esc(String(id).slice(0,14))}…</button><span class="mut">${esc(goal)}</span>`;
  list.appendChild(d)});
 showView('view-project');$('#view-session').classList.add('hide')}
async function newSession(){const goal=prompt('用大白话说清楚你想做什么:');
 if(!goal)return;const r=await api('/api/project/'+encodeURIComponent(ST.project)+'/session',{method:'POST',body:{goal}});
 openSession(r.session.id)}
async function openSession(sid){ST.session=sid;const v=await api('/api/project/'+encodeURIComponent(ST.project)+'/session/'+sid);
 ST.revision=v.session.revision;$('#sess-goal').textContent='🎯 '+v.session.goal;
 $('#sess-status').textContent='状态:'+(v.status||'-')+' · 版本 r'+ST.revision;
 const q=$('#questions');q.innerHTML='';
 (v.session.questions||[]).forEach(qq=>{const cur=v.session.answers[qq.id]||'';
  const d=document.createElement('div');d.className='q';
  d.innerHTML=`<b>${qq.question}</b><div class="why">${qq.why}</div>
   <div class="row"><input id="ans-${qq.id}" value="${cur.replace(/"/g,'&quot;')}" style="flex:1">
   <button onclick="answer('${qq.id}')">回答</button></div>
   ${cur?'<span class="badge ok">已回答</span>':''}`;
  q.appendChild(d)});
 // W1:重新打开会话时,已存在的评估卡片必须仍然显示(不再被隐藏)
 renderEvals(v.evaluations||[]);
 renderResume(v.resume||{});renderRuns(v.runs||[]);
 showView('view-project');$('#view-session').classList.remove('hide');
 $('#plan-area').innerHTML=''}
function renderResume(rs){const box=$('#resume-area');box.innerHTML='';
 if(!rs||!rs.available){if(rs&&rs.note)box.innerHTML='<p class="mut">'+esc(rs.note)+'</p>';return}
 box.innerHTML=`<div class="card" style="border-color:#9fd0b4">
  <h2>⏭ 有一次没做完的生成可以接着做</h2>
  <p class="mut">继续它会复用已经写好的候选文件,<b>不会</b>重新向 AI 发起请求;
  选“重新生成”才会产生新的一次 AI 消耗。</p>
  <ul class="history"><li>运行编号:${esc(rs.run_id||'-')}</li>
   <li>已尝试 ${esc(String(rs.attempt||0))} 次</li>
   <li>待写入文件:${(rs.files||[]).map(esc).join('、')||'-'}</li></ul>
  <div class="row"><button onclick="doResume('${esc(rs.run_id||'')}')">继续原候选(零新 AI 请求)</button>
  <button class="sec" onclick="doGenerate(true)">重新生成(会产生新的 AI 请求)</button></div></div>`}
function renderRuns(runs){const box=$('#runs-area');
 if(!runs||!runs.length){box.innerHTML='<p class="mut">还没有运行记录。</p>';return}
 box.innerHTML='<ul class="history">'+runs.map(r=>`<li><b>${esc(String(r.run_id))}</b> · ${esc(String(r.status||'-'))} · 尝试 ${esc(String(r.attempt||0))} 次
   ${r.committed?'· 已提交':'· 未提交'} ${r.updated_at?'· '+esc(String(r.updated_at)):''}
   ${r.status==='delivered'||r.committed?' <button class="sec" onclick="openResult(\''+esc(String(r.run_id))+'\')">打开结果</button>':''}
   ${r.failure?'<br><span class="mut">失败原因:'+esc(String(r.failure))+'</span>':''}</li>`).join('')+'</ul>'}
async function openResult(rid){try{
 const r=await api(`/api/project/${encodeURIComponent(ST.project)}/result?run_id=${encodeURIComponent(rid)}`);
 const head=`<div class="card"><h2>产物位置</h2><p class="mut">项目目录:${esc(r.project_root)}</p><ul class="history">`
  +r.files.map(f=>`<li><b>${esc(f.path)}</b> ${f.exists?'':'<span class="mut">(文件不在)</span>'}
    <button class="sec" onclick="window.open('${esc(f.raw_url)}','_blank')">下载文件</button>
    <button class="sec" onclick="copyPath(this)" data-path="${esc(f.absolute)}">复制完整路径</button></li>`).join('')
  +`</ul><p class="mut">${esc(r.note||'')}</p></div>`;
 $('#plan-area').innerHTML=head+$('#plan-area').innerHTML;toast('已列出产物')}
 catch(e){toast('打开结果失败:'+e.message)}}
function copyPath(el){const p=el.getAttribute('data-path');
 if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(p);toast('已复制路径')}
 else{toast(p)}}
async function answer(qid){const val=$('#ans-'+qid).value.trim();if(!val)return toast('请输入回答');
 const r=await api(`/api/project/${encodeURIComponent(ST.project)}/session/${ST.session}/answer`,
  {method:'POST',body:{question_id:qid,answer:val,revision:ST.revision}});
 ST.revision=r.session.revision;toast('已记录');openSession(ST.session)}
function renderEvals(evs){if(!evs.length)return;const e=evs[0];ST.evaluation=e;
 $('#eval-card').classList.remove('hide');
 $('#eval-meta').textContent=`${e.provider}/${e.model} · ${e.real?'真实调用':'模拟(测试)'} · ${e.status==='pending_confirmation'?'待确认':e.status}`;
 $('#eval-body').innerHTML=`<p><b>首选平台:</b>${esc(e.structured.choice)}</p><p>${esc(e.structured.summary||'')}</p>
  <ul class="history">${(e.structured.reasons||[]).map(x=>'<li>'+esc(x)+'</li>').join('')}</ul>
  ${renderRec(e.structured.recommendation)}`}
function renderRec(r){if(!r||!Object.keys(r).length)return'';
 return `<details><summary class="mut">展开:技术栈 / 备选 / 假设 / 未知 / 改判条件</summary>
  <p><b>技术栈:</b>${esc(r.stack||'-')}</p>
  ${r.rejected&&r.rejected.length?'<b>为什么不选其他:</b><ul class="history">'+r.rejected.map(x=>`<li>${esc(x.option)}:${esc(x.reason)}</li>`).join('')+'</ul>':''}
  ${r.assumptions&&r.assumptions.length?'<b>假设:</b><ul class="history">'+r.assumptions.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':''}
  ${r.unknowns&&r.unknowns.length?'<b>还缺的关键信息:</b><ul class="history">'+r.unknowns.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':''}
  ${r.revisit_when&&r.revisit_when.length?'<b>什么情况要重新评估:</b><ul class="history">'+r.revisit_when.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':''}
  ${r.clarifying_questions&&r.clarifying_questions.length?'<b>想问你的问题:</b><ul class="history">'+r.clarifying_questions.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':''}
 </details>`}
async function runEvaluation(){$('#btn-eval').disabled=true;$('#eval-hint').textContent='正在调用真实 AI 评估,约需 30-90 秒…';
 try{const r=await api(`/api/project/${encodeURIComponent(ST.project)}/session/${ST.session}/evaluate`,{method:'POST',body:{}});
  toast('评估完成');renderEvals([r])}catch(e){console.error(e)}finally{$('#btn-eval').disabled=false;
  $('#eval-hint').textContent='回答完问题后,让真实 AI 给出有依据的首选方案'}}
async function confirmEval(ok){const r=await api(`/api/project/${encodeURIComponent(ST.project)}/session/${ST.session}/confirm-evaluation`,
  {method:'POST',body:{evaluation_id:ST.evaluation.evaluation_id,accepted:ok,revision:ST.revision}});
 ST.revision=r.view.session.revision;
 toast(ok?'已采纳评估':(r.evaluation.confirmation&&r.evaluation.confirmation.platform_discrepancy?'已记录:AI首选与你的回答不同,如需改选请重新回答平台问题':'已记录'));
 openSession(ST.session)}
function planBlock(t,d){return `<details open><summary>${t}</summary><pre>${d.replace(/</g,'&lt;')}</pre></details>`}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
async function doAdopt(){const r=await api(`/api/project/${encodeURIComponent(ST.project)}/session/${ST.session}/adopt`,{method:'POST',body:{}});
 $('#plan-area').innerHTML=planBlock('已采用评估计划',JSON.stringify(r,null,1));toast('已采用,可以生成文档预览')}
async function doPreview(){const r=await api(`/api/project/${encodeURIComponent(ST.project)}/session/${ST.session}/preview`);
 ST.preview=r;const files=(r.file_plan&&r.file_plan.entries)||[];
 $('#plan-area').innerHTML=`<p>将创建 ${files.length} 份项目文档:</p><ul class="history">${files.map(f=>'<li>'+f.path+'</li>').join('')}</ul>`
  +planBlock('完整预览(点击展开)',JSON.stringify(r,null,1));toast('预览已生成,请确认后写入')}
async function doApproveApply(){if(!ST.preview)return toast('请先生成文档预览');
 const a=await api(`/api/project/${encodeURIComponent(ST.project)}/session/${ST.session}/approve`,{method:'POST',body:{preview:ST.preview}});
 ST.approval=a;
 const r=await api(`/api/project/${encodeURIComponent(ST.project)}/session/${ST.session}/apply`,{method:'POST',body:{approval:ST.approval}});
 $('#plan-area').innerHTML+=planBlock('写入结果',JSON.stringify(r,null,1));toast('项目文档已写入')}
function fmtUsed(r){const lin=r.budget_lineage||{};return (lin.inherited_ai_requests||0)+(r.ai_requests_dispatched||0)}
async function doResume(rid){if(!rid)return toast('缺少运行编号');
 $('#plan-area').innerHTML='<p class="mut">⏭ 正在继续原候选(不新增 AI 请求)…</p>';
 try{const r=await api(`/api/project/${encodeURIComponent(ST.project)}/session/${ST.session}/resume`,{method:'POST',body:{run_id:rid}});
  toast('接续结果:'+(r.status||'-'));renderRunResult(r.receipt||r);openSession(ST.session)}
 catch(e){$('#plan-area').innerHTML+='<p class="mut">接续失败:'+esc(e.message)+'</p>'}}
async function doGenerate(forceNew){$('#plan-area').innerHTML='<p class="mut">🚀 正在让真实 AI 编写产物并在沙箱中实测…请勿关闭页面。</p>';
 try{const start=await api(`/api/project/${encodeURIComponent(ST.project)}/session/${ST.session}/generate`,
   {method:'POST',body:forceNew?{mode:'regenerate'}:{}});
  if(start.status==='pending_verification'){renderPaused(start.run_id,start.blocked_by||{},start.note);return}
  if(start.status==='already_running'){toast('已有进行中的生成;正在显示其状态');await pollRun(start.run_id);return}
  if(start.status==='candidate_resumable'){renderResume({available:true,run_id:start.run_id,attempt:start.attempt,files:start.files});return}
  await pollRun(start.run_id)}
 catch(e){$('#plan-area').innerHTML+='<p class="mut">生成中断:'+esc(e.message)+'</p>'}}
async function pollRun(rid){let r=null;
 for(let i=0;i<120;i++){await new Promise(res=>setTimeout(res,5000));
  r=await api(`/api/project/${encodeURIComponent(ST.project)}/session/${ST.session}/gen-status/${rid}`);
  $('#plan-area').innerHTML='<p class="mut">🚀 正在让真实 AI 编写产物并在沙箱中实测…(已 '+((i+1)*5)+' 秒,状态:'+r.status+',本任务累计 AI 请求 '+fmtUsed(r)+' 次)</p>';
  if(r.status!=='running')break}
 renderRunResult(r)}
function renderRunResult(r){if(!r){toast('未取得运行状态');return}
 $('#plan-area').innerHTML+=planBlock('生成结果(' + r.status + ')',JSON.stringify(r,null,1));
 if(r.status==='pending_verification'||r.status==='failed_transport'||r.status==='not_started'){
  renderPaused(r.run_id,r.blocked_by||{failure:r.failure},r.note||r.failure)}
 else{const msg = r.status==='delivered' ? '产物已生成并通过隔离验证,已写入 app/'
   : r.status==='blocked_execution' ? '本机暂无实际受限执行后端:候选已保留为未采用材料,未执行未提交'
   : r.status==='cancelled' ? '已取消(收到你的取消请求)' : '生成未完成:'+r.status;
  toast(msg);
  if(r.run_id&&(r.status==='delivered'||r.transaction_id))
   $('#plan-area').innerHTML+=`<div class="row"><button onclick="openResult('${esc(String(r.run_id))}')">📂 打开结果</button>
    <button class="sec" onclick="openSession('${esc(String(ST.session))}')">刷新会话</button></div>`;
  if(r.status==='blocked_execution')
   $('#plan-area').innerHTML+='<p class="mut">这台机器上没有可用的受限执行后端:候选已经完整保存,'
    +'等有可用后端时可以“继续原候选”直接提交,不需要重新让 AI 写一遍。</p>'}}
function renderPaused(rid,blocked,note){const b=blocked||{};
 $('#plan-area').innerHTML=`<div class="card" style="border-color:#f0c36d">
  <h2>⏸ 生成已暂停:此前有结果未知的请求</h2>
  <p class="mut">${esc(note||'上一次请求已发出但结果未知(可能传输失败/进程中断)。')}</p>
  <ul class="history">
   <li>旧运行编号:<b>${esc(rid||'-')}</b></li>
   <li>旧请求编号:${esc(b.request_id||'-')}</li>
   <li>当时状态:${esc(b.status||'-')}</li>
   <li>失败原因:${esc(b.failure||'(无记录)')}</li></ul>
  <p class="mut">远端是否已停止、是否已计费,本机无法核实。确认后不会重置预算:新尝试将继续计入同一任务的累计 AI 请求消费。</p>
  <div class="row">
   <button onclick="doAckUnknown('${esc(rid||'')}')">我已了解,接受不确定性并发起新尝试</button>
   <button class="sec" onclick="toast('已保留暂停;可稍后刷新页面再处理')">先不重试</button></div></div>`}
async function doAckUnknown(rid){if(!rid)return toast('缺少运行编号');
 await api(`/api/project/${encodeURIComponent(ST.project)}/session/${ST.session}/ack-unknown`,
  {method:'POST',body:{run_id:rid,note:'我已了解旧请求结果未知,同意继续消耗同一任务预算后重试'}});
 toast('已确认;正在发起新尝试');doGenerate()}
function showWizard(){showView('view-wizard')}
function wzSwitch(){const p=$('#wz-provider').value;$('#wz-gw').classList.toggle('hide',p!=='workbuddy_gateway');
 $('#wz-oc').classList.toggle('hide',p!=='openai_compatible')}
function wzBody(){const p=$('#wz-provider').value;
 if(p==='workbuddy_gateway')return{provider:p,endpoint:$('#wz-endpoint').value.trim(),publishable_key:$('#wz-pk').value.trim(),model:$('#wz-model').value.trim()};
 return{provider:p,base_url:$('#wz-base').value.trim(),api_key:$('#wz-key').value.trim(),model:$('#wz-model2').value.trim()}}
async function saveWizard(){try{
 const r=await api('/api/ai/config',{method:'POST',body:wzBody()});
 ST.ai=r.ai||ST.ai;ST.aiTest=r.test||{state:'not_tested'};
 $('#wz-state').textContent='已保存(未测试)。要点“测试连接”才会真正访问外部服务。';
 toast('配置已保存');aiBadge(ST.ai,ST.aiTest)}
 catch(e){toast('保存失败:'+e.message)}}
async function testConnection(){
 if(!confirm('将向外部服务发起一次真实请求,只验证地址可达与凭据是否有效,不会消耗生成额度。继续?'))return;
 $('#wz-state').textContent='正在测试连接…';
 try{const r=await api('/api/ai/test',{method:'POST',body:{confirm:true}});
  const t=r.test||{};ST.aiTest=t;aiBadge(ST.ai,t);
  $('#wz-state').textContent=(t.state==='verified'?'✅ 连接已验证':'⚠️ '+(t.reason||'连接未核实'))
   +' · '+(t.scope||'');toast(t.state==='verified'?'连接已验证':t.state==='reachable_unverified'?'地址可达,认证尚未核实':'连接受阻')}
 catch(e){$('#wz-state').textContent='测试失败:'+e.message;toast('测试失败')}}
loadBoot();
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "OpenCodingWorkbench/1.0"
    bench: Workbench

    def log_message(self, fmt, *args):  # 不回显 query(token)
        pass

    def _authorized(self) -> bool:
        cookies = self.headers.get("Cookie", "")
        for part in cookies.split(";"):
            if part.strip() == "oc_token=" + self.bench.token:
                return True
        return False
    def _send_json(self, status: int, doc: dict, *, cookie: str | None = None) -> None:
        data = json.dumps(doc, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie + "; HttpOnly; SameSite=Strict; Path=/")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_BODY_BYTES:
            raise WorkbenchError(413, "body_too_large", "请求体过大")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise WorkbenchError(400, "invalid_json", "请求体不是合法 JSON")
        if not isinstance(doc, dict):
            raise WorkbenchError(400, "invalid_json", "请求体必须是 JSON 对象")
        return doc

    def _serve_raw(self, spec: str) -> None:
        """只读回传项目内文件:供页面"下载文件产物"使用。

        限制:必须落在工作区某个项目目录内;拒绝符号链接/重解析点;单文件
        上限 2 MB;不提供目录列举。
        """
        import mimetypes

        if "/" not in spec:
            raise WorkbenchError(400, "invalid_path", "文件路径不完整")
        raw_name, _, rel = spec.partition("/")
        name = urllib.parse.unquote(raw_name)
        rel = urllib.parse.unquote(rel)
        root = _project_root(self.bench.workspace, name)
        target = (root / rel).resolve()
        base = root.resolve()
        if target != base and base not in target.parents:
            raise WorkbenchError(400, "path_out_of_project", "文件不在项目内")
        if not target.is_file() or target.is_symlink():
            raise WorkbenchError(404, "file_not_found", "文件不存在或是链接")
        if target.stat().st_size > 2 * 1024 * 1024:
            raise WorkbenchError(413, "file_too_large", "文件超过 2 MB,请用本地编辑器打开")
        data = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "application/javascript"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        # All project bytes are untrusted, including SVG and XHTML.
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", "attachment")
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(urllib.parse.unquote(self.path, errors="replace"))
        query = dict(urllib.parse.parse_qsl(parsed.query))
        try:
            if not self._authorized():
                token = query.get("t", "")
                if method == "GET" and token and secrets.compare_digest(token, self.bench.token):
                    page = _html(token).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Set-Cookie", "oc_token=" + token + "; HttpOnly; SameSite=Strict; Path=/")
                    self.send_header("Content-Length", str(len(page)))
                    self.end_headers()
                    self.wfile.write(page)
                    return
                raise WorkbenchError(401, "unauthorized", "请从启动窗口给出的完整地址打开工作台")
            if method == "GET" and parsed.path in ("/", "/index.html"):
                page = _html(self.bench.token).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            if method == "GET" and parsed.path.startswith("/api/raw/"):
                self._serve_raw(parsed.path[len("/api/raw/"):])
                return
            if not parsed.path.startswith("/api/"):
                raise WorkbenchError(404, "not_found", "未知路径")
            body = self._read_body() if method == "POST" else {}
            result = self.bench.api(method, parsed.path, query, body)
            self._send_json(200, result)
        except WorkbenchError as exc:
            self._send_json(exc.status, {"code": exc.code, "message": exc.message})
        except service.ServiceError as exc:
            self._send_json(400, {"code": getattr(exc, "code", "service_error"), "message": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"code": "invalid_request", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 统一错误出口,不泄露内部栈
            self._send_json(500, {"code": "internal_error", "message": "内部错误:" + type(exc).__name__ + " " + sanitize_msg(exc)})

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")


def sanitize_msg(exc: Exception) -> str:
    from .safety import sanitize_text

    return sanitize_text(str(exc))[:200]


def _default_workspace() -> Path:
    """默认项目目录;home 不可用时退回当前目录,不让启动直接崩溃。"""
    try:
        return Path.home() / "OpenCoding-项目"
    except RuntimeError:
        return Path.cwd() / "OpenCoding-项目"


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenCoding 本地可视化工作台")
    parser.add_argument("--workspace", default=None,
                        help="项目存放目录(默认 用户目录/OpenCoding-项目)")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    bench = Workbench(Path(args.workspace) if args.workspace else _default_workspace())
    handler = type("_BenchHandler", (_Handler,), {"bench": bench})
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/?t={bench.token}"
    print("OpenCoding 工作台已启动:", url)
    print("项目目录:", bench.workspace)
    print("关闭本窗口即退出工作台;项目数据不受影响。")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
