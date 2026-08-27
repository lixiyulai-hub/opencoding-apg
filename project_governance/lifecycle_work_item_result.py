"""Source-bound next-unblocked Work Item result for a P3-G lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from .autonomous_task_orchestration import render_autonomous_task_plan
from .goal_delivery_lifecycle import (
    GoalDeliveryLifecycle,
    GoalDeliveryLifecycleError,
    LifecyclePhase,
    parse_goal_delivery_lifecycle,
    render_goal_delivery_lifecycle,
)
from .storage import canonical_json_bytes
from .work_item_board import (
    WorkItemBoardError,
    build_work_item_board,
    render_work_item_board,
)


P3G_WORK_ITEM_RESULT_SCHEMA_VERSION = "1.0"
MAX_NEXT_WORK_ITEMS = 32


class LifecycleWorkItemResultError(ValueError):
    """Raised when a P3-G Work Item result is not source-bound and canonical."""


def _code(value: object, label: str) -> str:
    if type(value) is not str or not value or len(value) > 128:
        raise LifecycleWorkItemResultError(f"{label} must be bounded non-empty text")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
    if value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789" or any(
        character not in allowed for character in value
    ):
        raise LifecycleWorkItemResultError(f"{label} must be a stable code")
    return value


def _digest(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise LifecycleWorkItemResultError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _next_ids(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > MAX_NEXT_WORK_ITEMS:
        raise LifecycleWorkItemResultError("next_work_item_ids must be a bounded immutable tuple")
    normalized = tuple(_code(item, f"next_work_item_ids[{index}]") for index, item in enumerate(value))
    if normalized != tuple(sorted(set(normalized))):
        raise LifecycleWorkItemResultError("next_work_item_ids must use canonical unique order")
    return normalized


@dataclass(frozen=True)
class LifecycleWorkItemResult:
    """Read-only P3-G result with the exact next-unblocked Work Item IDs."""

    schema_version: str
    lifecycle_sha256: str
    lifecycle: GoalDeliveryLifecycle
    board_sha256: str
    status: str
    result: str
    next_step: str
    phase: LifecyclePhase
    next_work_item_ids: tuple[str, ...]
    execution_performed: bool

    def __post_init__(self) -> None:
        if type(self) is not LifecycleWorkItemResult:
            raise LifecycleWorkItemResultError("LifecycleWorkItemResult subclasses are not accepted")
        if self.schema_version != P3G_WORK_ITEM_RESULT_SCHEMA_VERSION:
            raise LifecycleWorkItemResultError("unsupported lifecycle Work Item result schema_version")
        _digest(self.lifecycle_sha256, "lifecycle_sha256")
        if type(self.lifecycle) is not GoalDeliveryLifecycle:
            raise LifecycleWorkItemResultError("lifecycle must be an exact GoalDeliveryLifecycle")
        if hashlib.sha256(render_goal_delivery_lifecycle(self.lifecycle)).hexdigest() != self.lifecycle_sha256:
            raise LifecycleWorkItemResultError("lifecycle_sha256 does not bind lifecycle")
        _digest(self.board_sha256, "board_sha256")
        _code(self.status, "status")
        _code(self.result, "result")
        _code(self.next_step, "next_step")
        if type(self.phase) is not LifecyclePhase:
            raise LifecycleWorkItemResultError("phase must be LifecyclePhase")
        _next_ids(self.next_work_item_ids)
        if self.execution_performed is not False:
            raise LifecycleWorkItemResultError("P3-G Work Item result cannot claim execution")


def _build(lifecycle: GoalDeliveryLifecycle) -> LifecycleWorkItemResult:
    lifecycle_payload = render_goal_delivery_lifecycle(lifecycle)
    try:
        board = build_work_item_board(
            render_autonomous_task_plan(lifecycle.plan),
            lifecycle_payload,
        )
        board_payload = render_work_item_board(board)
    except (WorkItemBoardError, GoalDeliveryLifecycleError, TypeError, ValueError) as error:
        raise LifecycleWorkItemResultError("lifecycle cannot produce a bound Work Item Board") from error
    return LifecycleWorkItemResult(
        schema_version=P3G_WORK_ITEM_RESULT_SCHEMA_VERSION,
        lifecycle_sha256=hashlib.sha256(lifecycle_payload).hexdigest(),
        lifecycle=lifecycle,
        board_sha256=hashlib.sha256(board_payload).hexdigest(),
        status=lifecycle.user_result.status_code,
        result=lifecycle.user_result.result_code,
        next_step=lifecycle.user_result.next_step_code,
        phase=lifecycle.phase,
        next_work_item_ids=board.next_work_item_ids,
        execution_performed=False,
    )


def build_lifecycle_work_item_result(
    lifecycle_payload: bytes | bytearray | memoryview,
) -> LifecycleWorkItemResult:
    """Project canonical P3-G lifecycle bytes into its next-unblocked Work Item result."""

    try:
        lifecycle = parse_goal_delivery_lifecycle(lifecycle_payload)
    except (GoalDeliveryLifecycleError, TypeError, ValueError) as error:
        raise LifecycleWorkItemResultError("lifecycle payload is invalid") from error
    return _build(lifecycle)


def _mapping(value: LifecycleWorkItemResult) -> dict[str, object]:
    return {
        "board_sha256": value.board_sha256,
        "execution_performed": value.execution_performed,
        "lifecycle": json.loads(render_goal_delivery_lifecycle(value.lifecycle)),
        "lifecycle_sha256": value.lifecycle_sha256,
        "next_step": value.next_step,
        "next_work_item_ids": list(value.next_work_item_ids),
        "phase": value.phase.value,
        "result": value.result,
        "schema_version": value.schema_version,
        "status": value.status,
    }


def render_lifecycle_work_item_result(value: LifecycleWorkItemResult) -> bytes:
    """Render a canonical result that is recomputed from its P3-G lifecycle source."""

    if type(value) is not LifecycleWorkItemResult:
        raise TypeError("value must be an exact LifecycleWorkItemResult")
    expected = _build(value.lifecycle)
    if expected != value:
        raise LifecycleWorkItemResultError("result does not match its lifecycle Work Item projection")
    return canonical_json_bytes(_mapping(value))


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LifecycleWorkItemResultError("duplicate JSON object key")
        result[key] = value
    return result


def _closed(value: object) -> dict[str, object]:
    fields = frozenset(
        {
            "board_sha256",
            "execution_performed",
            "lifecycle",
            "lifecycle_sha256",
            "next_step",
            "next_work_item_ids",
            "phase",
            "result",
            "schema_version",
            "status",
        }
    )
    if not isinstance(value, dict) or set(value) != fields:
        raise LifecycleWorkItemResultError("result has unknown or missing fields")
    return value


def parse_lifecycle_work_item_result(
    payload: bytes | bytearray | memoryview,
) -> LifecycleWorkItemResult:
    """Parse and source-recompute a canonical P3-G Work Item result."""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    raw = bytes(payload)
    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LifecycleWorkItemResultError("result payload is not valid UTF-8 JSON") from error
    item = _closed(decoded)
    if item["schema_version"] != P3G_WORK_ITEM_RESULT_SCHEMA_VERSION:
        raise LifecycleWorkItemResultError("unsupported lifecycle Work Item result schema_version")
    if type(item["execution_performed"]) is not bool:
        raise LifecycleWorkItemResultError("execution_performed must be boolean")
    if not isinstance(item["next_work_item_ids"], list):
        raise LifecycleWorkItemResultError("next_work_item_ids must be a JSON array")
    if type(item["phase"]) is not str:
        raise LifecycleWorkItemResultError("phase must be a string enum")
    try:
        lifecycle = parse_goal_delivery_lifecycle(canonical_json_bytes(item["lifecycle"]))
        phase = LifecyclePhase(item["phase"])
    except (GoalDeliveryLifecycleError, TypeError, ValueError) as error:
        raise LifecycleWorkItemResultError("result lifecycle or phase is invalid") from error
    result = LifecycleWorkItemResult(
        schema_version=item["schema_version"],
        lifecycle_sha256=_digest(item["lifecycle_sha256"], "lifecycle_sha256"),
        lifecycle=lifecycle,
        board_sha256=_digest(item["board_sha256"], "board_sha256"),
        status=_code(item["status"], "status"),
        result=_code(item["result"], "result"),
        next_step=_code(item["next_step"], "next_step"),
        phase=phase,
        next_work_item_ids=_next_ids(tuple(item["next_work_item_ids"])),
        execution_performed=item["execution_performed"],
    )
    expected = _build(lifecycle)
    if result != expected or render_lifecycle_work_item_result(result) != raw:
        raise LifecycleWorkItemResultError("result payload does not match canonical source projection")
    return result


def lifecycle_work_item_user_result(value: LifecycleWorkItemResult) -> dict[str, str]:
    """Return the compact P3-G result and its source-bound next-unblocked work IDs."""

    if type(value) is not LifecycleWorkItemResult:
        raise TypeError("value must be an exact LifecycleWorkItemResult")
    render_lifecycle_work_item_result(value)
    return {
        "status": value.status,
        "result": value.result,
        "next_step": value.next_step,
        "phase": value.phase.value,
        "next_work_items": ",".join(value.next_work_item_ids) or "none",
    }


__all__ = [
    "P3G_WORK_ITEM_RESULT_SCHEMA_VERSION",
    "MAX_NEXT_WORK_ITEMS",
    "LifecycleWorkItemResultError",
    "LifecycleWorkItemResult",
    "build_lifecycle_work_item_result",
    "render_lifecycle_work_item_result",
    "parse_lifecycle_work_item_result",
    "lifecycle_work_item_user_result",
]
