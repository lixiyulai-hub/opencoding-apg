"""Deterministic offline recommendations derived from business answers."""

from __future__ import annotations

from typing import Any

from .intake import QUESTION_DEFINITIONS, SCHEMA_VERSION, SUPPORTED_PLATFORMS, _validate_session_shape, sanitize_text


CAPABILITY_IDS = ("server", "database", "api", "auth", "payment", "notifications", "admin", "storage")


def _requirements(session: dict[str, Any]) -> dict[str, dict[str, Any]]:
    _validate_session_shape(session)
    return session["requirements"]


def _platform_recommendation(requirements: dict[str, dict[str, Any]]) -> dict[str, Any]:
    requirement = requirements.get("platform", {})
    requested = [item for item in requirement.get("platforms", []) if item in SUPPORTED_PLATFORMS]
    unresolved: list[str] = []
    if not requested:
        primary = "web"
        confidence = "low"
        unresolved.append("platform:needs_user_confirmation")
        reason = "没有得到可确认的平台，先给出网页低置信度草案；系统不会把默认建议伪装成用户选择。"
    else:
        primary = requested[0]
        confidence = "high" if len(requested) == 1 and requirement.get("kind") == "known" else "medium"
        reason = "根据用户明确提到的平台生成建议；多个平台需要确认首要范围和顺序。"
        if len(requested) > 1:
            unresolved.append("platform:multiple_targets_need_scope_order")
    if requirement.get("kind") in {"unknown", "ambiguous", "conflict"}:
        unresolved.append("platform:needs_user_confirmation")
        confidence = "low"
    return {"requested": list(dict.fromkeys(requested)), "primary": primary, "reason": reason, "confidence": confidence, "unresolved": list(dict.fromkeys(unresolved))}


def _stack_item(technology: str, reason: str, alternatives: list[str], maintenance: str, cost_note: str) -> dict[str, Any]:
    return {
        "technology": technology,
        "reason": reason,
        "alternatives": alternatives,
        "version_basis": "技术版本和工具链未在本次离线事务中联网核实，以后续实施环境锁定为准。",
        "maintenance": maintenance,
        "cost_note": cost_note,
    }


def _stack(primary: str, needs: dict[str, str]) -> dict[str, dict[str, Any]]:
    client_choices = {
        "windows": ("Tauri 2 + TypeScript", ["原生 Windows"], "适合轻量桌面客户端，后续需要 Windows 打包实测。"),
        "macos": ("SwiftUI + Xcode", ["Tauri 2 + TypeScript"], "适合 Mac 原生桌面体验，后续需要 macOS 环境实测。"),
        "ios": ("SwiftUI + Xcode", ["React Native"], "适合苹果手机原生交互，后续需要 iOS 构建实测。"),
        "android": ("Kotlin + Jetpack Compose", ["Flutter"], "适合安卓原生交互，后续需要 Android 构建实测。"),
        "web": ("TypeScript + Vite", ["原生 HTML/CSS/JavaScript"], "适合先做本地网页工作台，浏览器兼容性需要实测。"),
        "mini_program": ("微信小程序原生 WXML/基础库", ["跨端小程序框架"], "适合小程序首屏和平台能力，需开发工具与真机实测。"),
        "cli": ("Python 3.11+ argparse", ["Rust clap"], "适合中文命令行向导和离线运行，需目标终端实测。"),
    }
    technology, alternatives, reason = client_choices.get(primary, client_choices["web"])
    client = _stack_item(technology, reason, alternatives, "平台工具链、升级和打包签名需要单独维护。", "本地方案无服务费用承诺；构建或商店费用未核实。")
    server_required = needs["server"] == "required"
    backend = _stack_item(
        "Python 3.11+ 标准库服务边界" if server_required else "无需远程后端服务（本地优先）",
        "跨设备、第三方内容、后台或多人协作需要服务边界。" if server_required else "远程后端需求尚未确认；本轮不因本地保存强制增加远程服务器。" if needs["server"] == "unknown" else "本轮仅依据业务答案，不因本地保存强制增加远程服务器。",
        ["FastAPI"] if server_required else ["范围变化后再评估服务"],
        "服务激活后需日志、备份、升级和安全维护；本地模式维护负担较低。",
        "本地模式无托管费用；云服务成本未核实。",
    )
    database = _stack_item(
        "SQLite 3（本地优先）" if needs["database"] == "required" else "数据库需求待确认" if needs["database"] == "unknown" else "当前范围不安排数据库",
        "用于保存用户明确要求保留的输入和结果；跨设备同步另需服务。" if needs["database"] == "required" else "数据库是否需要取决于尚未确认的保存需求。" if needs["database"] == "unknown" else "用户明确表示当前不需要保存数据。",
        ["PostgreSQL（多人服务化时）"] if needs["database"] == "required" else ["确认业务保存范围后再选型"],
        "本地数据库维护简单；多人服务化时需要迁移、备份和并发运维。",
        "本地存储无托管费用；云数据库费用未核实。",
    )
    runtime = _stack_item(
        "本地进程" if not server_required else "本地工作台 + 明确的服务边界",
        "先保证离线旅程可运行，外部连接器另行受控激活。",
        ["回环地址本地服务"],
        "需要维护本机运行环境；服务模式另需部署与安全验收。",
        "不承诺部署费用；外部运行成本需要后续确认。",
    )
    return {"client": client, "backend": backend, "database": database, "runtime": runtime}


