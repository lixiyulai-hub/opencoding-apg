#!/usr/bin/env python3
"""Chinese-first APG beginner intake and offline executor-adapter simulator.

This module creates deterministic in-memory previews only.  It never invokes a
Host, Provider, network, runtime, deployment target, Git, publication, or credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from typing import Any

CONSEQUENTIAL_CODES = (
    "secret",
    "money",
    "network",
    "deployment-choice",
    "real-data",
    "git-release",
    "irreversible-external-action",
)
TERMS = {
    "secret": ("secret", "password", "token", "api key", "密钥", "密码", "令牌"),
    "money": ("money", "payment", "charge", "付费", "付款", "金钱"),
    "network": ("network", "internet", "api", "联网", "网络", "接口"),
    "deployment-choice": ("deploy", "deployment", "hosting", "部署", "上线", "托管"),
    "real-data": ("real data", "production data", "真实数据", "生产数据"),
    "git-release": ("git push", "release", "publish", "github", "发布", "推送"),
    "irreversible-external-action": ("delete", "send", "不可逆", "删除", "发送"),
}
_SECRET_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_ -]?key)\s*[:=]\s*[^\s,;]+|"
    r"\b(?:sk|ghp)_[A-Za-z0-9_-]{8,}\b"
)

PLATFORM_TERMS = {
    "windows": ("windows", "电脑", "桌面"),
    "macos": ("macos", "mac", "苹果电脑"),
    "ios": ("ios", "iphone", "ipad"),
    "android": ("android", "安卓"),
    "web": ("web", "网页", "浏览器", "网站"),
}

CAPABILITY_RULES = (
    ("server", ("联网", "多人", "在线", "服务器", "上线", "同步"), "通常需要一个后端服务来处理登录、业务规则和数据同步。", "离线优先；若确认联网，建议托管 API 服务。"),
    ("database", ("保存", "数据", "记录", "账号", "登录", "多人", "订单", "成绩"), "需要长期保存并查询用户或业务数据。", "先用关系型数据库；数据结构稳定后再优化。"),
    ("api", ("api", "接口", "联网", "第三方", "地图", "短信", "AI", "支付", "同步", "登录", "通知", "提醒", "后台", "保存", "记录"), "需要和客户端、第三方服务或后台交换数据。", "先定义本地契约和模拟接口，真实接入另立 Gate。"),
    ("auth", ("登录", "账号", "用户", "家长", "权限", "注册"), "需要识别用户并区分权限。", "先做本地假登录和权限矩阵，再接真实身份服务。"),
    ("payment", ("支付", "付款", "付费", "订阅", "购买", "订单"), "涉及收费、订单或退款，属于 consequential 能力。", "先做支付流程模拟和订单状态机，不保存真实密钥。"),
    ("notifications", ("通知", "消息", "提醒", "推送", "短信", "邮件"), "需要把进度或事件主动告诉用户。", "先做通知事件与失败重试模拟，再选择具体渠道。"),
    ("admin", ("后台", "管理", "审核", "运营", "管理员", "报表"), "需要管理内容、用户、订单或审核记录。", "先做最小后台角色和审计列表。"),
    ("storage", ("上传", "文件", "图片", "视频", "附件", "资料"), "需要保存用户上传的文件或媒体。", "先用本地文件假实现，再规划对象存储。"),
)

DOCUMENT_SPECS = (
    ("PROJECT_BRIEF.md", "项目目标和范围", ()),
    ("PRODUCT_PLAN.md", "产品目标、用户和验收", ("PROJECT_BRIEF.md",)),
    ("UX_FLOW.md", "用户操作流程和关键页面", ("PRODUCT_PLAN.md",)),
    ("ARCHITECTURE.md", "系统组成和边界", ("PROJECT_BRIEF.md", "PRODUCT_PLAN.md")),
    ("STACK_DECISION.md", "平台与技术方案建议", ("ARCHITECTURE.md",)),
    ("TASK_GRAPH.md", "任务依赖和执行波次", ("STACK_DECISION.md",)),
    ("QUALITY_PLAN.md", "测试、证据和验收", ("TASK_GRAPH.md",)),
    ("DEPLOYMENT_PLAN.md", "部署前置条件和 Gate", ("QUALITY_PLAN.md",)),
    ("AGENTS.md", "项目执行规则和边界", ("TASK_GRAPH.md",)),
    ("memory.md", "项目长期记忆和决定", ("PROJECT_BRIEF.md", "STACK_DECISION.md")),
    ("PRG.md", "自动计划循环和 Requeue 规则", ("TASK_GRAPH.md", "QUALITY_PLAN.md")),
    ("plan.md", "当前阶段执行计划", ("PRG.md", "TASK_GRAPH.md")),
)


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


def redact(text: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", text)


def consequential_reasons(request: str) -> tuple[str, ...]:
    folded = request.casefold()
    return tuple(
        code for code in CONSEQUENTIAL_CODES
        if any(term.casefold() in folded for term in TERMS[code])
    )


def grill_me_questions() -> list[dict[str, str]]:
    return [
        {"id": "audience-problem", "question": "它主要帮谁解决什么问题？", "why": "知道服务对象，才能把想法拆成真正有用的需求。"},
        {"id": "success", "question": "做到什么样，你会觉得它已经可以用了？", "why": "完成标准会变成验收证据，避免只做出一半。"},
        {"id": "constraints", "question": "有没有不能做、必须保留，或特别在意的事情？", "why": "限制会决定安全默认方案和后续任务顺序。"},
    ]


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    folded = text.casefold()
    return any(term.casefold() in folded for term in terms)


def discovery_questions(goal: str) -> list[dict[str, str]]:
    """Return plain-language questions selected from the project idea."""
    questions = [
        {"id": "platforms", "question": "你准备让谁在哪些设备上使用？比如 Windows、Mac、iPhone、安卓手机或网页。", "why": "设备不同，系统会自动给出不同的实现方案。"},
        {"id": "online", "question": "这个项目要不要联网、登录、多人一起用，或者把数据保存起来？", "why": "这些答案会决定是否需要服务器、数据库和接口。"},
    ]
    if _has_any(goal, ("支付", "付款", "付费", "订阅", "订单")):
        questions.append({"id": "payment", "question": "支付是必须功能，还是以后再加？", "why": "支付会涉及订单、退款、密钥和人工确认。"})
    elif _has_any(goal, ("通知", "消息", "提醒", "推送")):
        questions.append({"id": "notifications", "question": "消息提醒要发给谁？允许哪些通知方式？", "why": "通知需要明确对象、渠道和失败后的处理方式。"})
    else:
        questions.append({"id": "operations", "question": "以后是否需要后台管理、审核内容或查看数据？", "why": "提前知道运营需求，才能安排后台和权限任务。"})
    return questions


def recommend_platforms(goal: str) -> dict[str, Any]:
    detected = [name for name, terms in PLATFORM_TERMS.items() if _has_any(goal, terms)]
    if not detected:
        detected = ["web"]
    if "ios" in detected and "android" in detected and "web" not in detected:
        detected.append("web")
    primary = "web" if "web" in detected else detected[0]
    labels = {"windows": "Windows", "macos": "macOS", "ios": "iOS", "android": "Android", "web": "Web"}
    return {
        "requested": [labels[item] for item in detected],
        "primary": labels[primary],
        "recommendation": "先做 Web 版" if primary == "web" else f"先做 {labels[primary]} 版，再按需要扩展到其他平台",
        "alternatives": ["跨平台客户端" if len(detected) > 1 else "原生客户端", "响应式网页"],
        "reason": "先覆盖用户真正使用的设备，并优先选择便于验证和回滚的方案。",
        "confidence": "high" if len(detected) > 0 else "medium",
        "unresolved": [] if len(detected) == 1 else ["是否要求所有平台同时上线"],
    }


def recommend_solution(platforms: dict[str, Any], capabilities: list[dict[str, Any]]) -> dict[str, Any]:
    needs_server = any(item["capability"] == "server" and item["need"] for item in capabilities)
    needs_db = any(item["capability"] == "database" and item["need"] for item in capabilities)
    needs_api = any(item["capability"] == "api" and item["need"] for item in capabilities)
    client = "响应式 Web 客户端" if platforms["primary"] == "Web" else "跨平台客户端 + 响应式 Web 管理端"
    return {
        "client": {"recommendation": client, "reason": "系统根据目标设备给方案，小白不需要先学前端技术名词。"},
        "backend": {"recommendation": "轻量后端 API" if needs_server or needs_api else "先不设后端", "reason": "只在确实需要联网、多人或第三方接口时安排后端。"},
        "database": {"recommendation": "关系型数据库" if needs_db else "暂不需要数据库", "reason": "需要长期保存和查询的数据才进入数据库任务。"},
        "delivery": {"mode": "offline-preview", "real_services": False, "note": "真实服务器、数据库、API 和第三方服务需单独确认。"},
    }


def capability_matrix(goal: str) -> list[dict[str, Any]]:
    rows = []
    for capability, terms, reason, recommendation in CAPABILITY_RULES:
        need = _has_any(goal, terms)
        rows.append({
            "capability": capability,
            "need": need,
            "status": "recommended" if need else "not-indicated",
            "reason": reason if need else "当前描述没有明确提出，先不增加复杂度。",
            "source": "request.text" if need else "request.text (no signal)",
            "recommendation": recommendation if need else "暂不安排；若后续出现需求，再生成新计划。",
            "risk": "consequential" if capability in {"payment", "server", "api", "database"} and need else "low",
            "gate_required": capability in {"payment", "server", "api", "database", "notifications"} and need,
            "evidence": ["request_digest", "capability-matrix-sha256"],
            "rollback": "移除该能力任务并保留当前文档证据。",
        })
    return rows


def task_waves(capabilities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active = {item["capability"] for item in capabilities if item["need"]}
    waves = [
        {"wave": 1, "name": "想法和需求", "tasks": ["intake", "grill-me", "project-brief"], "depends_on": []},
        {"wave": 2, "name": "平台和方案", "tasks": ["platform-recommendation", "solution-recommendation", "plan-confirmation"], "depends_on": [1]},
        {"wave": 3, "name": "产品和界面", "tasks": ["product-plan", "ux-flow", "architecture"], "depends_on": [2]},
    ]
    service_tasks = [name for name in ("server", "database", "api", "auth", "storage") if name in active]
    if service_tasks:
        waves.append({"wave": 4, "name": "基础能力", "tasks": service_tasks, "depends_on": [3]})
    integrations = [name for name in ("payment", "notifications", "admin") if name in active]
    if integrations:
        waves.append({"wave": 5, "name": "业务集成", "tasks": integrations, "depends_on": [4 if service_tasks else 3]})
    waves.extend([
        {"wave": 6, "name": "测试和验收", "tasks": ["quality-plan", "offline-validation"], "depends_on": [waves[-1]["wave"]]},
        {"wave": 7, "name": "报告和下一步", "tasks": ["report", "requeue"], "depends_on": [6]},
    ])
    return waves


def _document_content(name: str, goal: str, platforms: dict[str, Any], solution: dict[str, Any], matrix: list[dict[str, Any]], waves: list[dict[str, Any]]) -> str:
    active = [item["capability"] for item in matrix if item["need"]]
    wave_lines = "\n".join(f"- 第 {item['wave']} 波：{item['name']}（任务：{'、'.join(item['tasks'])}）" for item in waves)
    return "\n".join((
        f"# {name}", "", "## 项目目标", goal, "",
        "## 平台建议", f"- 首选：{platforms['recommendation']}", f"- 覆盖：{'、'.join(platforms['requested'])}", f"- 理由：{platforms['reason']}", "",
        "## 技术方案", f"- 客户端：{solution['client']['recommendation']}", f"- 后端：{solution['backend']['recommendation']}", f"- 数据库：{solution['database']['recommendation']}", "",
        "## 能力判断", f"- 已识别：{'、'.join(active) if active else '暂无额外能力'}", "- 未识别的能力保持 unknown，不凭空增加。", "",
        "## 任务波次", wave_lines, "",
        "## 边界", "- 当前只做离线预览，不连接真实服务器、数据库、API、支付、通知、Host、Provider 或 GitHub。", "",
    ))


def document_package(goal: str, platforms: dict[str, Any], solution: dict[str, Any], matrix: list[dict[str, Any]], waves: list[dict[str, Any]]) -> dict[str, Any]:
    documents = {}
    for name, title, dependencies in DOCUMENT_SPECS:
        content = _document_content(title, goal, platforms, solution, matrix, waves)
        documents[name] = {"path": name, "title": title, "dependencies": list(dependencies), "status": "preview", "content": content, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
    return {"format": "markdown", "status": "in-memory-preview", "documents": documents, "generated_paths": list(documents), "bundle_sha256": hashlib.sha256(canonical_bytes(documents)).hexdigest()}


def _markdown(goal: str, questions: list[dict[str, str]], route: str, gate_reasons: tuple[str, ...], terminal: str, resume: str) -> str:
    question_lines = "\n".join(
        f"- **{item['question']}**  \n  为什么：{item['why']}" for item in questions
    )
    gate = "无；系统采用安全离线默认方案。" if not gate_reasons else "合并为一次 consequential Gate：" + "、".join(gate_reasons)
    task_state = "自动推进" if route == "auto" else "等待唯一 Gate"
    return "\n".join((
        "# 项目 Markdown 知识包（离线预览）",
        "",
        "## 项目目标",
        goal,
        "",
        "## 初心者澄清（Grill Me）",
        question_lines,
        "",
        "## 需求",
        "- 用中文说明目标、用户、完成标准与限制。",
        "- 系统自动采用安全的离线默认方案；建议不是审批。",
        "",
        "## 任务编排",
        f"- intake → knowledge-pack → task-graph → adapter-preview → validate → report → requeue（当前：{task_state}）",
        "- 依赖：知识包完成后才能生成任务图；任务图完成后才能生成 adapter 预览。",
        "",
        "## Gate",
        gate,
        "",
        "## 证据",
        "- 确定性 JSON 结果、知识包 SHA-256、契约测试、独立复核和回滚演练。",
        "",
        "## 回滚",
        "- 本阶段没有实际执行器或外部动作；失败时保留证据并回到离线预览。",
        "",
        "## Requeue",
        f"- terminal={terminal}; resume_condition={resume}",
        "",
    ))


def _task_graph(route: str, gate_reasons: tuple[str, ...]) -> dict[str, Any]:
    nodes = [
        {"id": "intake", "label": "中文想法入口", "lane": "auto"},
        {"id": "grill-me", "label": "Grill Me 澄清", "lane": "auto"},
        {"id": "knowledge-pack", "label": "项目 Markdown 知识包", "lane": "auto"},
        {"id": "task-graph", "label": "需求与任务图", "lane": "auto"},
        {"id": "safe-default", "label": "安全默认方案", "lane": "recommend"},
        {"id": "adapter-preview", "label": "离线 executor adapter", "lane": "auto"},
        {"id": "validate-report", "label": "验证、报告与 requeue", "lane": "auto"},
    ]
    if gate_reasons:
        nodes.append({"id": "consequential-gate", "label": "唯一 consequential Gate", "lane": "gate"})
    edges = [
        ["intake", "grill-me"], ["grill-me", "knowledge-pack"],
        ["knowledge-pack", "task-graph"], ["task-graph", "safe-default"],
        ["safe-default", "adapter-preview"], ["adapter-preview", "validate-report"],
    ]
    if gate_reasons:
        edges.append(["task-graph", "consequential-gate"])
    return {"route": route, "nodes": nodes, "edges": edges, "legend": {"auto": "自动推进", "recommend": "系统采用安全默认方案", "gate": "唯一 consequential Gate"}}


def simulate(request: str, *, failure: str | None = None) -> dict[str, Any]:
    if not isinstance(request, str) or not request.strip():
        raise ValueError("request must be non-empty text")
    goal = redact(request.strip())
    reasons = consequential_reasons(goal)
    route = "consequential-gate" if reasons else "auto"
    failure_code = failure.strip().casefold().replace(" ", "-") if isinstance(failure, str) and failure.strip() else ""
    terminal = "FREEZE" if failure_code else "REQUEUE"
    loop_states = ["INSPECT", "PROGRESS", "PLAN", "DISPATCH", "VALIDATE"]
    if failure_code:
        loop_states.append("FREEZE")
        resume = f"resume.after-{failure_code}-is-resolved"
    else:
        loop_states.extend(["REPORT", "REQUEUE"])
        resume = "resume.after-bounded-loop-continues-without-stop-condition"
    questions = grill_me_questions()
    discovery = discovery_questions(goal)
    platforms = recommend_platforms(goal)
    matrix = capability_matrix(goal)
    solution = recommend_solution(platforms, matrix)
    waves = task_waves(matrix)
    documents = document_package(goal, platforms, solution, matrix, waves)
    markdown = _markdown(goal, questions, route, reasons, terminal, resume)
    external_actions = {"host": False, "provider": False, "network": False, "runtime": False, "deployment": False, "git": False, "publication": False}
    adapter = {
        "schema_version": "1.0",
        "mode": "offline-simulation",
        "task_context": {
            "work_item_id": "apg.beginner.preview",
            "read_scope": ["input.request", "intake", "intake_routing", "stack_decision"],
            "planned_write_scope": ["in-memory.preview"],
            "preimage_hash": "not-applicable-preview",
            "postimage_hash": "not-computable-preview",
            "real_write_performed": False,
        },
        "gate_prerequisites": list(reasons),
        "dispatch_permitted": not reasons and not failure_code,
        "simulated_output": {"status": "SIMULATED" if not failure_code else "FREEZE", "evidence": ["knowledge-pack-sha256", "task-graph", "replay-digest"]},
        "error_classification": "none" if not failure_code else failure_code,
        "retry": {"strategy": "requeue-after-resume-condition", "max_automatic_attempts": 1},
        "rollback": "no external side effect; discard in-memory preview and preserve evidence",
        "resume_condition": resume,
        "external_actions": external_actions,
    }
    return {
        "schema_version": "1.0",
        "mode": "offline-simulation",
        "language": "zh-CN",
        "request": goal,
        "grill_me": {"name": "Grill Me", "questions": questions, "beginner_message": "我会先把你的想法整理好，再用简单问题补齐关键信息。"},
        "intake": {
            "request_text": goal,
            "language": "zh-CN",
            "discovery_questions": discovery,
            "beginner_message": "你不用先选技术，我会根据目标和使用设备给出方案。",
        },
        "intake_routing": {
            "route": route,
            "platforms": platforms,
            "capability_matrix": matrix,
            "unresolved": platforms["unresolved"],
        },
        "stack_decision": solution,
        "plan_confirmation": {
            "state": "awaiting-human-confirmation" if reasons else "recommendation-ready",
            "message": "方案已整理好；确认后才能进入下一步。" if reasons else "方案已整理好；普通离线任务可以继续预览。",
            "required_for": list(reasons),
        },
        "route": route,
        "human_gate": bool(reasons),
        "gate_reasons": list(reasons),
        "default_policy": "auto-select-safe-defaults",
        "loop_states": loop_states,
        "terminal_state": terminal,
        "resume_condition": resume,
        "knowledge_pack": {"format": "markdown", "content": markdown, "sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest()},
        "task_graph": _task_graph(route, reasons),
        "task_waves": waves,
        "document_package": documents,
        "adapter": adapter,
        "execution_performed": False,
        "external_actions": external_actions,
    }


def replay_digest(request: str, *, failure: str | None = None) -> str:
    return hashlib.sha256(canonical_bytes(simulate(request, failure=failure))).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="APG Chinese-first offline beginner preview")
    parser.add_argument("request", nargs="+", help="Chinese natural-language project idea")
    parser.add_argument("--failure", default=None, help="offline failure code for FREEZE testing")
    args = parser.parse_args(argv)
    sys.stdout.buffer.write(canonical_bytes(simulate(" ".join(args.request), failure=args.failure)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
