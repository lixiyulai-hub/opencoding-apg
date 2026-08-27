"""Pure local publication-readiness assessment for beginner-facing APG flows."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from typing import Any, Mapping

from .project_materialization_apply import ActionContext, assess_action
from .storage import canonical_json_bytes

PUBLICATION_READINESS_SCHEMA_VERSION = "1.0"
_FIELDS = frozenset({
    "schema_version", "public_surface", "content_scope", "source_artifact_sha256",
    "source_summary", "takedown_plan", "observation_window", "action_context",
    "state", "reason_codes", "explanation_zh", "execution_performed",
})
_CONTEXT_FIELDS = frozenset({
    "policy_sha256", "evidence_refs", "bounded_scope", "reversible", "no_secret_values",
    "no_network", "no_cost", "no_credentials", "no_real_data", "public_delivery",
    "irreversible", "security_change", "privacy_change", "materially_ambiguous",
    "recommendation_only", "runtime_launch", "deployment",
})


class PublicationReadinessError(ValueError):
    pass


class ReadinessState(str, Enum):
    READY = "ready"
    NEEDS_OWNER_GATE = "needs-owner-gate"
    BLOCKED = "blocked"


def _text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise PublicationReadinessError(f"{label} must be bounded text or null")
    return value


def _digest(value: object, label: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise PublicationReadinessError(f"{label} must be a lowercase SHA-256 digest or null")
    return value


def _codes(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not tuple or any(type(item) is not str or not item for item in value) or value != tuple(sorted(set(value))):
        raise PublicationReadinessError(f"{label} must be canonical")
    return value


def _context_mapping(value: ActionContext) -> dict[str, object]:
    return {name: getattr(value, name) for name in sorted(_CONTEXT_FIELDS)}


def _parse_context(value: object) -> ActionContext:
    if not isinstance(value, Mapping) or set(value) != _CONTEXT_FIELDS:
        raise PublicationReadinessError("action_context has unknown or missing fields")
    refs = value["evidence_refs"]
    if not isinstance(refs, list):
        raise PublicationReadinessError("action_context.evidence_refs must be an array")
    try:
        return ActionContext(
            policy_sha256=value["policy_sha256"], evidence_refs=tuple(refs),
            **{name: value[name] for name in _CONTEXT_FIELDS if name not in {"policy_sha256", "evidence_refs"}},
        )
    except (TypeError, ValueError) as error:
        raise PublicationReadinessError("action_context is invalid") from error


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in pairs:
        if key in result:
            raise PublicationReadinessError("publication readiness contains duplicate object fields")
        result[key] = item
    return result


def _reject_constant(value: str) -> None:
    raise PublicationReadinessError(f"publication readiness contains unsupported JSON constant: {value}")


@dataclass(frozen=True)
class PublicationReadiness:
    schema_version: str
    public_surface: str | None
    content_scope: str | None
    source_artifact_sha256: str | None
    source_summary: str | None
    takedown_plan: str | None
    observation_window: str | None
    action_context: ActionContext
    state: ReadinessState
    reason_codes: tuple[str, ...]
    explanation_zh: str
    execution_performed: bool

    def __post_init__(self) -> None:
        if type(self) is not PublicationReadiness or self.schema_version != PUBLICATION_READINESS_SCHEMA_VERSION:
            raise PublicationReadinessError("invalid publication-readiness record")
        for name in ("public_surface", "content_scope", "source_summary", "takedown_plan", "observation_window"):
            _text(getattr(self, name), name)
        _digest(self.source_artifact_sha256, "source_artifact_sha256")
        if type(self.action_context) is not ActionContext or type(self.state) is not ReadinessState:
            raise PublicationReadinessError("invalid readiness source types")
        _codes(self.reason_codes, "reason_codes")
        if type(self.explanation_zh) is not str or not self.explanation_zh or type(self.execution_performed) is not bool or self.execution_performed:
            raise PublicationReadinessError("invalid readiness result")


def _assess(values: dict[str, object], context: ActionContext) -> tuple[ReadinessState, tuple[str, ...], str]:
    missing = tuple(sorted(k for k, v in values.items() if v is None))
    action = assess_action(context)
    if action.classification.value == "block":
        return ReadinessState.BLOCKED, tuple(sorted(set(missing + action.reason_codes))), "当前发布资料不安全或范围不清，系统已停止，不能继续。"
    action_reasons = action.reason_codes if action.requires_owner_approval or action.classification.value == "recommend" else ()
    reasons = tuple(sorted(set(missing + action_reasons + (("owner-gate-publication",) if context.public_delivery else ()))))
    if reasons:
        return ReadinessState.NEEDS_OWNER_GATE, reasons, "发布资料还不完整，或需要你确认一次；系统现在不会公开发布。"
    return ReadinessState.READY, (), "发布所需资料已齐全，可以进入下一步检查；系统仍不会公开发布。"


def build_publication_readiness(*, public_surface: str | None, content_scope: str | None,
                                source_artifact_sha256: str | None, source_summary: str | None,
                                takedown_plan: str | None, observation_window: str | None,
                                action_context: ActionContext) -> PublicationReadiness:
    values = {
        "public_surface": _text(public_surface, "public_surface"),
        "content_scope": _text(content_scope, "content_scope"),
        "source_artifact_sha256": _digest(source_artifact_sha256, "source_artifact_sha256"),
        "source_summary": _text(source_summary, "source_summary"),
        "takedown_plan": _text(takedown_plan, "takedown_plan"),
        "observation_window": _text(observation_window, "observation_window"),
    }
    state, reasons, explanation = _assess(values, action_context)
    return PublicationReadiness(PUBLICATION_READINESS_SCHEMA_VERSION, **values, action_context=action_context,
                                state=state, reason_codes=reasons, explanation_zh=explanation,
                                execution_performed=False)


def render_publication_readiness(value: PublicationReadiness) -> bytes:
    expected = build_publication_readiness(
        public_surface=value.public_surface, content_scope=value.content_scope,
        source_artifact_sha256=value.source_artifact_sha256, source_summary=value.source_summary,
        takedown_plan=value.takedown_plan, observation_window=value.observation_window,
        action_context=value.action_context,
    )
    if type(value) is not PublicationReadiness or value != expected:
        raise PublicationReadinessError("assessment is not source-recomputed")
    return canonical_json_bytes({
        "action_context": _context_mapping(value.action_context),
        "content_scope": value.content_scope,
        "execution_performed": False,
        "explanation_zh": value.explanation_zh,
        "observation_window": value.observation_window,
        "public_surface": value.public_surface,
        "reason_codes": list(value.reason_codes),
        "schema_version": value.schema_version,
        "source_artifact_sha256": value.source_artifact_sha256,
        "source_summary": value.source_summary,
        "state": value.state.value,
        "takedown_plan": value.takedown_plan,
    })


def parse_publication_readiness(payload: bytes | bytearray | memoryview) -> PublicationReadiness:
    try:
        raw = bytes(payload)
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except PublicationReadinessError:
        raise
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise PublicationReadinessError("payload is not valid UTF-8 JSON") from error
    if not isinstance(decoded, Mapping) or set(decoded) != _FIELDS:
        raise PublicationReadinessError("payload has unknown or missing fields")
    context = _parse_context(decoded["action_context"])
    value = build_publication_readiness(
        public_surface=decoded["public_surface"], content_scope=decoded["content_scope"],
        source_artifact_sha256=decoded["source_artifact_sha256"], source_summary=decoded["source_summary"],
        takedown_plan=decoded["takedown_plan"], observation_window=decoded["observation_window"],
        action_context=context,
    )
    if (decoded["state"] != value.state.value or decoded["reason_codes"] != list(value.reason_codes)
            or decoded["explanation_zh"] != value.explanation_zh or decoded["execution_performed"] is not False
            or render_publication_readiness(value) != raw):
        raise PublicationReadinessError("payload does not match source projection")
    return value


__all__ = ["PUBLICATION_READINESS_SCHEMA_VERSION", "PublicationReadinessError", "ReadinessState",
           "PublicationReadiness", "build_publication_readiness", "render_publication_readiness",
           "parse_publication_readiness"]
