"""Offline, Chinese-first product intake with explicit unresolved states."""

from __future__ import annotations

from copy import deepcopy
import re
import uuid
from typing import Any


SCHEMA_VERSION = "1.1"

QUESTION_DEFINITIONS = (
    {"id": "audience", "question": "主要给谁使用？", "why": "使用者决定权限、界面语言和验收场景。", "required": True},
    {"id": "outcome", "question": "用户完成什么具体事情，才算这个项目有用？", "why": "把愿望变成可检查的业务结果。", "required": True},
    {"id": "platform", "question": "希望在哪里使用？可以说手机、Mac、网页、小程序或命令行。", "why": "平台决定交互方式和实测边界，不要求你选择技术。", "required": True},
    {"id": "data_persistence", "question": "是否要保存输入和结果，之后还能查看？", "why": "只判断是否保存数据，不把本地保存误当成云服务。", "required": True},
    {"id": "cross_device", "question": "是否要在不同设备之间同步？", "why": "区分本机保存和跨设备服务。", "required": True},
    {"id": "file_storage", "question": "是否要上传、保存照片或其他附件？", "why": "附件存储和数据库是两件事。", "required": True},
    {"id": "external_data", "question": "是否要从别的服务获取内容，比如地图、天气或智能问答？", "why": "第三方内容需要单独设计接口和风险边界。", "required": True},
    {"id": "admin_access", "question": "是否需要专人管理内容、成员或处理订单？", "why": "后台管理不等同于普通多人使用。", "required": True},
    {"id": "account_access", "question": "是否需要登录，或区分不同用户和角色？", "why": "登录与角色会增加身份、权限和隐私边界。", "required": True},
    {"id": "notifications", "question": "是否需要消息提醒或定时通知？", "why": "通知涉及发送渠道和未来服务激活。", "required": True},
    {"id": "payments", "question": "是否涉及支付、收费、订单或退款？", "why": "支付是高风险外部动作，必须单独确认。", "required": True},
    {"id": "multi_user", "question": "是否需要多人一起使用或协作？", "why": "多人协作影响角色、数据同步和任务拆分。", "required": True},
)

_QUESTION_MAP = {item["id"]: item for item in QUESTION_DEFINITIONS}
_PLATFORM_ALIASES = {
    "windows": ("windows", "win", "微软电脑"),
    "macos": ("macos", "mac os", "mac", "苹果电脑", "mac桌面"),
    "ios": ("ios", "iphone", "ipad", "苹果手机", "苹果平板"),
    "android": ("android", "安卓", "安卓手机"),
    "web": ("web", "网页", "网站", "浏览器"),
    "mini_program": ("小程序", "微信小程序", "mini program", "mini_program"),
    "cli": ("cli", "命令行", "终端"),
}
SUPPORTED_PLATFORMS = tuple(_PLATFORM_ALIASES)
_UNKNOWN_MARKERS = ("暂时不知道", "无法判断", "不确定", "不清楚", "没想好", "不知道", "unsure", "unknown")
_NO_MARKERS = ("不需要", "不支持", "不是", "并不是", "并非", "不可以", "不能", "不希望", "不愿意", "不想要", "不想", "不要", "不用", "无需", "不涉及", "没有", "否", "no", "false", "拒绝")
_YES_MARKERS = ("需要", "想要", "要", "是", "有", "可以", "支持", "希望", "yes", "true")
_NEGATION_RE = re.compile(r"(?:不需要|不支持|不是|并不是|并非|不可以|不能|不希望|不愿意|不想要|不想|不要|不用|无需|不涉及|没有|否|拒绝|no|false)")
_UNCONSUMED_NEGATION_RE = re.compile(r"(?<!不)不[\u4e00-\u9fff]{1,8}")

_SECRET_PATTERNS = (
    re.compile(r"(?is)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|password|passwd|secret)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|\S+)"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{10,}|AKIA[A-Z0-9]{12,})\b"),
)


