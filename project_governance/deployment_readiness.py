"""Pure local deployment-readiness assessment for beginner-facing APG flows."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
import json
from typing import Any, Mapping
from .project_materialization_apply import ActionContext, assess_action
from .storage import canonical_json_bytes

DEPLOYMENT_READINESS_SCHEMA_VERSION = "1.0"
_FIELDS = frozenset({"schema_version", "target", "artifact_manifest_sha256", "deployment_command", "rollback_target", "acceptance_criteria", "observation_window", "action_context", "state", "reason_codes", "explanation_zh", "execution_performed"})
_CONTEXT_FIELDS = frozenset({"policy_sha256", "evidence_refs", "bounded_scope", "reversible", "no_secret_values", "no_network", "no_cost", "no_credentials", "no_real_data", "public_delivery", "irreversible", "security_change", "privacy_change", "materially_ambiguous", "recommendation_only", "runtime_launch", "deployment"})

class DeploymentReadinessError(ValueError):
    pass

class ReadinessState(str, Enum):
    READY = "ready"
    NEEDS_OWNER_GATE = "needs-owner-gate"
    BLOCKED = "blocked"

def _text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise DeploymentReadinessError(f"{label} must be bounded text or null")
    return value

def _digest(value: object, label: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise DeploymentReadinessError(f"{label} must be a lowercase SHA-256 digest or null")
    return value

def _codes(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not tuple or any(type(item) is not str or not item for item in value) or value != tuple(sorted(set(value))):
        raise DeploymentReadinessError(f"{label} must be canonical")
    return value

def _context_mapping(value: ActionContext) -> dict[str, object]:
    return {name: getattr(value, name) for name in sorted(_CONTEXT_FIELDS)}

def _parse_context(value: object) -> ActionContext:
    if not isinstance(value, Mapping) or set(value) != _CONTEXT_FIELDS:
        raise DeploymentReadinessError("action_context has unknown or missing fields")
    refs = value["evidence_refs"]
    if not isinstance(refs, list):
        raise DeploymentReadinessError("action_context.evidence_refs must be an array")
    try:
        return ActionContext(policy_sha256=value["policy_sha256"], evidence_refs=tuple(refs), **{name: value[name] for name in _CONTEXT_FIELDS if name not in {"policy_sha256", "evidence_refs"}})
    except (TypeError, ValueError) as error:
        raise DeploymentReadinessError("action_context is invalid") from error


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON object keys instead of silently taking the last one."""
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise DeploymentReadinessError("deployment readiness contains duplicate object fields")
        result[key] = item
    return result


def _reject_constant(value: str) -> None:
    raise DeploymentReadinessError(
        f"deployment readiness contains unsupported JSON constant: {value}"
    )

@dataclass(frozen=True)
class DeploymentReadiness:
    schema_version: str
    target: str | None
    artifact_manifest_sha256: str | None
    deployment_command: str | None
    rollback_target: str | None
    acceptance_criteria: str | None
    observation_window: str | None
    action_context: ActionContext
    state: ReadinessState
    reason_codes: tuple[str, ...]
    explanation_zh: str
    execution_performed: bool

    def __post_init__(self) -> None:
        if type(self) is not DeploymentReadiness or self.schema_version != DEPLOYMENT_READINESS_SCHEMA_VERSION:
            raise DeploymentReadinessError("invalid deployment-readiness record")
        for name in ("target", "deployment_command", "rollback_target", "acceptance_criteria", "observation_window"):
            _text(getattr(self, name), name)
        _digest(self.artifact_manifest_sha256, "artifact_manifest_sha256")
        if type(self.action_context) is not ActionContext or type(self.state) is not ReadinessState:
            raise DeploymentReadinessError("invalid readiness source types")
        _codes(self.reason_codes, "reason_codes")
        if type(self.explanation_zh) is not str or not self.explanation_zh or type(self.execution_performed) is not bool or self.execution_performed:
            raise DeploymentReadinessError("invalid readiness result")

