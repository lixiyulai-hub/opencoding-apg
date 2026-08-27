"""Additive user-result presentation with one terminal Status Snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re

from .autonomous_task_orchestration import (
    AutonomousTaskAcceptance,
    render_autonomous_task_acceptance,
)
from .commands.progress import ProgressOutcome, run_progress
from .continuous_delivery_harness import ContinuousDeliveryPlan
from .goal_delivery_lifecycle import GoalDeliveryLifecycle, lifecycle_user_result
from .idea_result_session import IdeaResultSession, idea_result_user_result
from .lifecycle_work_item_result import (
    LifecycleWorkItemResult,
    lifecycle_work_item_user_result,
)
from .intake_routing import RoutingDisposition
from .intake_ux import GuidedIntakeView, render_guided_intake_view
from .requirement_trace_consolidation import (
    RequirementTraceConsolidation,
    requirement_trace_user_result,
)
from .target_project_orchestration import (
    TargetProjectOrchestration,
    target_project_user_result,
)


MAX_PRESENTATION_VALUE_LENGTH = 2048
_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,95}\Z")
_REQUIRED_RESULT_KEYS = frozenset({"status", "result", "next_step", "phase"})
_STATUS_PREFIXES = (
    "Scope: ",
    "Progress basis: ",
    "Completed work: ",
    "Total progress: ",
    "Program progress: ",
    "Current phase: ",
    "Lifecycle stage: ",
    "Program stage (current): ",
    "Next lifecycle boundary: ",
    "Immediate program transaction: ",
    "Following program stage: ",
    "Roadmap: ",
    "Delivery and Gates: ",
    "Next automatic work: ",
    "Human gate: ",
    "Blockers and review: ",
    "Later boundaries: ",
    "Continuation: ",
)


class PresentationError(ValueError):
    """Raised when a user-facing envelope is incomplete or contradictory."""


def _code(value: object, label: str) -> str:
    if type(value) is not str or not _CODE.fullmatch(value):
        raise PresentationError(f"{label} must be a bounded stable code")
    return value


def _single_line(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_PRESENTATION_VALUE_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise PresentationError(f"{label} must be bounded single-line text")
    return value


def _result_items(value: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        raise TypeError("compact_result must be a mapping")
    items = tuple(value.items())
    if not items or len(items) > 16:
        raise PresentationError("compact_result must contain a bounded result")
    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, (key, item) in enumerate(items):
        normalized_key = _code(key, f"compact_result key {index}")
        if normalized_key in seen:
            raise PresentationError("compact_result keys must be unique")
        seen.add(normalized_key)
        normalized.append(
            (
                normalized_key,
                _single_line(item, f"compact_result[{normalized_key}]"),
            )
        )
    if not _REQUIRED_RESULT_KEYS.issubset(seen):
        missing = ",".join(sorted(_REQUIRED_RESULT_KEYS - seen))
        raise PresentationError(f"compact_result is missing required keys: {missing}")
    return tuple(normalized)


def _validate_status_snapshot(value: str) -> str:
    if type(value) is not str:
        raise TypeError("status_snapshot must be text")
    normalized = value.rstrip("\n")
    lines = normalized.splitlines()
    if (
        not lines
        or lines[0] != "Status Snapshot"
        or lines.count("Status Snapshot") != 1
        or not lines[-1].startswith("Continuation: ")
        or any(
            sum(line.startswith(prefix) for line in lines) != 1
            for prefix in _STATUS_PREFIXES
        )
    ):
        raise PresentationError("status_snapshot does not satisfy the fixed envelope")
    return normalized


def _not_computable_snapshot(
    *,
    phase: str,
    human_gate: str | None = None,
    continuation: ContinuousDeliveryPlan | None = None,
) -> str:
    if continuation is None:
        continuation_text = (
            "state=inspect; action=inspect-progress-scope; owner=harness-controller; "
            "requires_existing_authority=false; dispatch_permitted=false; "
            "resume_condition=resume.after-progress-source-is-readable"
        )
        next_action = "inspect-progress-scope"
        gate = human_gate or "none"
    else:
        if type(continuation) is not ContinuousDeliveryPlan:
            raise TypeError("continuation must be an exact ContinuousDeliveryPlan")
        if not continuation.actions:
            raise PresentationError("continuation must contain an action")
        primary = continuation.actions[0]
        next_action = primary.action_code
        gate = primary.action_code if continuation.human_gate_required else human_gate or "none"
        continuation_text = (
            f"state={continuation.state.value}; action={primary.action_code}; "
            f"owner={primary.owner.value}; "
            f"requires_existing_authority={'true' if primary.requires_existing_authority else 'false'}; "
            f"dispatch_permitted={'true' if continuation.dispatch_permitted else 'false'}; "
            f"resume_condition={continuation.resume_condition}"
        )
    return "\n".join(
        (
            "Status Snapshot",
            "Scope: not-computable",
            "Progress basis: definition_id=absent; denominator_tasks=not-computable; "
            "denominator_weight=not-computable; lifecycle_ref=not-computable; "
            "plan_id=not-computable",
            "Completed work: unavailable",
            "Total progress: execution=not-computable verified=not-computable",
            "Program progress: scope=not-computable; definition_id=absent; "
            "execution=not-computable verified=not-computable; "
            "reason=progress-source-unavailable",
            f"Current phase: {phase}",
            "Lifecycle stage: unavailable; execution=not-computable verified=not-computable",
            "Program stage (current): unavailable/not-computable; packages=not-computable; "
            "execution=not-computable verified=not-computable",
            "Next lifecycle boundary: unavailable/not-computable",
            "Immediate program transaction: unavailable/not-computable",
            "Following program stage: unavailable/not-computable",
            "Roadmap: unavailable/not-computable",
            "Delivery and Gates: target=none; state=not-computable; gate_health=unavailable",
            f"Next automatic work: {next_action}",
            f"Human gate: {gate}",
            "Blockers and review: reasons=progress-source-unavailable; "
            "independent_review=unavailable",
            "Later boundaries: unavailable/not-computable",
            f"Continuation: {continuation_text}",
        )
    )


def _status_snapshot(
    *,
    compact_result: Mapping[str, str],
    target: str | Path | None,
    progress_outcome: ProgressOutcome | None,
    human_gate: str | None = None,
    continuation: ContinuousDeliveryPlan | None = None,
) -> str:
    if target is not None and progress_outcome is not None:
        raise PresentationError("provide target or progress_outcome, not both")
    if progress_outcome is not None:
        if type(progress_outcome) is not ProgressOutcome:
            raise TypeError("progress_outcome must be an exact ProgressOutcome")
        if continuation is not None and progress_outcome.continuation != continuation:
            raise PresentationError("continuation does not match the progress outcome")
        return _validate_status_snapshot(progress_outcome.status_snapshot)
    if target is not None:
        outcome = run_progress(target)
        if continuation is not None and outcome.continuation != continuation:
            raise PresentationError("continuation does not match the target progress outcome")
        return _validate_status_snapshot(outcome.status_snapshot)
    return _validate_status_snapshot(
        _not_computable_snapshot(
            phase=compact_result["phase"],
            human_gate=human_gate,
            continuation=continuation,
        )
    )


@dataclass(frozen=True)
class UserFacingPresentation:
    """Immutable additive envelope; the underlying domain result stays unchanged."""

    route_id: str
    result_items: tuple[tuple[str, str], ...]
    status_snapshot: str

    def __post_init__(self) -> None:
        if type(self) is not UserFacingPresentation:
            raise PresentationError("UserFacingPresentation subclasses are not accepted")
        _code(self.route_id, "route_id")
        if type(self.result_items) is not tuple:
            raise PresentationError("result_items must be an immutable tuple")
        validated = _result_items(dict(self.result_items))
        if validated != self.result_items:
            raise PresentationError("result_items must be canonical and unique")
        _validate_status_snapshot(self.status_snapshot)

    @property
    def compact_result(self) -> dict[str, str]:
        return dict(self.result_items)

    def render_text(self) -> str:
        lines = ["User Result", f"Route: {self.route_id}"]
        lines.extend(
            f"{key.replace('_', ' ').title()}: {value}"
            for key, value in self.result_items
        )
        rendered = "\n".join((*lines, self.status_snapshot))
        if rendered.splitlines().count("Status Snapshot") != 1:
            raise PresentationError("presentation must contain exactly one Status Snapshot")
        if not rendered.splitlines()[-1].startswith("Continuation: "):
            raise PresentationError("Continuation must be the final presentation line")
        return rendered


def present_user_result(
    route_id: str,
    compact_result: Mapping[str, str],
    *,
    target: str | Path | None = None,
    progress_outcome: ProgressOutcome | None = None,
    human_gate: str | None = None,
    continuation: ContinuousDeliveryPlan | None = None,
) -> UserFacingPresentation:
    items = _result_items(compact_result)
    normalized = dict(items)
    if human_gate is not None:
        _code(human_gate, "human_gate")
    return UserFacingPresentation(
        route_id=_code(route_id, "route_id"),
        result_items=items,
        status_snapshot=_status_snapshot(
            compact_result=normalized,
            target=target,
            progress_outcome=progress_outcome,
            human_gate=human_gate,
            continuation=continuation,
        ),
    )


def present_lifecycle_result(
    value: GoalDeliveryLifecycle,
    **progress: object,
) -> UserFacingPresentation:
    return present_user_result("p3-g.lifecycle", lifecycle_user_result(value), **progress)


def present_lifecycle_work_item_result(
    value: LifecycleWorkItemResult,
    **progress: object,
) -> UserFacingPresentation:
    """Present the P3-G result together with its next-unblocked Work Item IDs."""

    return present_user_result(
        "p3-g.lifecycle-work-items",
        lifecycle_work_item_user_result(value),
        **progress,
    )


def present_idea_result_session(
    value: IdeaResultSession,
    **progress: object,
) -> UserFacingPresentation:
    return present_user_result("p3-i.idea-result", idea_result_user_result(value), **progress)


def present_requirement_trace(
    value: RequirementTraceConsolidation,
    **progress: object,
) -> UserFacingPresentation:
    return present_user_result(
        "p3-h.requirement-trace",
        requirement_trace_user_result(value),
        **progress,
    )


def present_target_project(
    value: TargetProjectOrchestration,
    **progress: object,
) -> UserFacingPresentation:
    return present_user_result(
        "p3-j.target-project",
        target_project_user_result(value),
        **progress,
    )


def present_guided_intake_view(
    value: GuidedIntakeView,
    **progress: object,
) -> UserFacingPresentation:
    payload = json.loads(render_guided_intake_view(value).decode("utf-8"))
    disposition = value.disposition
    if disposition is RoutingDisposition.NEXT_QUESTION:
        next_step = "answer-guided-question"
        human_gate = None
    elif disposition is RoutingDisposition.OWNER_GATE:
        next_step = "provide-owner-decision"
        human_gate = "owner-decision"
    else:
        next_step = "prepare-governance-preview"
        human_gate = None
    return present_user_result(
        "p2-e.guided-intake",
        {
            "status": payload["disposition"],
            "result": payload["status_message"],
            "next_step": next_step,
            "phase": "guided-intake",
        },
        human_gate=human_gate,
        **progress,
    )


def present_p3f_acceptance(
    value: AutonomousTaskAcceptance,
    **progress: object,
) -> UserFacingPresentation:
    payload = json.loads(render_autonomous_task_acceptance(value).decode("utf-8"))
    if payload["blocked_task_ids"]:
        next_step = "resolve-blocked-task-evidence"
    elif payload["pending_task_ids"]:
        next_step = "collect-pending-task-evidence"
    else:
        next_step = "review-autonomous-task-acceptance"
    return present_user_result(
        "p3-f.task-acceptance",
        {
            "status": payload["state"],
            "result": payload["user_summary_code"],
            "next_step": next_step,
            "phase": "p3-f-acceptance",
        },
        **progress,
    )


def present_loop_completion(
    continuation: ContinuousDeliveryPlan,
    *,
    target: str | Path | None = None,
    progress_outcome: ProgressOutcome | None = None,
) -> UserFacingPresentation:
    if type(continuation) is not ContinuousDeliveryPlan:
        raise TypeError("continuation must be an exact ContinuousDeliveryPlan")
    if not continuation.actions:
        raise PresentationError("continuation must contain an action")
    primary = continuation.actions[0]
    return present_user_result(
        "p6.loop-completion",
        {
            "status": continuation.state.value,
            "result": primary.action_code,
            "next_step": continuation.resume_condition,
            "phase": "continuous-delivery-harness",
        },
        target=target,
        progress_outcome=progress_outcome,
        continuation=continuation,
    )


__all__ = [
    "MAX_PRESENTATION_VALUE_LENGTH",
    "PresentationError",
    "UserFacingPresentation",
    "present_user_result",
    "present_lifecycle_result",
    "present_lifecycle_work_item_result",
    "present_idea_result_session",
    "present_requirement_trace",
    "present_target_project",
    "present_guided_intake_view",
    "present_p3f_acceptance",
    "present_loop_completion",
]
