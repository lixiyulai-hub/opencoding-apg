"""Pure P3-G goal-to-delivery lifecycle state transitions.

P3-G consumes one exact P3-F plan and caller-supplied execution evidence. It
tracks dependency-closed waves, consequential decision boundaries, append-only
checkpoints, and phase-scoped acceptance without performing any external work.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence
import unicodedata

from .autonomous_task_orchestration import (
    AutonomousTaskOrchestrationError,
    AutonomousTaskPlan,
    ReviewVerdict,
    TaskExecutionEvidence,
    TaskResultStatus,
    parse_autonomous_task_plan,
    render_autonomous_task_plan,
)
from .authorization_session import AuthorizationSession, authorization_session_sha256
from .project_materialization_apply import AuthorizationClass
from .storage import SchemaError, canonical_json_bytes


P3G_SCHEMA_VERSION = "1.0"
MAX_GOAL_DELIVERY_LIFECYCLE_BYTES = 1024 * 1024
MAX_CHECKPOINTS = 256
MAX_REFERENCES = 128

_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z\Z"
)
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")
_SENSITIVE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}|\bghp_[A-Za-z0-9]{8,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.|"
    r"\bbearer\s+[A-Za-z0-9._~+/-]{8,}|"
    r"\b(?:api[_-]?key|token|password|secret)\s*[:=]|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)


class GoalDeliveryLifecycleError(ValueError):
    """Raised when P3-G input or canonical state is malformed."""


class LifecycleState(str, Enum):
    AUTO = "auto"
    RECOMMEND = "recommend"
    CONFIRM = "confirm"
    BLOCK = "block"
    COMPLETE = "complete"


class LifecycleTaskState(str, Enum):
    PENDING = "pending"
    READY = "ready"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"


class LifecyclePhase(str, Enum):
    PLANNED = "planned"
    REPOSITORY_VALIDATED = "repository-validated"
    RUNTIME_VERIFIED = "runtime-verified"
    DEPLOYMENT_VERIFIED = "deployment-verified"
    PUBLICATION_VERIFIED = "publication-verified"
    PILOT_ACCEPTED = "pilot-accepted"
    RELEASE_ACCEPTED = "release-accepted"


class DecisionOutcome(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"


_PHASE_ORDER = tuple(LifecyclePhase)


def _scalar(value: object, label: str, maximum: int = 240) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise GoalDeliveryLifecycleError(f"{label} must be bounded non-empty text")
    if value != unicodedata.normalize("NFC", value):
        raise GoalDeliveryLifecycleError(f"{label} must use NFC Unicode")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise GoalDeliveryLifecycleError(f"{label} contains control characters")
    if _SENSITIVE.search(value):
        raise GoalDeliveryLifecycleError(f"{label} contains a sensitive-value pattern")
    return value


def _code(value: object, label: str) -> str:
    text = _scalar(value, label, 128)
    if not _CODE.fullmatch(text):
        raise GoalDeliveryLifecycleError(f"{label} must be a bounded stable code")
    return text


def _digest(value: object, label: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise GoalDeliveryLifecycleError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: object, label: str) -> str:
    text = _scalar(value, label, 40)
    if not _TIMESTAMP.fullmatch(text):
        raise GoalDeliveryLifecycleError(f"{label} must be a UTC timestamp ending in Z")
    return text


def _reference(value: object, label: str) -> str:
    text = _scalar(value, label, 240)
    if _CODE.fullmatch(text):
        return text
    if (
        "\\" not in text
        and not text.startswith("/")
        and not _WINDOWS_DRIVE.match(text)
        and "://" not in text
        and ":" not in text
        and "?" not in text
        and "#" not in text
    ):
        parts = text.split("/")
        if (
            all(part not in ("", ".", "..") for part in parts)
            and all(not part.endswith((".", " ")) for part in parts)
            and tuple(PurePosixPath(text).parts) == tuple(parts)
        ):
            return text
    raise GoalDeliveryLifecycleError(
        f"{label} must be a stable code or contained relative path"
    )


def _tuple(value: object, label: str, maximum: int) -> tuple[object, ...]:
    if type(value) is not tuple or len(value) > maximum:
        raise GoalDeliveryLifecycleError(f"{label} must be a bounded immutable tuple")
    return value


def _canonical_codes(
    value: object,
    label: str,
    *,
    allow_empty: bool = True,
    maximum: int = MAX_REFERENCES,
) -> tuple[str, ...]:
    values = tuple(
        _code(item, f"{label}[{index}]")
        for index, item in enumerate(_tuple(value, label, maximum))
    )
    if not allow_empty and not values:
        raise GoalDeliveryLifecycleError(f"{label} must not be empty")
    if values != tuple(sorted(set(values))):
        raise GoalDeliveryLifecycleError(f"{label} must use canonical unique order")
    return values


def _canonical_refs(
    value: object,
    label: str,
    *,
    allow_empty: bool = True,
    maximum: int = MAX_REFERENCES,
) -> tuple[str, ...]:
    values = tuple(
        _reference(item, f"{label}[{index}]")
        for index, item in enumerate(_tuple(value, label, maximum))
    )
    if not allow_empty and not values:
        raise GoalDeliveryLifecycleError(f"{label} must not be empty")
    if values != tuple(sorted(set(values))):
        raise GoalDeliveryLifecycleError(f"{label} must use canonical unique order")
    return values


def _enum(value: object, enum_type: type[Enum], label: str) -> Enum:
    if type(value) is not str:
        raise GoalDeliveryLifecycleError(f"{label} must be a string enum")
    try:
        return enum_type(value)
    except ValueError as error:
        raise GoalDeliveryLifecycleError(f"{label} has an unsupported value") from error


def _task_scope(plan: AutonomousTaskPlan, task_id: str) -> tuple[str, ...]:
    route = next(item for item in plan.routes if item.task_id == task_id)
    return tuple(sorted(set(route.context.read_paths + route.context.write_paths)))


@dataclass(frozen=True)
class LifecycleDecision:
    decision_id: str
    lifecycle_run_id: str
    plan_id: str
    plan_sha256: str
    task_id: str
    scope: tuple[str, ...]
    outcome: DecisionOutcome
    selected_option: str
    timestamp_utc: str

    def __post_init__(self) -> None:
        if type(self) is not LifecycleDecision:
            raise GoalDeliveryLifecycleError("LifecycleDecision subclasses are not accepted")
        _code(self.decision_id, "decision.decision_id")
        _code(self.lifecycle_run_id, "decision.lifecycle_run_id")
        _code(self.plan_id, "decision.plan_id")
        _digest(self.plan_sha256, "decision.plan_sha256")
        _code(self.task_id, "decision.task_id")
        _canonical_refs(self.scope, "decision.scope", allow_empty=False)
        if type(self.outcome) is not DecisionOutcome:
            raise GoalDeliveryLifecycleError("decision.outcome must be a DecisionOutcome")
        _code(self.selected_option, "decision.selected_option")
        _timestamp(self.timestamp_utc, "decision.timestamp_utc")


@dataclass(frozen=True)
class LifecycleApproval:
    approval_id: str
    transaction_id: str
    lifecycle_run_id: str
    plan_id: str
    plan_sha256: str
    task_id: str
    scope: tuple[str, ...]
    actor: str
    role: str
    timestamp_utc: str

    def __post_init__(self) -> None:
        if type(self) is not LifecycleApproval:
            raise GoalDeliveryLifecycleError("LifecycleApproval subclasses are not accepted")
        _code(self.approval_id, "approval.approval_id")
        _code(self.transaction_id, "approval.transaction_id")
        _code(self.lifecycle_run_id, "approval.lifecycle_run_id")
        _code(self.plan_id, "approval.plan_id")
        _digest(self.plan_sha256, "approval.plan_sha256")
        _code(self.task_id, "approval.task_id")
        _canonical_refs(self.scope, "approval.scope", allow_empty=False)
        _scalar(self.actor, "approval.actor", 128)
        _code(self.role, "approval.role")
        _timestamp(self.timestamp_utc, "approval.timestamp_utc")


def _session_approvals(
    lifecycle: "GoalDeliveryLifecycle",
    authorization_session: AuthorizationSession,
    *,
    exact_replay: bool = False,
) -> tuple[LifecycleApproval, ...]:
    """Expand one exact session into task evidence for one confirmation wave."""

    if type(authorization_session) is not AuthorizationSession:
        raise TypeError("authorization_session must be an exact AuthorizationSession")
    routes = tuple(
        item
        for item in lifecycle.plan.routes
        if item.task_id in lifecycle.current_wave_task_ids
        and item.classification is AuthorizationClass.CONFIRM
        and item.task_id not in {approval.task_id for approval in lifecycle.approvals}
    )
    if not routes:
        raise GoalDeliveryLifecycleError(
            "authorization session does not bind a current confirmation wave"
        )
    scope = tuple(
        sorted({path for route in routes for path in _task_scope(lifecycle.plan, route.task_id)})
    )
    reasons = tuple(
        sorted({reason for route in routes for reason in route.reason_codes})
    )
    task_ids = tuple(sorted(route.task_id for route in routes))
    replay_now = (
        datetime.fromisoformat(authorization_session.issued_at_utc.replace("Z", "+00:00"))
        if exact_replay
        else None
    )
    if (
        authorization_session.lifecycle_run_id != lifecycle.lifecycle_run_id
        or authorization_session.plan_id != lifecycle.plan_id
        or authorization_session.wave_index != lifecycle.current_wave_index
        or authorization_session.task_ids != task_ids
        or authorization_session.source_sha256 != lifecycle.plan_sha256
        or authorization_session.preimage_sha256 is not None
        or len(authorization_session.transaction_ids) != 1
        or authorization_session.scope != scope
        or authorization_session.reason_codes != reasons
        or not authorization_session.covers(
            transaction_id=authorization_session.transaction_ids[0],
            policy_sha256=lifecycle.plan.policy_sha256,
            source_sha256=lifecycle.plan_sha256,
            scope=scope,
            reason_codes=reasons,
            preimage_sha256=None,
            now=replay_now,
        )
    ):
        raise GoalDeliveryLifecycleError(
            "authorization session does not bind the exact confirmation wave"
        )
    session_sha256 = authorization_session_sha256(authorization_session)
    transaction_id = authorization_session.transaction_ids[0]
    return tuple(
        LifecycleApproval(
            approval_id=(
                "approval.session."
                + hashlib.sha256(
                    (
                        session_sha256
                        + "|"
                        + route.task_id
                    ).encode("utf-8")
                ).hexdigest()
            ),
            transaction_id=transaction_id,
            lifecycle_run_id=lifecycle.lifecycle_run_id,
            plan_id=lifecycle.plan_id,
            plan_sha256=lifecycle.plan_sha256,
            task_id=route.task_id,
            scope=_task_scope(lifecycle.plan, route.task_id),
            actor=authorization_session.actor,
            role="owner-delegated",
            timestamp_utc=authorization_session.issued_at_utc,
        )
        for route in sorted(routes, key=lambda item: item.task_id)
    )


@dataclass(frozen=True)
class TaskConsolidation:
    task_id: str
    consolidation_ref: str

    def __post_init__(self) -> None:
        if type(self) is not TaskConsolidation:
            raise GoalDeliveryLifecycleError("TaskConsolidation subclasses are not accepted")
        _code(self.task_id, "consolidation.task_id")
        _reference(self.consolidation_ref, "consolidation.consolidation_ref")


@dataclass(frozen=True)
class LifecyclePhaseAcceptance:
    acceptance_id: str
    lifecycle_run_id: str
    plan_sha256: str
    phase: LifecyclePhase
    evidence_domain: LifecyclePhase
    evidence_refs: tuple[str, ...]
    actor: str
    timestamp_utc: str

    def __post_init__(self) -> None:
        if type(self) is not LifecyclePhaseAcceptance:
            raise GoalDeliveryLifecycleError(
                "LifecyclePhaseAcceptance subclasses are not accepted"
            )
        _code(self.acceptance_id, "phase_acceptance.acceptance_id")
        _code(self.lifecycle_run_id, "phase_acceptance.lifecycle_run_id")
        _digest(self.plan_sha256, "phase_acceptance.plan_sha256")
        if type(self.phase) is not LifecyclePhase or type(self.evidence_domain) is not LifecyclePhase:
            raise GoalDeliveryLifecycleError(
                "phase acceptance phase and evidence_domain must be LifecyclePhase values"
            )
        if self.phase is LifecyclePhase.PLANNED or self.evidence_domain is not self.phase:
            raise GoalDeliveryLifecycleError(
                "phase acceptance evidence domain must exactly match a post-plan phase"
            )
        _canonical_refs(
            self.evidence_refs,
            "phase_acceptance.evidence_refs",
            allow_empty=False,
        )
        _scalar(self.actor, "phase_acceptance.actor", 128)
        _timestamp(self.timestamp_utc, "phase_acceptance.timestamp_utc")


@dataclass(frozen=True)
class LifecycleTaskCursor:
    task_id: str
    wave_index: int
    state: LifecycleTaskState

    def __post_init__(self) -> None:
        if type(self) is not LifecycleTaskCursor:
            raise GoalDeliveryLifecycleError("LifecycleTaskCursor subclasses are not accepted")
        _code(self.task_id, "task_cursor.task_id")
        if type(self.wave_index) is not int or self.wave_index < 0:
            raise GoalDeliveryLifecycleError("task_cursor.wave_index must be non-negative")
        if type(self.state) is not LifecycleTaskState:
            raise GoalDeliveryLifecycleError("task_cursor.state must be LifecycleTaskState")


@dataclass(frozen=True)
class LifecycleUserResult:
    status_code: str
    result_code: str
    next_step_code: str
    phase: LifecyclePhase

    def __post_init__(self) -> None:
        if type(self) is not LifecycleUserResult:
            raise GoalDeliveryLifecycleError("LifecycleUserResult subclasses are not accepted")
        _code(self.status_code, "user_result.status_code")
        _code(self.result_code, "user_result.result_code")
        _code(self.next_step_code, "user_result.next_step_code")
        if type(self.phase) is not LifecyclePhase:
            raise GoalDeliveryLifecycleError("user_result.phase must be LifecyclePhase")


@dataclass(frozen=True)
class LifecycleCheckpoint:
    sequence: int
    previous_checkpoint_sha256: str | None
    event_sha256: str
    task_evidence: tuple[TaskExecutionEvidence, ...]
    decisions: tuple[LifecycleDecision, ...]
    approvals: tuple[LifecycleApproval, ...]
    consolidations: tuple[TaskConsolidation, ...]
    phase_acceptances: tuple[LifecyclePhaseAcceptance, ...]
    checkpoint_sha256: str

    def __post_init__(self) -> None:
        if type(self) is not LifecycleCheckpoint:
            raise GoalDeliveryLifecycleError("LifecycleCheckpoint subclasses are not accepted")
        if type(self.sequence) is not int or self.sequence < 1:
            raise GoalDeliveryLifecycleError("checkpoint.sequence must be positive")
        if self.previous_checkpoint_sha256 is not None:
            _digest(
                self.previous_checkpoint_sha256,
                "checkpoint.previous_checkpoint_sha256",
            )
        _digest(self.event_sha256, "checkpoint.event_sha256")
        _digest(self.checkpoint_sha256, "checkpoint.checkpoint_sha256")
        for label, values, record_type in (
            ("task_evidence", self.task_evidence, TaskExecutionEvidence),
            ("decisions", self.decisions, LifecycleDecision),
            ("approvals", self.approvals, LifecycleApproval),
            ("consolidations", self.consolidations, TaskConsolidation),
            ("phase_acceptances", self.phase_acceptances, LifecyclePhaseAcceptance),
        ):
            records = _tuple(values, f"checkpoint.{label}", MAX_REFERENCES)
            if any(type(item) is not record_type for item in records):
                raise GoalDeliveryLifecycleError(
                    f"checkpoint.{label} contains invalid records"
                )


@dataclass(frozen=True)
class GoalDeliveryLifecycle:
    schema_version: str
    lifecycle_run_id: str
    plan_id: str
    plan_sha256: str
    plan: AutonomousTaskPlan
    checkpoints: tuple[LifecycleCheckpoint, ...]
    task_evidence: tuple[TaskExecutionEvidence, ...]
    decisions: tuple[LifecycleDecision, ...]
    approvals: tuple[LifecycleApproval, ...]
    consolidations: tuple[TaskConsolidation, ...]
    phase_acceptances: tuple[LifecyclePhaseAcceptance, ...]
    task_cursor: tuple[LifecycleTaskCursor, ...]
    current_wave_index: int | None
    current_wave_task_ids: tuple[str, ...]
    next_task_ids: tuple[str, ...]
    accepted_task_ids: tuple[str, ...]
    pending_task_ids: tuple[str, ...]
    blocked_task_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    state: LifecycleState
    phase: LifecyclePhase
    user_result: LifecycleUserResult
    execution_performed: bool

    def __post_init__(self) -> None:
        if type(self) is not GoalDeliveryLifecycle:
            raise GoalDeliveryLifecycleError("GoalDeliveryLifecycle subclasses are not accepted")
        if self.schema_version != P3G_SCHEMA_VERSION:
            raise GoalDeliveryLifecycleError("unsupported goal-delivery schema_version")
        _code(self.lifecycle_run_id, "lifecycle_run_id")
        _code(self.plan_id, "plan_id")
        _digest(self.plan_sha256, "plan_sha256")
        if type(self.plan) is not AutonomousTaskPlan:
            raise GoalDeliveryLifecycleError("plan must be an exact AutonomousTaskPlan")
        if self.plan_id != self.plan.plan_id:
            raise GoalDeliveryLifecycleError("plan_id does not bind the plan")
        if hashlib.sha256(render_autonomous_task_plan(self.plan)).hexdigest() != self.plan_sha256:
            raise GoalDeliveryLifecycleError("plan_sha256 does not bind the plan")
        checkpoints = _tuple(self.checkpoints, "checkpoints", MAX_CHECKPOINTS)
        if any(type(item) is not LifecycleCheckpoint for item in checkpoints):
            raise GoalDeliveryLifecycleError("checkpoints contain invalid records")
        for label, values, record_type in (
            ("task_evidence", self.task_evidence, TaskExecutionEvidence),
            ("decisions", self.decisions, LifecycleDecision),
            ("approvals", self.approvals, LifecycleApproval),
            ("consolidations", self.consolidations, TaskConsolidation),
            ("phase_acceptances", self.phase_acceptances, LifecyclePhaseAcceptance),
            ("task_cursor", self.task_cursor, LifecycleTaskCursor),
        ):
            records = _tuple(values, label, MAX_REFERENCES * 2)
            if any(type(item) is not record_type for item in records):
                raise GoalDeliveryLifecycleError(f"{label} contains invalid records")
        if self.current_wave_index is not None and (
            type(self.current_wave_index) is not int or self.current_wave_index < 0
        ):
            raise GoalDeliveryLifecycleError("current_wave_index must be null or non-negative")
        for label, values in (
            ("current_wave_task_ids", self.current_wave_task_ids),
            ("next_task_ids", self.next_task_ids),
            ("accepted_task_ids", self.accepted_task_ids),
            ("pending_task_ids", self.pending_task_ids),
            ("blocked_task_ids", self.blocked_task_ids),
            ("reason_codes", self.reason_codes),
        ):
            _canonical_codes(values, label, maximum=MAX_REFERENCES * 2)
        if type(self.state) is not LifecycleState:
            raise GoalDeliveryLifecycleError("state must be LifecycleState")
        if type(self.phase) is not LifecyclePhase:
            raise GoalDeliveryLifecycleError("phase must be LifecyclePhase")
        if type(self.user_result) is not LifecycleUserResult:
            raise GoalDeliveryLifecycleError("user_result must be LifecycleUserResult")
        if self.execution_performed is not False:
            raise GoalDeliveryLifecycleError("P3-G cannot claim executor activity")


def _evidence_mapping(value: TaskExecutionEvidence) -> dict[str, object]:
    return {
        "acceptance_refs": list(value.acceptance_refs),
        "authorization_ref": value.authorization_ref,
        "decision_ref": value.decision_ref,
        "executor_id": value.executor_id,
        "gate_refs": list(value.gate_refs),
        "output_refs": list(value.output_refs),
        "review_verdict": value.review_verdict.value,
        "reviewer_id": value.reviewer_id,
        "rollback_ref": value.rollback_ref,
        "status": value.status.value,
        "task_id": value.task_id,
    }


def _decision_mapping(value: LifecycleDecision) -> dict[str, object]:
    return {
        "decision_id": value.decision_id,
        "lifecycle_run_id": value.lifecycle_run_id,
        "outcome": value.outcome.value,
        "plan_id": value.plan_id,
        "plan_sha256": value.plan_sha256,
        "scope": list(value.scope),
        "selected_option": value.selected_option,
        "task_id": value.task_id,
        "timestamp_utc": value.timestamp_utc,
    }


def _approval_mapping(value: LifecycleApproval) -> dict[str, object]:
    return {
        "actor": value.actor,
        "approval_id": value.approval_id,
        "lifecycle_run_id": value.lifecycle_run_id,
        "plan_id": value.plan_id,
        "plan_sha256": value.plan_sha256,
        "role": value.role,
        "scope": list(value.scope),
        "task_id": value.task_id,
        "timestamp_utc": value.timestamp_utc,
        "transaction_id": value.transaction_id,
    }


def _consolidation_mapping(value: TaskConsolidation) -> dict[str, object]:
    return {
        "consolidation_ref": value.consolidation_ref,
        "task_id": value.task_id,
    }


def _phase_acceptance_mapping(value: LifecyclePhaseAcceptance) -> dict[str, object]:
    return {
        "acceptance_id": value.acceptance_id,
        "actor": value.actor,
        "evidence_domain": value.evidence_domain.value,
        "evidence_refs": list(value.evidence_refs),
        "lifecycle_run_id": value.lifecycle_run_id,
        "phase": value.phase.value,
        "plan_sha256": value.plan_sha256,
        "timestamp_utc": value.timestamp_utc,
    }


def _event_mapping(
    *,
    sequence: int,
    task_evidence: tuple[TaskExecutionEvidence, ...],
    decisions: tuple[LifecycleDecision, ...],
    approvals: tuple[LifecycleApproval, ...],
    consolidations: tuple[TaskConsolidation, ...],
    phase_acceptances: tuple[LifecyclePhaseAcceptance, ...],
) -> dict[str, object]:
    return {
        "approvals": [_approval_mapping(item) for item in approvals],
        "consolidations": [_consolidation_mapping(item) for item in consolidations],
        "decisions": [_decision_mapping(item) for item in decisions],
        "phase_acceptances": [
            _phase_acceptance_mapping(item) for item in phase_acceptances
        ],
        "sequence": sequence,
        "task_evidence": [_evidence_mapping(item) for item in task_evidence],
    }


def _checkpoint_digest(
    *, sequence: int, previous: str | None, event_sha256: str
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "event_sha256": event_sha256,
                "previous_checkpoint_sha256": previous,
                "sequence": sequence,
            }
        )
    ).hexdigest()


def _validate_checkpoint_chain(
    checkpoints: tuple[LifecycleCheckpoint, ...],
    plan: AutonomousTaskPlan | None = None,
) -> None:
    previous: str | None = None
    evidence_ids: set[str] = set()
    decision_ids: set[str] = set()
    decision_tasks: set[str] = set()
    approval_ids: set[str] = set()
    approval_tasks: set[str] = set()
    consolidation_tasks: set[str] = set()
    phase_ids: set[str] = set()
    route_ids = (
        {item.task_id for item in plan.routes}
        if plan is not None
        else None
    )
    for index, checkpoint in enumerate(checkpoints, start=1):
        if checkpoint.sequence != index:
            raise GoalDeliveryLifecycleError("checkpoint sequence is not contiguous")
        if checkpoint.previous_checkpoint_sha256 != previous:
            raise GoalDeliveryLifecycleError("checkpoint previous digest does not bind the chain")
        event_sha256 = hashlib.sha256(
            canonical_json_bytes(
                _event_mapping(
                    sequence=checkpoint.sequence,
                    task_evidence=checkpoint.task_evidence,
                    decisions=checkpoint.decisions,
                    approvals=checkpoint.approvals,
                    consolidations=checkpoint.consolidations,
                    phase_acceptances=checkpoint.phase_acceptances,
                )
            )
        ).hexdigest()
        if checkpoint.event_sha256 != event_sha256:
            raise GoalDeliveryLifecycleError("checkpoint event digest does not bind the event")
        expected = _checkpoint_digest(
            sequence=checkpoint.sequence,
            previous=previous,
            event_sha256=event_sha256,
        )
        if checkpoint.checkpoint_sha256 != expected:
            raise GoalDeliveryLifecycleError("checkpoint digest does not bind the chain")
        if len(checkpoint.phase_acceptances) > 1:
            raise GoalDeliveryLifecycleError("one checkpoint may advance only one phase")
        for value, seen, label in (
            ((item.task_id for item in checkpoint.task_evidence), evidence_ids, "task evidence"),
            ((item.decision_id for item in checkpoint.decisions), decision_ids, "decision identifier"),
            ((item.task_id for item in checkpoint.decisions), decision_tasks, "decision task"),
            ((item.approval_id for item in checkpoint.approvals), approval_ids, "approval identifier"),
            ((item.task_id for item in checkpoint.approvals), approval_tasks, "approval task"),
            ((item.task_id for item in checkpoint.consolidations), consolidation_tasks, "consolidation task"),
            ((item.acceptance_id for item in checkpoint.phase_acceptances), phase_ids, "phase acceptance identifier"),
        ):
            identifiers = tuple(value)
            if len(set(identifiers)) != len(identifiers):
                raise GoalDeliveryLifecycleError(f"duplicate {label} within checkpoint")
            if seen.intersection(identifiers):
                raise GoalDeliveryLifecycleError(f"duplicate {label} across checkpoints")
            seen.update(identifiers)
        if route_ids is not None:
            for record in checkpoint.task_evidence:
                if record.task_id not in route_ids:
                    raise GoalDeliveryLifecycleError("checkpoint contains unknown task evidence")
            for record in checkpoint.decisions:
                if record.task_id not in route_ids:
                    raise GoalDeliveryLifecycleError("checkpoint contains unknown decision task")
            for record in checkpoint.approvals:
                if record.task_id not in route_ids:
                    raise GoalDeliveryLifecycleError("checkpoint contains unknown approval task")
            for record in checkpoint.consolidations:
                if record.task_id not in route_ids:
                    raise GoalDeliveryLifecycleError("checkpoint contains unknown consolidation task")
        previous = checkpoint.checkpoint_sha256


def _evidence_reasons(route, evidence: TaskExecutionEvidence) -> tuple[str, ...]:
    reasons: set[str] = set()
    if evidence.executor_id != route.context.executor_id:
        reasons.add("executor-binding-mismatch")
    if evidence.rollback_ref != route.context.rollback_ref:
        reasons.add("rollback-binding-mismatch")
    if not set(route.context.gate_ids).issubset(evidence.gate_refs):
        reasons.add("gate-evidence-missing")
    if not set(route.context.acceptance_refs).issubset(evidence.acceptance_refs):
        reasons.add("acceptance-evidence-missing")
    if evidence.reviewer_id == evidence.executor_id:
        reasons.add("independent-review-required")
    if evidence.status is TaskResultStatus.FAIL:
        reasons.add("task-result-failed")
    if evidence.review_verdict is ReviewVerdict.BLOCK:
        reasons.add("independent-review-blocked")
    return tuple(sorted(reasons))


def _derive(
    *,
    lifecycle_run_id: str,
    plan: AutonomousTaskPlan,
    checkpoints: tuple[LifecycleCheckpoint, ...],
) -> GoalDeliveryLifecycle:
    plan_bytes = render_autonomous_task_plan(plan)
    _validate_checkpoint_chain(checkpoints, plan)
    plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    evidence = tuple(
        sorted(
            (item for checkpoint in checkpoints for item in checkpoint.task_evidence),
            key=lambda item: item.task_id,
        )
    )
    decisions = tuple(
        sorted(
            (item for checkpoint in checkpoints for item in checkpoint.decisions),
            key=lambda item: item.decision_id,
        )
    )
    approvals = tuple(
        sorted(
            (item for checkpoint in checkpoints for item in checkpoint.approvals),
            key=lambda item: item.approval_id,
        )
    )
    consolidations = tuple(
        sorted(
            (item for checkpoint in checkpoints for item in checkpoint.consolidations),
            key=lambda item: item.task_id,
        )
    )
    phase_acceptances = tuple(
        item for checkpoint in checkpoints for item in checkpoint.phase_acceptances
    )
    evidence_by_id = {item.task_id: item for item in evidence}
    decision_by_task = {item.task_id: item for item in decisions}
    approval_by_task = {item.task_id: item for item in approvals}
    consolidation_by_task = {item.task_id: item for item in consolidations}

    accepted: set[str] = set()
    pending: set[str] = set()
    blocked: set[str] = set()
    reasons: set[str] = set(plan.blocker_codes)
    cursor: list[LifecycleTaskCursor] = []
    route_by_id = {item.task_id: item for item in plan.routes}
    state_by_task: dict[str, LifecycleTaskState] = {}
    for route in plan.routes:
        route_reasons: set[str] = set()
        if route.classification is AuthorizationClass.BLOCK:
            route_reasons.add("plan-task-blocked")
        if any(dependency in blocked for dependency in route.depends_on):
            route_reasons.add("dependency-blocked")
        item = evidence_by_id.get(route.task_id)
        if item is not None:
            route_reasons.update(_evidence_reasons(route, item))
        decision = decision_by_task.get(route.task_id)
        if decision is not None and decision.outcome is DecisionOutcome.REJECT:
            route_reasons.add("recommendation-rejected")
        if route_reasons:
            blocked.add(route.task_id)
            reasons.update(f"{reason}.{route.task_id}" for reason in route_reasons)
            state_by_task[route.task_id] = LifecycleTaskState.BLOCKED
            continue
        if any(dependency not in accepted for dependency in route.depends_on):
            pending.add(route.task_id)
            state_by_task[route.task_id] = LifecycleTaskState.PENDING
            continue
        if item is None:
            pending.add(route.task_id)
            state_by_task[route.task_id] = LifecycleTaskState.READY
            continue
        if route.classification is AuthorizationClass.RECOMMEND and decision is None:
            pending.add(route.task_id)
            reasons.add(f"recommendation-decision-pending.{route.task_id}")
            state_by_task[route.task_id] = LifecycleTaskState.READY
            continue
        if route.classification is AuthorizationClass.CONFIRM and route.task_id not in approval_by_task:
            pending.add(route.task_id)
            reasons.add(f"owner-authorization-pending.{route.task_id}")
            state_by_task[route.task_id] = LifecycleTaskState.READY
            continue
        if route.task_id not in consolidation_by_task:
            pending.add(route.task_id)
            reasons.add(f"consolidation-evidence-pending.{route.task_id}")
            state_by_task[route.task_id] = LifecycleTaskState.READY
            continue
        accepted.add(route.task_id)
        state_by_task[route.task_id] = LifecycleTaskState.ACCEPTED

    for route in plan.routes:
        cursor.append(
            LifecycleTaskCursor(
                task_id=route.task_id,
                wave_index=route.wave_index,
                state=state_by_task[route.task_id],
            )
        )

    if blocked:
        state = LifecycleState.BLOCK
        current_wave_index = None
        current_wave_task_ids: tuple[str, ...] = ()
        next_task_ids: tuple[str, ...] = ()
        user_result = LifecycleUserResult(
            status_code="status.blocked",
            result_code="result.goal-delivery-blocked",
            next_step_code="next.resolve-blocker",
            phase=LifecyclePhase.PLANNED,
        )
    elif len(accepted) == len(plan.routes):
        state = LifecycleState.COMPLETE
        current_wave_index = None
        current_wave_task_ids = ()
        next_task_ids = ()
        user_result = LifecycleUserResult(
            status_code="status.complete",
            result_code="result.goal-delivery-complete",
            next_step_code="next.review-final-result",
            phase=LifecyclePhase.PLANNED,
        )
    else:
        eligible = tuple(
            route
            for route in plan.routes
            if route.task_id not in accepted
            and all(dependency in accepted for dependency in route.depends_on)
        )
        if not eligible:
            raise GoalDeliveryLifecycleError("lifecycle has no dependency-closed next task")
        current_wave_index = min(item.wave_index for item in eligible)
        current = tuple(
            item for item in eligible if item.wave_index == current_wave_index
        )
        current_wave_task_ids = tuple(sorted(item.task_id for item in current))
        auto_ready = tuple(
            sorted(
                item.task_id
                for item in current
                if item.classification is AuthorizationClass.AUTO
                or (
                    item.classification is AuthorizationClass.RECOMMEND
                    and item.task_id in decision_by_task
                    and decision_by_task[item.task_id].outcome is DecisionOutcome.ACCEPT
                )
                or (
                    item.classification is AuthorizationClass.CONFIRM
                    and item.task_id in approval_by_task
                )
            )
        )
        recommend = tuple(
            sorted(
                item.task_id
                for item in current
                if item.classification is AuthorizationClass.RECOMMEND
                and item.task_id not in decision_by_task
            )
        )
        confirm = tuple(
            sorted(
                item.task_id
                for item in current
                if item.classification is AuthorizationClass.CONFIRM
                and item.task_id not in approval_by_task
            )
        )
        if auto_ready:
            state = LifecycleState.AUTO
            next_task_ids = auto_ready
            result_code = "result.automatic-work-ready"
            next_step_code = "next.execute-current-wave"
        elif recommend:
            state = LifecycleState.RECOMMEND
            next_task_ids = recommend
            result_code = "result.recommendation-ready"
            next_step_code = "next.decide-recommendation"
        elif confirm:
            state = LifecycleState.CONFIRM
            next_task_ids = confirm
            result_code = "result.confirmation-required"
            next_step_code = "next.provide-transaction-approval"
        else:
            raise GoalDeliveryLifecycleError("current wave has no valid next action")
        user_result = LifecycleUserResult(
            status_code=f"status.{state.value}",
            result_code=result_code,
            next_step_code=next_step_code,
            phase=LifecyclePhase.PLANNED,
        )

    phase = LifecyclePhase.PLANNED
    for acceptance in phase_acceptances:
        if state is not LifecycleState.COMPLETE:
            raise GoalDeliveryLifecycleError(
                "phase acceptance requires completed task lifecycle"
            )
        if (
            acceptance.lifecycle_run_id != lifecycle_run_id
            or acceptance.plan_sha256 != plan_sha256
        ):
            raise GoalDeliveryLifecycleError("phase acceptance does not bind lifecycle")
        phase_index = _PHASE_ORDER.index(phase)
        if phase_index + 1 >= len(_PHASE_ORDER):
            raise GoalDeliveryLifecycleError("release-accepted is the terminal phase")
        if acceptance.phase is not _PHASE_ORDER[phase_index + 1]:
            raise GoalDeliveryLifecycleError(
                "phase acceptance must advance exactly one phase"
            )
        phase = acceptance.phase
    if user_result.phase is not phase:
        user_result = LifecycleUserResult(
            status_code=user_result.status_code,
            result_code=user_result.result_code,
            next_step_code=user_result.next_step_code,
            phase=phase,
        )
    return GoalDeliveryLifecycle(
        schema_version=P3G_SCHEMA_VERSION,
        lifecycle_run_id=lifecycle_run_id,
        plan_id=plan.plan_id,
        plan_sha256=plan_sha256,
        plan=plan,
        checkpoints=checkpoints,
        task_evidence=evidence,
        decisions=decisions,
        approvals=approvals,
        consolidations=consolidations,
        phase_acceptances=phase_acceptances,
        task_cursor=tuple(sorted(cursor, key=lambda item: item.task_id)),
        current_wave_index=current_wave_index,
        current_wave_task_ids=current_wave_task_ids,
        next_task_ids=next_task_ids,
        accepted_task_ids=tuple(sorted(accepted)),
        pending_task_ids=tuple(sorted(pending)),
        blocked_task_ids=tuple(sorted(blocked)),
        reason_codes=tuple(sorted(reasons)),
        state=state,
        phase=phase,
        user_result=user_result,
        execution_performed=False,
    )


def start_goal_delivery_lifecycle(
    plan_payload: bytes | bytearray | memoryview,
    *,
    lifecycle_run_id: str,
) -> GoalDeliveryLifecycle:
    """Start one pure lifecycle bound to exact canonical P3-F bytes."""

    try:
        plan = parse_autonomous_task_plan(plan_payload)
    except (AutonomousTaskOrchestrationError, TypeError, ValueError) as error:
        raise GoalDeliveryLifecycleError("P3-F plan is invalid") from error
    return _derive(
        lifecycle_run_id=_code(lifecycle_run_id, "lifecycle_run_id"),
        plan=plan,
        checkpoints=(),
    )


def _canonical_event_records(
    values: Sequence[object],
    *,
    label: str,
    record_type: type,
    key,
) -> tuple[object, ...]:
    if not isinstance(values, (tuple, list)) or len(values) > MAX_REFERENCES:
        raise GoalDeliveryLifecycleError(f"{label} must be a bounded sequence")
    records = tuple(values)
    if any(type(item) is not record_type for item in records):
        raise GoalDeliveryLifecycleError(f"{label} contains invalid records")
    ordered = tuple(sorted(records, key=key))
    identifiers = tuple(key(item) for item in ordered)
    if len(set(identifiers)) != len(identifiers):
        raise GoalDeliveryLifecycleError(f"{label} contains duplicate records")
    return ordered


def advance_goal_delivery_lifecycle(
    lifecycle: GoalDeliveryLifecycle,
    *,
    sequence: int,
    task_evidence: Sequence[TaskExecutionEvidence] = (),
    decisions: Sequence[LifecycleDecision] = (),
    approvals: Sequence[LifecycleApproval] = (),
    authorization_session: AuthorizationSession | None = None,
    consolidations: Sequence[TaskConsolidation] = (),
    phase_acceptances: Sequence[LifecyclePhaseAcceptance] = (),
) -> GoalDeliveryLifecycle:
    """Append one CAS checkpoint or return the run for an exact replay."""

    if type(lifecycle) is not GoalDeliveryLifecycle:
        raise TypeError("lifecycle must be an exact GoalDeliveryLifecycle")
    expected = _derive(
        lifecycle_run_id=lifecycle.lifecycle_run_id,
        plan=lifecycle.plan,
        checkpoints=lifecycle.checkpoints,
    )
    if expected != lifecycle:
        raise GoalDeliveryLifecycleError("lifecycle does not match recomputed checkpoints")
    if type(sequence) is not int or sequence < 1:
        raise GoalDeliveryLifecycleError("sequence must be a positive integer")
    evidence = _canonical_event_records(
        task_evidence,
        label="task_evidence",
        record_type=TaskExecutionEvidence,
        key=lambda item: item.task_id,
    )
    decision_records = _canonical_event_records(
        decisions,
        label="decisions",
        record_type=LifecycleDecision,
        key=lambda item: item.decision_id,
    )
    approval_records = _canonical_event_records(
        approvals,
        label="approvals",
        record_type=LifecycleApproval,
        key=lambda item: item.approval_id,
    )
    if authorization_session is not None:
        # A retry is checked against the state immediately before its checkpoint.
        # The current lifecycle can already have advanced to a later wave.
        session_lifecycle = lifecycle
        if sequence <= len(lifecycle.checkpoints):
            session_lifecycle = _derive(
                lifecycle_run_id=lifecycle.lifecycle_run_id,
                plan=lifecycle.plan,
                checkpoints=lifecycle.checkpoints[: sequence - 1],
            )
        session_approval_records = _session_approvals(
            session_lifecycle,
            authorization_session,
            exact_replay=sequence <= len(lifecycle.checkpoints),
        )
        approval_records = _canonical_event_records(
            approval_records + session_approval_records,
            label="approvals",
            record_type=LifecycleApproval,
            key=lambda item: item.approval_id,
        )
        by_task = {item.task_id: item for item in session_approval_records}
        evidence = tuple(
            replace(
                item,
                authorization_ref=by_task[item.task_id].approval_id,
            )
            if item.task_id in by_task and item.authorization_ref is None
            else item
            for item in evidence
        )
    consolidation_records = _canonical_event_records(
        consolidations,
        label="consolidations",
        record_type=TaskConsolidation,
        key=lambda item: item.task_id,
    )
    phase_records = _canonical_event_records(
        phase_acceptances,
        label="phase_acceptances",
        record_type=LifecyclePhaseAcceptance,
        key=lambda item: item.acceptance_id,
    )
    if not any((evidence, decision_records, approval_records, consolidation_records, phase_records)):
        raise GoalDeliveryLifecycleError("checkpoint must contain bounded evidence")
    event = _event_mapping(
        sequence=sequence,
        task_evidence=evidence,
        decisions=decision_records,
        approvals=approval_records,
        consolidations=consolidation_records,
        phase_acceptances=phase_records,
    )
    event_sha256 = hashlib.sha256(canonical_json_bytes(event)).hexdigest()
    if sequence <= len(lifecycle.checkpoints):
        prior = lifecycle.checkpoints[sequence - 1]
        if prior.event_sha256 == event_sha256:
            return lifecycle
        raise GoalDeliveryLifecycleError("checkpoint sequence replay does not match prior event")
    if sequence != len(lifecycle.checkpoints) + 1:
        raise GoalDeliveryLifecycleError("checkpoint sequence is stale or skips the CAS cursor")
    if lifecycle.state in (LifecycleState.BLOCK, LifecycleState.COMPLETE) and not phase_records:
        raise GoalDeliveryLifecycleError("terminal lifecycle accepts only phase evidence")
    if phase_records and lifecycle.state is not LifecycleState.COMPLETE:
        raise GoalDeliveryLifecycleError(
            "phase acceptance requires a previously completed task lifecycle"
        )
    if len(phase_records) > 1:
        raise GoalDeliveryLifecycleError("one checkpoint may advance only one phase")

    route_by_id = {item.task_id: item for item in lifecycle.plan.routes}
    current_ids = set(lifecycle.current_wave_task_ids)
    existing_evidence = {item.task_id for item in lifecycle.task_evidence}
    existing_decision_ids = {item.decision_id for item in lifecycle.decisions}
    existing_decision_tasks = {item.task_id for item in lifecycle.decisions}
    existing_approval_ids = {item.approval_id for item in lifecycle.approvals}
    existing_approval_tasks = {item.task_id for item in lifecycle.approvals}
    existing_consolidations = {item.task_id for item in lifecycle.consolidations}
    existing_phase_ids = {item.acceptance_id for item in lifecycle.phase_acceptances}
    evidence_ids = {item.task_id for item in evidence}
    if evidence_ids & existing_evidence:
        raise GoalDeliveryLifecycleError("task evidence is append-only and cannot be replaced")
    if evidence_ids - current_ids:
        raise GoalDeliveryLifecycleError("task evidence is outside the current dependency-closed wave")
    if any(item.decision_id in existing_decision_ids for item in decision_records):
        raise GoalDeliveryLifecycleError("decision identifier was already used")
    if any(item.task_id in existing_decision_tasks for item in decision_records):
        raise GoalDeliveryLifecycleError("task already has decision evidence")
    if any(item.approval_id in existing_approval_ids for item in approval_records):
        raise GoalDeliveryLifecycleError("approval identifier was already used")
    if any(item.task_id in existing_approval_tasks for item in approval_records):
        raise GoalDeliveryLifecycleError("task already has approval evidence")
    if any(item.task_id in existing_consolidations for item in consolidation_records):
        raise GoalDeliveryLifecycleError("task already has consolidation evidence")
    if any(item.acceptance_id in existing_phase_ids for item in phase_records):
        raise GoalDeliveryLifecycleError("phase acceptance identifier was already used")

    new_decision_by_task = {item.task_id: item for item in decision_records}
    new_approval_by_task = {item.task_id: item for item in approval_records}
    for item in decision_records:
        route = route_by_id.get(item.task_id)
        if route is None or item.task_id not in current_ids:
            raise GoalDeliveryLifecycleError("decision is outside the current wave")
        if route.classification is not AuthorizationClass.RECOMMEND:
            raise GoalDeliveryLifecycleError("decision can bind only a RECOMMEND task")
        if (
            item.lifecycle_run_id != lifecycle.lifecycle_run_id
            or item.plan_id != lifecycle.plan_id
            or item.plan_sha256 != lifecycle.plan_sha256
            or item.scope != _task_scope(lifecycle.plan, item.task_id)
        ):
            raise GoalDeliveryLifecycleError("decision binding does not match the lifecycle task")
    for item in approval_records:
        route = route_by_id.get(item.task_id)
        if route is None or item.task_id not in current_ids:
            raise GoalDeliveryLifecycleError("approval is outside the current wave")
        if route.classification is not AuthorizationClass.CONFIRM:
            raise GoalDeliveryLifecycleError("approval can bind only a CONFIRM task")
        if (
            item.lifecycle_run_id != lifecycle.lifecycle_run_id
            or item.plan_id != lifecycle.plan_id
            or item.plan_sha256 != lifecycle.plan_sha256
            or item.scope != _task_scope(lifecycle.plan, item.task_id)
        ):
            raise GoalDeliveryLifecycleError("approval binding does not match the lifecycle task")
    prior_decision_tasks = {item.task_id for item in lifecycle.decisions}
    prior_approval_tasks = {item.task_id for item in lifecycle.approvals}
    for item in evidence:
        route = route_by_id[item.task_id]
        bound_decision = next(
            (
                value
                for value in decision_records
                if value.task_id == item.task_id
            ),
            next(
                (
                    value
                    for value in lifecycle.decisions
                    if value.task_id == item.task_id
                ),
                None,
            ),
        )
        bound_approval = next(
            (
                value
                for value in approval_records
                if value.task_id == item.task_id
            ),
            next(
                (
                    value
                    for value in lifecycle.approvals
                    if value.task_id == item.task_id
                ),
                None,
            ),
        )
        if (
            route.classification is AuthorizationClass.RECOMMEND
            and item.task_id not in prior_decision_tasks
            and item.task_id not in new_decision_by_task
        ):
            raise GoalDeliveryLifecycleError("RECOMMEND task evidence requires bound decision evidence")
        if (
            route.classification is AuthorizationClass.RECOMMEND
            and bound_decision is not None
            and item.decision_ref != bound_decision.decision_id
        ):
            raise GoalDeliveryLifecycleError("RECOMMEND task evidence does not reference its decision")
        if (
            route.classification is AuthorizationClass.CONFIRM
            and item.task_id not in prior_approval_tasks
            and item.task_id not in new_approval_by_task
        ):
            raise GoalDeliveryLifecycleError("CONFIRM task evidence requires bound approval evidence")
        if (
            route.classification is AuthorizationClass.CONFIRM
            and bound_approval is not None
            and item.authorization_ref != bound_approval.approval_id
        ):
            raise GoalDeliveryLifecycleError("CONFIRM task evidence does not reference its approval")
    consolidation_ids = {item.task_id for item in consolidation_records}
    if consolidation_ids - evidence_ids:
        raise GoalDeliveryLifecycleError("consolidation must bind task evidence in the same checkpoint")
    successful_ids = {
        item.task_id
        for item in evidence
        if item.status is TaskResultStatus.PASS
        and item.review_verdict is ReviewVerdict.ACCEPT
        and not _evidence_reasons(route_by_id[item.task_id], item)
    }
    if successful_ids - consolidation_ids:
        raise GoalDeliveryLifecycleError("successful task evidence requires consolidation evidence")

    if phase_records:
        phase_index = _PHASE_ORDER.index(lifecycle.phase)
        for record in phase_records:
            if phase_index + 1 >= len(_PHASE_ORDER):
                raise GoalDeliveryLifecycleError("release-accepted is the terminal phase")
            if (
                record.lifecycle_run_id != lifecycle.lifecycle_run_id
                or record.plan_sha256 != lifecycle.plan_sha256
            ):
                raise GoalDeliveryLifecycleError("phase acceptance does not bind lifecycle")
            if record.phase is not _PHASE_ORDER[phase_index + 1]:
                raise GoalDeliveryLifecycleError("phase acceptance must advance exactly one phase")
            phase_index += 1

    previous = (
        lifecycle.checkpoints[-1].checkpoint_sha256
        if lifecycle.checkpoints
        else None
    )
    checkpoint = LifecycleCheckpoint(
        sequence=sequence,
        previous_checkpoint_sha256=previous,
        event_sha256=event_sha256,
        task_evidence=evidence,
        decisions=decision_records,
        approvals=approval_records,
        consolidations=consolidation_records,
        phase_acceptances=phase_records,
        checkpoint_sha256=_checkpoint_digest(
            sequence=sequence,
            previous=previous,
            event_sha256=event_sha256,
        ),
    )
    updated = _derive(
        lifecycle_run_id=lifecycle.lifecycle_run_id,
        plan=lifecycle.plan,
        checkpoints=lifecycle.checkpoints + (checkpoint,),
    )
    return updated


def _checkpoint_mapping(value: LifecycleCheckpoint) -> dict[str, object]:
    mapping = _event_mapping(
        sequence=value.sequence,
        task_evidence=value.task_evidence,
        decisions=value.decisions,
        approvals=value.approvals,
        consolidations=value.consolidations,
        phase_acceptances=value.phase_acceptances,
    )
    mapping.update(
        {
            "checkpoint_sha256": value.checkpoint_sha256,
            "event_sha256": value.event_sha256,
            "previous_checkpoint_sha256": value.previous_checkpoint_sha256,
        }
    )
    return mapping


def _lifecycle_mapping(value: GoalDeliveryLifecycle) -> dict[str, object]:
    return {
        "accepted_task_ids": list(value.accepted_task_ids),
        "approvals": [_approval_mapping(item) for item in value.approvals],
        "blocked_task_ids": list(value.blocked_task_ids),
        "checkpoints": [_checkpoint_mapping(item) for item in value.checkpoints],
        "consolidations": [
            _consolidation_mapping(item) for item in value.consolidations
        ],
        "current_wave_index": value.current_wave_index,
        "current_wave_task_ids": list(value.current_wave_task_ids),
        "decisions": [_decision_mapping(item) for item in value.decisions],
        "execution_performed": value.execution_performed,
        "lifecycle_run_id": value.lifecycle_run_id,
        "next_task_ids": list(value.next_task_ids),
        "pending_task_ids": list(value.pending_task_ids),
        "phase": value.phase.value,
        "phase_acceptances": [
            _phase_acceptance_mapping(item) for item in value.phase_acceptances
        ],
        "plan": json.loads(render_autonomous_task_plan(value.plan)),
        "plan_id": value.plan_id,
        "plan_sha256": value.plan_sha256,
        "reason_codes": list(value.reason_codes),
        "schema_version": value.schema_version,
        "state": value.state.value,
        "task_cursor": [
            {
                "state": item.state.value,
                "task_id": item.task_id,
                "wave_index": item.wave_index,
            }
            for item in value.task_cursor
        ],
        "task_evidence": [_evidence_mapping(item) for item in value.task_evidence],
        "user_result": {
            "next_step_code": value.user_result.next_step_code,
            "phase": value.user_result.phase.value,
            "result_code": value.user_result.result_code,
            "status_code": value.user_result.status_code,
        },
    }


def render_goal_delivery_lifecycle(value: GoalDeliveryLifecycle) -> bytes:
    """Render canonical P3-G state after full checkpoint recomputation."""

    if type(value) is not GoalDeliveryLifecycle:
        raise TypeError("value must be an exact GoalDeliveryLifecycle")
    expected = _derive(
        lifecycle_run_id=value.lifecycle_run_id,
        plan=value.plan,
        checkpoints=value.checkpoints,
    )
    if expected != value:
        raise GoalDeliveryLifecycleError("lifecycle does not match recomputed checkpoints")
    replayed = start_goal_delivery_lifecycle(
        render_autonomous_task_plan(value.plan),
        lifecycle_run_id=value.lifecycle_run_id,
    )
    for checkpoint in value.checkpoints:
        replayed = advance_goal_delivery_lifecycle(
            replayed,
            sequence=checkpoint.sequence,
            task_evidence=checkpoint.task_evidence,
            decisions=checkpoint.decisions,
            approvals=checkpoint.approvals,
            consolidations=checkpoint.consolidations,
            phase_acceptances=checkpoint.phase_acceptances,
        )
        if replayed.checkpoints[-1] != checkpoint:
            raise GoalDeliveryLifecycleError(
                "lifecycle checkpoint is not the canonical replay result"
            )
    if replayed != value:
        raise GoalDeliveryLifecycleError("lifecycle state is not the canonical replay result")
    try:
        rendered = canonical_json_bytes(_lifecycle_mapping(value))
    except SchemaError as error:
        raise GoalDeliveryLifecycleError(f"lifecycle cannot be encoded: {error}") from error
    if len(rendered) > MAX_GOAL_DELIVERY_LIFECYCLE_BYTES:
        raise GoalDeliveryLifecycleError("rendered lifecycle exceeds its byte bound")
    return rendered


def _closed(value: object, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GoalDeliveryLifecycleError(f"{label} must be an object")
    keys = set(value)
    if any(type(key) is not str for key in keys):
        raise GoalDeliveryLifecycleError(f"{label} keys must be strings")
    unknown = sorted(keys - fields)
    missing = sorted(fields - keys)
    if unknown:
        raise GoalDeliveryLifecycleError(
            f"{label} contains unknown fields: {', '.join(unknown)}"
        )
    if missing:
        raise GoalDeliveryLifecycleError(
            f"{label} is missing fields: {', '.join(missing)}"
        )
    return value


def _array(value: object, label: str, maximum: int) -> tuple[object, ...]:
    if type(value) is not list or len(value) > maximum:
        raise GoalDeliveryLifecycleError(f"{label} must be a bounded array")
    return tuple(value)


def _parse_refs(value: object, label: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    refs = tuple(
        _reference(item, f"{label}[{index}]")
        for index, item in enumerate(_array(value, label, MAX_REFERENCES))
    )
    if not allow_empty and not refs:
        raise GoalDeliveryLifecycleError(f"{label} must not be empty")
    return refs


def _parse_evidence(value: object, label: str) -> TaskExecutionEvidence:
    item = _closed(
        value,
        frozenset(
            {
                "acceptance_refs",
                "authorization_ref",
                "decision_ref",
                "executor_id",
                "gate_refs",
                "output_refs",
                "review_verdict",
                "reviewer_id",
                "rollback_ref",
                "status",
                "task_id",
            }
        ),
        label,
    )
    decision_ref = item["decision_ref"]
    authorization_ref = item["authorization_ref"]
    return TaskExecutionEvidence(
        task_id=_code(item["task_id"], f"{label}.task_id"),
        executor_id=_code(item["executor_id"], f"{label}.executor_id"),
        status=_enum(item["status"], TaskResultStatus, f"{label}.status"),
        output_refs=_parse_refs(item["output_refs"], f"{label}.output_refs", allow_empty=False),
        gate_refs=_parse_refs(item["gate_refs"], f"{label}.gate_refs"),
        acceptance_refs=_parse_refs(
            item["acceptance_refs"], f"{label}.acceptance_refs", allow_empty=False
        ),
        rollback_ref=_reference(item["rollback_ref"], f"{label}.rollback_ref"),
        reviewer_id=_code(item["reviewer_id"], f"{label}.reviewer_id"),
        review_verdict=_enum(
            item["review_verdict"], ReviewVerdict, f"{label}.review_verdict"
        ),
        decision_ref=(
            None
            if decision_ref is None
            else _reference(decision_ref, f"{label}.decision_ref")
        ),
        authorization_ref=(
            None
            if authorization_ref is None
            else _reference(authorization_ref, f"{label}.authorization_ref")
        ),
    )


def _parse_decision(value: object, label: str) -> LifecycleDecision:
    item = _closed(
        value,
        frozenset(
            {
                "decision_id",
                "lifecycle_run_id",
                "outcome",
                "plan_id",
                "plan_sha256",
                "scope",
                "selected_option",
                "task_id",
                "timestamp_utc",
            }
        ),
        label,
    )
    return LifecycleDecision(
        decision_id=_code(item["decision_id"], f"{label}.decision_id"),
        lifecycle_run_id=_code(item["lifecycle_run_id"], f"{label}.lifecycle_run_id"),
        plan_id=_code(item["plan_id"], f"{label}.plan_id"),
        plan_sha256=_digest(item["plan_sha256"], f"{label}.plan_sha256"),
        task_id=_code(item["task_id"], f"{label}.task_id"),
        scope=_parse_refs(item["scope"], f"{label}.scope", allow_empty=False),
        outcome=_enum(item["outcome"], DecisionOutcome, f"{label}.outcome"),
        selected_option=_code(item["selected_option"], f"{label}.selected_option"),
        timestamp_utc=_timestamp(item["timestamp_utc"], f"{label}.timestamp_utc"),
    )


def _parse_approval(value: object, label: str) -> LifecycleApproval:
    item = _closed(
        value,
        frozenset(
            {
                "actor",
                "approval_id",
                "lifecycle_run_id",
                "plan_id",
                "plan_sha256",
                "role",
                "scope",
                "task_id",
                "timestamp_utc",
                "transaction_id",
            }
        ),
        label,
    )
    return LifecycleApproval(
        approval_id=_code(item["approval_id"], f"{label}.approval_id"),
        transaction_id=_code(item["transaction_id"], f"{label}.transaction_id"),
        lifecycle_run_id=_code(item["lifecycle_run_id"], f"{label}.lifecycle_run_id"),
        plan_id=_code(item["plan_id"], f"{label}.plan_id"),
        plan_sha256=_digest(item["plan_sha256"], f"{label}.plan_sha256"),
        task_id=_code(item["task_id"], f"{label}.task_id"),
        scope=_parse_refs(item["scope"], f"{label}.scope", allow_empty=False),
        actor=_scalar(item["actor"], f"{label}.actor", 128),
        role=_code(item["role"], f"{label}.role"),
        timestamp_utc=_timestamp(item["timestamp_utc"], f"{label}.timestamp_utc"),
    )


def _parse_consolidation(value: object, label: str) -> TaskConsolidation:
    item = _closed(value, frozenset({"consolidation_ref", "task_id"}), label)
    return TaskConsolidation(
        task_id=_code(item["task_id"], f"{label}.task_id"),
        consolidation_ref=_reference(
            item["consolidation_ref"], f"{label}.consolidation_ref"
        ),
    )


def _parse_phase_acceptance(value: object, label: str) -> LifecyclePhaseAcceptance:
    item = _closed(
        value,
        frozenset(
            {
                "acceptance_id",
                "actor",
                "evidence_domain",
                "evidence_refs",
                "lifecycle_run_id",
                "phase",
                "plan_sha256",
                "timestamp_utc",
            }
        ),
        label,
    )
    return LifecyclePhaseAcceptance(
        acceptance_id=_code(item["acceptance_id"], f"{label}.acceptance_id"),
        lifecycle_run_id=_code(item["lifecycle_run_id"], f"{label}.lifecycle_run_id"),
        plan_sha256=_digest(item["plan_sha256"], f"{label}.plan_sha256"),
        phase=_enum(item["phase"], LifecyclePhase, f"{label}.phase"),
        evidence_domain=_enum(
            item["evidence_domain"], LifecyclePhase, f"{label}.evidence_domain"
        ),
        evidence_refs=_parse_refs(
            item["evidence_refs"], f"{label}.evidence_refs", allow_empty=False
        ),
        actor=_scalar(item["actor"], f"{label}.actor", 128),
        timestamp_utc=_timestamp(item["timestamp_utc"], f"{label}.timestamp_utc"),
    )


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise GoalDeliveryLifecycleError("lifecycle contains duplicate object keys")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise GoalDeliveryLifecycleError(
        f"lifecycle contains unsupported JSON constant: {value}"
    )


def parse_goal_delivery_lifecycle(
    payload: bytes | bytearray | memoryview,
) -> GoalDeliveryLifecycle:
    """Parse canonical P3-G JSON by replaying every checkpoint."""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise GoalDeliveryLifecycleError("lifecycle payload must be bytes")
    raw = bytes(payload)
    if not raw or len(raw) > MAX_GOAL_DELIVERY_LIFECYCLE_BYTES:
        raise GoalDeliveryLifecycleError("lifecycle payload must use bounded non-empty bytes")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except GoalDeliveryLifecycleError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError) as error:
        raise GoalDeliveryLifecycleError("lifecycle payload is not valid UTF-8 JSON") from error
    item = _closed(
        value,
        frozenset(
            {
                "accepted_task_ids",
                "approvals",
                "blocked_task_ids",
                "checkpoints",
                "consolidations",
                "current_wave_index",
                "current_wave_task_ids",
                "decisions",
                "execution_performed",
                "lifecycle_run_id",
                "next_task_ids",
                "pending_task_ids",
                "phase",
                "phase_acceptances",
                "plan",
                "plan_id",
                "plan_sha256",
                "reason_codes",
                "schema_version",
                "state",
                "task_cursor",
                "task_evidence",
                "user_result",
            }
        ),
        "lifecycle",
    )
    try:
        plan = parse_autonomous_task_plan(canonical_json_bytes(item["plan"]))
    except (AutonomousTaskOrchestrationError, SchemaError, TypeError, ValueError) as error:
        raise GoalDeliveryLifecycleError("embedded P3-F plan is invalid") from error
    lifecycle = start_goal_delivery_lifecycle(
        render_autonomous_task_plan(plan),
        lifecycle_run_id=_code(item["lifecycle_run_id"], "lifecycle.lifecycle_run_id"),
    )
    checkpoints = _array(item["checkpoints"], "lifecycle.checkpoints", MAX_CHECKPOINTS)
    for index, record in enumerate(checkpoints):
        label = f"lifecycle.checkpoints[{index}]"
        checkpoint = _closed(
            record,
            frozenset(
                {
                    "approvals",
                    "checkpoint_sha256",
                    "consolidations",
                    "decisions",
                    "event_sha256",
                    "phase_acceptances",
                    "previous_checkpoint_sha256",
                    "sequence",
                    "task_evidence",
                }
            ),
            label,
        )
        sequence = checkpoint["sequence"]
        if type(sequence) is not int:
            raise GoalDeliveryLifecycleError(f"{label}.sequence must be an integer")
        evidence = tuple(
            _parse_evidence(value, f"{label}.task_evidence[{offset}]")
            for offset, value in enumerate(
                _array(
                    checkpoint["task_evidence"],
                    f"{label}.task_evidence",
                    MAX_REFERENCES,
                )
            )
        )
        decisions = tuple(
            _parse_decision(value, f"{label}.decisions[{offset}]")
            for offset, value in enumerate(
                _array(checkpoint["decisions"], f"{label}.decisions", MAX_REFERENCES)
            )
        )
        approvals = tuple(
            _parse_approval(value, f"{label}.approvals[{offset}]")
            for offset, value in enumerate(
                _array(checkpoint["approvals"], f"{label}.approvals", MAX_REFERENCES)
            )
        )
        consolidations = tuple(
            _parse_consolidation(value, f"{label}.consolidations[{offset}]")
            for offset, value in enumerate(
                _array(
                    checkpoint["consolidations"],
                    f"{label}.consolidations",
                    MAX_REFERENCES,
                )
            )
        )
        phase_acceptances = tuple(
            _parse_phase_acceptance(value, f"{label}.phase_acceptances[{offset}]")
            for offset, value in enumerate(
                _array(
                    checkpoint["phase_acceptances"],
                    f"{label}.phase_acceptances",
                    MAX_REFERENCES,
                )
            )
        )
        lifecycle = advance_goal_delivery_lifecycle(
            lifecycle,
            sequence=sequence,
            task_evidence=evidence,
            decisions=decisions,
            approvals=approvals,
            consolidations=consolidations,
            phase_acceptances=phase_acceptances,
        )
        actual = lifecycle.checkpoints[-1]
        if (
            actual.event_sha256 != checkpoint["event_sha256"]
            or actual.checkpoint_sha256 != checkpoint["checkpoint_sha256"]
            or actual.previous_checkpoint_sha256
            != checkpoint["previous_checkpoint_sha256"]
        ):
            raise GoalDeliveryLifecycleError("checkpoint digest chain was tampered")
    if render_goal_delivery_lifecycle(lifecycle) != raw:
        raise GoalDeliveryLifecycleError("lifecycle JSON is not canonical or was tampered")
    return lifecycle


def lifecycle_user_result(value: GoalDeliveryLifecycle) -> dict[str, str]:
    """Return only the compact ordinary-user result and next step."""

    if type(value) is not GoalDeliveryLifecycle:
        raise TypeError("value must be an exact GoalDeliveryLifecycle")
    render_goal_delivery_lifecycle(value)
    return {
        "status": value.user_result.status_code,
        "result": value.user_result.result_code,
        "next_step": value.user_result.next_step_code,
        "phase": value.phase.value,
    }


__all__ = [
    "P3G_SCHEMA_VERSION",
    "MAX_GOAL_DELIVERY_LIFECYCLE_BYTES",
    "MAX_CHECKPOINTS",
    "GoalDeliveryLifecycleError",
    "LifecycleState",
    "LifecycleTaskState",
    "LifecyclePhase",
    "DecisionOutcome",
    "LifecycleDecision",
    "LifecycleApproval",
    "TaskConsolidation",
    "LifecyclePhaseAcceptance",
    "LifecycleTaskCursor",
    "LifecycleUserResult",
    "LifecycleCheckpoint",
    "GoalDeliveryLifecycle",
    "start_goal_delivery_lifecycle",
    "advance_goal_delivery_lifecycle",
    "render_goal_delivery_lifecycle",
    "parse_goal_delivery_lifecycle",
    "lifecycle_user_result",
]
