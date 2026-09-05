"""离线业务任务图、严格结构校验和确定性波次。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from .documents import SCHEMA_VERSION, capability_map, document_names, render_documents, validate_recommendation

_ACTION_FIELDS = {
    "review_requirements": {"type", "document"},
    "render_document": {"type", "document"},
    "define_schema": {"type", "capability"},
    "define_interface": {"type", "capability"},
    "define_access": {"type", "capability"},
    "security_review": {"type"},
    "plan_delivery": {"type"},
    "integration_design": {"type", "capability"},
    "implement_feature": {"type", "scenario_id", "platform"},
    "verify_feature": {"type", "scenario_id", "platform"},
}
_CAPABILITIES = {"server", "database", "api", "auth", "payment", "notifications", "admin", "storage", "deployment"}
_PLATFORMS = {"windows", "macos", "ios", "android", "web", "mini_program", "cli"}
_TASK_KEYS = {"id", "title", "description", "depends_on", "inputs", "outputs", "action", "acceptance", "rollback", "retry", "activation_gate"}
_PLAN_KEYS = {"schema_version", "session_id", "revision", "tasks", "waves", "unresolved"}
_SAFE_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_RESERVED_DIRS = {".git", ".governance", ".opencoding"}
_INVALID_WINDOWS_CHARS = set('<>"|?*')
_ACTION_CAPABILITIES = {
    "define_schema": {"database"},
    "define_interface": {"api"},
    "define_access": {"auth", "admin"},
    "integration_design": {"payment", "notifications", "deployment"},
}


def _gate(reason: str, required: bool = False) -> dict[str, Any]:
    return {"required": required, "reason": reason}


def _task(task_id: str, title: str, description: str, depends_on: list[str], inputs: list[str], outputs: list[str], action: dict[str, Any], acceptance: list[str], activation_gate: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": task_id, "title": title, "description": description, "depends_on": depends_on, "inputs": inputs, "outputs": outputs, "action": action, "acceptance": acceptance, "rollback": "仅移除本任务生成的草案输出，保留此前证据和用户修改。", "retry": {"max_attempts": 2}, "activation_gate": activation_gate or _gate("仅离线规划，不激活外部服务。")}


def _feature_artifacts(platform: str, technology: str) -> tuple[str, str] | None:
    """Choose artifacts from the confirmed platform and client technology."""
    normalized = technology.casefold()
    if platform == "windows":
        if any(token in normalized for token in ("typescript", "javascript", "tauri")):
            return (".ts", ".test.ts")
        return None
    if platform in {"ios", "macos"}:
        if any(token in normalized for token in ("swift", "swiftui", "xcode")):
            return (".swift", ".swift")
        return None
    if platform == "android":
        if any(token in normalized for token in ("kotlin", "compose")):
            return (".kt", ".kt")
        return None
    if platform == "web":
        if any(token in normalized for token in ("typescript", "javascript", "react", "vue", "svelte", "vite")):
            return (".ts", ".test.ts")
        return None
    if platform == "mini_program":
        if any(token in normalized for token in ("wxml", "wechat", "mini program", "miniprogram")):
            return (".wxml", ".test.ts")
        return None
    if platform == "cli":
        if "python" in normalized:
            return (".py", ".py")
        if any(token in normalized for token in ("typescript", "javascript")):
            return (".ts", ".test.ts")
        return None
    return None


def _canonical_path(path: str) -> str:
    return unicodedata.normalize("NFKC", path).casefold()


def _path_error(path: Any) -> str | None:
    if not isinstance(path, str) or not path:
        return "not_string"
    normalized = unicodedata.normalize("NFKC", path)
    if "\\" in normalized or normalized.startswith("/") or normalized.startswith("//") or re.match(r"^[A-Za-z]:", normalized):
        return "absolute_or_unc"
    if ":" in normalized:
        return "alternate_data_stream"
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return "dot_or_empty_segment"
    for part in parts:
        if any(ord(char) < 32 or ord(char) == 127 for char in part) or any(char in _INVALID_WINDOWS_CHARS for char in part):
            return "windows_invalid_character"
        if part.endswith((".", " ")):
            return "trailing_space_or_dot"
        stem = part.rstrip(" .").split(".", 1)[0].casefold()
        if stem in _WINDOWS_RESERVED:
            return "windows_device_name"
        if part.casefold() in _RESERVED_DIRS:
            return "reserved_directory"
    return None


def _related(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _compute_waves(tasks: list[Mapping[str, Any]]) -> list[list[str]]:
    task_ids = [task["id"] for task in tasks]
    task_set = set(task_ids)
    dependencies = {task["id"]: set(task["depends_on"]) for task in tasks}
    missing = sorted({dep for deps in dependencies.values() for dep in deps - task_set})
    if missing:
        raise ValueError("missing dependency")
    if any(task_id in deps for task_id, deps in dependencies.items()):
        raise ValueError("cycle")
    remaining = set(task_ids)
    waves: list[list[str]] = []
    while remaining:
        ready = sorted(task_id for task_id in remaining if not dependencies[task_id] & remaining)
        if not ready:
            raise ValueError("cycle")
        waves.append(ready)
        remaining -= set(ready)
    return waves


def _depends_on(task_id: str, ancestor: str, dependencies: Mapping[str, set[str]]) -> bool:
    """Return whether ancestor is a transitive dependency of task_id."""
    pending = list(dependencies.get(task_id, set()))
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if current == ancestor:
            return True
        pending.extend(dependencies.get(current, set()))
    return False


def _validate_plan_shape(plan: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(plan, Mapping):
        return ["plan_not_mapping"]
    try:
        if set(plan) != _PLAN_KEYS:
            errors.append("invalid_plan_fields")
    except (TypeError, ValueError):
        return ["invalid_plan_fields"]
    if plan.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported_schema_version")
    if not isinstance(plan.get("session_id"), str) or not plan.get("session_id").strip():
        errors.append("invalid_session_id")
    if type(plan.get("revision")) is not int or plan.get("revision", -1) < 0:
        errors.append("invalid_revision")
    if not isinstance(plan.get("tasks"), list):
        errors.append("tasks_not_list")
    if not isinstance(plan.get("waves"), list):
        errors.append("waves_not_list")
    unresolved = plan.get("unresolved")
    if not isinstance(unresolved, list) or any(not isinstance(item, str) for item in unresolved):
        errors.append("invalid_unresolved")
    return errors


def _validate_action(action: Any, task_id: str, outputs: list[str], errors: list[str]) -> None:
    if not isinstance(action, Mapping):
        errors.append(f"invalid_action:{task_id}")
        return
    action_type = action.get("type")
    if not isinstance(action_type, str) or action_type not in _ACTION_FIELDS:
        errors.append(f"unknown_action:{task_id}")
        return
    if set(action) != _ACTION_FIELDS[action_type]:
        errors.append(f"invalid_action_fields:{task_id}")
    if action_type in {"review_requirements", "render_document"}:
        document = action.get("document")
        if not isinstance(document, str) or document not in outputs:
            errors.append(f"action_document_not_output:{task_id}")
    elif action_type in {"define_schema", "define_interface", "define_access", "integration_design"}:
        capability = action.get("capability")
        if not isinstance(capability, str) or capability not in _ACTION_CAPABILITIES[action_type]:
            errors.append(f"invalid_capability_for_action:{task_id}")
    elif action_type in {"implement_feature", "verify_feature"}:
        scenario_id = action.get("scenario_id")
        if not isinstance(scenario_id, str) or not _SAFE_ID.fullmatch(scenario_id):
            errors.append(f"invalid_scenario_id:{task_id}")
        if not isinstance(action.get("platform"), str) or action.get("platform") not in _PLATFORMS:
            errors.append(f"invalid_platform:{task_id}")


def validate_task_plan(plan: Any) -> dict[str, Any]:
    """返回结构化结果；所有畸形输入都返回 invalid，不把异常泄漏给调用方。"""
    errors = _validate_plan_shape(plan)
    tasks = plan.get("tasks", []) if isinstance(plan, Mapping) else []
    if not isinstance(tasks, list):
        tasks = []
    ids: list[str] = []
    prepared: list[dict[str, Any]] = []
    output_owners: dict[str, list[str]] = {}
    output_paths: list[tuple[str, str]] = []
    for index, task in enumerate(tasks):
        if not isinstance(task, Mapping):
            errors.append(f"invalid_task:{index}")
            continue
        try:
            fields = set(task)
        except (TypeError, ValueError):
            errors.append(f"invalid_task:{index}")
            continue
        if fields != _TASK_KEYS:
            errors.append(f"invalid_task_fields:{index}")
            continue
        task_id = task.get("id")
        if not isinstance(task_id, str) or not _SAFE_ID.fullmatch(task_id):
            errors.append(f"invalid_task_id:{index}")
            continue
        ids.append(task_id)
        if not isinstance(task.get("title"), str) or not task["title"].strip():
            errors.append(f"invalid_title:{task_id}")
        if not isinstance(task.get("description"), str) or not task["description"].strip():
            errors.append(f"invalid_description:{task_id}")
        for field in ("depends_on", "inputs", "outputs", "acceptance"):
            value = task.get(field)
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                errors.append(f"invalid_{field}:{task_id}")
        acceptance = task.get("acceptance")
        if isinstance(acceptance, list) and all(isinstance(item, str) for item in acceptance):
            if not acceptance or any(not item.strip() for item in acceptance):
                errors.append(f"empty_acceptance:{task_id}")
        depends = task.get("depends_on") if isinstance(task.get("depends_on"), list) and all(isinstance(item, str) for item in task.get("depends_on")) else []
        inputs = task.get("inputs") if isinstance(task.get("inputs"), list) and all(isinstance(item, str) for item in task.get("inputs")) else []
        outputs = task.get("outputs") if isinstance(task.get("outputs"), list) and all(isinstance(item, str) for item in task.get("outputs")) else []
        if len(set(depends)) != len(depends):
            errors.append(f"duplicate_dependencies:{task_id}")
        if isinstance(task.get("outputs"), list) and all(isinstance(item, str) for item in task.get("outputs")):
            seen_local: set[str] = set()
            for path in outputs:
                reason = _path_error(path)
                if reason:
                    errors.append(f"invalid_output_path:{task_id}:{reason}")
                    continue
                canonical = _canonical_path(path)
                if canonical in seen_local:
                    errors.append(f"duplicate_outputs:{task_id}")
                seen_local.add(canonical)
                output_owners.setdefault(canonical, []).append(task_id)
                output_paths.append((canonical, task_id))
            for left_index, left_path in enumerate(sorted(seen_local)):
                for right_path in sorted(seen_local)[left_index + 1:]:
                    if _related(left_path, right_path):
                        errors.append(f"parent_child_output_conflict:{task_id}")
        for field, values in (("inputs", inputs), ("outputs", outputs)):
            for path in values:
                reason = _path_error(path)
                if reason:
                    errors.append(f"invalid_{field}_path:{task_id}:{reason}")
        _validate_action(task.get("action"), task_id, outputs, errors)
        retry = task.get("retry")
        if not isinstance(retry, Mapping) or set(retry) != {"max_attempts"} or type(retry.get("max_attempts")) is not int or retry["max_attempts"] < 1:
            errors.append(f"invalid_retry:{task_id}")
        if not isinstance(task.get("rollback"), str) or not task["rollback"].strip():
            errors.append(f"invalid_rollback:{task_id}")
        gate = task.get("activation_gate")
        if not isinstance(gate, Mapping) or set(gate) != {"required", "reason"} or type(gate.get("required")) is not bool or not isinstance(gate.get("reason"), str) or not gate["reason"].strip():
            errors.append(f"invalid_activation_gate:{task_id}")
        prepared.append({"id": task_id, "depends_on": depends, "inputs": inputs, "outputs": outputs})
    if len(set(ids)) != len(ids):
        errors.append("duplicate_task_id")
    computed_waves: list[list[str]] = []
    if prepared and len(ids) == len(prepared) and len(set(ids)) == len(ids) and not any(error.startswith(("invalid_depends_on", "invalid_task_id", "invalid_task_fields")) for error in errors):
        try:
            computed_waves = _compute_waves(prepared)
        except ValueError as error:
            errors.append(str(error))
    elif not prepared and tasks:
        computed_waves = []
    supplied_waves = plan.get("waves") if isinstance(plan, Mapping) else None
    if isinstance(supplied_waves, list):
        supplied_ids: list[str] = []
        for wave_index, wave in enumerate(supplied_waves):
            if not isinstance(wave, list) or any(not isinstance(item, str) for item in wave):
                errors.append(f"invalid_wave:{wave_index}")
                continue
            if len(set(wave)) != len(wave):
                errors.append(f"duplicate_wave_task:{wave_index}")
            supplied_ids.extend(wave)
        if len(set(supplied_ids)) != len(supplied_ids):
            errors.append("duplicate_wave_task")
        if set(supplied_ids) != set(ids):
            errors.append("waves_do_not_cover_tasks")
        if supplied_waves != computed_waves:
            errors.append("waves_not_deterministic")
    by_id = {task["id"]: task for task in prepared}
    dependencies = {task["id"]: set(task["depends_on"]) for task in prepared}
    for path, owners in output_owners.items():
        for index, left_id in enumerate(owners):
            for right_id in owners[index + 1:]:
                if not (_depends_on(left_id, right_id, dependencies) or _depends_on(right_id, left_id, dependencies)):
                    errors.append(f"duplicate_output_path:{right_id}:{path}")
    for index, (left_path, left_id) in enumerate(output_paths):
        for right_path, right_id in output_paths[index + 1:]:
            if left_id == right_id or not _related(left_path, right_path) or left_path == right_path:
                continue
            errors.append(f"parent_child_output_conflict:{left_id}:{right_id}")
    for wave_index, wave in enumerate(computed_waves):
        for left_index, left_id in enumerate(wave):
            left = by_id[left_id]
            left_outputs = [_canonical_path(path) for path in left["outputs"] if not _path_error(path)]
            left_inputs = [_canonical_path(path) for path in left["inputs"] if not _path_error(path)]
            for right_id in wave[left_index + 1:]:
                right = by_id[right_id]
                right_outputs = [_canonical_path(path) for path in right["outputs"] if not _path_error(path)]
                right_inputs = [_canonical_path(path) for path in right["inputs"] if not _path_error(path)]
                if any(_related(a, b) for a in left_outputs for b in right_outputs):
                    errors.append(f"same_wave_write_conflict:{wave_index}:{left_id}:{right_id}")
                if any(_related(a, b) for a in left_outputs for b in right_inputs) or any(_related(a, b) for a in right_outputs for b in left_inputs):
                    errors.append(f"same_wave_read_write_conflict:{wave_index}:{left_id}:{right_id}")
    return {"valid": not errors, "status": "valid" if not errors else "invalid", "errors": sorted(set(errors)), "waves": computed_waves}


def build_task_plan(recommendation: dict) -> dict:
    """从 Recommendation 1.1 构造业务任务图，不执行任何任务。"""
    validate_recommendation(recommendation)
    capabilities = capability_map(recommendation)
    available_docs = document_names(recommendation)
    tasks: list[dict[str, Any]] = []
    tasks.append(_task("requirements-confirmed", "确认业务目标与范围", "核对目标、使用者、业务结果、场景和未解决问题。", [], ["memory.md"], ["PRG.md"], {"type": "review_requirements", "document": "PRG.md"}, ["业务目标和验收结果已列出。", "未解决问题仍可见。"]))
    tasks.append(_task("product-and-ui", "描述用户业务流程", "把真实场景转成产品边界和用户流程，不把 OpenCoding 问答当成用户业务。", ["requirements-confirmed"], ["PRG.md"], ["product.md", "ui.md"], {"type": "render_document", "document": "product.md"}, ["产品和界面都引用真实业务场景。", "未声称实现已完成。"]))
    tasks.append(_task("architecture", "确定实现边界", "根据业务结果记录技术方案、能力需求和维护理由。", ["product-and-ui"], ["product.md", "ui.md"], ["architecture.md"], {"type": "render_document", "document": "architecture.md"}, ["四项 stack 均有理由和替代方案。", "unknown 能力没有被当作授权。"]))
    if capabilities["database"]["need"] != "not_needed":
        tasks.append(_task("data-model", "定义业务数据", "为已确认的场景设计最小数据模型。", ["architecture"], ["architecture.md"], ["data.md"], {"type": "define_schema", "capability": "database"}, ["数据字段服务于业务结果。", "不包含真实数据。"]))
    if capabilities["api"]["need"] != "not_needed":
        tasks.append(_task("api-contract", "定义业务接口", "绑定场景动作、请求和结果的类型化边界。", ["architecture"], ["architecture.md"], ["interface.md"], {"type": "define_interface", "capability": "api"}, ["接口对应业务动作。", "没有连接外部服务。"]))
    if capabilities["auth"]["need"] != "not_needed" or capabilities["admin"]["need"] != "not_needed":
        access_capability = "auth" if capabilities["auth"]["need"] != "not_needed" else "admin"
        tasks.append(_task("permissions", "定义角色权限", "定义谁可以执行已确认的业务动作。", ["product-and-ui"], ["product.md", "ui.md"], ["permissions.md"], {"type": "define_access", "capability": access_capability}, ["角色边界清楚。", "未回答的权限不被授予。"]))
    security_deps = ["architecture"] + (["permissions"] if any(task["id"] == "permissions" for task in tasks) else [])
    tasks.append(_task("security-review", "检查安全与证据边界", "检查秘密、外部影响和未解决事项是否仍受控。", security_deps, ["architecture.md"], ["security.md"], {"type": "security_review"}, ["无秘密进入生成内容。", "外部激活仍有确认边界。"]))
    for capability_id, task_id, title, output in (("payment", "payment-boundary", "定义支付边界", "payment.md"), ("notifications", "notification-boundary", "定义通知边界", "notifications.md")):
        if capabilities[capability_id]["need"] != "not_needed":
            tasks.append(_task(task_id, title, f"描述 {capability_id} 对业务结果的影响，不连接服务商。", ["architecture", "security-review"], ["architecture.md", "security.md"], [output], {"type": "integration_design", "capability": capability_id}, ["能力被标记为计划而非已接通。", "风险和费用边界清楚。"], _gate("当前仅设计产物；未来激活外部服务时另行确认。")))
    if "deployment.md" in available_docs:
        tasks.append(_task("deployment-boundary", "定义交付边界", "记录部署条件但不执行部署。", ["architecture", "security-review"], ["architecture.md", "security.md"], ["deployment.md"], {"type": "integration_design", "capability": "deployment"}, ["目标、回滚和数据范围已列出。", "没有执行部署。"], _gate("当前仅设计产物；部署激活时另行确认。")))
    design_dependencies = [task["id"] for task in tasks]
    scenario_platform = recommendation["platforms"]["primary"] or (recommendation["platforms"]["requested"][0] if recommendation["platforms"]["requested"] else None)
    plan_unresolved = list(recommendation["unresolved"]) + list(recommendation["platforms"]["unresolved"])
    if scenario_platform and recommendation["status"] == "ready" and not plan_unresolved:
        client_technology = recommendation["stack"]["client"]["technology"]
        artifact_kind = _feature_artifacts(scenario_platform, client_technology)
        if artifact_kind is None:
            plan_unresolved.append(f"平台 {scenario_platform} 与客户端技术 {client_technology} 的实现组合待确认")
        for scenario in recommendation["project"]["scenarios"]:
            if artifact_kind is None:
                break
            scenario_id = scenario["id"]
            source_suffix, test_suffix = artifact_kind
            source_path = f"src/features/{scenario_id}{source_suffix}"
            test_path = f"tests/features/test_{scenario_id}{test_suffix}"
            implement_id = f"implement-{scenario_id}"
            verify_id = f"verify-{scenario_id}"
            tasks.append(_task(implement_id, f"实现业务场景：{scenario['title']}", f"待执行：在 {scenario_platform} 上实现“{scenario['action']}”，使结果达到“{scenario['result']}”。", design_dependencies, ["product.md", "architecture.md"], [source_path], {"type": "implement_feature", "scenario_id": scenario_id, "platform": scenario_platform}, [f"待执行实现应支持业务结果：{scenario['result']}。", "本任务只是计划，不代表源码已经存在。"]))
            tasks.append(_task(verify_id, f"验证业务场景：{scenario['title']}", f"待执行：验证“{scenario['action']}”完成后能得到“{scenario['result']}”。", [implement_id], [source_path], [test_path], {"type": "verify_feature", "scenario_id": scenario_id, "platform": scenario_platform}, [f"待执行测试应验证业务结果：{scenario['result']}。", "本任务只是计划，不代表测试已通过。"]))
    tasks.append(_task("delivery-plan", "汇总可逆交付计划", "从已生成的任务图汇总规则、顺序、验收和回滚。", [task["id"] for task in tasks], sorted(available_docs), ["AGENTS.md", "plan.md"], {"type": "plan_delivery"}, ["核心文档职责清楚。", "plan.md 与任务图波次一致。", "未解决问题和激活门槛可见。"]))
    waves = _compute_waves(tasks)
    plan = {"schema_version": SCHEMA_VERSION, "session_id": recommendation["session_id"], "revision": recommendation["revision"], "tasks": tasks, "waves": waves, "unresolved": plan_unresolved}
    result = validate_task_plan(plan)
    if not result["valid"]:
        raise ValueError("generated task plan is invalid: " + ", ".join(result["errors"]))
    return plan


def task_waves(plan: dict) -> list[list[str]]:
    result = validate_task_plan(plan)
    if not result["valid"]:
        raise ValueError("invalid task plan: " + ", ".join(result["errors"]))
    return [list(wave) for wave in result["waves"]]


__all__ = ["build_task_plan", "validate_task_plan", "task_waves"]
