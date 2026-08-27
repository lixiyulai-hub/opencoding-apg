"""Read-only Work Item Board projection for P3-F/P3-G task evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Any, Mapping

from .autonomous_task_orchestration import (
    AutonomousTaskPlan,
    AutonomousTaskOrchestrationError,
    AuthorizationClass,
    parse_autonomous_task_plan,
    render_autonomous_task_plan,
)
from .goal_delivery_lifecycle import (
    GoalDeliveryLifecycle,
    GoalDeliveryLifecycleError,
    LifecycleTaskState,
    parse_goal_delivery_lifecycle,
    render_goal_delivery_lifecycle,
)
from .storage import SchemaError, canonical_json_bytes


WORK_ITEM_BOARD_SCHEMA_VERSION = "1.0"
MAX_WORK_ITEMS = 32
MAX_REFS_PER_ITEM = 64


class WorkItemBoardError(ValueError):
    """Raised when a Work Item Board is malformed or not source-bound."""


class WorkItemState(str, Enum):
    UNBLOCKED = "unblocked"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    NEEDS_EVIDENCE = "needs-evidence"


def _code(value: object, label: str) -> str:
    if type(value) is not str or not value or len(value) > 128:
        raise WorkItemBoardError(f"{label} must be bounded non-empty text")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
    if value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789" or any(
        char not in allowed for char in value
    ):
        raise WorkItemBoardError(f"{label} must be a stable code")
    return value


def _digest(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise WorkItemBoardError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _refs(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > MAX_REFS_PER_ITEM:
        raise WorkItemBoardError(f"{label} must be a bounded immutable tuple")
    if not allow_empty and not value:
        raise WorkItemBoardError(f"{label} must not be empty")
    normalized = tuple(_code(item, f"{label}[{index}]") for index, item in enumerate(value))
    if normalized != tuple(sorted(set(normalized))):
        raise WorkItemBoardError(f"{label} must use canonical unique order")
    return normalized


@dataclass(frozen=True)
class WorkItem:
    task_id: str
    wave_index: int
    source_refs: tuple[str, ...]
    depends_on: tuple[str, ...]
    slice_goal: str
    integration_surfaces: tuple[str, ...]
    acceptance_refs: tuple[str, ...]
    rollback_ref: str
    state: WorkItemState
    next_action_code: str

    def __post_init__(self) -> None:
        if type(self) is not WorkItem:
            raise WorkItemBoardError("WorkItem subclasses are not accepted")
        _code(self.task_id, "work_item.task_id")
        if type(self.wave_index) is not int or self.wave_index < 0:
            raise WorkItemBoardError("work_item.wave_index must be a non-negative integer")
        _refs(self.source_refs, "work_item.source_refs")
        _refs(self.depends_on, "work_item.depends_on", allow_empty=True)
        _code(self.slice_goal, "work_item.slice_goal")
        _refs(self.integration_surfaces, "work_item.integration_surfaces")
        _refs(self.acceptance_refs, "work_item.acceptance_refs")
        _code(self.rollback_ref, "work_item.rollback_ref")
        if type(self.state) is not WorkItemState:
            raise WorkItemBoardError("work_item.state must be WorkItemState")
        _code(self.next_action_code, "work_item.next_action_code")


@dataclass(frozen=True)
class WorkItemBoard:
    schema_version: str
    plan_sha256: str
    plan: AutonomousTaskPlan
    lifecycle_sha256: str | None
    lifecycle: GoalDeliveryLifecycle | None
    items: tuple[WorkItem, ...]
    next_work_item_ids: tuple[str, ...]
    execution_performed: bool

    def __post_init__(self) -> None:
        if type(self) is not WorkItemBoard:
            raise WorkItemBoardError("WorkItemBoard subclasses are not accepted")
        if self.schema_version != WORK_ITEM_BOARD_SCHEMA_VERSION:
            raise WorkItemBoardError("unsupported work-item-board schema_version")
        _digest(self.plan_sha256, "board.plan_sha256")
        if type(self.plan) is not AutonomousTaskPlan:
            raise WorkItemBoardError("board.plan must be an exact AutonomousTaskPlan")
        if hashlib.sha256(render_autonomous_task_plan(self.plan)).hexdigest() != self.plan_sha256:
            raise WorkItemBoardError("board.plan_sha256 does not bind plan")
        if (self.lifecycle is None) != (self.lifecycle_sha256 is None):
            raise WorkItemBoardError("board lifecycle and digest must be both present or both null")
        if self.lifecycle is not None:
            if type(self.lifecycle) is not GoalDeliveryLifecycle:
                raise WorkItemBoardError("board.lifecycle must be an exact GoalDeliveryLifecycle")
            _digest(self.lifecycle_sha256, "board.lifecycle_sha256")
            if hashlib.sha256(render_goal_delivery_lifecycle(self.lifecycle)).hexdigest() != self.lifecycle_sha256:
                raise WorkItemBoardError("board.lifecycle_sha256 does not bind lifecycle")
            if self.lifecycle.plan_sha256 != self.plan_sha256:
                raise WorkItemBoardError("board lifecycle must bind board plan")
        if type(self.items) is not tuple or not self.items or len(self.items) > MAX_WORK_ITEMS:
            raise WorkItemBoardError("board.items must be a bounded non-empty tuple")
        if any(type(item) is not WorkItem for item in self.items):
            raise WorkItemBoardError("board.items must contain exact WorkItem records")
        identifiers = tuple(item.task_id for item in self.items)
        if identifiers != tuple(sorted(set(identifiers))):
            raise WorkItemBoardError("board.items must use canonical task ID order")
        _refs(self.next_work_item_ids, "board.next_work_item_ids", allow_empty=True)
        if any(item not in identifiers for item in self.next_work_item_ids):
            raise WorkItemBoardError("board.next_work_item_ids must reference known work items")
        if type(self.execution_performed) is not bool or self.execution_performed:
            raise WorkItemBoardError("Work Item Board cannot claim execution")


def _state_and_next_action(plan: AutonomousTaskPlan, route, lifecycle: GoalDeliveryLifecycle | None) -> tuple[WorkItemState, str]:
    if lifecycle is not None:
        cursor = next(item for item in lifecycle.task_cursor if item.task_id == route.task_id)
        if cursor.state is LifecycleTaskState.ACCEPTED:
            return WorkItemState.ACCEPTED, "next.no-action"
        if cursor.state is LifecycleTaskState.BLOCKED:
            return WorkItemState.BLOCKED, "next.remediate-evidence"
        if route.task_id in lifecycle.next_task_ids:
            if route.classification is AuthorizationClass.RECOMMEND:
                return WorkItemState.NEEDS_EVIDENCE, "next.provide-decision"
            if route.classification is AuthorizationClass.CONFIRM:
                return WorkItemState.NEEDS_EVIDENCE, "next.provide-approval"
            return WorkItemState.UNBLOCKED, "next.prepare-bounded-slice"
        return WorkItemState.NEEDS_EVIDENCE, "next.complete-dependencies"
    if route.classification is AuthorizationClass.BLOCK:
        return WorkItemState.BLOCKED, "next.remediate-evidence"
    if route.task_id in plan.next_task_ids:
        if route.classification is AuthorizationClass.RECOMMEND:
            return WorkItemState.NEEDS_EVIDENCE, "next.provide-decision"
        if route.classification is AuthorizationClass.CONFIRM:
            return WorkItemState.NEEDS_EVIDENCE, "next.provide-approval"
        return WorkItemState.UNBLOCKED, "next.prepare-bounded-slice"
    return WorkItemState.NEEDS_EVIDENCE, "next.complete-dependencies"


def _build(plan: AutonomousTaskPlan, lifecycle: GoalDeliveryLifecycle | None) -> WorkItemBoard:
    plan_payload = render_autonomous_task_plan(plan)
    plan_sha256 = hashlib.sha256(plan_payload).hexdigest()
    if lifecycle is not None and lifecycle.plan_sha256 != plan_sha256:
        raise WorkItemBoardError("lifecycle must bind the exact supplied plan")
    items = []
    for route in plan.routes:
        state, action = _state_and_next_action(plan, route, lifecycle)
        items.append(
            WorkItem(
                task_id=route.task_id,
                wave_index=route.wave_index,
                source_refs=route.context.action_context.evidence_refs,
                depends_on=route.depends_on,
                slice_goal=route.output_code,
                integration_surfaces=tuple(sorted(set((f"phase.{route.phase}",) + route.context.gate_ids))),
                acceptance_refs=route.context.acceptance_refs,
                rollback_ref=route.context.rollback_ref,
                state=state,
                next_action_code=action,
            )
        )
    next_ids = tuple(sorted(item.task_id for item in items if item.state is WorkItemState.UNBLOCKED))
    lifecycle_payload = None if lifecycle is None else render_goal_delivery_lifecycle(lifecycle)
    return WorkItemBoard(
        schema_version=WORK_ITEM_BOARD_SCHEMA_VERSION,
        plan_sha256=plan_sha256,
        plan=plan,
        lifecycle_sha256=None if lifecycle_payload is None else hashlib.sha256(lifecycle_payload).hexdigest(),
        lifecycle=lifecycle,
        items=tuple(sorted(items, key=lambda item: item.task_id)),
        next_work_item_ids=next_ids,
        execution_performed=False,
    )


def build_work_item_board(
    plan_payload: bytes | bytearray | memoryview,
    lifecycle_payload: bytes | bytearray | memoryview | None = None,
) -> WorkItemBoard:
    """Build a deterministic, read-only view of P3-F/P3-G task state."""
    try:
        plan = parse_autonomous_task_plan(plan_payload)
        lifecycle = None if lifecycle_payload is None else parse_goal_delivery_lifecycle(lifecycle_payload)
    except (AutonomousTaskOrchestrationError, GoalDeliveryLifecycleError, TypeError, ValueError) as error:
        raise WorkItemBoardError("source plan or lifecycle is invalid") from error
    return _build(plan, lifecycle)


def _item_mapping(value: WorkItem) -> dict[str, object]:
    return {
        "acceptance_refs": list(value.acceptance_refs),
        "depends_on": list(value.depends_on),
        "integration_surfaces": list(value.integration_surfaces),
        "next_action_code": value.next_action_code,
        "rollback_ref": value.rollback_ref,
        "slice_goal": value.slice_goal,
        "source_refs": list(value.source_refs),
        "state": value.state.value,
        "task_id": value.task_id,
        "wave_index": value.wave_index,
    }


def render_work_item_board(value: WorkItemBoard) -> bytes:
    """Render a source-recomputed Work Item Board in canonical JSON."""
    if type(value) is not WorkItemBoard:
        raise TypeError("value must be an exact WorkItemBoard")
    expected = _build(value.plan, value.lifecycle)
    if expected != value:
        raise WorkItemBoardError("board does not match recomputed source projection")
    return canonical_json_bytes(
        {
            "execution_performed": value.execution_performed,
            "items": [_item_mapping(item) for item in value.items],
            "lifecycle": None if value.lifecycle is None else json.loads(render_goal_delivery_lifecycle(value.lifecycle)),
            "lifecycle_sha256": value.lifecycle_sha256,
            "next_work_item_ids": list(value.next_work_item_ids),
            "plan": json.loads(render_autonomous_task_plan(value.plan)),
            "plan_sha256": value.plan_sha256,
            "schema_version": value.schema_version,
        }
    )


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkItemBoardError("duplicate JSON object key")
        result[key] = value
    return result


def _closed(value: object, fields: frozenset[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise WorkItemBoardError(f"{label} has unknown or missing fields")
    return value


def _parse_refs(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise WorkItemBoardError(f"{label} must be a JSON array")
    return _refs(tuple(value), label, allow_empty=allow_empty)


def _parse_item(value: object) -> WorkItem:
    item = _closed(value, frozenset({"acceptance_refs", "depends_on", "integration_surfaces", "next_action_code", "rollback_ref", "slice_goal", "source_refs", "state", "task_id", "wave_index"}), "work_item")
    if type(item["wave_index"]) is not int:
        raise WorkItemBoardError("work_item.wave_index must be an integer")
    if type(item["state"]) is not str:
        raise WorkItemBoardError("work_item.state must be a string enum")
    try:
        state = WorkItemState(item["state"])
    except ValueError as error:
        raise WorkItemBoardError("work_item.state has an unsupported value") from error
    return WorkItem(
        task_id=_code(item["task_id"], "work_item.task_id"),
        wave_index=item["wave_index"],
        source_refs=_parse_refs(item["source_refs"], "work_item.source_refs"),
        depends_on=_parse_refs(item["depends_on"], "work_item.depends_on", allow_empty=True),
        slice_goal=_code(item["slice_goal"], "work_item.slice_goal"),
        integration_surfaces=_parse_refs(item["integration_surfaces"], "work_item.integration_surfaces"),
        acceptance_refs=_parse_refs(item["acceptance_refs"], "work_item.acceptance_refs"),
        rollback_ref=_code(item["rollback_ref"], "work_item.rollback_ref"),
        state=state,
        next_action_code=_code(item["next_action_code"], "work_item.next_action_code"),
    )


def parse_work_item_board(payload: bytes | bytearray | memoryview) -> WorkItemBoard:
    """Parse and source-recompute a canonical Work Item Board."""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    raw = bytes(payload)
    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkItemBoardError("board payload is not valid UTF-8 JSON") from error
    item = _closed(decoded, frozenset({"execution_performed", "items", "lifecycle", "lifecycle_sha256", "next_work_item_ids", "plan", "plan_sha256", "schema_version"}), "board")
    if item["schema_version"] != WORK_ITEM_BOARD_SCHEMA_VERSION:
        raise WorkItemBoardError("unsupported work-item-board schema_version")
    if type(item["execution_performed"]) is not bool:
        raise WorkItemBoardError("board.execution_performed must be boolean")
    try:
        plan = parse_autonomous_task_plan(canonical_json_bytes(item["plan"]))
        lifecycle = None if item["lifecycle"] is None else parse_goal_delivery_lifecycle(canonical_json_bytes(item["lifecycle"]))
    except (SchemaError, AutonomousTaskOrchestrationError, GoalDeliveryLifecycleError, TypeError, ValueError) as error:
        raise WorkItemBoardError("board source projection is invalid") from error
    if item["lifecycle_sha256"] is not None:
        _digest(item["lifecycle_sha256"], "board.lifecycle_sha256")
    items = item["items"]
    if not isinstance(items, list):
        raise WorkItemBoardError("board.items must be a JSON array")
    board = WorkItemBoard(
        schema_version=item["schema_version"],
        plan_sha256=_digest(item["plan_sha256"], "board.plan_sha256"),
        plan=plan,
        lifecycle_sha256=item["lifecycle_sha256"],
        lifecycle=lifecycle,
        items=tuple(_parse_item(value) for value in items),
        next_work_item_ids=_parse_refs(item["next_work_item_ids"], "board.next_work_item_ids", allow_empty=True),
        execution_performed=item["execution_performed"],
    )
    expected = _build(plan, lifecycle)
    if board != expected or render_work_item_board(board) != raw:
        raise WorkItemBoardError("board payload does not match canonical source projection")
    return board


__all__ = [
    "WORK_ITEM_BOARD_SCHEMA_VERSION",
    "WorkItemBoardError",
    "WorkItemState",
    "WorkItem",
    "WorkItemBoard",
    "build_work_item_board",
    "render_work_item_board",
    "parse_work_item_board",
]

