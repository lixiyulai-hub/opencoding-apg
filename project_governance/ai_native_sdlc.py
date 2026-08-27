"""Pure, source-bound AI-Native SDLC V1 Slice A preview.

The facade turns existing P3-F/P3-G records into a local decision preview.  It
does not execute a task, write a receipt, invoke a process, or grant authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Any, Mapping

from .feedback_loops import LoopStopState
from .storage import canonical_json_bytes
from .vertical_slice_evaluator import (
    VerticalSliceEvaluation,
    VerticalSliceEvaluationError,
    VerticalSliceState,
    evaluate_vertical_slices,
    parse_vertical_slice_evaluation,
    render_vertical_slice_evaluation,
)
from .work_item_board import (
    WorkItemBoard,
    WorkItemBoardError,
    WorkItemState,
    build_work_item_board,
    parse_work_item_board,
    render_work_item_board,
)


AI_NATIVE_SDLC_PREVIEW_SCHEMA_VERSION = "1.0"
MAX_TASK_PACKS = 32
_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")


class AiNativeSdlcError(ValueError):
    """Raised when a Slice A preview is not canonical or source-bound."""


class SliceRisk(str, Enum):
    P0 = "p0"
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"


class PreviewState(str, Enum):
    AUTO_CONTINUE = "auto-continue"
    FROZEN = "frozen"
    HUMAN_GATE = "human-gate"


def _code(value: object, label: str) -> str:
    if type(value) is not str or not value or len(value) > 128:
        raise AiNativeSdlcError(f"{label} must be bounded non-empty text")
    if value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789" or any(
        character not in _CODE_CHARS for character in value
    ):
        raise AiNativeSdlcError(f"{label} must be a stable code")
    return value


def _digest(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise AiNativeSdlcError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _codes(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > MAX_TASK_PACKS * 4:
        raise AiNativeSdlcError(f"{label} must be a bounded immutable tuple")
    if not allow_empty and not value:
        raise AiNativeSdlcError(f"{label} must not be empty")
    result = tuple(_code(item, f"{label}[{index}]") for index, item in enumerate(value))
    if result != tuple(sorted(set(result))):
        raise AiNativeSdlcError(f"{label} must use canonical unique order")
    return result


def _paths(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > MAX_TASK_PACKS * 4:
        raise AiNativeSdlcError(f"{label} must be a bounded immutable tuple")
    if not allow_empty and not value:
        raise AiNativeSdlcError(f"{label} must not be empty")
    result = []
    for index, item in enumerate(value):
        if type(item) is not str or not item or item.startswith("/") or "\\" in item:
            raise AiNativeSdlcError(f"{label}[{index}] must be a contained slash path")
        parts = item.split("/")
        if any(part in ("", ".", "..") for part in parts) or any(
            any(character not in _CODE_CHARS for character in part) for part in parts
        ):
            raise AiNativeSdlcError(f"{label}[{index}] must be a contained slash path")
        result.append(item)
    normalized = tuple(result)
    if normalized != tuple(sorted(set(normalized))):
        raise AiNativeSdlcError(f"{label} must use canonical unique order")
    return normalized


@dataclass(frozen=True)
class AgentTaskPack:
    """Least-privilege local task input derived from one P3-F route."""

    task_id: str
    wave_index: int
    source_refs: tuple[str, ...]
    read_paths: tuple[str, ...]
    bounded_changed_paths: tuple[str, ...]
    integration_surfaces: tuple[str, ...]
    acceptance_refs: tuple[str, ...]
    rollback_ref: str
    dependency_task_ids: tuple[str, ...]
    policy_sha256: str
    risk: SliceRisk
    execution_performed: bool

    def __post_init__(self) -> None:
        if type(self) is not AgentTaskPack:
            raise AiNativeSdlcError("AgentTaskPack subclasses are not accepted")
        _code(self.task_id, "task_pack.task_id")
        if type(self.wave_index) is not int or self.wave_index < 0:
            raise AiNativeSdlcError("task_pack.wave_index must be non-negative")
        for label, value, empty in (
            ("source_refs", self.source_refs, False),
            ("integration_surfaces", self.integration_surfaces, False),
            ("acceptance_refs", self.acceptance_refs, False),
            ("dependency_task_ids", self.dependency_task_ids, True),
        ):
            _codes(value, f"task_pack.{label}", allow_empty=empty)
        _paths(self.read_paths, "task_pack.read_paths", allow_empty=True)
        _paths(self.bounded_changed_paths, "task_pack.bounded_changed_paths", allow_empty=True)
        _code(self.rollback_ref, "task_pack.rollback_ref")
        _digest(self.policy_sha256, "task_pack.policy_sha256")
        if type(self.risk) is not SliceRisk:
            raise AiNativeSdlcError("task_pack.risk must be SliceRisk")
        if type(self.execution_performed) is not bool or self.execution_performed:
            raise AiNativeSdlcError("task pack cannot claim execution")


@dataclass(frozen=True)
class AiNativeSdlcPreview:
    schema_version: str
    board_sha256: str
    board: WorkItemBoard
    evaluation_sha256: str
    evaluation: VerticalSliceEvaluation
    task_packs: tuple[AgentTaskPack, ...]
    loop_stop_state: LoopStopState
    state: PreviewState
    reason_codes: tuple[str, ...]
    next_task_ids: tuple[str, ...]
    human_gate_required: bool
    resume_condition: str
    execution_performed: bool

    def __post_init__(self) -> None:
        if type(self) is not AiNativeSdlcPreview:
            raise AiNativeSdlcError("AiNativeSdlcPreview subclasses are not accepted")
        if self.schema_version != AI_NATIVE_SDLC_PREVIEW_SCHEMA_VERSION:
            raise AiNativeSdlcError("unsupported AI-Native SDLC preview schema version")
        _digest(self.board_sha256, "preview.board_sha256")
        _digest(self.evaluation_sha256, "preview.evaluation_sha256")
        if type(self.board) is not WorkItemBoard or type(self.evaluation) is not VerticalSliceEvaluation:
            raise AiNativeSdlcError("preview sources must be exact canonical records")
        if hashlib.sha256(render_work_item_board(self.board)).hexdigest() != self.board_sha256:
            raise AiNativeSdlcError("preview.board_sha256 does not bind board")
        if hashlib.sha256(render_vertical_slice_evaluation(self.evaluation)).hexdigest() != self.evaluation_sha256:
            raise AiNativeSdlcError("preview.evaluation_sha256 does not bind evaluation")
        if self.evaluation.board != self.board:
            raise AiNativeSdlcError("preview evaluation must bind preview board")
        if type(self.task_packs) is not tuple or len(self.task_packs) > MAX_TASK_PACKS:
            raise AiNativeSdlcError("preview.task_packs must be bounded immutable records")
        if any(type(item) is not AgentTaskPack for item in self.task_packs):
            raise AiNativeSdlcError("preview.task_packs must contain exact task packs")
        if tuple(item.task_id for item in self.task_packs) != tuple(sorted(item.task_id for item in self.task_packs)):
            raise AiNativeSdlcError("preview.task_packs must use canonical task order")
        if type(self.loop_stop_state) is not LoopStopState:
            raise AiNativeSdlcError("preview.loop_stop_state must be LoopStopState")
        if type(self.state) is not PreviewState:
            raise AiNativeSdlcError("preview.state must be PreviewState")
        _codes(self.reason_codes, "preview.reason_codes", allow_empty=True)
        _codes(self.next_task_ids, "preview.next_task_ids", allow_empty=True)
        if any(task_id not in tuple(item.task_id for item in self.task_packs) for task_id in self.next_task_ids):
            raise AiNativeSdlcError("preview.next_task_ids must be packed tasks")
        if type(self.human_gate_required) is not bool:
            raise AiNativeSdlcError("preview.human_gate_required must be boolean")
        if self.human_gate_required != (self.state is PreviewState.HUMAN_GATE):
            raise AiNativeSdlcError("human gate state must agree with preview state")
        if type(self.resume_condition) is not str or not self.resume_condition.startswith("resume."):
            raise AiNativeSdlcError("preview.resume_condition must be a stable resume code")
        if type(self.execution_performed) is not bool or self.execution_performed:
            raise AiNativeSdlcError("preview cannot claim execution")


def _route_risk(route) -> SliceRisk:
    context = route.context
    action = context.action_context
    if (
        context.git_operation
        or context.release
        or not action.no_network
        or not action.no_cost
        or not action.no_credentials
        or not action.no_real_data
        or action.public_delivery
        or action.irreversible
        or action.security_change
        or action.privacy_change
        or action.materially_ambiguous
        or action.runtime_launch
        or action.deployment
    ):
        return SliceRisk.P3
    if len(context.write_paths) > 1 or route.depends_on or len(context.gate_ids) > 1:
        return SliceRisk.P2
    if not context.write_paths:
        return SliceRisk.P0
    return SliceRisk.P1


def _task_packs(board: WorkItemBoard) -> tuple[AgentTaskPack, ...]:
    routes = {route.task_id: route for route in board.plan.routes}
    packs = []
    for item in board.items:
        route = routes[item.task_id]
        packs.append(
            AgentTaskPack(
                task_id=item.task_id,
                wave_index=item.wave_index,
                source_refs=item.source_refs,
                read_paths=route.context.read_paths,
                bounded_changed_paths=route.context.write_paths,
                integration_surfaces=item.integration_surfaces,
                acceptance_refs=item.acceptance_refs,
                rollback_ref=item.rollback_ref,
                dependency_task_ids=item.depends_on,
                policy_sha256=route.context.action_context.policy_sha256,
                risk=_route_risk(route),
                execution_performed=False,
            )
        )
    return tuple(sorted(packs, key=lambda item: item.task_id))


def _decision(
    board: WorkItemBoard,
    evaluation: VerticalSliceEvaluation,
    packs: tuple[AgentTaskPack, ...],
    loop_stop_state: LoopStopState,
) -> tuple[PreviewState, tuple[str, ...], tuple[str, ...], str]:
    assessment = {item.task_id: item for item in evaluation.assessments}
    pack_by_id = {item.task_id: item for item in packs}
    # The canonical plan identifies the next selected tasks even when the board
    # has already frozen them at a consequential boundary.  Route P3 tasks
    # must surface their required human Gate rather than being collapsed into
    # an indistinguishable no-unblocked-work-item result.
    candidates = tuple(task_id for task_id in board.plan.next_task_ids if task_id in pack_by_id)
    if not candidates:
        return PreviewState.FROZEN, ("no-unblocked-work-item",), (), "resume.after-unblocked-work-item-is-available"
    for task_id in candidates:
        if pack_by_id[task_id].risk is SliceRisk.P3:
            return PreviewState.HUMAN_GATE, ("external-boundary-confirmation-required",), (), "resume.after-owner-decision-is-recorded"
    if loop_stop_state is LoopStopState.OWNER_DECISION_REQUIRED:
        return PreviewState.HUMAN_GATE, ("feedback-loop-owner-decision-required",), (), "resume.after-owner-decision-is-recorded"
    if loop_stop_state is not LoopStopState.CONTINUE:
        return PreviewState.FROZEN, (f"feedback-loop-{loop_stop_state.value}",), (), "resume.after-feedback-loop-stop-condition-is-resolved"
    for task_id in candidates:
        if pack_by_id[task_id].risk is SliceRisk.P2:
            return PreviewState.FROZEN, ("plan-bound-validation-required",), (), "resume.after-selected-gates-pass"
        item = next(item for item in board.items if item.task_id == task_id)
        evaluated = assessment[task_id]
        if item.state is not WorkItemState.UNBLOCKED or evaluated.state is not VerticalSliceState.ACCEPTED:
            return PreviewState.FROZEN, ("vertical-slice-evidence-pending",), (), "resume.after-vertical-slice-evidence-is-accepted"
    return PreviewState.AUTO_CONTINUE, (), candidates, "resume.after-bounded-local-task-is-verified"


def _build(
    board: WorkItemBoard,
    loop_stop_state: LoopStopState,
) -> AiNativeSdlcPreview:
    board_payload = render_work_item_board(board)
    evaluation = evaluate_vertical_slices(board_payload)
    packs = _task_packs(board)
    state, reasons, next_ids, resume = _decision(board, evaluation, packs, loop_stop_state)
    return AiNativeSdlcPreview(
        schema_version=AI_NATIVE_SDLC_PREVIEW_SCHEMA_VERSION,
        board_sha256=hashlib.sha256(board_payload).hexdigest(),
        board=board,
        evaluation_sha256=hashlib.sha256(render_vertical_slice_evaluation(evaluation)).hexdigest(),
        evaluation=evaluation,
        task_packs=packs,
        loop_stop_state=loop_stop_state,
        state=state,
        reason_codes=reasons,
        next_task_ids=next_ids,
        human_gate_required=state is PreviewState.HUMAN_GATE,
        resume_condition=resume,
        execution_performed=False,
    )


def build_ai_native_sdlc_preview(
    plan_payload: bytes | bytearray | memoryview,
    lifecycle_payload: bytes | bytearray | memoryview | None = None,
    *,
    loop_stop_state: LoopStopState = LoopStopState.CONTINUE,
) -> AiNativeSdlcPreview:
    """Build a pure Slice A preview from existing canonical P3-F/P3-G records."""
    if type(loop_stop_state) is not LoopStopState:
        raise AiNativeSdlcError("loop_stop_state must be an exact LoopStopState")
    try:
        board = build_work_item_board(plan_payload, lifecycle_payload)
    except (WorkItemBoardError, TypeError, ValueError) as error:
        raise AiNativeSdlcError("source plan or lifecycle is invalid") from error
    return _build(board, loop_stop_state)


def _task_pack_mapping(value: AgentTaskPack) -> dict[str, object]:
    return {
        "acceptance_refs": list(value.acceptance_refs),
        "bounded_changed_paths": list(value.bounded_changed_paths),
        "dependency_task_ids": list(value.dependency_task_ids),
        "execution_performed": value.execution_performed,
        "integration_surfaces": list(value.integration_surfaces),
        "policy_sha256": value.policy_sha256,
        "read_paths": list(value.read_paths),
        "risk": value.risk.value,
        "rollback_ref": value.rollback_ref,
        "source_refs": list(value.source_refs),
        "task_id": value.task_id,
        "wave_index": value.wave_index,
    }


def render_ai_native_sdlc_preview(value: AiNativeSdlcPreview) -> bytes:
    """Render only an exactly source-recomputed Slice A preview."""
    if type(value) is not AiNativeSdlcPreview:
        raise TypeError("value must be an exact AiNativeSdlcPreview")
    expected = _build(value.board, value.loop_stop_state)
    if value != expected:
        raise AiNativeSdlcError("preview does not match recomputed source projection")
    return canonical_json_bytes(
        {
            "board": json.loads(render_work_item_board(value.board)),
            "board_sha256": value.board_sha256,
            "evaluation": json.loads(render_vertical_slice_evaluation(value.evaluation)),
            "evaluation_sha256": value.evaluation_sha256,
            "execution_performed": value.execution_performed,
            "human_gate_required": value.human_gate_required,
            "loop_stop_state": value.loop_stop_state.value,
            "next_task_ids": list(value.next_task_ids),
            "reason_codes": list(value.reason_codes),
            "resume_condition": value.resume_condition,
            "schema_version": value.schema_version,
            "state": value.state.value,
            "task_packs": [_task_pack_mapping(item) for item in value.task_packs],
        }
    )


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AiNativeSdlcError("duplicate JSON object key")
        result[key] = value
    return result


def parse_ai_native_sdlc_preview(payload: bytes | bytearray | memoryview) -> AiNativeSdlcPreview:
    """Parse and source-recompute a canonical Slice A preview."""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    try:
        raw = bytes(payload)
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AiNativeSdlcError("preview payload is not valid UTF-8 JSON") from error
    required = {
        "board", "board_sha256", "evaluation", "evaluation_sha256", "execution_performed",
        "human_gate_required", "loop_stop_state", "next_task_ids", "reason_codes",
        "resume_condition", "schema_version", "state", "task_packs",
    }
    if not isinstance(decoded, Mapping) or set(decoded) != required:
        raise AiNativeSdlcError("preview has unknown or missing fields")
    if decoded["schema_version"] != AI_NATIVE_SDLC_PREVIEW_SCHEMA_VERSION:
        raise AiNativeSdlcError("unsupported AI-Native SDLC preview schema version")
    if type(decoded["loop_stop_state"]) is not str:
        raise AiNativeSdlcError("preview.loop_stop_state must be a string enum")
    try:
        loop_stop_state = LoopStopState(decoded["loop_stop_state"])
        board = parse_work_item_board(canonical_json_bytes(decoded["board"]))
        evaluation = parse_vertical_slice_evaluation(canonical_json_bytes(decoded["evaluation"]))
    except (ValueError, WorkItemBoardError, VerticalSliceEvaluationError, TypeError) as error:
        raise AiNativeSdlcError("preview source records are invalid") from error
    expected = _build(board, loop_stop_state)
    if (
        _digest(decoded["board_sha256"], "preview.board_sha256") != expected.board_sha256
        or _digest(decoded["evaluation_sha256"], "preview.evaluation_sha256") != expected.evaluation_sha256
        or decoded["execution_performed"] is not False
        or decoded["human_gate_required"] != expected.human_gate_required
        or decoded["state"] != expected.state.value
        or decoded["reason_codes"] != list(expected.reason_codes)
        or decoded["next_task_ids"] != list(expected.next_task_ids)
        or decoded["resume_condition"] != expected.resume_condition
        or decoded["task_packs"] != [_task_pack_mapping(item) for item in expected.task_packs]
        or evaluation != expected.evaluation
        or render_ai_native_sdlc_preview(expected) != raw
    ):
        raise AiNativeSdlcError("preview payload does not match canonical source projection")
    return expected


__all__ = [
    "AI_NATIVE_SDLC_PREVIEW_SCHEMA_VERSION",
    "AiNativeSdlcError",
    "SliceRisk",
    "PreviewState",
    "AgentTaskPack",
    "AiNativeSdlcPreview",
    "build_ai_native_sdlc_preview",
    "render_ai_native_sdlc_preview",
    "parse_ai_native_sdlc_preview",
]