def sanitize_text(text: str) -> str:
    """Remove common secret-shaped text before it can enter a session or recommendation."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    sanitized = text
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


def _normalise(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _contains_marker(normalised: str, markers: tuple[str, ...]) -> bool:
    return any(marker in normalised for marker in markers)


def _marker_present_without_negation(normalised: str) -> bool:
    """Detect an affirmative phrase after removing complete negative phrases."""

    remainder = normalised
    for marker in sorted(_NO_MARKERS, key=len, reverse=True):
        remainder = remainder.replace(marker, " ")
    return _contains_marker(remainder, _YES_MARKERS)


def _platforms_in(value: str) -> tuple[list[str], list[str]]:
    normalised = _normalise(value)
    requested: list[str] = []
    excluded: list[str] = []
    negative_pattern = re.compile(r"(?:不要|不选|排除|不用|不想用|无需|不支持|no)\s*(?:使用\s*)?")
    positive_pattern = re.compile(r"(?:只用|只要|仅用|仅限|首选|优先选择)\s*")
    for platform, aliases in _PLATFORM_ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            start = normalised.find(alias)
            if start < 0:
                continue
            boundaries = [normalised.rfind(mark, 0, start) for mark in ("，", ",", "。", "；", ";", "、", "\n")]
            boundary = max(boundaries, default=-1)
            clause = normalised[boundary + 1:]
            relative = start - boundary - 1
            context = clause[max(0, relative - 16):relative]
            suffix = clause[relative + len(alias):relative + len(alias) + 10]
            negatives = list(negative_pattern.finditer(clause[:relative]))
            positives = list(positive_pattern.finditer(clause[:relative]))
            latest_negative = negatives[-1].start() if negatives else -1
            latest_positive = positives[-1].start() if positives else -1
            excluded_before = latest_negative > latest_positive and latest_negative >= 0
            excluded_after = re.match(r"^\s*(?:不要|不选|排除|不用)", suffix) is not None
            excluded_here = excluded_before or excluded_after
            if excluded_here:
                excluded.append(platform)
            else:
                requested.append(platform)
            break
    return list(dict.fromkeys(requested)), list(dict.fromkeys(excluded))


def _interpret_boolean(clean: str) -> tuple[str, bool | None, list[str]]:
    normalised = _normalise(clean)
    if _contains_marker(normalised, _UNKNOWN_MARKERS):
        return "unknown", None, ["answer_unknown"]
    negations = _NEGATION_RE.findall(normalised)
    if len(negations) > 1:
        return "ambiguous", None, ["answer_ambiguous"]
    has_no = _contains_marker(normalised, _NO_MARKERS)
    has_yes = _marker_present_without_negation(normalised)
    remainder = normalised
    for marker in sorted(_NO_MARKERS, key=len, reverse=True):
        remainder = remainder.replace(marker, " ")
    if _UNCONSUMED_NEGATION_RE.search(remainder):
        return "ambiguous", None, ["answer_ambiguous"]
    if has_no and has_yes:
        return "ambiguous", None, ["answer_ambiguous"]
    if has_no:
        return "negative", False, []
    if has_yes:
        return "affirmative", True, []
    return "ambiguous", None, ["answer_not_yes_or_no"]


def new_session(goal: str) -> dict[str, Any]:
    """Create a revision-zero draft without external calls or side effects."""

    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal must be a non-empty string")
    return {
        "schema_version": SCHEMA_VERSION,
        "id": f"session-{uuid.uuid4().hex}",
        "revision": 0,
        "goal": sanitize_text(goal.strip()),
        "answers": {},
        "requirements": {},
        "questions": deepcopy(list(QUESTION_DEFINITIONS)),
        "answer_history": [],
        "state": "clarifying",
    }


def _validate_session_shape(session: dict[str, Any]) -> None:
    if not isinstance(session, dict):
        raise TypeError("session must be a dictionary")
    expected = {"schema_version", "id", "revision", "goal", "answers", "requirements", "questions", "answer_history", "state"}
    if set(session) != expected:
        raise ValueError("session fields do not match schema")
    if session["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported session schema_version")
    if not isinstance(session["id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", session["id"]):
        raise ValueError("session id is required")
    if isinstance(session["revision"], bool) or not isinstance(session["revision"], int) or session["revision"] < 0:
        raise ValueError("session revision must be a non-negative integer")
    if not isinstance(session["goal"], str) or not session["goal"].strip():
        raise ValueError("session goal is required")
    if not isinstance(session["answers"], dict) or not isinstance(session["requirements"], dict):
        raise ValueError("session answers and requirements must be dictionaries")
    if session["state"] not in {"clarifying", "recommendation_ready"}:
        raise ValueError("unsupported session state")
    if not isinstance(session["questions"], list) or len(session["questions"]) != len(QUESTION_DEFINITIONS):
        raise ValueError("session questions do not match schema")
    for actual, expected_question in zip(session["questions"], QUESTION_DEFINITIONS):
        if not isinstance(actual, dict) or set(actual) != {"id", "question", "why", "required"} or actual != expected_question:
            raise ValueError("invalid session question shape")
    if not isinstance(session["answer_history"], list):
        raise ValueError("answer_history must be a list")
    for expected_revision, history in enumerate(session["answer_history"], start=1):
        if not isinstance(history, dict) or set(history) != {"revision", "question_id", "previous", "answer", "changed", "conflict"}:
            raise ValueError("invalid answer_history shape")
        if isinstance(history["revision"], bool) or not isinstance(history["revision"], int) or not isinstance(history["question_id"], str):
            raise ValueError("invalid answer_history values")
        if history["revision"] != expected_revision:
            raise ValueError("invalid answer_history revision")
        if history["question_id"] not in _QUESTION_MAP:
            raise ValueError("invalid answer_history question_id")
        if history["previous"] is not None and (not isinstance(history["previous"], str) or not history["previous"].strip()):
            raise ValueError("invalid answer_history previous")
        if not isinstance(history["answer"], str) or not history["answer"].strip() or not isinstance(history["changed"], bool) or not isinstance(history["conflict"], bool):
            raise ValueError("invalid answer_history values")
    if len(session["answer_history"]) != session["revision"]:
        raise ValueError("answer_history must match revision")
    for question_id, answer in session["answers"].items():
        if question_id not in _QUESTION_MAP or not isinstance(answer, str) or not answer.strip():
            raise ValueError("invalid session answer")
    for question_id, requirement in session["requirements"].items():
        if question_id not in _QUESTION_MAP or not isinstance(requirement, dict):
            raise ValueError("invalid session requirement")
        if set(requirement) != {"kind", "value", "platforms", "reason_codes", "source", "changed", "conflict"}:
            raise ValueError("invalid session requirement shape")
        if requirement["kind"] not in {"known", "affirmative", "negative", "unknown", "ambiguous", "conflict"}:
            raise ValueError("invalid requirement kind")
        if not isinstance(requirement["platforms"], list) or not all(item in SUPPORTED_PLATFORMS for item in requirement["platforms"]):
            raise ValueError("invalid requirement platforms")
        if not isinstance(requirement["reason_codes"], list) or not all(isinstance(item, str) for item in requirement["reason_codes"]):
            raise ValueError("invalid requirement reason_codes")
        if requirement["source"] != "user" or not isinstance(requirement["changed"], bool) or not isinstance(requirement["conflict"], bool):
            raise ValueError("invalid requirement metadata")
        kind = requirement["kind"]
        value = requirement["value"]
        if kind in {"affirmative", "negative"} and not isinstance(value, bool):
            raise ValueError("boolean requirement must have a boolean value")
        if kind in {"unknown", "ambiguous", "conflict"} and value is not None:
            raise ValueError("unresolved requirement must have a null value")
        if kind == "known" and not isinstance(value, str):
            raise ValueError("known requirement must have text value")
    if set(session["answers"]) != set(session["requirements"]):
        raise ValueError("answers and requirements must have the same question ids")
    replay_answers: dict[str, str] = {}
    replay_requirements: dict[str, dict[str, Any]] = {}
    for history in session["answer_history"]:
        question_id = history["question_id"]
        previous = replay_answers.get(question_id)
        if history["previous"] != previous:
            raise ValueError("answer_history previous does not match its predecessor")
        answer = history["answer"]
        if sanitize_text(answer) != answer:
            raise ValueError("session answers must be sanitized")
        interpretation = interpret_answer(question_id, answer)
        changed = previous is not None and previous != answer
        conflict = _is_conflict(replay_requirements.get(question_id), interpretation) if changed else False
        if replay_requirements.get(question_id, {}).get("conflict") and not conflict:
            interpretation["reason_codes"] = [*interpretation.get("reason_codes", []), "answer_conflict_resolved"]
        if conflict:
            interpretation["kind"] = "conflict"
            interpretation["value"] = None
            interpretation["reason_codes"] = [*interpretation.get("reason_codes", []), "answer_conflict"]
        if history["changed"] != changed or history["conflict"] != conflict:
            raise ValueError("answer_history metadata is inconsistent")
        interpretation["changed"] = changed
        interpretation["conflict"] = conflict
        replay_answers[question_id] = answer
        replay_requirements[question_id] = interpretation
    if replay_answers != session["answers"] or replay_requirements != session["requirements"]:
        raise ValueError("session answers or requirements do not match answer history")
    required_ids = {item["id"] for item in QUESTION_DEFINITIONS if item["required"]}
    expected_state = "recommendation_ready" if required_ids.issubset(replay_requirements) and all(_resolved(replay_requirements[item]) for item in required_ids) else "clarifying"
    if session["state"] != expected_state:
        raise ValueError("session state is inconsistent with requirements")


def interpret_answer(question_id: str, answer: str) -> dict[str, Any]:
    """Interpret only what can be supported by the user's words."""

    if question_id not in _QUESTION_MAP:
        raise ValueError(f"unknown question_id: {question_id}")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("answer must be a non-empty string")
    clean = sanitize_text(answer.strip())
    normalised = _normalise(clean)
    if question_id == "platform":
        requested, excluded = _platforms_in(clean)
        if _contains_marker(normalised, _UNKNOWN_MARKERS):
            kind, reason = "unknown", ["platform_unknown"]
        elif len(requested) > 1:
            kind, reason = "ambiguous", ["platform_multiple_targets_need_scope_order"]
        elif requested:
            kind, reason = "known", []
        elif excluded:
            kind, reason = "ambiguous", ["platform_all_excluded"]
        else:
            kind, reason = "ambiguous", ["platform_unrecognised"]
        return {"kind": kind, "value": requested[0] if len(requested) == 1 else None, "platforms": requested, "reason_codes": reason, "source": "user"}
    if question_id in {"data_persistence", "cross_device", "file_storage", "external_data", "admin_access", "account_access", "notifications", "payments", "multi_user"}:
        kind, value, reason = _interpret_boolean(clean)
        return {"kind": kind, "value": value, "platforms": [], "reason_codes": reason, "source": "user"}
    if _contains_marker(normalised, _UNKNOWN_MARKERS):
        return {"kind": "unknown", "value": None, "platforms": [], "reason_codes": ["answer_unknown"], "source": "user"}
    return {"kind": "known", "value": clean, "platforms": [], "reason_codes": [], "source": "user"}


