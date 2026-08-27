"""Canonical vertical-slice invariant evaluation for Work Item Board projections."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Mapping

from .goal_delivery_lifecycle import ReviewVerdict
from .storage import canonical_json_bytes
from .work_item_board import (
    WorkItem,
    WorkItemBoard,
    WorkItemBoardError,
    WorkItemState,
    parse_work_item_board,
    render_work_item_board,
)


VERTICAL_SLICE_EVALUATION_SCHEMA_VERSION = "1.0"
MAX_VERTICAL_SLICE_ASSESSMENTS = 32
MAX_REASON_CODES = 64


class VerticalSliceEvaluationError(ValueError):
    """Raised when vertical-slice evidence is malformed or not source-bound."""


class VerticalSliceState(str, Enum):
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    NEEDS_EVIDENCE = "needs-evidence"


def _code(value: object, label: str) -> str:
    if type(value) is not str or not value or len(value) > 128:
        raise VerticalSliceEvaluationError(f"{label} must be bounded non-empty text")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
    if value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789" or any(
        char not in allowed for char in value
    ):
        raise VerticalSliceEvaluationError(f"{label} must be a stable code")
    return value


def _digest(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise VerticalSliceEvaluationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _codes(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > MAX_REASON_CODES:
        raise VerticalSliceEvaluationError(f"{label} must be a bounded immutable tuple")
    if not allow_empty and not value:
        raise VerticalSliceEvaluationError(f"{label} must not be empty")
    normalized = tuple(_code(item, f"{label}[{index}]") for index, item in enumerate(value))
    if normalized != tuple(sorted(set(normalized))):
        raise VerticalSliceEvaluationError(f"{label} must use canonical unique order")
    return normalized


@dataclass(frozen=True)
class VerticalSliceAssessment:
    task_id: str
    state: VerticalSliceState
    reason_codes: tuple[str, ...]
    missing_evidence_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self) is not VerticalSliceAssessment:
            raise VerticalSliceEvaluationError("VerticalSliceAssessment subclasses are not accepted")
        _code(self.task_id, "assessment.task_id")
        if type(self.state) is not VerticalSliceState:
            raise VerticalSliceEvaluationError("assessment.state must be VerticalSliceState")
        reasons = _codes(self.reason_codes, "assessment.reason_codes", allow_empty=True)
        missing = _codes(self.missing_evidence_codes, "assessment.missing_evidence_codes", allow_empty=True)
        if self.state is VerticalSliceState.ACCEPTED and (reasons or missing):
            raise VerticalSliceEvaluationError("accepted assessment cannot retain reasons or missing evidence")
        if self.state is VerticalSliceState.BLOCKED and not reasons:
            raise VerticalSliceEvaluationError("blocked assessment requires a reason")
        if self.state is VerticalSliceState.NEEDS_EVIDENCE and not missing:
            raise VerticalSliceEvaluationError("needs-evidence assessment requires missing evidence")


@dataclass(frozen=True)
class VerticalSliceEvaluation:
    schema_version: str
    board_sha256: str
    board: WorkItemBoard
    assessments: tuple[VerticalSliceAssessment, ...]
    accepted_task_ids: tuple[str, ...]
    blocked_task_ids: tuple[str, ...]
    needs_evidence_task_ids: tuple[str, ...]
    execution_performed: bool

    def __post_init__(self) -> None:
        if type(self) is not VerticalSliceEvaluation:
            raise VerticalSliceEvaluationError("VerticalSliceEvaluation subclasses are not accepted")
        if self.schema_version != VERTICAL_SLICE_EVALUATION_SCHEMA_VERSION:
            raise VerticalSliceEvaluationError("unsupported vertical-slice evaluation schema_version")
        _digest(self.board_sha256, "evaluation.board_sha256")
        if type(self.board) is not WorkItemBoard:
            raise VerticalSliceEvaluationError("evaluation.board must be an exact WorkItemBoard")
        if hashlib.sha256(render_work_item_board(self.board)).hexdigest() != self.board_sha256:
            raise VerticalSliceEvaluationError("evaluation.board_sha256 does not bind board")
        if type(self.assessments) is not tuple or not self.assessments or len(self.assessments) > MAX_VERTICAL_SLICE_ASSESSMENTS:
            raise VerticalSliceEvaluationError("evaluation.assessments must be a bounded non-empty tuple")
        if any(type(item) is not VerticalSliceAssessment for item in self.assessments):
            raise VerticalSliceEvaluationError("evaluation.assessments must contain exact assessments")
        identifiers = tuple(item.task_id for item in self.assessments)
        if identifiers != tuple(sorted(set(identifiers))):
            raise VerticalSliceEvaluationError("evaluation.assessments must use canonical task ID order")
        board_ids = tuple(item.task_id for item in self.board.items)
        if identifiers != board_ids:
            raise VerticalSliceEvaluationError("evaluation assessments must exactly cover board items")
        for label, value in (
            ("accepted_task_ids", self.accepted_task_ids),
            ("blocked_task_ids", self.blocked_task_ids),
            ("needs_evidence_task_ids", self.needs_evidence_task_ids),
        ):
            _codes(value, f"evaluation.{label}", allow_empty=True)
            if any(task_id not in board_ids for task_id in value):
                raise VerticalSliceEvaluationError(f"evaluation.{label} references an unknown task")
        if type(self.execution_performed) is not bool or self.execution_performed:
            raise VerticalSliceEvaluationError("vertical-slice evaluation cannot claim execution")


def _evidence_by_task(board: WorkItemBoard):
    if board.lifecycle is None:
        return {}
    return {item.task_id: item for item in board.lifecycle.task_evidence}


def _is_vertical_slice_planned(item: WorkItem) -> tuple[str, ...]:
    missing: set[str] = set()
    if not item.source_refs:
        missing.add("source-reference-missing")
    if not item.acceptance_refs:
        missing.add("acceptance-reference-missing")
    if not item.rollback_ref:
        missing.add("rollback-reference-missing")
    if not any(surface.startswith("phase.") for surface in item.integration_surfaces):
        missing.add("integration-phase-missing")
    if not any(not surface.startswith("phase.") for surface in item.integration_surfaces):
        missing.add("test-or-gate-surface-missing")
    return tuple(sorted(missing))


def _assessment(board: WorkItemBoard, item: WorkItem, evidence_by_task: Mapping[str, object]) -> VerticalSliceAssessment:
    planned_missing = _is_vertical_slice_planned(item)
    if item.state is WorkItemState.BLOCKED:
        return VerticalSliceAssessment(item.task_id, VerticalSliceState.BLOCKED, ("work-item-blocked",), planned_missing)
    if planned_missing:
        return VerticalSliceAssessment(item.task_id, VerticalSliceState.NEEDS_EVIDENCE, (), planned_missing)
    if item.state is WorkItemState.ACCEPTED:
        evidence = evidence_by_task.get(item.task_id)
        if evidence is None:
            return VerticalSliceAssessment(item.task_id, VerticalSliceState.BLOCKED, ("accepted-evidence-missing",), ())
        if evidence.reviewer_id == evidence.executor_id or evidence.review_verdict is not ReviewVerdict.ACCEPT:
            return VerticalSliceAssessment(item.task_id, VerticalSliceState.BLOCKED, ("independent-review-invalid",), ())
        return VerticalSliceAssessment(item.task_id, VerticalSliceState.ACCEPTED, (), ())
    missing = {"independent-review-pending"}
    if item.depends_on:
        accepted = set(board.lifecycle.accepted_task_ids) if board.lifecycle is not None else set()
        if not set(item.depends_on).issubset(accepted):
            missing.add("dependency-closure-pending")
    return VerticalSliceAssessment(item.task_id, VerticalSliceState.NEEDS_EVIDENCE, (), tuple(sorted(missing)))


def _build(board: WorkItemBoard) -> VerticalSliceEvaluation:
    board_payload = render_work_item_board(board)
    evidence_by_task = _evidence_by_task(board)
    assessments = tuple(sorted((_assessment(board, item, evidence_by_task) for item in board.items), key=lambda item: item.task_id))
    return VerticalSliceEvaluation(
        schema_version=VERTICAL_SLICE_EVALUATION_SCHEMA_VERSION,
        board_sha256=hashlib.sha256(board_payload).hexdigest(),
        board=board,
        assessments=assessments,
        accepted_task_ids=tuple(item.task_id for item in assessments if item.state is VerticalSliceState.ACCEPTED),
        blocked_task_ids=tuple(item.task_id for item in assessments if item.state is VerticalSliceState.BLOCKED),
        needs_evidence_task_ids=tuple(item.task_id for item in assessments if item.state is VerticalSliceState.NEEDS_EVIDENCE),
        execution_performed=False,
    )


def evaluate_vertical_slices(board_payload: bytes | bytearray | memoryview) -> VerticalSliceEvaluation:
    """Evaluate vertical-slice evidence without executing a task or granting authority."""
    try:
        board = parse_work_item_board(board_payload)
    except (WorkItemBoardError, TypeError, ValueError) as error:
        raise VerticalSliceEvaluationError("work item board is invalid") from error
    return _build(board)


def _assessment_mapping(value: VerticalSliceAssessment) -> dict[str, object]:
    return {
        "missing_evidence_codes": list(value.missing_evidence_codes),
        "reason_codes": list(value.reason_codes),
        "state": value.state.value,
        "task_id": value.task_id,
    }


def render_vertical_slice_evaluation(value: VerticalSliceEvaluation) -> bytes:
    """Render an evaluation only when it exactly matches its source board."""
    if type(value) is not VerticalSliceEvaluation:
        raise TypeError("value must be an exact VerticalSliceEvaluation")
    expected = _build(value.board)
    if expected != value:
        raise VerticalSliceEvaluationError("evaluation does not match recomputed board")
    return canonical_json_bytes(
        {
            "accepted_task_ids": list(value.accepted_task_ids),
            "assessments": [_assessment_mapping(item) for item in value.assessments],
            "blocked_task_ids": list(value.blocked_task_ids),
            "board": json.loads(render_work_item_board(value.board)),
            "board_sha256": value.board_sha256,
            "execution_performed": value.execution_performed,
            "needs_evidence_task_ids": list(value.needs_evidence_task_ids),
            "schema_version": value.schema_version,
        }
    )


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise VerticalSliceEvaluationError("duplicate JSON object key")
        result[key] = value
    return result


def _closed(value: object, fields: frozenset[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise VerticalSliceEvaluationError(f"{label} has unknown or missing fields")
    return value


def _parse_codes(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise VerticalSliceEvaluationError(f"{label} must be a JSON array")
    return _codes(tuple(value), label, allow_empty=allow_empty)


def _parse_assessment(value: object) -> VerticalSliceAssessment:
    item = _closed(value, frozenset({"missing_evidence_codes", "reason_codes", "state", "task_id"}), "assessment")
    if type(item["state"]) is not str:
        raise VerticalSliceEvaluationError("assessment.state must be a string enum")
    try:
        state = VerticalSliceState(item["state"])
    except ValueError as error:
        raise VerticalSliceEvaluationError("assessment.state has an unsupported value") from error
    return VerticalSliceAssessment(
        task_id=_code(item["task_id"], "assessment.task_id"),
        state=state,
        reason_codes=_parse_codes(item["reason_codes"], "assessment.reason_codes", allow_empty=True),
        missing_evidence_codes=_parse_codes(item["missing_evidence_codes"], "assessment.missing_evidence_codes", allow_empty=True),
    )


def parse_vertical_slice_evaluation(payload: bytes | bytearray | memoryview) -> VerticalSliceEvaluation:
    """Parse and source-recompute a canonical vertical-slice evaluation."""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    raw = bytes(payload)
    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VerticalSliceEvaluationError("evaluation payload is not valid UTF-8 JSON") from error
    item = _closed(decoded, frozenset({"accepted_task_ids", "assessments", "blocked_task_ids", "board", "board_sha256", "execution_performed", "needs_evidence_task_ids", "schema_version"}), "evaluation")
    if item["schema_version"] != VERTICAL_SLICE_EVALUATION_SCHEMA_VERSION:
        raise VerticalSliceEvaluationError("unsupported vertical-slice evaluation schema_version")
    if type(item["execution_performed"]) is not bool:
        raise VerticalSliceEvaluationError("evaluation.execution_performed must be boolean")
    try:
        board = parse_work_item_board(canonical_json_bytes(item["board"]))
    except (WorkItemBoardError, TypeError, ValueError) as error:
        raise VerticalSliceEvaluationError("evaluation board is invalid") from error
    assessments = item["assessments"]
    if not isinstance(assessments, list):
        raise VerticalSliceEvaluationError("evaluation.assessments must be a JSON array")
    evaluation = VerticalSliceEvaluation(
        schema_version=item["schema_version"],
        board_sha256=_digest(item["board_sha256"], "evaluation.board_sha256"),
        board=board,
        assessments=tuple(_parse_assessment(value) for value in assessments),
        accepted_task_ids=_parse_codes(item["accepted_task_ids"], "evaluation.accepted_task_ids", allow_empty=True),
        blocked_task_ids=_parse_codes(item["blocked_task_ids"], "evaluation.blocked_task_ids", allow_empty=True),
        needs_evidence_task_ids=_parse_codes(item["needs_evidence_task_ids"], "evaluation.needs_evidence_task_ids", allow_empty=True),
        execution_performed=item["execution_performed"],
    )
    expected = _build(board)
    if evaluation != expected or render_vertical_slice_evaluation(evaluation) != raw:
        raise VerticalSliceEvaluationError("evaluation payload does not match canonical source evaluation")
    return evaluation


__all__ = [
    "VERTICAL_SLICE_EVALUATION_SCHEMA_VERSION",
    "VerticalSliceEvaluationError",
    "VerticalSliceState",
    "VerticalSliceAssessment",
    "VerticalSliceEvaluation",
    "evaluate_vertical_slices",
    "render_vertical_slice_evaluation",
    "parse_vertical_slice_evaluation",
]
