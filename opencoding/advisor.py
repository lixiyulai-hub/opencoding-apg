# -*- coding: utf-8 -*-
"""真实 AI 评估与重评:把获准模型的结构化判断接入决策-计划-执行链。

设计要点:
- 输入:会话(12 题中文答案)、事实簿、规则推荐与硬约束、真实能力视图、上次评估(重评时)。
- 输出:AIAdapter/WorkBuddyGatewayAdapter 的 evaluate 结构化响应(含扩展 recommendation 字段),
  经既有绑定/nonce/敏感校验后落盘 `.opencoding/evaluations/<id>.json`。
- AI 判断不自动生效:必须由用户确认;确认后若平台选择变化,通过 service.submit_answer
  写入会话(带 revision 乐观并发),从而真实改变任务计划与执行绑定。
- 密钥/网关凭据只存在于适配器请求头,本模块不记录、不回显。
"""

from __future__ import annotations

import hashlib
import json
import platform as _platform
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import plansource, service
from .aiadapter import AIRequestError
from .service import ServiceError
from .decisions import (  # 私有符号:硬约束守门与技术栈随平台重算
    _CONSTRAINT_EFFECTS, _stack, build_recommendation, derive_fact_constraints)
from .facts import list_facts
from .safety import sanitize_text, sha256_bytes

EVALUATIONS_SCHEMA_VERSION = 1
EVALUATION_STATUSES = ("pending_confirmation", "confirmed", "superseded", "rejected")

_EVAL_DIR_NAME = "evaluations"

_KNOWN_PLATFORMS = ("windows", "mac", "iphone", "android", "web", "cli", "miniprogram")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _eval_dir(root: Path) -> Path:
    return Path(root) / ".opencoding" / _EVAL_DIR_NAME


