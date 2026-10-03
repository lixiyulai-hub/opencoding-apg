"""离线生成中文业务文档，并严格校验 OpenCoding Recommendation 1.1。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

SCHEMA_VERSION = "1.1"
_RECOMMENDATION_KEYS = {"schema_version", "session_id", "revision", "status", "project", "platforms", "stack", "capabilities", "assumptions", "unresolved", "acceptance"}
_PROJECT_KEYS = {"goal", "audience", "outcome", "scenarios"}
_SCENARIO_KEYS = {"id", "title", "actor", "action", "result", "source"}
_PLATFORM_KEYS = {"requested", "primary", "reason", "confidence", "unresolved"}
_STACK_KEYS = {"client", "backend", "database", "runtime"}
_STACK_ENTRY_KEYS = {"technology", "reason", "alternatives", "version_basis", "maintenance", "cost_note"}
_CAPABILITY_KEYS = {"id", "need", "reason", "source", "activation_gate"}
_CAPABILITY_IDS = {"server", "database", "api", "auth", "payment", "notifications", "admin", "storage"}
_PLATFORMS = {"windows", "macos", "ios", "android", "web", "mini_program", "cli"}
_NEEDS = {"required", "optional", "not_needed", "unknown"}
_STATUSES = {"draft", "ready"}
_CONFIDENCE = {"low", "medium", "high"}
_SCENARIO_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_BASE_DOCUMENT_NAMES = ("AGENTS.md", "memory.md", "PRG.md", "plan.md", "product.md", "architecture.md", "ui.md", "security.md")


def _non_empty(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


def _string_list(value: Any, field: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")


def _validate_scenarios(value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError("project.scenarios must be a list")
    seen: set[str] = set()
    for scenario in value:
        if not isinstance(scenario, Mapping) or set(scenario) != _SCENARIO_KEYS:
            raise ValueError("scenario entries must use the shared schema")
        scenario_id = scenario["id"]
        if not isinstance(scenario_id, str) or not _SCENARIO_ID.fullmatch(scenario_id):
            raise ValueError("scenario id must be a stable ASCII identifier")
        if scenario_id in seen:
            raise ValueError("scenario ids must be unique")
        seen.add(scenario_id)
        for field in _SCENARIO_KEYS - {"id"}:
            _non_empty(scenario[field], f"scenario.{field}")


def validate_recommendation(recommendation: Mapping[str, Any]) -> None:
    """Validate the closed Recommendation 1.1 boundary without doing I/O."""
    if not isinstance(recommendation, Mapping):
        raise ValueError("recommendation must be a mapping")
    if set(recommendation) != _RECOMMENDATION_KEYS:
        missing = sorted(_RECOMMENDATION_KEYS - set(recommendation))
        unknown = sorted(set(recommendation) - _RECOMMENDATION_KEYS)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise ValueError("invalid recommendation fields: " + "; ".join(details))
    if recommendation["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported recommendation schema_version")
    _non_empty(recommendation["session_id"], "session_id")
    if type(recommendation["revision"]) is not int or recommendation["revision"] < 0:
        raise ValueError("revision must be a non-negative integer")
    if not isinstance(recommendation["status"], str) or recommendation["status"] not in _STATUSES:
        raise ValueError("status must be draft or ready")
    project = recommendation["project"]
    if not isinstance(project, Mapping) or set(project) != _PROJECT_KEYS:
        raise ValueError("project must contain goal, audience, outcome, scenarios")
    _non_empty(project["goal"], "project.goal")
    for field in ("audience", "outcome"):
        if project[field] is not None and not isinstance(project[field], str):
            raise ValueError(f"project.{field} must be a string or null")
        if isinstance(project[field], str) and not project[field].strip():
            raise ValueError(f"project.{field} cannot be blank")
    _validate_scenarios(project["scenarios"])
    platforms = recommendation["platforms"]
    if not isinstance(platforms, Mapping) or set(platforms) != _PLATFORM_KEYS:
        raise ValueError("platforms must use the shared schema")
    if not isinstance(platforms["requested"], list) or any(not isinstance(item, str) or item not in _PLATFORMS for item in platforms["requested"]):
        raise ValueError("platforms.requested must contain known platform strings")
    if len(set(platforms["requested"])) != len(platforms["requested"]):
        raise ValueError("platforms.requested must be unique")
    if platforms["primary"] is not None and (not isinstance(platforms["primary"], str) or platforms["primary"] not in _PLATFORMS):
        raise ValueError("platforms.primary must be a known platform or null")
    if platforms["primary"] is not None and platforms["primary"] not in platforms["requested"]:
        if not (recommendation["status"] == "draft" and platforms["confidence"] == "low"):
            raise ValueError("platforms.primary must be one of platforms.requested unless it is a low-confidence draft candidate")
    _non_empty(platforms["reason"], "platforms.reason")
    if not isinstance(platforms["confidence"], str) or platforms["confidence"] not in _CONFIDENCE:
        raise ValueError("platforms.confidence must be low, medium, or high")
    if platforms["confidence"] == "high" and (not platforms["requested"] or not platforms["primary"] or platforms["unresolved"]):
        raise ValueError("high confidence requires a confirmed platform without unresolved questions")
    _string_list(platforms["unresolved"], "platforms.unresolved")
    stack = recommendation["stack"]
    if not isinstance(stack, Mapping) or set(stack) != _STACK_KEYS:
        raise ValueError("stack must contain client, backend, database, runtime")
    for key in _STACK_KEYS:
        entry = stack[key]
        if not isinstance(entry, Mapping) or set(entry) != _STACK_ENTRY_KEYS:
            raise ValueError(f"invalid stack entry fields: {key}")
        for field in _STACK_ENTRY_KEYS - {"alternatives"}:
            _non_empty(entry[field], f"stack.{key}.{field}")
        _string_list(entry["alternatives"], f"stack.{key}.alternatives")
    capabilities = recommendation["capabilities"]
    if not isinstance(capabilities, list) or len(capabilities) != len(_CAPABILITY_IDS):
        raise ValueError("capabilities must contain all eight capability entries")
    seen: set[str] = set()
    for capability in capabilities:
        if not isinstance(capability, Mapping) or set(capability) != _CAPABILITY_KEYS:
            raise ValueError("capability entries must use the shared schema")
        capability_id = capability["id"]
        if not isinstance(capability_id, str) or capability_id not in _CAPABILITY_IDS or capability_id in seen:
            raise ValueError("capability ids must be known and unique")
        seen.add(capability_id)
        if not isinstance(capability["need"], str) or capability["need"] not in _NEEDS:
            raise ValueError(f"unknown capability need: {capability_id}")
        for field in ("reason", "source", "activation_gate"):
            _non_empty(capability[field], f"capability.{capability_id}.{field}")
    if seen != _CAPABILITY_IDS:
        raise ValueError("capabilities must include exactly the eight known ids")
    for field in ("assumptions", "unresolved", "acceptance"):
        _string_list(recommendation[field], field)
    if recommendation["status"] == "ready":
        if not isinstance(project["audience"], str) or not isinstance(project["outcome"], str):
            raise ValueError("ready recommendation requires audience and outcome")
        if not project["scenarios"]:
            raise ValueError("ready recommendation requires at least one scenario")
        if recommendation["unresolved"] or platforms["unresolved"]:
            raise ValueError("ready recommendation cannot contain unresolved questions")
    if not recommendation["acceptance"]:
        raise ValueError("acceptance must contain at least one business result")
    business_text = " ".join(recommendation["acceptance"])
    if not any(token and token in business_text for token in (project["goal"], project["outcome"] or "")):
        raise ValueError("acceptance must carry the project business result")


def capability_map(recommendation: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    validate_recommendation(recommendation)
    return {item["id"]: item for item in recommendation["capabilities"]}


def _need(capabilities: Mapping[str, Mapping[str, Any]], capability_id: str) -> str:
    return capabilities[capability_id]["need"]


def document_names(recommendation: Mapping[str, Any]) -> list[str]:
    """Return the deterministic document set without invoking task planning."""
    validate_recommendation(recommendation)
    capabilities = {item["id"]: item for item in recommendation["capabilities"]}
    names = list(_BASE_DOCUMENT_NAMES)
    if _need(capabilities, "database") != "not_needed":
        names.append("data.md")
    if _need(capabilities, "api") != "not_needed":
        names.append("interface.md")
    if _need(capabilities, "auth") != "not_needed" or _need(capabilities, "admin") != "not_needed":
        names.append("permissions.md")
    if _need(capabilities, "payment") != "not_needed":
        names.append("payment.md")
    if _need(capabilities, "notifications") != "not_needed":
        names.append("notifications.md")
    if any(_need(capabilities, item) != "not_needed" for item in ("server", "storage", "api", "payment", "notifications")):
        names.append("deployment.md")
    return names


def _bullet(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- 暂无记录。"


def _business_context(recommendation: Mapping[str, Any]) -> list[str]:
    project = recommendation["project"]
    lines = [f"- 目标：{project['goal']}", f"- 使用者：{project['audience'] or '待澄清'}", f"- 业务结果：{project['outcome'] or '待澄清'}"]
    if project["scenarios"]:
        lines.append("- 业务场景：" + "；".join(item["title"] for item in project["scenarios"]))
    return lines


def render_documents(recommendation: dict, task_plan: Mapping[str, Any] | None = None) -> dict[str, str]:
    """返回不写盘的中文文档；可传同源 TaskPlan 注入 plan.md 的图摘要。"""
    validate_recommendation(recommendation)
    if task_plan is None:
        from .planning import build_task_plan

        task_plan = build_task_plan(recommendation)
    else:
        from .planning import validate_task_plan

        if not isinstance(task_plan, Mapping):
            raise ValueError("task_plan must be a mapping")
        plan_result = validate_task_plan(task_plan)
        if not plan_result["valid"]:
            raise ValueError("invalid task plan: " + ", ".join(plan_result["errors"]))
        if task_plan.get("session_id") != recommendation["session_id"] or task_plan.get("revision") != recommendation["revision"]:
            raise ValueError("task_plan source session or revision does not match recommendation")
        from .planning import build_task_plan

        expected_plan = build_task_plan(recommendation)
        if task_plan.get("tasks") != expected_plan.get("tasks") or task_plan.get("waves") != expected_plan.get("waves"):
            raise ValueError("task_plan graph does not match recommendation content")
    capabilities = capability_map(recommendation)
    project = recommendation["project"]
    platforms = recommendation["platforms"]
    platform = platforms["primary"] or (platforms["requested"][0] if platforms["requested"] else "待选择")
    status = recommendation["status"]
    unresolved: list[str] = []
    for item in list(recommendation["unresolved"]) + list(platforms["unresolved"]) + list(task_plan.get("unresolved", [])):
        if item not in unresolved:
            unresolved.append(item)
    context = _business_context(recommendation)
    scenario_lines = [f"{item['id']}：{item['title']}（{item['actor']}）→ {item['action']}，结果：{item['result']}。来源：{item['source']}" for item in project["scenarios"]]
    docs: dict[str, str] = {
        "AGENTS.md": "\n".join(["# 项目工作规则", "", "本文件规定本项目计划阶段的范围、验证方式和确认边界。", "", "## 允许范围", "", _bullet(["只围绕已确认的业务目标与场景编写计划。", "所有实现任务均为待执行计划，不代表代码已经存在或已经通过验证。", "不调用 Host、Provider、网络、凭据或真实业务服务。", "外部服务激活、部署、发布和真实数据使用必须单独确认。"]), "", "## 当前业务上下文", "", *context, "", "## 验证", "", "- 使用离线单元测试和任务图校验；失败时保留证据并停止自动推进。"]),
        "memory.md": "\n".join(["# 决策记忆", "", "这里记录业务事实、选择理由和未解决问题，不替代执行日志。", "", "## 用户已确认事实（source=user.answers）", "", *context, "", "## 业务场景（由用户回答生成，保留 source）", "", _bullet(scenario_lines), "", "## 方案选择", "", _bullet([f"{key}：{value['technology']}。理由：{value['reason']}。替代方案：{', '.join(value['alternatives']) or '无'}。" for key, value in recommendation["stack"].items()]), "", "## Agent 临时假设（source=agent.assumption，不是用户需求）", "", _bullet(recommendation["assumptions"]), "", "## 未解决问题", "", _bullet(unresolved)]),
        "PRG.md": "\n".join(["# 自动推进规则", "", "本文件描述规划事务如何自动推进，不是第二份产品需求书。", "", "## 状态循环", "", _bullet(["INSPECT：读取当前事实、版本和既有证据。", "PROGRESS：记录当前阶段和可计算的进展。", "PLAN：从同一任务图生成顺序、依赖和验收。", "DISPATCH：仅派发离线、可逆的本地规划工作。", "VALIDATE：校验结构、路径、依赖、证据和业务结果。", "REPORT：写入结果、哈希、失败项和限制。", "REQUEUE：只有在失败项可定位且恢复条件满足时重新排队。"]), "", "## FREEZE 与恢复", "", _bullet(["首次失败立即 FREEZE，保留原始输入、receipt、快照和部分结果，不覆盖后续用户修改。", "只有修复原因、重新通过结构校验、确认输入与证据哈希未漂移后，才可从失败任务重新排队。", "普通本地事务可自动继续；Provider、Host、凭据、真实数据、部署和发布到达边界时必须重新确认。"]), "", "## 当前范围", "", *context, "", "## 当前状态", "", f"- Recommendation：{status}；平台：{platform}。", f"- 版本：session `{recommendation['session_id']}` revision {recommendation['revision']}。"]),
        "plan.md": "\n".join(["# 交付计划", "", "本文件由同一份 TaskPlan 任务图生成，只描述待执行工作，不执行命令或写入用户项目。", "", "## 业务目标", "", *context, "", "## 任务顺序", "", _bullet([f"{task['id']}：{task['title']}（依赖：{', '.join(task['depends_on']) or '无'}；产物：{', '.join(task['outputs']) or '无'}；验收：{'；'.join(task['acceptance']) or '无'}；回滚：{task['rollback']}；激活边界：{task['activation_gate']['reason']}）" for task in task_plan.get("tasks", [])] if task_plan else ["等待 TaskPlan 生成后注入任务图。"]), "", "## 波次", "", _bullet([f"第 {index + 1} 波：{', '.join(wave)}" for index, wave in enumerate(task_plan.get("waves", []))] if task_plan else []), "", "## 验收与回滚", "", _bullet(["每项任务必须绑定具体业务结果、源码或测试输出路径。", "失败时只保留可识别的部分结果，按 receipt 和 before/after 哈希恢复。", *recommendation["acceptance"]]), "", "## 待解决问题", "", _bullet(unresolved)]),
        "product.md": "\n".join(["# 产品定义", "", *context, "", "## 业务场景（来源可追溯）", "", _bullet(scenario_lines), "", "## 业务验收", "", _bullet(recommendation["acceptance"]), "", "## 来源边界", "", "- `user.answers.*` 是用户回答。", "- `agent.assumption:*` 只是系统工作假设，不得写回用户需求或作为已确认规则。"]),
        "architecture.md": "\n".join(["# 实现边界", "", *context, "", "## 技术方案", "", _bullet([f"{key}：{value['technology']}。{value['reason']}" for key, value in recommendation["stack"].items()]), "", "## 能力判断", "", _bullet([f"{item['id']}：{item['need']}。{item['reason']}（激活边界：{item['activation_gate']}）" for item in recommendation["capabilities"]])]),
        "ui.md": "\n".join(["# 界面与用户流程", "", f"围绕目标“{project['goal']}”服务于{project['audience'] or '待确认使用者'}。", "", "## 流程", "", _bullet([f"场景“{item['title']}”：{item['actor']}执行“{item['action']}”，得到“{item['result']}”。" for item in project["scenarios"]] or ["场景尚未确认，不编造用户流程。"]), "", "## 交互验收", "", _bullet(["流程能完成项目业务结果。", "未解决问题在任何写入或外部动作前可见。"])]),
        "security.md": "\n".join(["# 安全与证据边界", "", "本轮只生成离线计划，不连接外部系统。", "", "## 规则", "", _bullet(["不在文档、日志或任务参数中保存秘密。", "未知能力保持 unknown，不等同于授权。", "计划完成不等同于实现、连接、部署或发布完成。", "所有失败保留证据并按精确恢复条件继续。"])]),
    }
    if _need(capabilities, "database") != "not_needed":
        docs["data.md"] = "\n".join(["# 数据模型", "", f"为“{project['goal']}”定义最小数据结构。", "", f"- 数据库需求：{_need(capabilities, 'database')}", f"- 业务结果：{project['outcome'] or '待澄清'}", "- 本文不包含真实用户或儿童数据。"])
    if _need(capabilities, "api") != "not_needed":
        docs["interface.md"] = "\n".join(["# 接口契约", "", f"为业务场景定义类型化边界；当前 API 需求：{_need(capabilities, 'api')}。", "", _bullet([f"场景 {item['id']} 的“{item['action']}”需要可验证的请求与结果边界。" for item in project["scenarios"]] or ["场景未确认，不生成接口细节。"]), "", "外部连接仍未激活。"])
    if _need(capabilities, "auth") != "not_needed" or _need(capabilities, "admin") != "not_needed":
        docs["permissions.md"] = "\n".join(["# 权限边界", "", f"认证需求：{_need(capabilities, 'auth')}；后台需求：{_need(capabilities, 'admin')}。", "", "先按业务角色定义可执行动作，不因未回答问题授予权限。"])
    if _need(capabilities, "payment") != "not_needed":
        docs["payment.md"] = "\n".join(["# 支付边界", "", f"支付需求：{_need(capabilities, 'payment')}。", "", "这里只设计业务与风险边界，不连接支付服务；服务商、凭据、费用和撤销流程需要单独确认。"])
    if _need(capabilities, "notifications") != "not_needed":
        docs["notifications.md"] = "\n".join(["# 通知边界", "", f"通知需求：{_need(capabilities, 'notifications')}。", "", "这里只描述收件人、触发条件和退出方式，不联系通知服务。"])
    if any(_need(capabilities, item) != "not_needed" for item in ("server", "storage", "api", "payment", "notifications")):
        docs["deployment.md"] = "\n".join(["# 部署边界", "", "部署是后续事务，不由文档渲染执行。", "", "- 在目标、数据范围、凭据、费用和回滚条件确认前不得激活。", "- 本地规划结果不是部署或发布证据。"])
    return docs


__all__ = ["SCHEMA_VERSION", "render_documents", "validate_recommendation", "capability_map", "document_names"]