def _is_conflict(previous: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    if not previous:
        return False
    if previous.get("kind") == "affirmative" and current.get("kind") == "negative":
        return True
    if previous.get("kind") == "negative" and current.get("kind") == "affirmative":
        return True
    old = set(previous.get("platforms", []))
    new = set(current.get("platforms", []))
    return bool(old and new and old != new)


def _resolved(requirement: dict[str, Any]) -> bool:
    return requirement.get("kind") in {"known", "affirmative", "negative"} and not requirement.get("conflict")


def answer_question(session: dict[str, Any], question_id: str, answer: str) -> dict[str, Any]:
    """Return a new immutable session revision and preserve every change in history."""

    _validate_session_shape(session)
    if question_id not in _QUESTION_MAP:
        raise ValueError(f"unknown question_id: {question_id}")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("answer must be a non-empty string")
    updated = deepcopy(session)
    previous_answer = updated["answers"].get(question_id)
    previous_requirement = updated["requirements"].get(question_id)
    clean = sanitize_text(answer.strip())
    interpretation = interpret_answer(question_id, clean)
    changed = previous_answer is not None and previous_answer != clean
    conflict = _is_conflict(previous_requirement, interpretation) if changed else False
    if previous_requirement and previous_requirement.get("conflict") and not conflict:
        interpretation["reason_codes"] = [*interpretation.get("reason_codes", []), "answer_conflict_resolved"]
    if conflict:
        interpretation["kind"] = "conflict"
        interpretation["value"] = None
        interpretation["reason_codes"] = [*interpretation.get("reason_codes", []), "answer_conflict"]
    interpretation["changed"] = changed
    interpretation["conflict"] = conflict
    updated["revision"] += 1
    updated["answers"][question_id] = clean
    updated["requirements"][question_id] = interpretation
    updated["answer_history"].append({
        "revision": updated["revision"],
        "question_id": question_id,
        "previous": previous_answer,
        "answer": clean,
        "changed": changed,
        "conflict": conflict,
    })
    required_ids = {item["id"] for item in QUESTION_DEFINITIONS if item["required"]}
    all_resolved = required_ids.issubset(updated["requirements"]) and all(_resolved(updated["requirements"][item]) for item in required_ids)
    updated["state"] = "recommendation_ready" if all_resolved else "clarifying"
    return updated


def next_questions(session: dict[str, Any]) -> list[dict[str, Any]]:
    """List unanswered, ambiguous, unknown, or conflicting questions in stable order."""

    _validate_session_shape(session)
    result = []
    for question in QUESTION_DEFINITIONS:
        question_id = question["id"]
        requirement = session["requirements"].get(question_id)
        if question_id not in session["answers"]:
            item = deepcopy(question)
            item["status"] = "unanswered"
            result.append(item)
        elif requirement and requirement.get("conflict"):
            item = deepcopy(question)
            item["status"] = "needs_confirmation"
            result.append(item)
        elif requirement and requirement.get("kind") in {"unknown", "ambiguous"}:
            item = deepcopy(question)
            item["status"] = "needs_clarification"
            result.append(item)
    return result


__all__ = ["QUESTION_DEFINITIONS", "SCHEMA_VERSION", "SUPPORTED_PLATFORMS", "answer_question", "interpret_answer", "new_session", "next_questions", "sanitize_text"]