def _atomic_write(path: Path, doc: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def capability_view() -> dict[str, Any]:
    """真实能力视图:只记录实测过的本机事实,不做能力自报。"""
    view: dict[str, Any] = {
        "os": _platform.platform(),
        "python": _platform.python_version(),
        "toolchains": {},
    }
    for tool in ("node", "npm", "git", "rustc", "cargo"):
        view["toolchains"][tool] = shutil.which(tool) is not None
    try:
        import sys

        view["python_executable_configured"] = bool(sys.executable)
    except Exception:  # pragma: no cover - sys 恒可用
        view["python_executable_configured"] = False
    return view


def _facts_summary(facts: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    out = []
    for fact in facts:
        out.append({
            "kind": str(fact.get("kind", "")),
            "text": sanitize_text(str(fact.get("text", "")))[:300],
            "source": str(fact.get("source_type", "")),
            "confirmed": bool(fact.get("confirmed")),
        })
    return out


def _answers_summary(session: Mapping[str, Any]) -> dict[str, str]:
    answers = session.get("answers") or {}
    return {str(k): sanitize_text(str(v))[:200] for k, v in sorted(answers.items())}


def build_evaluation_messages(
    session: Mapping[str, Any],
    facts: list[Mapping[str, Any]],
    recommendation: Mapping[str, Any],
    constraints: list[Mapping[str, Any]],
    capability: Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """构造评估请求消息(全部来自获准项目材料,不含任何密钥)。"""
    system = (
        "你是 OpenCoding 的项目方案评估助手。你会收到:用户的中文项目目标与逐题答案、"
        "项目事实簿、系统规则给出的初步推荐与硬约束、本机真实能力视图、以及上次评估(重评时)。"
        "你的任务:基于这些事实比较可行路线,给出首选方案、关键理由、确有价值的备选及不选原因、"
        "假设、未知与改判条件。禁止凭空编造用户没说的事实;禁止推荐与硬约束冲突的方案;"
        "本机能力只说明开发/测试环境,不限制你建议的目标平台。只输出一个 JSON 对象。"
    )
    spec = {
        "输出字段": {
            "summary": "非空字符串,给小白看的两三句中文结论",
            "choice": "首选目标平台,取值 windows/mac/iphone/android/web/cli/miniprogram 之一",
            "reasons": "非空字符串列表,关键理由(中文)",
            "implementation_contract": {
                "runtime": "python-stdlib",
                "entry_module": "app.main",
                "files": [{"path": "app/main.py", "purpose": "用途"}],
                "functions": {"函数名": "签名与行为说明"},
                "data_dir": "app/data",
                "steps": [
                    {"op": "call", "function": "函数名", "args": ["参数"], "save_as": "变量", "phase": "main"},
                    {"op": "assert", "saved": "变量", "contains": [{"键": "值"}], "phase": "main"},
                    {"op": "call", "function": "函数名", "args": [], "expect_exception": True, "phase": "main"},
                    {"op": "call", "function": "list", "args": [], "save_as": "after_restart", "phase": "restart"},
                    {"op": "assert", "saved": "after_restart", "contains": [{"键": "值"}], "phase": "restart"},
                    {"op": "assert_file_exists", "path": "data/xx.json", "phase": "restart"}
                ],
                "steps_phase_说明": "main 阶段与 restart 阶段在两个独立进程中执行;restart 阶段必须包含"
                                    "重新调用函数读取状态的步骤和断言,用于验证数据真实落盘(缺 restart 阶段会被拒绝)",
            },
            "recommendation": {
                "stack": "首选技术栈一句话",
                "rejected": [{"option": "备选项", "reason": "不选的原因"}],
                "assumptions": ["你做的假设"],
                "unknowns": ["缺少的关键事实"],
                "revisit_when": ["什么情况出现时应重新评估"],
                "clarifying_questions": ["还需要问用户的关键问题(没有则空)"],
                "server_needed": "required/optional/not_needed",
                "database_needed": "required/optional/not_needed",
                "auth_needed": "required/optional/not_needed",
                "payment_needed": "required/optional/not_needed",
            },
        }
    }
    payload = {
        "goal": sanitize_text(str(session.get("goal", "")))[:500],
        "contract_spec": "implementation_contract 必须给出:本项目(不是模板)应交付的文件、"
                         "公开函数契约(签名+行为)、数据落盘目录(app/ 之下),以及 steps 数据驱动"
                         "验收步骤(算子仅限 call/assert/reload/assert_file_exists/assert_absent;"
                         "必须覆盖核心业务正反路径与重启保存)。所有 files.path 都必须位于 app/ 目录下；"
                         "测试步骤写在 implementation_contract.steps 中，不要把 tests/、README.md 或其他项目根文件"
                         "放入 files；生成阶段只接受 app/ 下的候选文件。main 阶段的写入调用必须返回可识别的"
                         "业务记录标识（例如字符串 name/text/id 组合中的业务文本），restart 阶段的 assert"
                         "必须明确包含同一业务记录标识；不能只断言数量、版本号或布尔值。",
        "answers": _answers_summary(session),
        "facts": _facts_summary(facts),
        "rule_recommendation": {
            "platform": (recommendation.get("platforms") or {}).get("primary"),
            "confidence": (recommendation.get("platforms") or {}).get("confidence"),
            "stack": recommendation.get("stack"),
            "capabilities": recommendation.get("capabilities"),
            "unresolved": (recommendation.get("unresolved") or [])[:20],
        },
        "hard_constraints": [
            {"kind": c.get("kind"), "applied": c.get("applied"), "explanation": sanitize_text(str(c.get("explanation", "")))[:200]}
            for c in constraints
        ],
        "capability_view": capability,
    }
    if previous:
        payload["previous_evaluation"] = {
            "choice": previous.get("choice"),
            "recommendation": previous.get("recommendation"),
            "summary": sanitize_text(str(previous.get("summary", "")))[:300],
        }
        payload["说明"] = "这是重新评估:对比上次评估,说明哪些判断因新事实/新约束改变,哪些保持不变。"
    user = json.dumps({"evaluation_input": payload, "response_spec": spec}, ensure_ascii=False, indent=1)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "项目评估输入:\n" + user},
    ]


def _validate_contract(value: Any) -> dict[str, Any]:
    """宽松结构校验:implementation_contract 原样保留(生成侧会再次严格校验)。

    R08:runtime 字段必须透传保存——不得在 advisor 层丢失后让 generic 默认 Python。
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    if isinstance(value.get("runtime"), str) and value["runtime"].strip():
        out["runtime"] = value["runtime"][:60]
    if isinstance(value.get("entry_module"), str):
        out["entry_module"] = value["entry_module"][:120]
    if isinstance(value.get("files"), list):
        out["files"] = [{"path": str(f.get("path", ""))[:200], "purpose": sanitize_text(str(f.get("purpose", "")))[:200]}
                        for f in value["files"] if isinstance(f, dict) and f.get("path")][:10]
    if isinstance(value.get("functions"), dict):
        out["functions"] = {str(k)[:80]: sanitize_text(str(v))[:300]
                            for k, v in list(value["functions"].items())[:12]}
    if isinstance(value.get("data_dir"), str):
        out["data_dir"] = value["data_dir"][:120]
    if isinstance(value.get("steps"), list):
        out["steps"] = value["steps"][:40]
    return out


def _validate_recommendation(value: Any) -> dict[str, Any]:
    """扩展字段宽松校验:结构不符时降级为空,不让畸形推荐进入下游。"""
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    if isinstance(value.get("stack"), str):
        out["stack"] = value["stack"][:300]
    for list_field in ("rejected", "assumptions", "unknowns", "revisit_when", "clarifying_questions"):
        items = value.get(list_field)
        if isinstance(items, list):
            if list_field == "rejected":
                out[list_field] = [
                    {"option": sanitize_text(str(i.get("option", "")))[:120],
                     "reason": sanitize_text(str(i.get("reason", "")))[:400]}
                    for i in items if isinstance(i, dict)
                ][:10]
            else:
                out[list_field] = [sanitize_text(str(i))[:300] for i in items if isinstance(i, str)][:15]
    for flag in ("server_needed", "database_needed", "auth_needed", "payment_needed"):
        if value.get(flag) in ("required", "optional", "not_needed"):
            out[flag] = value[flag]
    return out


def run_ai_evaluation(
    root: str | Path,
    session_id: str,
    adapter: Any,
    *,
    run_id: str = "advisor",
) -> dict[str, Any]:
    """发起一次真实 AI 评估并落盘。返回评估记录(脱敏,不含凭据)。"""
    root_path = Path(root)
    view = service.session_view(root_path, session_id)
    session = view["session"] if "session" in view else view
    facts = list_facts(root_path, session_id)
    constraints = derive_fact_constraints(facts)
    recommendation = build_recommendation(session, constraints)
    capability = capability_view()
    previous = latest_evaluation(root_path, session_id, status="confirmed")
    messages = build_evaluation_messages(
        session, facts, recommendation, constraints, capability,
        previous.get("structured") if previous else None,
    )
    task_id = "ai-evaluation"
    result = adapter.complete(
        messages,
        request_kind="evaluate",
        run_id=run_id,
        task_id=task_id,
        attempt=1,
        allowed_outputs=[],
    )
    structured = result.get("structured") or {}
    if structured.get("request_kind") != "evaluate":
        raise AIRequestError("response_binding_invalid", "评估响应类别不正确")
    choice = str(structured.get("choice", "")).strip().lower()
    if choice not in _KNOWN_PLATFORMS:
        raise AIRequestError("response_schema_invalid", "AI 评估的 platform choice 不在已知集合内:" + sanitize_text(choice)[:40])
    advisor_module_sha = sha256_bytes(Path(__file__).read_bytes())
    record = {
        "schema_version": EVALUATIONS_SCHEMA_VERSION,
        "evaluation_id": "eval-" + hashlib.sha256((result["request_id"] + _now()).encode("utf-8")).hexdigest()[:16],
        "advisor_module_sha256": advisor_module_sha,
        "session_id": session_id,
        "created_at": _now(),
        "status": "pending_confirmation",
        "real": bool(result.get("real")),
        "provider": result.get("provider"),
        "model": result.get("model"),
        "request_id": result.get("request_id"),
        "input_sha256": result.get("input_sha256"),
        "response_sha256": result.get("response_sha256"),
        "latency_ms": result.get("latency_ms"),
        "session_revision_at_eval": session.get("revision"),
        "rule_platform_at_eval": (recommendation.get("platforms") or {}).get("primary"),
        "structured": {
            "summary": structured.get("summary"),
            "choice": choice,
            "reasons": structured.get("reasons"),
            "recommendation": _validate_recommendation(structured.get("recommendation")),
            "implementation_contract": _validate_contract(structured.get("implementation_contract")),
        },
        "confirmation": None,
    }
    _atomic_write(_eval_dir(root_path) / (record["evaluation_id"] + ".json"), record)
    # 旧的同会话待确认评估标记为被取代
    for old in _eval_dir(root_path).glob("eval-*.json"):
        if old.name == record["evaluation_id"] + ".json":
            continue
        doc = _load(old)
        if doc.get("session_id") == session_id and doc.get("status") == "pending_confirmation":
            doc["status"] = "superseded"
            doc["superseded_by"] = record["evaluation_id"]
            _atomic_write(old, doc)
    return _public(record)


def _hard_constraint_conflict(session_id_facts: list[Mapping[str, Any]], choice: str,
                              session: Mapping[str, Any]) -> tuple[str, str] | None:
    """硬约束是否禁止 AI 首选;返回 (fact_kind, effect) 或 None。

    规则在此处只做**硬约束守门**:已登记的授权/仓库事实若强制某种形态
    (如禁止独立命令行、必须在既有网站内增量修改),AI 首选与之冲突时不予采用,
    必须重新评估;不靠改用户答案或静默回退来"消化"冲突。
    实现统一收敛到 `plansource.hard_constraint_conflict`,采用时与漂移核对时同源。
    """
    return plansource.hard_constraint_conflict(derive_fact_constraints(session_id_facts), choice)


def _capability_needs(recommendation: Mapping[str, Any]) -> dict[str, str]:
    return plansource.capability_needs(recommendation)


def _ai_plan_recommendation(rule_recommendation: Mapping[str, Any],
                            structured: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """以**用户已确认的 AI 决策**为准构造正式计划输入,并返回 AI 说明层。

    统一委托 `plansource`:同一份 AI 决策在采用时、展示时、漂移核对时、
    恢复后重算时必须得到逐字节相同的 plan_input(见 CP5 §4.2)。
    """
    choice = str(structured.get("choice", "")).strip()
    ai_view = plansource.build_ai_view(
        structured.get("recommendation") if isinstance(structured.get("recommendation"), Mapping) else {})
    return plansource.plan_input_for_choice(rule_recommendation, choice, ai_view), ai_view


def adopt_confirmed_evaluation(root: str | Path, session_id: str) -> dict[str, Any]:
    """R07-A2 / CP5 §4.2:用户已确认的 AI 决策成为**正式计划本身**。

    实现要点:
    - recommendation/task_plan/plan_digest/executor_mapping 全部由确认后的 AI 决策
      派生（与规则判断走同一条生成链）,不再在规则计划上贴一个 ai_binding;
    - 规则判断保留为 ``rule_reference`` 用于约束核对与差异说明;
    - 硬约束冲突、计划结构校验失败一律明确拒绝,不静默回退到规则计划。
    """
    record = latest_evaluation(root, session_id, status="confirmed")
    if record is None:
        raise ValueError("当前会话没有已确认的 AI 评估;请先完成评估并确认后再采用计划")
    view = service.session_view(root, session_id)
    session = view["session"] if "session" in view else view
    if record.get("session_revision_at_eval") != session.get("revision"):
        raise ValueError("已确认评估基于旧会话版本(r%s,当前 r%s);请在当前版本下重新评估并确认"
                         % (record.get("session_revision_at_eval"), session.get("revision")))
    structured = record.get("structured") or {}
    contract = structured.get("implementation_contract") or {}
    if not contract.get("files") or not contract.get("steps"):
        raise ValueError("已确认评估缺少实现契约;请在最新版本下重新评估")
    facts = list_facts(Path(root), session_id)
    ai_choice = str(structured.get("choice") or "")
    conflict = _hard_constraint_conflict(facts, ai_choice, session)
    if conflict:
        raise ValueError("AI 首选与已登记硬约束冲突(" + conflict[0] + "):" + conflict[1]
                         + ";请重新评估或调整方案后再采用")
    # 规则版本:只作为约束来源与差异对照(不落为正式计划);必须显式关闭计划来源,
    # 否则再次采用时"规则对照"会被上一次 AI 计划顶替,差异证明会自我抹平。
    rule_evaluated = service.evaluate_session(root, session_id, use_plan_source=False)
    rule_rec = rule_evaluated.get("recommendation") or {}
    ai_rec, ai_view = _ai_plan_recommendation(rule_rec, structured)
    # 先按同一计划来源自算一次:得到 AI 派生计划摘要,用于写死采用记录前的对照证据
    ai_probe = service.evaluate_session(root, session_id, recommendation_override=ai_rec,
                                        use_plan_source=False)
    rule_choice = (rule_rec.get("platforms") or {}).get("primary")
    rule_plan_digest = service.plan_digest(rule_evaluated.get("task_plan"))
    ai_plan_digest = ai_probe.get("adopted_plan_digest")
    ai_binding = {
        "evaluation_id": record.get("evaluation_id"),
        "ai_choice": ai_choice or None,
        "rule_choice_at_adopt": rule_choice,
        "choice_discrepancy": bool(rule_choice and ai_choice
                                   and str(rule_choice).lower() != ai_choice.lower()),
        "contract_sha256": sha256_bytes(
            json.dumps(contract, ensure_ascii=False, sort_keys=True).encode("utf-8")),
        "adopted_at": _now(),
        "source": "confirmed_ai_evaluation",
        # CP5 §4.2:不只是"绑定标签",必须证明正式计划确实随 AI 首选变化
        "plan_source": "confirmed_ai_evaluation",
        "plan_follows_ai": True,
        "rule_plan_digest": rule_plan_digest,
        "ai_plan_digest": ai_plan_digest,
        "plan_changed_by_ai": bool(rule_plan_digest != ai_plan_digest),
    }
    if ai_view:
        ai_binding["ai_decision_view"] = ai_view
    extras = {
        "ai_binding": ai_binding,
        # CP5 §4.2:计划来源必须与正式计划同一次落盘,且携带重算所需的全部输入,
        # 供运行前/提交前/恢复后按同一来源重算核对(见 service._plan_override_from_adoption)
        "plan_source": {
            "kind": "confirmed_ai_evaluation",
            "evaluation_id": record.get("evaluation_id"),
            "ai_choice": ai_choice or None,
            "ai_view": ai_view,
            "note": "recommendation/task_plan/plan_digest 均由已确认 AI 决策派生",
        },
        "rule_reference": {
            "recommendation_platform": rule_choice,
            "plan_digest": rule_plan_digest,
            "role": "硬约束来源与差异对照;不是本次执行目标",
        },
    }
    try:
        adopted = service.adopt_evaluation_plan(root, session_id,
                                                recommendation_override=ai_rec, extras=extras)
    except ServiceError as exc:
        if str(exc.code) != "evaluation_plan_invalid":
            raise
        raise ValueError("AI 决策派生的正式计划未通过结构校验:" + sanitize_text(str(exc))[:200]
                         + ";请在当前会话重新评估并确认") from exc
    return adopted


def _public(record: Mapping[str, Any]) -> dict[str, Any]:
    return dict(record)


def list_evaluations(root: str | Path, session_id: str | None = None) -> list[dict[str, Any]]:
    out = []
    for path in sorted(_eval_dir(Path(root)).glob("eval-*.json")):
        try:
            doc = _load(path)
        except (OSError, ValueError):
            continue
        if session_id is None or doc.get("session_id") == session_id:
            out.append(doc)
    out.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return out


def latest_evaluation(root: str | Path, session_id: str, *, status: str | None = None) -> dict[str, Any] | None:
    for doc in list_evaluations(root, session_id):
        if status is None or doc.get("status") == status:
            return doc
    return None


def get_evaluation(root: str | Path, evaluation_id: str) -> dict[str, Any] | None:
    path = _eval_dir(Path(root)) / (evaluation_id + ".json")
    if not re.fullmatch(r"eval-[0-9a-f]{16}", evaluation_id) or not path.is_file():
        return None
    return _load(path)


def confirm_evaluation(
    root: str | Path,
    session_id: str,
    evaluation_id: str,
    *,
    expected_revision: int,
    accepted: bool,
) -> dict[str, Any]:
    """用户确认/拒绝 AI 评估。确认且平台选择变化时,真实更新会话答案。"""
    record = get_evaluation(root, evaluation_id)
    if record is None:
        raise ValueError("评估记录不存在")
    if record.get("session_id") != session_id:
        raise ValueError("评估记录与会话不匹配")
    if record.get("status") != "pending_confirmation":
        raise ValueError("评估记录当前状态不可确认:" + str(record.get("status")))
    # 过期评估拒绝:评估时的会话修订与当前修订不一致 → 要求重新评估
    view = service.session_view(root, session_id)
    current_revision = (view["session"] if "session" in view else view).get("revision")
    if record.get("session_revision_at_eval") != current_revision:
        raise ValueError(
            "评估已过期:评估时会话版本 r%s,当前 r%d;请在当前版本下重新评估"
            % (record.get("session_revision_at_eval"), current_revision))
    record["confirmation"] = {
        "accepted": bool(accepted),
        "at": _now(),
        "session_revision_before": expected_revision,
    }
    applied_platform_change = None
    if accepted:
        record["status"] = "confirmed"
        # R17-C05:AI 首选不得覆盖用户原始回答。仅记录差异;
        # 若用户决定改选 AI 平台,由用户自己在问答流里重新回答 platform。
        choice = record["structured"]["choice"]
        view = service.session_view(root, session_id)
        session = view["session"] if "session" in view else view
        answers = session.get("answers") or {}
        user_platform = str(answers.get("platform", "")).strip()
        record["confirmation"]["user_platform_answer"] = user_platform
        if user_platform.lower() != choice:
            record["confirmation"]["platform_discrepancy"] = {
                "user_answer": user_platform,
                "ai_choice": choice,
                "note": "AI 首选与你当前回答不同;如需改选,请在问答中重新回答平台问题",
            }
    else:
        record["status"] = "rejected"
    _atomic_write(_eval_dir(Path(root)) / (evaluation_id + ".json"), record)
    return _public(record)


__all__ = [
    "EVALUATIONS_SCHEMA_VERSION",
    "adopt_confirmed_evaluation",
    "capability_view",
    "confirm_evaluation",
    "get_evaluation",
    "latest_evaluation",
    "list_evaluations",
    "run_ai_evaluation",
]
