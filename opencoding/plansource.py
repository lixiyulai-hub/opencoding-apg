# -*- coding: utf-8 -*-
"""计划来源构造（CP5 §4.2）：让**用户已确认的 AI 决策**成为正式计划输入。

该模块被 `advisor`（采用时构造）与 `service`（核对是否漂移时重算）共同引用，
保证同一份 AI 决策在派发前、提交前、恢复时得到的计划**完全可复现**；
规则判断仍然负责事实/硬约束与题干，不通过漂移或回退改写已确认的 AI 决策。

Recommendation 1.1 是封闭 schema，因此：
- 允许随 AI 决策变化：platforms.primary/requested/confidence/reason/unresolved、
  stack（由平台与能力需求重新派生）、assumptions（追加说明）；
- 由控制器保留：project、capabilities、revision、session_id、事实派生结论。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .decisions import _CONSTRAINT_EFFECTS, _stack  # 受控复用：约束清单与技术栈派生


def capability_needs(recommendation: Mapping[str, Any]) -> dict[str, str]:
    needs: dict[str, str] = {}
    for item in (recommendation.get("capabilities") or []):
        if not isinstance(item, Mapping):
            continue
        needs[str(item.get("id") or "")] = str(
            item.get("need") or item.get("status") or "unknown")
    return needs


def build_ai_view(ai_recommendation: Mapping[str, Any] | None) -> dict[str, Any]:
    """抽取 AI 的**解释性材料**（拒选项、未知项、复评触发条件、说明）。

    这些材料只冻结为采用记录的说明层，不进入 Recommendation 本体，
    避免模型输出结构变化影响正式计划的可复现性。
    """
    ai_rec = ai_recommendation if isinstance(ai_recommendation, Mapping) else {}
    view: dict[str, Any] = {}
    for field in ("rejected", "unknowns", "revisit_when", "clarifying_questions"):
        if ai_rec.get(field):
            view[field] = ai_rec[field]
    if isinstance(ai_rec.get("assumptions"), list):
        notes = [str(a)[:300] for a in ai_rec["assumptions"] if str(a).strip()][:10]
        if notes:
            view["assumptions"] = notes
    if isinstance(ai_rec.get("stack"), str) and ai_rec["stack"].strip():
        view["stack_note"] = str(ai_rec["stack"])[:300]
    return view


def plan_input_for_choice(rule_recommendation: Mapping[str, Any], choice: str,
                          ai_view: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """按已确认的 AI 首选平台重算正式计划输入（纯函数、可复现）。"""
    plan_input = json.loads(json.dumps(dict(rule_recommendation), ensure_ascii=False))
    view = dict(ai_view or {})
    choice_text = str(choice or "").strip()
    platforms = dict(plan_input.get("platforms") or {})
    rule_primary = platforms.get("primary")
    if choice_text:
        requested = [str(item) for item in (platforms.get("requested") or [])]
        platforms["requested"] = [choice_text] + [i for i in requested if i != choice_text]
        platforms["primary"] = choice_text
        rule_conf = str(platforms.get("confidence") or "medium")
        platforms["confidence"] = rule_conf if choice_text == rule_primary else "medium"
        platforms["reason"] = ("用户已确认 AI 评估给出的首选平台:" + choice_text
                               + ("(与规则草案一致)" if choice_text == rule_primary
                                  else "(规则草案原为 " + str(rule_primary) + ",以用户确认为准)"))
        platforms["unresolved"] = [str(u) for u in (platforms.get("unresolved") or [])
                                   if u != "platform:needs_user_confirmation"]
        plan_input["platforms"] = platforms
        plan_input["unresolved"] = [str(u) for u in (plan_input.get("unresolved") or [])
                                    if u != "platform:needs_user_confirmation"]
    needs = capability_needs(plan_input)
    stack_failed = False
    try:
        plan_input["stack"] = _stack(choice_text or str(rule_primary or "web"), {
            "server": needs.get("server", "unknown"),
            "database": needs.get("database", "unknown"),
            "api": needs.get("api", "unknown"),
        })
    except Exception:  # noqa: BLE001 - 派生失败必须可见，不让计划悄悄回到规则栈
        stack_failed = True
    assumptions = [str(a) for a in (plan_input.get("assumptions") or [])]
    assumptions.append("本次计划的首选平台来自用户已确认的 AI 评估("
                       + (choice_text or "未选择") + "),技术栈随之重新派生。")
    if stack_failed:
        assumptions.append("AI 首选平台的技术栈派生失败,正式计划仍沿用规则栈,须重新评估确认。")
    if isinstance(view.get("stack_note"), str) and view["stack_note"].strip():
        assumptions.append("AI 技术栈说明:" + str(view["stack_note"])[:300])
    for text in (view.get("assumptions") or []):
        if str(text).strip():
            assumptions.append("AI 评估说明:" + str(text)[:300])
    plan_input["assumptions"] = list(dict.fromkeys(assumptions))
    return plan_input


def hard_constraint_conflict(constraints: list[Mapping[str, Any]], choice: str) -> tuple[str, str] | None:
    """已登记硬约束是否禁止该 AI 首选；返回 (fact_kind, effect) 或 None。"""
    for item in constraints or []:
        kind = str(item.get("kind") or "")
        if kind not in _CONSTRAINT_EFFECTS:
            continue
        if kind == "no_standalone_cli" and str(choice).lower() == "cli":
            return kind, _CONSTRAINT_EFFECTS[kind]
        if kind == "web_only_incremental" and str(choice).lower() != "web":
            return kind, _CONSTRAINT_EFFECTS[kind]
    return None


__all__ = ["build_ai_view", "capability_needs", "hard_constraint_conflict",
           "plan_input_for_choice"]
