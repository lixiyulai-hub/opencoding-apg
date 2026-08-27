"""Pure, source-bound progress projections over P3-G lifecycle facts.

This module never executes work, changes lifecycle state, or infers delivery
acceptance from task activity.  It only turns a canonical
``GoalDeliveryLifecycle`` and an optional explicit progress definition into a
deterministic status snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any, Mapping
import unicodedata

from .autonomous_task_orchestration import TaskResultStatus
from .goal_delivery_lifecycle import (
    GoalDeliveryLifecycle,
    LifecyclePhase,
    LifecycleState,
    render_goal_delivery_lifecycle,
)
from .program_progress import (
    ProgramProgressSnapshot,
    render_program_status_lines,
)
from .storage import canonical_json_bytes


PROGRESS_DEFINITION_SCHEMA_VERSION = "1.0"
PROGRESS_SNAPSHOT_SCHEMA_VERSION = "1.0"
MAX_PROGRESS_DEFINITION_BYTES = 64 * 1024
MAX_PROGRESS_SNAPSHOT_BYTES = 128 * 1024
MAX_PROGRESS_TASKS = 64
MAX_NEXT_ACTIONS = 5
MAX_WEIGHT = 1_000_000
MAX_TOTAL_WEIGHT = 10_000_000
MAX_BASIS_POINTS = 10_000

_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")
_SENSITIVE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}|\bghp_[A-Za-z0-9]{8,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.|"
    r"\bbearer\s+[A-Za-z0-9._~+/-]{8,}|"
    r"\b(?:api[_-]?key|token|password|secret)\s*[:=]|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
_PHASE_ORDER = tuple(LifecyclePhase)


class ProgressProjectionError(ValueError):
    """Raised when a progress projection is malformed or not source-bound."""


class ProgressScopeStatus(str, Enum):
    COMPUTABLE = "computable"
    NOT_COMPUTABLE = "not-computable"


class DeliveryState(str, Enum):
    NOT_COMPUTABLE = "not-computable"
    WORK_IN_PROGRESS = "work-in-progress"
    BLOCKED = "blocked"
    TARGET_PHASE_PENDING = "target-phase-pending"
    TARGET_REACHED = "target-reached"


class GateHealth(str, Enum):
    NOT_REQUIRED = "not-required"
    EVIDENCE_PENDING = "evidence-pending"
    EVIDENCE_ACCEPTED = "evidence-accepted"
    EVIDENCE_BLOCKED = "evidence-blocked"


def _scalar(value: object, label: str, maximum: int = 240) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ProgressProjectionError(f"{label} must be bounded non-empty text")
    if value != unicodedata.normalize("NFC", value):
        raise ProgressProjectionError(f"{label} must use NFC Unicode")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ProgressProjectionError(f"{label} contains control characters")
    if _SENSITIVE.search(value):
        raise ProgressProjectionError(f"{label} contains a sensitive-value pattern")
    return value


def _code(value: object, label: str) -> str:
    text = _scalar(value, label, 128)
    if not _CODE.fullmatch(text):
        raise ProgressProjectionError(f"{label} must be a bounded stable code")
    return text


def _optional_code(value: object, label: str) -> str | None:
    return None if value is None else _code(value, label)


def _digest(value: object, label: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise ProgressProjectionError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _optional_digest(value: object, label: str) -> str | None:
    return None if value is None else _digest(value, label)


def _path(value: object, label: str) -> str:
    text = _scalar(value, label, 240)
    if (
        "\\" in text
        or text.startswith("/")
        or _WINDOWS_DRIVE.match(text)
        or ":" in text
        or "?" in text
        or "#" in text
    ):
        raise ProgressProjectionError(f"{label} must be a contained relative path")
    parts = text.split("/")
    if (
        any(part in ("", ".", "..") for part in parts)
        or any(part.endswith((".", " ")) for part in parts)
        or tuple(PurePosixPath(text).parts) != tuple(parts)
    ):
        raise ProgressProjectionError(f"{label} must be a contained relative path")
    return text


def _optional_path(value: object, label: str) -> str | None:
    return None if value is None else _path(value, label)


def _tuple(value: object, label: str, maximum: int) -> tuple[object, ...]:
    if type(value) is not tuple or len(value) > maximum:
        raise ProgressProjectionError(f"{label} must be a bounded immutable tuple")
    return value


def _ordered_codes(
    value: object,
    label: str,
    *,
    maximum: int = MAX_PROGRESS_TASKS,
) -> tuple[str, ...]:
    items = tuple(
        _code(item, f"{label}[{index}]")
        for index, item in enumerate(_tuple(value, label, maximum))
    )
    if len(set(items)) != len(items):
        raise ProgressProjectionError(f"{label} must contain unique codes")
    return items


def _canonical_codes(
    value: object,
    label: str,
    *,
    maximum: int = MAX_PROGRESS_TASKS,
) -> tuple[str, ...]:
    items = _ordered_codes(value, label, maximum=maximum)
    if items != tuple(sorted(items)):
        raise ProgressProjectionError(f"{label} must use canonical unique order")
    return items


def _phase(value: object, label: str) -> LifecyclePhase:
    if type(value) is not str:
        raise ProgressProjectionError(f"{label} must be a string delivery phase")
    try:
        return LifecyclePhase(value)
    except ValueError as error:
        raise ProgressProjectionError(f"{label} has an unsupported delivery phase") from error


def _optional_phase(value: object, label: str) -> LifecyclePhase | None:
    return None if value is None else _phase(value, label)


def _phase_tuple(value: object, label: str) -> tuple[LifecyclePhase, ...]:
    phases = tuple(
        (
            item
            if type(item) is LifecyclePhase
            else _phase(item, f"{label}[{index}]")
        )
        for index, item in enumerate(_tuple(value, label, len(_PHASE_ORDER)))
    )
    expected = tuple(sorted(set(phases), key=_PHASE_ORDER.index))
    if phases != expected:
        raise ProgressProjectionError(
            f"{label} must use canonical unique delivery-phase order"
        )
    return phases


def _basis_points(numerator: int, denominator: int) -> int:
    if type(numerator) is not int or type(denominator) is not int:
        raise ProgressProjectionError("progress weights must be exact integers")
    if numerator < 0 or denominator <= 0 or numerator > denominator:
        raise ProgressProjectionError("progress weights are outside the declared scope")
    return numerator * MAX_BASIS_POINTS // denominator


def _validate_basis_points(value: object, label: str, *, required: bool) -> int | None:
    if value is None:
        if required:
            raise ProgressProjectionError(f"{label} is required for computable progress")
        return None
    if type(value) is not int or not 0 <= value <= MAX_BASIS_POINTS:
        raise ProgressProjectionError(f"{label} must be an integer basis-point value")
    return value


@dataclass(frozen=True)
class ProgressTaskWeight:
    task_id: str
    weight: int

    def __post_init__(self) -> None:
        if type(self) is not ProgressTaskWeight:
            raise ProgressProjectionError("ProgressTaskWeight subclasses are not accepted")
        _code(self.task_id, "task_weight.task_id")
        if type(self.weight) is not int or not 1 <= self.weight <= MAX_WEIGHT:
            raise ProgressProjectionError("task_weight.weight must be a positive integer")


@dataclass(frozen=True)
class ProgressDefinition:
    """Optional explicit scope and denominator for one lifecycle progress view.

    ``None`` fields are intentionally permitted so a repository may retain an
    incomplete definition.  Such a definition renders canonically but produces
    ``not-computable`` rather than an inferred percentage.
    """

    schema_version: str
    definition_id: str
    lifecycle_ref: str | None
    lifecycle_run_id: str | None
    plan_id: str | None
    plan_sha256: str | None
    task_weights: tuple[ProgressTaskWeight, ...]
    total_weight: int | None
    target_delivery_phase: LifecyclePhase | None
    out_of_scope_phases: tuple[LifecyclePhase, ...]
    program_roadmap_ref: str | None = None

    def __post_init__(self) -> None:
        if type(self) is not ProgressDefinition:
            raise ProgressProjectionError("ProgressDefinition subclasses are not accepted")
        if self.schema_version != PROGRESS_DEFINITION_SCHEMA_VERSION:
            raise ProgressProjectionError("unsupported progress-definition schema_version")
        _code(self.definition_id, "definition_id")
        _optional_path(self.lifecycle_ref, "lifecycle_ref")
        _optional_code(self.lifecycle_run_id, "lifecycle_run_id")
        _optional_code(self.plan_id, "plan_id")
        _optional_digest(self.plan_sha256, "plan_sha256")
        weights = _tuple(self.task_weights, "task_weights", MAX_PROGRESS_TASKS)
        if any(type(item) is not ProgressTaskWeight for item in weights):
            raise ProgressProjectionError("task_weights contain invalid records")
        identifiers = tuple(item.task_id for item in weights)
        if identifiers != tuple(sorted(set(identifiers))):
            raise ProgressProjectionError("task_weights must use canonical unique task IDs")
        if self.total_weight is not None and (
            type(self.total_weight) is not int
            or self.total_weight < 0
            or self.total_weight > MAX_TOTAL_WEIGHT
        ):
            raise ProgressProjectionError("total_weight must be a bounded non-negative integer")
        if self.target_delivery_phase is not None and type(self.target_delivery_phase) is not LifecyclePhase:
            raise ProgressProjectionError(
                "target_delivery_phase must be an exact LifecyclePhase or null"
            )
        phases = _phase_tuple(self.out_of_scope_phases, "out_of_scope_phases")
        _optional_path(self.program_roadmap_ref, "program_roadmap_ref")
        if self.target_delivery_phase is None and phases:
            raise ProgressProjectionError(
                "out_of_scope_phases requires a target_delivery_phase"
            )
        if self.target_delivery_phase is not None:
            target_index = _PHASE_ORDER.index(self.target_delivery_phase)
            if any(_PHASE_ORDER.index(item) <= target_index for item in phases):
                raise ProgressProjectionError(
                    "out_of_scope_phases must be later than target_delivery_phase"
                )


@dataclass(frozen=True)
class ProgressTaskCounts:
    total: int
    execution_evidenced: int
    execution_succeeded: int
    independently_verified: int
    pending: int
    blocked: int

    def __post_init__(self) -> None:
        if type(self) is not ProgressTaskCounts:
            raise ProgressProjectionError("ProgressTaskCounts subclasses are not accepted")
        values = (
            self.total,
            self.execution_evidenced,
            self.execution_succeeded,
            self.independently_verified,
            self.pending,
            self.blocked,
        )
        if any(type(item) is not int or item < 0 or item > MAX_PROGRESS_TASKS for item in values):
            raise ProgressProjectionError("task counts must be bounded non-negative integers")
        if self.execution_succeeded > self.execution_evidenced:
            raise ProgressProjectionError("successful executions exceed execution evidence")
        if self.independently_verified > self.execution_succeeded:
            raise ProgressProjectionError("verified tasks exceed successful executions")
        if self.pending + self.blocked + self.independently_verified != self.total:
            raise ProgressProjectionError("task counts do not cover the declared task scope")


@dataclass(frozen=True)
class ProgressSnapshot:
    """Recomputable read-only progress facts for one lifecycle source."""

    schema_version: str
    definition_id: str | None
    definition_sha256: str | None
    lifecycle_ref: str | None
    lifecycle_sha256: str
    lifecycle_run_id: str
    plan_id: str
    plan_sha256: str
    scope_status: ProgressScopeStatus
    execution_progress_basis_points: int | None
    verified_progress_basis_points: int | None
    current_stage: str
    current_stage_task_ids: tuple[str, ...]
    current_stage_execution_basis_points: int | None
    current_stage_verified_basis_points: int | None
    task_counts: ProgressTaskCounts
    target_delivery_phase: LifecyclePhase | None
    out_of_scope_phases: tuple[LifecyclePhase, ...]
    delivery_state: DeliveryState
    gate_health: GateHealth
    next_actions: tuple[str, ...]
    reason_codes: tuple[str, ...]
    execution_performed: bool

    def __post_init__(self) -> None:
        if type(self) is not ProgressSnapshot:
            raise ProgressProjectionError("ProgressSnapshot subclasses are not accepted")
        if self.schema_version != PROGRESS_SNAPSHOT_SCHEMA_VERSION:
            raise ProgressProjectionError("unsupported progress-snapshot schema_version")
        _optional_code(self.definition_id, "snapshot.definition_id")
        _optional_digest(self.definition_sha256, "snapshot.definition_sha256")
        if (self.definition_id is None) != (self.definition_sha256 is None):
            raise ProgressProjectionError(
                "snapshot definition ID and digest must both be present or absent"
            )
        _optional_path(self.lifecycle_ref, "snapshot.lifecycle_ref")
        _digest(self.lifecycle_sha256, "snapshot.lifecycle_sha256")
        _code(self.lifecycle_run_id, "snapshot.lifecycle_run_id")
        _code(self.plan_id, "snapshot.plan_id")
        _digest(self.plan_sha256, "snapshot.plan_sha256")
        if type(self.scope_status) is not ProgressScopeStatus:
            raise ProgressProjectionError("snapshot.scope_status must be ProgressScopeStatus")
        computable = self.scope_status is ProgressScopeStatus.COMPUTABLE
        _validate_basis_points(
            self.execution_progress_basis_points,
            "snapshot.execution_progress_basis_points",
            required=computable,
        )
        _validate_basis_points(
            self.verified_progress_basis_points,
            "snapshot.verified_progress_basis_points",
            required=computable,
        )
        stage_execution = _validate_basis_points(
            self.current_stage_execution_basis_points,
            "snapshot.current_stage_execution_basis_points",
            required=False,
        )
        stage_verified = _validate_basis_points(
            self.current_stage_verified_basis_points,
            "snapshot.current_stage_verified_basis_points",
            required=False,
        )
        if (stage_execution is None) != (stage_verified is None):
            raise ProgressProjectionError("current-stage basis points must both be present or absent")
        _code(self.current_stage, "snapshot.current_stage")
        _canonical_codes(
            self.current_stage_task_ids,
            "snapshot.current_stage_task_ids",
            maximum=MAX_PROGRESS_TASKS,
        )
        if type(self.task_counts) is not ProgressTaskCounts:
            raise ProgressProjectionError("snapshot.task_counts must be ProgressTaskCounts")
        if self.target_delivery_phase is not None and type(self.target_delivery_phase) is not LifecyclePhase:
            raise ProgressProjectionError(
                "snapshot.target_delivery_phase must be LifecyclePhase or null"
            )
        _phase_tuple(self.out_of_scope_phases, "snapshot.out_of_scope_phases")
        if type(self.delivery_state) is not DeliveryState:
            raise ProgressProjectionError("snapshot.delivery_state must be DeliveryState")
        if type(self.gate_health) is not GateHealth:
            raise ProgressProjectionError("snapshot.gate_health must be GateHealth")
        _ordered_codes(self.next_actions, "snapshot.next_actions", maximum=MAX_NEXT_ACTIONS)
        _canonical_codes(
            self.reason_codes,
            "snapshot.reason_codes",
            maximum=MAX_PROGRESS_TASKS * 4,
        )
        if type(self.execution_performed) is not bool or self.execution_performed:
            raise ProgressProjectionError("progress projection cannot claim executor activity")
        if not computable and any(
            item is not None
            for item in (
                self.execution_progress_basis_points,
                self.verified_progress_basis_points,
                self.current_stage_execution_basis_points,
                self.current_stage_verified_basis_points,
            )
        ):
            raise ProgressProjectionError(
                "not-computable progress must not contain basis-point values"
            )
        if not computable and self.delivery_state is not DeliveryState.NOT_COMPUTABLE:
            raise ProgressProjectionError(
                "not-computable progress must not claim a delivery state"
            )


def _weight_mapping(value: ProgressDefinition) -> dict[str, int]:
    return {item.task_id: item.weight for item in value.task_weights}


def _definition_reasons(
    definition: ProgressDefinition | None,
    lifecycle: GoalDeliveryLifecycle,
) -> tuple[str, ...]:
    if definition is None:
        return ("progress-definition-absent",)
    reasons: set[str] = set()
    if definition.lifecycle_ref is None:
        reasons.add("progress-source-ref-missing")
    if definition.lifecycle_run_id is None:
        reasons.add("progress-source-run-missing")
    elif definition.lifecycle_run_id != lifecycle.lifecycle_run_id:
        reasons.add("progress-source-run-drift")
    if definition.plan_id is None:
        reasons.add("progress-source-plan-id-missing")
    elif definition.plan_id != lifecycle.plan_id:
        reasons.add("progress-source-plan-id-drift")
    if definition.plan_sha256 is None:
        reasons.add("progress-source-plan-digest-missing")
    elif definition.plan_sha256 != lifecycle.plan_sha256:
        reasons.add("progress-source-plan-digest-drift")
    if not definition.task_weights:
        reasons.add("progress-task-weights-missing")
    actual_task_ids = tuple(sorted(route.task_id for route in lifecycle.plan.routes))
    weighted_task_ids = tuple(item.task_id for item in definition.task_weights)
    if weighted_task_ids != actual_task_ids:
        reasons.add("progress-task-weight-scope-mismatch")
    if definition.total_weight is None:
        reasons.add("progress-total-weight-missing")
    elif definition.total_weight <= 0:
        reasons.add("progress-total-weight-invalid")
    elif sum(item.weight for item in definition.task_weights) != definition.total_weight:
        reasons.add("progress-total-weight-mismatch")
    if definition.target_delivery_phase is None:
        reasons.add("progress-target-delivery-phase-missing")
    return tuple(sorted(reasons))


def _gate_health(lifecycle: GoalDeliveryLifecycle) -> GateHealth:
    gate_task_ids = {
        route.task_id for route in lifecycle.plan.routes if route.context.gate_ids
    }
    if not gate_task_ids:
        return GateHealth.NOT_REQUIRED
    if any(reason.startswith("gate-evidence-missing.") for reason in lifecycle.reason_codes):
        return GateHealth.EVIDENCE_BLOCKED
    if gate_task_ids.issubset(set(lifecycle.accepted_task_ids)):
        return GateHealth.EVIDENCE_ACCEPTED
    return GateHealth.EVIDENCE_PENDING


def _current_stage(lifecycle: GoalDeliveryLifecycle) -> tuple[str, tuple[str, ...]]:
    if lifecycle.state is LifecycleState.BLOCK:
        return "stage.blocked", ()
    if lifecycle.state is LifecycleState.COMPLETE:
        # A completed lifecycle has no pending wave, but its completed stage
        # is still a real denominator: render it at 100% instead of hiding the
        # current-stage percentage behind an empty task set.
        return "stage.completed-work", tuple(
            sorted(route.task_id for route in lifecycle.plan.routes)
        )
    if lifecycle.current_wave_index is None:
        return "stage.unknown", ()
    task_ids = tuple(sorted(lifecycle.current_wave_task_ids))
    route_by_id = {route.task_id: route for route in lifecycle.plan.routes}
    phases = tuple(sorted({route_by_id[task_id].phase for task_id in task_ids}))
    if len(phases) == 1:
        phase_stage = f"stage.{phases[0]}"
        if len(phase_stage) <= 128:
            phase_task_ids = tuple(
                sorted(
                    route.task_id
                    for route in lifecycle.plan.routes
                    if route.phase == phases[0]
                )
            )
            return phase_stage, phase_task_ids
    return (
        f"stage.wave-{lifecycle.current_wave_index}",
        task_ids,
    )


def _delivery_state(
    lifecycle: GoalDeliveryLifecycle,
    target: LifecyclePhase | None,
    *,
    computable: bool,
) -> DeliveryState:
    if not computable or target is None:
        return DeliveryState.NOT_COMPUTABLE
    if lifecycle.state is LifecycleState.BLOCK:
        return DeliveryState.BLOCKED
    if lifecycle.state is not LifecycleState.COMPLETE:
        return DeliveryState.WORK_IN_PROGRESS
    if _PHASE_ORDER.index(lifecycle.phase) >= _PHASE_ORDER.index(target):
        return DeliveryState.TARGET_REACHED
    return DeliveryState.TARGET_PHASE_PENDING


def _next_actions(
    lifecycle: GoalDeliveryLifecycle,
    target: LifecyclePhase | None,
) -> tuple[str, ...]:
    actions: list[str] = []
    if lifecycle.state is LifecycleState.COMPLETE and target is not None:
        current_index = _PHASE_ORDER.index(lifecycle.phase)
        target_index = _PHASE_ORDER.index(target)
        if current_index < target_index:
            actions.append(f"next.verify-{_PHASE_ORDER[current_index + 1].value}")
        else:
            actions.append(lifecycle.user_result.next_step_code)
    else:
        actions.append(lifecycle.user_result.next_step_code)
        actions.extend(lifecycle.next_task_ids)
    result: list[str] = []
    for action in actions:
        if action not in result:
            result.append(action)
        if len(result) == MAX_NEXT_ACTIONS:
            break
    return tuple(result)


def _task_counts(lifecycle: GoalDeliveryLifecycle) -> ProgressTaskCounts:
    successful = {
        item.task_id
        for item in lifecycle.task_evidence
        if item.status is TaskResultStatus.PASS
    }
    return ProgressTaskCounts(
        total=len(lifecycle.plan.routes),
        execution_evidenced=len(lifecycle.task_evidence),
        execution_succeeded=len(successful),
        independently_verified=len(lifecycle.accepted_task_ids),
        pending=len(lifecycle.pending_task_ids),
        blocked=len(lifecycle.blocked_task_ids),
    )


def recompute_progress_snapshot(
    definition: ProgressDefinition | None,
    lifecycle: GoalDeliveryLifecycle,
) -> ProgressSnapshot:
    """Derive one pure status snapshot from exact lifecycle source facts.

    A missing, incomplete, or drifted definition is a valid condition for this
    reporting API.  It yields a source-preserving ``not-computable`` snapshot
    rather than an estimated percentage.
    """

    if definition is not None and type(definition) is not ProgressDefinition:
        raise TypeError("definition must be an exact ProgressDefinition or None")
    if type(lifecycle) is not GoalDeliveryLifecycle:
        raise TypeError("lifecycle must be an exact GoalDeliveryLifecycle")
    lifecycle_bytes = render_goal_delivery_lifecycle(lifecycle)
    lifecycle_sha256 = hashlib.sha256(lifecycle_bytes).hexdigest()
    definition_reasons = _definition_reasons(definition, lifecycle)
    computable = not definition_reasons
    target = None if definition is None else definition.target_delivery_phase
    current_stage, current_stage_task_ids = _current_stage(lifecycle)
    execution_progress: int | None = None
    verified_progress: int | None = None
    stage_execution: int | None = None
    stage_verified: int | None = None
    if computable:
        assert definition is not None
        weights = _weight_mapping(definition)
        denominator = definition.total_weight
        assert denominator is not None
        successful = {
            item.task_id
            for item in lifecycle.task_evidence
            if item.status is TaskResultStatus.PASS
        }
        verified = set(lifecycle.accepted_task_ids)
        execution_progress = _basis_points(
            sum(weights[task_id] for task_id in successful), denominator
        )
        verified_progress = _basis_points(
            sum(weights[task_id] for task_id in verified), denominator
        )
        if current_stage_task_ids:
            stage_denominator = sum(weights[task_id] for task_id in current_stage_task_ids)
            stage_execution = _basis_points(
                sum(weights[task_id] for task_id in current_stage_task_ids if task_id in successful),
                stage_denominator,
            )
            stage_verified = _basis_points(
                sum(weights[task_id] for task_id in current_stage_task_ids if task_id in verified),
                stage_denominator,
            )
    reasons = set(lifecycle.reason_codes)
    reasons.update(definition_reasons)
    delivery_state = _delivery_state(lifecycle, target, computable=computable)
    if delivery_state is DeliveryState.TARGET_PHASE_PENDING:
        reasons.add("delivery-target-phase-pending")
    if delivery_state is DeliveryState.BLOCKED:
        reasons.add("lifecycle-blocked")
    definition_sha256 = (
        None
        if definition is None
        else hashlib.sha256(render_progress_definition(definition)).hexdigest()
    )
    return ProgressSnapshot(
        schema_version=PROGRESS_SNAPSHOT_SCHEMA_VERSION,
        definition_id=None if definition is None else definition.definition_id,
        definition_sha256=definition_sha256,
        lifecycle_ref=None if definition is None else definition.lifecycle_ref,
        lifecycle_sha256=lifecycle_sha256,
        lifecycle_run_id=lifecycle.lifecycle_run_id,
        plan_id=lifecycle.plan_id,
        plan_sha256=lifecycle.plan_sha256,
        scope_status=(
            ProgressScopeStatus.COMPUTABLE
            if computable
            else ProgressScopeStatus.NOT_COMPUTABLE
        ),
        execution_progress_basis_points=execution_progress,
        verified_progress_basis_points=verified_progress,
        current_stage=current_stage,
        current_stage_task_ids=current_stage_task_ids,
        current_stage_execution_basis_points=stage_execution,
        current_stage_verified_basis_points=stage_verified,
        task_counts=_task_counts(lifecycle),
        target_delivery_phase=target,
        out_of_scope_phases=() if definition is None else definition.out_of_scope_phases,
        delivery_state=delivery_state,
        gate_health=_gate_health(lifecycle),
        next_actions=_next_actions(lifecycle, target),
        reason_codes=tuple(sorted(reasons)),
        execution_performed=False,
    )


def _task_weight_mapping(value: ProgressTaskWeight) -> dict[str, object]:
    if type(value) is not ProgressTaskWeight:
        raise TypeError("value must be an exact ProgressTaskWeight")
    return {"task_id": value.task_id, "weight": value.weight}


def _definition_mapping(
    value: ProgressDefinition,
    *,
    include_program_roadmap_ref: bool = True,
) -> dict[str, object]:
    if type(value) is not ProgressDefinition:
        raise TypeError("value must be an exact ProgressDefinition")
    result: dict[str, object] = {
        "schema_version": value.schema_version,
        "definition_id": value.definition_id,
        "lifecycle_ref": value.lifecycle_ref,
        "lifecycle_run_id": value.lifecycle_run_id,
        "plan_id": value.plan_id,
        "plan_sha256": value.plan_sha256,
        "task_weights": [_task_weight_mapping(item) for item in value.task_weights],
        "total_weight": value.total_weight,
        "target_delivery_phase": (
            None if value.target_delivery_phase is None else value.target_delivery_phase.value
        ),
        "out_of_scope_phases": [item.value for item in value.out_of_scope_phases],
    }
    if include_program_roadmap_ref:
        result["program_roadmap_ref"] = value.program_roadmap_ref
    return result


def render_progress_definition(value: ProgressDefinition) -> bytes:
    """Render an exact canonical progress-definition JSON document."""

    return canonical_json_bytes(_definition_mapping(value))


def _task_counts_mapping(value: ProgressTaskCounts) -> dict[str, int]:
    if type(value) is not ProgressTaskCounts:
        raise TypeError("value must be an exact ProgressTaskCounts")
    return {
        "total": value.total,
        "execution_evidenced": value.execution_evidenced,
        "execution_succeeded": value.execution_succeeded,
        "independently_verified": value.independently_verified,
        "pending": value.pending,
        "blocked": value.blocked,
    }


def _snapshot_mapping(value: ProgressSnapshot) -> dict[str, object]:
    if type(value) is not ProgressSnapshot:
        raise TypeError("value must be an exact ProgressSnapshot")
    return {
        "schema_version": value.schema_version,
        "definition_id": value.definition_id,
        "definition_sha256": value.definition_sha256,
        "lifecycle_ref": value.lifecycle_ref,
        "lifecycle_sha256": value.lifecycle_sha256,
        "lifecycle_run_id": value.lifecycle_run_id,
        "plan_id": value.plan_id,
        "plan_sha256": value.plan_sha256,
        "scope_status": value.scope_status.value,
        "execution_progress_basis_points": value.execution_progress_basis_points,
        "verified_progress_basis_points": value.verified_progress_basis_points,
        "current_stage": value.current_stage,
        "current_stage_task_ids": list(value.current_stage_task_ids),
        "current_stage_execution_basis_points": value.current_stage_execution_basis_points,
        "current_stage_verified_basis_points": value.current_stage_verified_basis_points,
        "task_counts": _task_counts_mapping(value.task_counts),
        "target_delivery_phase": (
            None if value.target_delivery_phase is None else value.target_delivery_phase.value
        ),
        "out_of_scope_phases": [item.value for item in value.out_of_scope_phases],
        "delivery_state": value.delivery_state.value,
        "gate_health": value.gate_health.value,
        "next_actions": list(value.next_actions),
        "reason_codes": list(value.reason_codes),
        "execution_performed": value.execution_performed,
    }


def render_progress_snapshot(value: ProgressSnapshot) -> bytes:
    """Render an exact canonical progress-snapshot JSON document."""

    return canonical_json_bytes(_snapshot_mapping(value))


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProgressProjectionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProgressProjectionError(f"unsupported JSON constant: {value}")


def _payload(value: str | bytes, *, maximum: int) -> tuple[Mapping[str, Any], bytes]:
    if type(value) is str:
        try:
            raw = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ProgressProjectionError("JSON text must be UTF-8 encodable") from error
    elif type(value) is bytes:
        raw = value
    else:
        raise TypeError("JSON value must be exact str or bytes")
    if not raw or len(raw) > maximum:
        raise ProgressProjectionError("JSON bytes are empty or exceed the fixed limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProgressProjectionError("JSON bytes must be UTF-8") from error
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, ProgressProjectionError):
            raise
        raise ProgressProjectionError("progress JSON is invalid") from error
    if type(parsed) is not dict:
        raise ProgressProjectionError("progress JSON root must be an object")
    return parsed, raw


def _closed(value: object, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise ProgressProjectionError(f"{label} must be an object")
    actual = frozenset(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise ProgressProjectionError(f"{label} has an invalid closed shape: {'; '.join(details)}")
    return value


def _array(value: object, label: str, maximum: int) -> tuple[object, ...]:
    if type(value) is not list or len(value) > maximum:
        raise ProgressProjectionError(f"{label} must be a bounded JSON array")
    return tuple(value)


def _parse_task_weight(value: object, label: str) -> ProgressTaskWeight:
    item = _closed(value, frozenset({"task_id", "weight"}), label)
    return ProgressTaskWeight(
        task_id=_code(item["task_id"], f"{label}.task_id"),
        weight=item["weight"],
    )


def parse_progress_definition(value: str | bytes) -> ProgressDefinition:
    """Parse canonical definition bytes and reject unknown fields or driftable form."""

    mapping, raw = _payload(value, maximum=MAX_PROGRESS_DEFINITION_BYTES)
    legacy_fields = frozenset(
        {
            "schema_version",
            "definition_id",
            "lifecycle_ref",
            "lifecycle_run_id",
            "plan_id",
            "plan_sha256",
            "task_weights",
            "total_weight",
            "target_delivery_phase",
            "out_of_scope_phases",
        }
    )
    current_fields = legacy_fields | {"program_roadmap_ref"}
    actual_fields = frozenset(mapping)
    legacy_definition = actual_fields == legacy_fields
    item = _closed(
        mapping,
        legacy_fields if legacy_definition else current_fields,
        "progress_definition",
    )
    weights = tuple(
        _parse_task_weight(entry, f"progress_definition.task_weights[{index}]")
        for index, entry in enumerate(
            _array(item["task_weights"], "progress_definition.task_weights", MAX_PROGRESS_TASKS)
        )
    )
    definition = ProgressDefinition(
        schema_version=_scalar(item["schema_version"], "progress_definition.schema_version", 32),
        definition_id=_code(item["definition_id"], "progress_definition.definition_id"),
        lifecycle_ref=_optional_path(item["lifecycle_ref"], "progress_definition.lifecycle_ref"),
        lifecycle_run_id=_optional_code(
            item["lifecycle_run_id"], "progress_definition.lifecycle_run_id"
        ),
        plan_id=_optional_code(item["plan_id"], "progress_definition.plan_id"),
        plan_sha256=_optional_digest(item["plan_sha256"], "progress_definition.plan_sha256"),
        task_weights=weights,
        total_weight=item["total_weight"],
        target_delivery_phase=_optional_phase(
            item["target_delivery_phase"], "progress_definition.target_delivery_phase"
        ),
        out_of_scope_phases=_phase_tuple(
            tuple(_array(item["out_of_scope_phases"], "progress_definition.out_of_scope_phases", len(_PHASE_ORDER))),
            "progress_definition.out_of_scope_phases",
        ),
        program_roadmap_ref=(
            None
            if legacy_definition
            else _optional_path(
                item["program_roadmap_ref"],
                "progress_definition.program_roadmap_ref",
            )
        ),
    )
    expected = canonical_json_bytes(
        _definition_mapping(
            definition,
            include_program_roadmap_ref=not legacy_definition,
        )
    )
    if raw != expected:
        raise ProgressProjectionError("progress definition JSON is not canonical")
    return definition


def _parse_task_counts(value: object, label: str) -> ProgressTaskCounts:
    item = _closed(
        value,
        frozenset(
            {
                "total",
                "execution_evidenced",
                "execution_succeeded",
                "independently_verified",
                "pending",
                "blocked",
            }
        ),
        label,
    )
    return ProgressTaskCounts(
        total=item["total"],
        execution_evidenced=item["execution_evidenced"],
        execution_succeeded=item["execution_succeeded"],
        independently_verified=item["independently_verified"],
        pending=item["pending"],
        blocked=item["blocked"],
    )


def _scope_status(value: object, label: str) -> ProgressScopeStatus:
    if type(value) is not str:
        raise ProgressProjectionError(f"{label} must be a string enum")
    try:
        return ProgressScopeStatus(value)
    except ValueError as error:
        raise ProgressProjectionError(f"{label} has an unsupported value") from error


def _delivery_state_value(value: object, label: str) -> DeliveryState:
    if type(value) is not str:
        raise ProgressProjectionError(f"{label} must be a string enum")
    try:
        return DeliveryState(value)
    except ValueError as error:
        raise ProgressProjectionError(f"{label} has an unsupported value") from error


def _gate_health_value(value: object, label: str) -> GateHealth:
    if type(value) is not str:
        raise ProgressProjectionError(f"{label} must be a string enum")
    try:
        return GateHealth(value)
    except ValueError as error:
        raise ProgressProjectionError(f"{label} has an unsupported value") from error


def parse_progress_snapshot(
    value: str | bytes,
    *,
    definition: ProgressDefinition | None = None,
    lifecycle: GoalDeliveryLifecycle | None = None,
) -> ProgressSnapshot:
    """Parse canonical snapshot bytes and optionally verify exact source recomputation."""

    if (definition is None) != (lifecycle is None):
        raise TypeError("definition and lifecycle must be supplied together for recomputation")
    if definition is not None and type(definition) is not ProgressDefinition:
        raise TypeError("definition must be an exact ProgressDefinition or None")
    if lifecycle is not None and type(lifecycle) is not GoalDeliveryLifecycle:
        raise TypeError("lifecycle must be an exact GoalDeliveryLifecycle or None")
    mapping, raw = _payload(value, maximum=MAX_PROGRESS_SNAPSHOT_BYTES)
    item = _closed(
        mapping,
        frozenset(
            {
                "schema_version",
                "definition_id",
                "definition_sha256",
                "lifecycle_ref",
                "lifecycle_sha256",
                "lifecycle_run_id",
                "plan_id",
                "plan_sha256",
                "scope_status",
                "execution_progress_basis_points",
                "verified_progress_basis_points",
                "current_stage",
                "current_stage_task_ids",
                "current_stage_execution_basis_points",
                "current_stage_verified_basis_points",
                "task_counts",
                "target_delivery_phase",
                "out_of_scope_phases",
                "delivery_state",
                "gate_health",
                "next_actions",
                "reason_codes",
                "execution_performed",
            }
        ),
        "progress_snapshot",
    )
    snapshot = ProgressSnapshot(
        schema_version=_scalar(item["schema_version"], "progress_snapshot.schema_version", 32),
        definition_id=_optional_code(item["definition_id"], "progress_snapshot.definition_id"),
        definition_sha256=_optional_digest(
            item["definition_sha256"], "progress_snapshot.definition_sha256"
        ),
        lifecycle_ref=_optional_path(item["lifecycle_ref"], "progress_snapshot.lifecycle_ref"),
        lifecycle_sha256=_digest(item["lifecycle_sha256"], "progress_snapshot.lifecycle_sha256"),
        lifecycle_run_id=_code(item["lifecycle_run_id"], "progress_snapshot.lifecycle_run_id"),
        plan_id=_code(item["plan_id"], "progress_snapshot.plan_id"),
        plan_sha256=_digest(item["plan_sha256"], "progress_snapshot.plan_sha256"),
        scope_status=_scope_status(item["scope_status"], "progress_snapshot.scope_status"),
        execution_progress_basis_points=_validate_basis_points(
            item["execution_progress_basis_points"],
            "progress_snapshot.execution_progress_basis_points",
            required=False,
        ),
        verified_progress_basis_points=_validate_basis_points(
            item["verified_progress_basis_points"],
            "progress_snapshot.verified_progress_basis_points",
            required=False,
        ),
        current_stage=_code(item["current_stage"], "progress_snapshot.current_stage"),
        current_stage_task_ids=_canonical_codes(
            tuple(
                _array(
                    item["current_stage_task_ids"],
                    "progress_snapshot.current_stage_task_ids",
                    MAX_PROGRESS_TASKS,
                )
            ),
            "progress_snapshot.current_stage_task_ids",
            maximum=MAX_PROGRESS_TASKS,
        ),
        current_stage_execution_basis_points=_validate_basis_points(
            item["current_stage_execution_basis_points"],
            "progress_snapshot.current_stage_execution_basis_points",
            required=False,
        ),
        current_stage_verified_basis_points=_validate_basis_points(
            item["current_stage_verified_basis_points"],
            "progress_snapshot.current_stage_verified_basis_points",
            required=False,
        ),
        task_counts=_parse_task_counts(item["task_counts"], "progress_snapshot.task_counts"),
        target_delivery_phase=_optional_phase(
            item["target_delivery_phase"], "progress_snapshot.target_delivery_phase"
        ),
        out_of_scope_phases=_phase_tuple(
            tuple(
                _array(item["out_of_scope_phases"], "progress_snapshot.out_of_scope_phases", len(_PHASE_ORDER))
            ),
            "progress_snapshot.out_of_scope_phases",
        ),
        delivery_state=_delivery_state_value(
            item["delivery_state"], "progress_snapshot.delivery_state"
        ),
        gate_health=_gate_health_value(item["gate_health"], "progress_snapshot.gate_health"),
        next_actions=_ordered_codes(
            tuple(_array(item["next_actions"], "progress_snapshot.next_actions", MAX_NEXT_ACTIONS)),
            "progress_snapshot.next_actions",
            maximum=MAX_NEXT_ACTIONS,
        ),
        reason_codes=_canonical_codes(
            tuple(
                _array(
                    item["reason_codes"],
                    "progress_snapshot.reason_codes",
                    MAX_PROGRESS_TASKS * 4,
                )
            ),
            "progress_snapshot.reason_codes",
            maximum=MAX_PROGRESS_TASKS * 4,
        ),
        execution_performed=item["execution_performed"],
    )
    if raw != render_progress_snapshot(snapshot):
        raise ProgressProjectionError("progress snapshot JSON is not canonical")
    if lifecycle is not None:
        expected = recompute_progress_snapshot(definition, lifecycle)
        if snapshot != expected:
            raise ProgressProjectionError("progress snapshot does not match recomputed source facts")
    return snapshot


_CONTINUATION_RE = re.compile(
    r"\A"
    r"state=(?P<state>[a-z0-9][a-z0-9._-]{0,127}); "
    r"action=(?P<action>[a-z0-9][a-z0-9._-]{0,127}); "
    r"owner=(?P<owner>[a-z0-9][a-z0-9._-]{0,127}); "
    r"requires_existing_authority=(?P<authority>true|false); "
    r"dispatch_permitted=(?P<dispatch>true|false); "
    r"resume_condition=(?P<resume>resume\.[a-z0-9][a-z0-9._-]{0,127})"
    r"\Z"
)


def _continuation_fields(value: object) -> dict[str, str]:
    """Validate and split the compact continuation contract used in text output."""

    if type(value) is not str or not value or "\n" in value or "\r" in value:
        raise ValueError("continuation must be a canonical status continuation")
    match = _CONTINUATION_RE.fullmatch(value)
    if match is None:
        raise ValueError("continuation must be a canonical status continuation")
    return match.groupdict()


def render_progress_status(
    value: ProgressSnapshot,
    *,
    current_phase: str | None = None,
    next_phase: str | None = None,
    definition_total_weight: int | None = None,
    continuation: str | None = None,
    program_snapshot: ProgramProgressSnapshot | None = None,
) -> str:
    """Return the fixed, source-preserving human Status Snapshot text.

    ``current_phase`` and ``next_phase`` are optional because a bare snapshot
    does not carry the lifecycle control phase. Callers that still have the
    parsed lifecycle should bind those values; otherwise the renderer reports
    them as unavailable instead of guessing from the task stage.
    """

    if type(value) is not ProgressSnapshot:
        raise TypeError("value must be an exact ProgressSnapshot")
    if program_snapshot is not None and type(program_snapshot) is not ProgramProgressSnapshot:
        raise TypeError("program_snapshot must be an exact ProgramProgressSnapshot or None")

    phase_text = current_phase or "unavailable/not-computable"
    next_phase_text = next_phase or "unavailable/not-computable"

    def format_basis_points(item: int | None) -> str:
        if item is None:
            return "not-computable"
        return f"{item // 100}.{item % 100:02d}%"

    if definition_total_weight is not None and (
        type(definition_total_weight) is not int or definition_total_weight <= 0
    ):
        raise TypeError("definition_total_weight must be a positive integer or None")

    targets = (
        "unavailable/not-computable"
        if value.target_delivery_phase is None
        and value.scope_status is ProgressScopeStatus.NOT_COMPUTABLE
        else "none"
        if value.target_delivery_phase is None
        else value.target_delivery_phase.value
    )
    out_of_scope = ",".join(item.value for item in value.out_of_scope_phases)
    if not out_of_scope:
        out_of_scope = (
            "unavailable/not-computable"
            if value.scope_status is ProgressScopeStatus.NOT_COMPUTABLE
            else "none"
        )
    next_actions = ",".join(value.next_actions) or "none"
    reasons = ",".join(value.reason_codes) or "none"
    counts = value.task_counts
    if value.scope_status is ProgressScopeStatus.COMPUTABLE and value.definition_id:
        denominator_tasks = str(counts.total)
        denominator_weight = (
            str(definition_total_weight)
            if definition_total_weight is not None
            else "not-computable"
        )
        definition_id = value.definition_id
        lifecycle_ref = value.lifecycle_ref or "not-computable"
        plan_id = value.plan_id
    else:
        definition_id = "absent" if value.definition_id is None else value.definition_id
        denominator_tasks = "not-computable"
        denominator_weight = "not-computable"
        lifecycle_ref = value.lifecycle_ref or "not-computable"
        plan_id = value.plan_id if value.plan_id else "not-computable"
    if continuation is None:
        continuation = (
            "state=inspect; action=inspect-progress-scope; "
            "owner=harness-controller; requires_existing_authority=false; "
            "dispatch_permitted=false; "
            "resume_condition=resume.after-progress-source-is-readable"
        )
    continuation_fields = _continuation_fields(continuation)
    continuation_state = continuation_fields["state"]
    continuation_action = continuation_fields["action"]
    human_gate = (
        continuation_action
        if continuation_state == "human-gate"
        else "confirmation-required"
        if any(
            item.startswith((
                "owner-authorization-pending.",
                "next.provide-transaction-approval",
                "owner-decision-required",
            ))
            for item in value.reason_codes
        )
        else "none"
    )
    if any(item == "independent-review-blocked" for item in value.reason_codes):
        review_state = "blocked"
    elif any(
        item == "independent-review-required"
        or item.startswith("consolidation-evidence-pending.")
        for item in value.reason_codes
    ) or (
        value.execution_progress_basis_points is not None
        and value.verified_progress_basis_points is not None
        and value.execution_progress_basis_points > value.verified_progress_basis_points
    ):
        review_state = "pending"
    elif counts.execution_succeeded and counts.independently_verified == counts.execution_succeeded:
        review_state = "accepted"
    else:
        review_state = "not-required-yet"
    if program_snapshot is None:
        program_lines = (
            "Program progress: scope=not-computable; definition_id=absent; "
            "execution=not-computable verified=not-computable; "
            "reason=program-roadmap-unavailable",
            "Program stage (current): unavailable/not-computable; packages=not-computable; "
            "execution=not-computable verified=not-computable",
            "Immediate program transaction: unavailable/not-computable",
            "Following program stage: unavailable/not-computable",
            "Roadmap: unavailable/not-computable",
        )
    else:
        program_lines = render_program_status_lines(program_snapshot)
    return "\n".join(
        (
            "Status Snapshot",
            f"Scope: {value.scope_status.value}",
            "Progress basis: "
            f"definition_id={definition_id}; denominator_tasks={denominator_tasks}; "
            f"denominator_weight={denominator_weight}; lifecycle_ref={lifecycle_ref}; "
            f"plan_id={plan_id}",
            "Completed work: "
            f"total={counts.total} executed={counts.execution_evidenced} "
            f"succeeded={counts.execution_succeeded} verified={counts.independently_verified} "
            f"pending={counts.pending} blocked={counts.blocked}",
            "Total progress: "
            f"execution={format_basis_points(value.execution_progress_basis_points)} "
            f"verified={format_basis_points(value.verified_progress_basis_points)}",
            *program_lines[:1],
            f"Current phase: {phase_text}",
            "Lifecycle stage: "
            f"{value.current_stage}; "
            f"execution={format_basis_points(value.current_stage_execution_basis_points)} "
            f"verified={format_basis_points(value.current_stage_verified_basis_points)}",
            program_lines[1],
            f"Next lifecycle boundary: {next_phase_text}",
            program_lines[2],
            program_lines[3],
            program_lines[4],
            "Delivery and Gates: "
            f"target={targets}; state={value.delivery_state.value}; "
            f"gate_health={value.gate_health.value}; "
            f"program_state={program_snapshot.scope_status.value if program_snapshot is not None else 'not-computable'}/"
            f"{program_snapshot.delivery_state.value if program_snapshot is not None else 'not-computable'}",
            f"Next automatic work: {continuation_action or next_actions}"
            + (
                f"; source_sequence={next_actions}"
                if continuation_action and continuation_action not in next_actions and next_actions != "none"
                else ""
            ),
            f"Human gate: {human_gate}",
            "Blockers and review: "
            f"reasons={reasons}; independent_review={review_state}",
            f"Later boundaries: {out_of_scope}",
            f"Continuation: {continuation}",
        )
    )


__all__ = [
    "PROGRESS_DEFINITION_SCHEMA_VERSION",
    "PROGRESS_SNAPSHOT_SCHEMA_VERSION",
    "MAX_PROGRESS_DEFINITION_BYTES",
    "MAX_PROGRESS_SNAPSHOT_BYTES",
    "MAX_PROGRESS_TASKS",
    "MAX_NEXT_ACTIONS",
    "MAX_BASIS_POINTS",
    "ProgressProjectionError",
    "ProgressScopeStatus",
    "DeliveryState",
    "GateHealth",
    "ProgressTaskWeight",
    "ProgressDefinition",
    "ProgressTaskCounts",
    "ProgressSnapshot",
    "render_progress_definition",
    "parse_progress_definition",
    "recompute_progress_snapshot",
    "render_progress_snapshot",
    "parse_progress_snapshot",
    "render_progress_status",
]