def _assess(values: dict[str, object], context: ActionContext) -> tuple[ReadinessState, tuple[str, ...], str]:
    missing = tuple(sorted(k for k, v in values.items() if v is None))
    action = assess_action(context)
    if action.classification.value == "block":
        return ReadinessState.BLOCKED, tuple(sorted(set(missing + action.reason_codes))), "当前资料不安全或范围不清，系统已停止，不能继续。"
    action_reasons = action.reason_codes if action.requires_owner_approval or action.classification.value == "recommend" else ()
    reasons = tuple(sorted(set(missing + action_reasons + (("owner-gate-deployment",) if context.deployment else ()))))
    if reasons:
        return ReadinessState.NEEDS_OWNER_GATE, reasons, "部署资料还不完整，或需要你确认一次；系统现在不会自动部署。"
    return ReadinessState.READY, (), "部署所需资料已齐全，可以进入下一步检查；系统仍不会自动部署。"

def build_deployment_readiness(*, target: str | None, artifact_manifest_sha256: str | None, deployment_command: str | None, rollback_target: str | None, acceptance_criteria: str | None, observation_window: str | None, action_context: ActionContext) -> DeploymentReadiness:
    values = {"target": _text(target, "target"), "artifact_manifest_sha256": _digest(artifact_manifest_sha256, "artifact_manifest_sha256"), "deployment_command": _text(deployment_command, "deployment_command"), "rollback_target": _text(rollback_target, "rollback_target"), "acceptance_criteria": _text(acceptance_criteria, "acceptance_criteria"), "observation_window": _text(observation_window, "observation_window")}
    state, reasons, explanation = _assess(values, action_context)
    return DeploymentReadiness(DEPLOYMENT_READINESS_SCHEMA_VERSION, **values, action_context=action_context, state=state, reason_codes=reasons, explanation_zh=explanation, execution_performed=False)

def render_deployment_readiness(value: DeploymentReadiness) -> bytes:
    expected = build_deployment_readiness(target=value.target, artifact_manifest_sha256=value.artifact_manifest_sha256, deployment_command=value.deployment_command, rollback_target=value.rollback_target, acceptance_criteria=value.acceptance_criteria, observation_window=value.observation_window, action_context=value.action_context)
    if type(value) is not DeploymentReadiness or value != expected:
        raise DeploymentReadinessError("assessment is not source-recomputed")
    return canonical_json_bytes({"acceptance_criteria": value.acceptance_criteria, "action_context": _context_mapping(value.action_context), "artifact_manifest_sha256": value.artifact_manifest_sha256, "deployment_command": value.deployment_command, "execution_performed": False, "explanation_zh": value.explanation_zh, "observation_window": value.observation_window, "reason_codes": list(value.reason_codes), "rollback_target": value.rollback_target, "schema_version": value.schema_version, "state": value.state.value, "target": value.target})

def parse_deployment_readiness(payload: bytes | bytearray | memoryview) -> DeploymentReadiness:
    try:
        raw = bytes(payload)
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except DeploymentReadinessError:
        raise
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise DeploymentReadinessError("payload is not valid UTF-8 JSON") from error
    if not isinstance(decoded, Mapping) or set(decoded) != _FIELDS:
        raise DeploymentReadinessError("payload has unknown or missing fields")
    context = _parse_context(decoded["action_context"])
    value = build_deployment_readiness(target=decoded["target"], artifact_manifest_sha256=decoded["artifact_manifest_sha256"], deployment_command=decoded["deployment_command"], rollback_target=decoded["rollback_target"], acceptance_criteria=decoded["acceptance_criteria"], observation_window=decoded["observation_window"], action_context=context)
    if decoded["state"] != value.state.value or decoded["reason_codes"] != list(value.reason_codes) or decoded["explanation_zh"] != value.explanation_zh or decoded["execution_performed"] is not False or render_deployment_readiness(value) != raw:
        raise DeploymentReadinessError("payload does not match source projection")
    return value

__all__ = ["DEPLOYMENT_READINESS_SCHEMA_VERSION", "DeploymentReadinessError", "ReadinessState", "DeploymentReadiness", "build_deployment_readiness", "render_deployment_readiness", "parse_deployment_readiness"]