def _need(requirement: dict[str, Any] | None, source: str, *, positive: str = "required", negative: str = "not_needed") -> tuple[str, str]:
    if not requirement or requirement.get("kind") in {"unknown", "ambiguous", "conflict"}:
        return "unknown", f"{source}:用户尚未给出可确定的答案。"
    if requirement.get("value") is True:
        return positive, f"{source}:用户明确表示需要。"
    if requirement.get("value") is False:
        return negative, f"{source}:用户明确表示不需要。"
    return "unknown", f"{source}:答案不是可执行的明确判断。"


def _capability(capability_id: str, need: str, reason: str, source: str) -> dict[str, str]:
    if need == "not_needed":
        gate = "当前不安排真实激活；若业务范围改变，重新确认方案。"
    elif need == "unknown":
        gate = "先补充业务答案；不得把未知需求当成已授权的连接或执行。"
    else:
        gate = "可先离线设计；真实服务、凭据、数据、成本和目标需绑定新的确认。"
    return {"id": capability_id, "need": need, "reason": reason, "source": source, "activation_gate": gate}


def _business_project(session: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    requirements = session["requirements"]
    goal = sanitize_text(session["goal"])
    audience_req = requirements.get("audience", {})
    outcome_req = requirements.get("outcome", {})
    audience = audience_req.get("value") if audience_req.get("kind") == "known" else None
    outcome = outcome_req.get("value") if outcome_req.get("kind") == "known" else None
    if isinstance(audience, str):
        audience = sanitize_text(audience)
    if isinstance(outcome, str):
        outcome = sanitize_text(outcome)
    unresolved: list[str] = []
    if not audience:
        unresolved.append("audience:needs_clarification")
    if not outcome:
        unresolved.append("outcome:needs_clarification")
    scenarios: list[dict[str, str]] = []
    if audience and outcome:
        scenarios.append({
            "id": "scenario-1",
            "title": goal,
            "actor": audience,
            "action": outcome,
            "result": outcome,
            "source": "answers.outcome",
        })
    return {"goal": goal, "audience": audience, "outcome": outcome, "scenarios": scenarios}, unresolved


def build_recommendation(session: dict[str, Any]) -> dict[str, Any]:
    """Build a strictly shaped JSON-safe Recommendation without network use."""

    if not isinstance(session, dict):
        raise TypeError("session must be a dictionary")
    _validate_session_shape(session)
    requirements = _requirements(session)
    platform_info = _platform_recommendation(requirements)
    project, project_unresolved = _business_project(session)
    data_need, data_reason = _need(requirements.get("data_persistence"), "data_persistence")
    cross_need, cross_reason = _need(requirements.get("cross_device"), "cross_device")
    file_need, file_reason = _need(requirements.get("file_storage"), "file_storage")
    external_need, external_reason = _need(requirements.get("external_data"), "external_data")
    admin_need, admin_reason = _need(requirements.get("admin_access"), "admin_access")
    account_need, account_reason = _need(requirements.get("account_access"), "account_access")
    notification_need, notification_reason = _need(requirements.get("notifications"), "notifications")
    payment_need, payment_reason = _need(requirements.get("payments"), "payments")
    multi_need, multi_reason = _need(requirements.get("multi_user"), "multi_user")
    if "required" in {cross_need, external_need, admin_need, multi_need}:
        server_need = "required"
    elif "unknown" in {cross_need, external_need, admin_need, multi_need}:
        server_need = "unknown"
    else:
        server_need = "not_needed"
    api_need = "required" if external_need == "required" else "unknown" if external_need == "unknown" else "optional"
    storage_need = "required" if file_need == "required" or data_need == "required" else "unknown" if file_need == "unknown" or data_need == "unknown" else "not_needed"
    capabilities = [
        _capability("server", server_need, "跨设备、第三方内容、后台或多人协作共同决定是否需要远程服务。", "cross_device,external_data,admin_access,multi_user"),
        _capability("database", data_need, data_reason, "data_persistence"),
        _capability("api", api_need, external_reason if external_need != "not_needed" else "当前未确认需要第三方内容。", "external_data"),
        _capability("auth", account_need, account_reason, "account_access"),
        _capability("payment", payment_need, payment_reason, "payments"),
        _capability("notifications", notification_need, notification_reason, "notifications"),
        _capability("admin", admin_need, admin_reason, "admin_access"),
        _capability("storage", storage_need, file_reason if file_need != "not_needed" else data_reason, "file_storage,data_persistence"),
    ]
    unresolved = list(platform_info["unresolved"])
    unresolved.extend(project_unresolved)
    for question in QUESTION_DEFINITIONS:
        question_id = question["id"]
        requirement = requirements.get(question_id)
        if question_id not in session["answers"]:
            unresolved.append(f"{question_id}:unanswered")
        elif requirement and requirement.get("kind") in {"unknown", "ambiguous", "conflict"}:
            for code in requirement.get("reason_codes", ["unresolved"]):
                unresolved.append(f"{question_id}:{code}")
        if requirement and requirement.get("conflict"):
            unresolved.append(f"{question_id}:answer_conflict")
    unresolved = list(dict.fromkeys(unresolved))
    assumptions = [
        "本推荐只代表离线方案，不表示已经连接服务器、数据库、账号、支付或通知服务。",
        "技术版本、平台工具链和费用未在本次离线事务中联网核实。",
    ]
    if not platform_info["requested"]:
        assumptions.append("平台暂未确认，网页只是低置信度占位建议。")
    status = "ready" if session["state"] == "recommendation_ready" and not unresolved else "draft"
    acceptance = [
        f"业务目标：{project['goal']}",
        f"业务结果：{project['outcome']}" if project["outcome"] else "业务结果待用户确认，当前不宣称 ready。",
        "技术建议、外部服务激活和真实交付分别验收，不把离线草案写成已完成。",
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": session["id"],
        "revision": session["revision"],
        "status": status,
        "project": project,
        "platforms": platform_info,
        "stack": _stack(platform_info["primary"], {"server": server_need, "database": data_need, "api": api_need}),
        "capabilities": capabilities,
        "assumptions": assumptions,
        "unresolved": unresolved,
        "acceptance": acceptance,
    }


__all__ = ["CAPABILITY_IDS", "SUPPORTED_PLATFORMS", "build_recommendation"]
