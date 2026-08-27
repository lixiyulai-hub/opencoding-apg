"""Pure continuity planner for source-bound delivery progress.

The harness converts a recomputable :class:`ProgressSnapshot` and optional
feedback-loop decision into a small, deterministic continuation plan.  It is
intentionally not an executor: a ``DISPATCH`` action only identifies the role
that would own already-authorized work, and it never supplies that authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Any

from .feedback_loops import LoopDecision, LoopStopState
from .progress_projection import (
    DeliveryState,
    GateHealth,
    ProgressScopeStatus,
    ProgressSnapshot,
    render_progress_snapshot,
)
from .program_progress import (
    ProgramDeliveryState,
    ProgramProgressSnapshot,
    ProgramScopeStatus,
)
from .storage import canonical_json_bytes


CONTINUOUS_DELIVERY_HARNESS_SCHEMA_VERSION = "1.0"
MAX_HARNESS_ACTIONS = 5
_BASIS_POINTS = 10_000
_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")


class ContinuousDeliveryHarnessError(ValueError):
    """Raised when a source-bound progress snapshot cannot be planned safely."""


class HarnessState(str, Enum):
    """The only states a continuity planner may recommend."""

    INSPECT = "inspect"
    # PROGRESS is the explicit read-only projection step between inspection
    # and planning. It never grants execution authority.
    PROGRESS = "progress"
    PLAN_GATE = "plan-gate"
    DISPATCH = "dispatch"
    VALIDATE = "validate"
    INDEPENDENT_VERIFY = "independent-verify"
    REPORT = "report"
    REQUEUE = "requeue"
    HUMAN_GATE = "human-gate"
    FREEZE = "freeze"
    COMPLETE = "complete"


# The planner's normal bounded loop.  This is intentionally a declarative
# contract: it documents the order in which a caller may move between
# read-only projection, an already-authorized transaction, its Gates, review,
# reporting, and a bounded requeue.  The harness never executes these steps.
CONTINUOUS_DELIVERY_WORKFLOW = (
    HarnessState.INSPECT,
    HarnessState.PROGRESS,
    HarnessState.PLAN_GATE,
    HarnessState.DISPATCH,
    HarnessState.VALIDATE,
    HarnessState.INDEPENDENT_VERIFY,
    HarnessState.REPORT,
    HarnessState.REQUEUE,
)


class HarnessOwner(str, Enum):
    """Stable roles responsible for a recommended continuation action."""

    CONTROLLER = "harness-controller"
    PLANNER = "plan-owner"
    EXECUTOR = "authorized-executor"
    VALIDATOR = "validator"
    INDEPENDENT_REVIEWER = "independent-reviewer"
    REPORTER = "status-reporter"
    OWNER = "project-owner"


_OWNER_BY_STATE = {
    HarnessState.INSPECT: HarnessOwner.CONTROLLER,
    HarnessState.PROGRESS: HarnessOwner.CONTROLLER,
    HarnessState.PLAN_GATE: HarnessOwner.PLANNER,
    HarnessState.DISPATCH: HarnessOwner.EXECUTOR,
    HarnessState.VALIDATE: HarnessOwner.VALIDATOR,
    HarnessState.INDEPENDENT_VERIFY: HarnessOwner.INDEPENDENT_REVIEWER,
    HarnessState.REPORT: HarnessOwner.REPORTER,
    HarnessState.REQUEUE: HarnessOwner.CONTROLLER,
    HarnessState.HUMAN_GATE: HarnessOwner.OWNER,
    HarnessState.FREEZE: HarnessOwner.CONTROLLER,
    HarnessState.COMPLETE: HarnessOwner.REPORTER,
}

_ACTION_CODE_BY_STATE = {
    HarnessState.INSPECT: "inspect-progress-scope",
    HarnessState.PROGRESS: "compute-progress-snapshot",
    HarnessState.PLAN_GATE: "prepare-plan-gate",
    HarnessState.DISPATCH: "queue-authorized-work",
    HarnessState.VALIDATE: "run-bound-validation",
    HarnessState.INDEPENDENT_VERIFY: "request-independent-verification",
    HarnessState.REPORT: "render-status-snapshot",
    HarnessState.REQUEUE: "requeue-bounded-loop",
    HarnessState.HUMAN_GATE: "request-owner-decision",
    HarnessState.FREEZE: "freeze-and-preserve-evidence",
    HarnessState.COMPLETE: "record-completion-status",
}

_RESUME_CONDITION_BY_STATE = {
    HarnessState.INSPECT: "resume.after-progress-source-is-readable",
    HarnessState.PROGRESS: "resume.after-source-bound-progress-is-computed",
    HarnessState.PLAN_GATE: "resume.after-plan-gate-pass-and-authority-is-bound",
    HarnessState.DISPATCH: "resume.after-authorized-executor-is-available",
    HarnessState.VALIDATE: "resume.after-selected-gates-pass",
    HarnessState.INDEPENDENT_VERIFY: "resume.after-independent-review-accepts-evidence",
    HarnessState.REPORT: "resume.after-status-snapshot-is-recorded",
    HarnessState.REQUEUE: "resume.after-bounded-loop-continues-without-stop-condition",
    HarnessState.HUMAN_GATE: "resume.after-owner-decision-is-recorded",
    HarnessState.FREEZE: "resume.after-blocker-scope-drift-or-missing-evidence-is-resolved",
    HarnessState.COMPLETE: "resume.only-on-an-explicit-successor-transaction-or-new-scope",
}

_DISPATCH_PERMITTED_STATES = frozenset(
    {
        HarnessState.DISPATCH,
    }
)

_FREEZE_LOOP_STATES = frozenset(
    {
        LoopStopState.BLOCKED,
        LoopStopState.BUDGET_EXHAUSTED,
        LoopStopState.FAILURE_THRESHOLD,
        LoopStopState.NO_PROGRESS,
    }
)


def _code(value: object, label: str) -> str:
    if type(value) is not str or not _CODE.fullmatch(value):
        raise ContinuousDeliveryHarnessError(f"{label} must be a stable code")
    return value


def _canonical_codes(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise ContinuousDeliveryHarnessError(f"{label} must be an immutable tuple")
    result = tuple(_code(item, f"{label}[{index}]") for index, item in enumerate(value))
    if result != tuple(sorted(set(result))):
        raise ContinuousDeliveryHarnessError(f"{label} must use canonical unique order")
    return result


def _basis_points(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= _BASIS_POINTS:
        raise ContinuousDeliveryHarnessError(
            f"{label} must be an integer from 0 through {_BASIS_POINTS}"
        )
    return value


def _snapshot_codes(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise ContinuousDeliveryHarnessError(f"snapshot.{label} must be an immutable tuple")
    return tuple(_code(item, f"snapshot.{label}[{index}]") for index, item in enumerate(value))


def _snapshot_field(snapshot: ProgressSnapshot, name: str) -> object:
    try:
        return getattr(snapshot, name)
    except AttributeError as error:
        raise ContinuousDeliveryHarnessError(
            f"ProgressSnapshot is missing required field: {name}"
        ) from error


def _validated_program_snapshot(
    snapshot: ProgramProgressSnapshot | None,
) -> tuple[
    ProgramScopeStatus | None,
    ProgramDeliveryState | None,
    str | None,
    str | None,
    tuple[str, ...],
]:
    """Validate optional program facts and return safe planner hints."""

    if snapshot is None:
        return None, None, None, None, ()
    if type(snapshot) is not ProgramProgressSnapshot:
        raise ContinuousDeliveryHarnessError(
            "program_snapshot must be an exact ProgramProgressSnapshot or None"
        )
    return (
        snapshot.scope_status,
        snapshot.delivery_state,
        snapshot.next_transaction_id,
        snapshot.human_gate_transaction_id,
        tuple(snapshot.reason_codes),
    )


def _validated_snapshot(
    snapshot: ProgressSnapshot,
) -> tuple[
    ProgressScopeStatus,
    DeliveryState,
    GateHealth,
    int | None,
    int | None,
    tuple[str, ...],
    tuple[str, ...],
]:
    if type(snapshot) is not ProgressSnapshot:
        raise ContinuousDeliveryHarnessError("snapshot must be an exact ProgressSnapshot")
    if _snapshot_field(snapshot, "execution_performed") is not False:
        raise ContinuousDeliveryHarnessError(
            "ProgressSnapshot must not claim execution was performed"
        )
    scope_status = _snapshot_field(snapshot, "scope_status")
    delivery_state = _snapshot_field(snapshot, "delivery_state")
    gate_health = _snapshot_field(snapshot, "gate_health")
    if type(scope_status) is not ProgressScopeStatus:
        raise ContinuousDeliveryHarnessError(
            "snapshot.scope_status must be a ProgressScopeStatus"
        )
    if type(delivery_state) is not DeliveryState:
        raise ContinuousDeliveryHarnessError(
            "snapshot.delivery_state must be a DeliveryState"
        )
    if type(gate_health) is not GateHealth:
        raise ContinuousDeliveryHarnessError("snapshot.gate_health must be a GateHealth")
    next_actions = _snapshot_codes(
        _snapshot_field(snapshot, "next_actions"), "next_actions"
    )
    reason_codes = _snapshot_codes(
        _snapshot_field(snapshot, "reason_codes"), "reason_codes"
    )
    if scope_status is not ProgressScopeStatus.COMPUTABLE:
        execution = _snapshot_field(snapshot, "execution_progress_basis_points")
        verified = _snapshot_field(snapshot, "verified_progress_basis_points")
        if execution is not None or verified is not None:
            raise ContinuousDeliveryHarnessError(
                "not-computable progress must use null execution and verified values"
            )
        return (
            scope_status,
            delivery_state,
            gate_health,
            None,
            None,
            next_actions,
            reason_codes,
        )
    execution = _basis_points(
        _snapshot_field(snapshot, "execution_progress_basis_points"),
        "execution_progress_basis_points",
    )
    verified = _basis_points(
        _snapshot_field(snapshot, "verified_progress_basis_points"),
        "verified_progress_basis_points",
    )
    if verified > execution:
        raise ContinuousDeliveryHarnessError(
            "verified progress must not exceed execution progress"
        )
    return (
        scope_status,
        delivery_state,
        gate_health,
        execution,
        verified,
        next_actions,
        reason_codes,
    )


def _has_hint(values: tuple[str, ...], *prefixes: str) -> bool:
    return any(
        item == prefix or item.startswith(f"{prefix}-") or item.startswith(f"{prefix}.")
        for item in values
        for prefix in prefixes
    )


def _primary_action(
    state: HarnessState,
    reason_codes: tuple[str, ...],
) -> "HarnessAction":
    return HarnessAction(
        state=state,
        owner=_OWNER_BY_STATE[state],
        action_code=_ACTION_CODE_BY_STATE[state],
        reason_codes=reason_codes,
        requires_existing_authority=state
        in {
            HarnessState.DISPATCH,
            HarnessState.VALIDATE,
            HarnessState.INDEPENDENT_VERIFY,
            HarnessState.REQUEUE,
        },
        execution_performed=False,
    )


@dataclass(frozen=True)
class HarnessAction:
    """One bounded recommendation, never an instruction that performs work."""

    state: HarnessState
    owner: HarnessOwner
    action_code: str
    reason_codes: tuple[str, ...]
    requires_existing_authority: bool
    execution_performed: bool = False

    def __post_init__(self) -> None:
        if type(self) is not HarnessAction:
            raise ContinuousDeliveryHarnessError("HarnessAction subclasses are not accepted")
        if type(self.state) is not HarnessState:
            raise ContinuousDeliveryHarnessError("action.state must be a HarnessState")
        if type(self.owner) is not HarnessOwner:
            raise ContinuousDeliveryHarnessError("action.owner must be a HarnessOwner")
        _code(self.action_code, "action.action_code")
        _canonical_codes(self.reason_codes, "action.reason_codes")
        if type(self.requires_existing_authority) is not bool:
            raise ContinuousDeliveryHarnessError(
                "action.requires_existing_authority must be a boolean"
            )
        if self.execution_performed is not False:
            raise ContinuousDeliveryHarnessError("HarnessAction cannot claim execution")


@dataclass(frozen=True)
class ContinuousDeliveryPlan:
    """A source-bound status and continuation recommendation for one checkpoint."""

    schema_version: str
    snapshot: ProgressSnapshot
    loop_stop_state: LoopStopState | None
    state: HarnessState
    actions: tuple[HarnessAction, ...]
    reason_codes: tuple[str, ...]
    dispatch_permitted: bool
    human_gate_required: bool
    resume_condition: str = "resume.after-status-snapshot-is-recorded"
    execution_performed: bool = False
    program_snapshot: ProgramProgressSnapshot | None = None

    def __post_init__(self) -> None:
        if type(self) is not ContinuousDeliveryPlan:
            raise ContinuousDeliveryHarnessError(
                "ContinuousDeliveryPlan subclasses are not accepted"
            )
        if self.schema_version != CONTINUOUS_DELIVERY_HARNESS_SCHEMA_VERSION:
            raise ContinuousDeliveryHarnessError("unsupported continuous-delivery schema")
        _validated_snapshot(self.snapshot)
        _validated_program_snapshot(self.program_snapshot)
        if self.loop_stop_state is not None and type(self.loop_stop_state) is not LoopStopState:
            raise ContinuousDeliveryHarnessError(
                "loop_stop_state must be a LoopStopState or null"
            )
        if type(self.state) is not HarnessState:
            raise ContinuousDeliveryHarnessError("state must be a HarnessState")
        if type(self.actions) is not tuple or not self.actions:
            raise ContinuousDeliveryHarnessError("actions must be a non-empty immutable tuple")
        if len(self.actions) > MAX_HARNESS_ACTIONS:
            raise ContinuousDeliveryHarnessError("actions exceeds the bounded limit")
        if any(type(action) is not HarnessAction for action in self.actions):
            raise ContinuousDeliveryHarnessError("actions contains an invalid action")
        if self.actions[0].state is not self.state:
            raise ContinuousDeliveryHarnessError("first action must bind the plan state")
        if len({action.state for action in self.actions}) != len(self.actions):
            raise ContinuousDeliveryHarnessError("actions must not repeat a state")
        _canonical_codes(self.reason_codes, "reason_codes")
        if type(self.dispatch_permitted) is not bool:
            raise ContinuousDeliveryHarnessError("dispatch_permitted must be a boolean")
        if self.dispatch_permitted is not (self.state in _DISPATCH_PERMITTED_STATES):
            raise ContinuousDeliveryHarnessError(
                "dispatch_permitted must match the primary harness state"
            )
        if self.snapshot.gate_health is GateHealth.EVIDENCE_PENDING and self.state in {
            HarnessState.DISPATCH,
            HarnessState.INDEPENDENT_VERIFY,
            HarnessState.REQUEUE,
        }:
            raise ContinuousDeliveryHarnessError(
                "pending Gate evidence must be validated before dispatch, review, or requeue"
            )
        if type(self.human_gate_required) is not bool:
            raise ContinuousDeliveryHarnessError("human_gate_required must be a boolean")
        if self.human_gate_required is not (self.state is HarnessState.HUMAN_GATE):
            raise ContinuousDeliveryHarnessError(
                "human_gate_required must match the primary harness state"
            )
        if self.resume_condition != _RESUME_CONDITION_BY_STATE[self.state]:
            raise ContinuousDeliveryHarnessError(
                "resume_condition must match the primary harness state"
            )
        if self.execution_performed is not False:
            raise ContinuousDeliveryHarnessError(
                "continuous-delivery planning cannot claim execution"
            )


def _snapshot_state(
    *,
    scope_status: ProgressScopeStatus,
    delivery_state: DeliveryState,
    gate_health: GateHealth,
    execution: int | None,
    verified: int | None,
    next_actions: tuple[str, ...],
    reason_codes: tuple[str, ...],
) -> tuple[HarnessState, tuple[str, ...]]:
    combined = tuple(sorted(set(next_actions + reason_codes)))
    if scope_status is not ProgressScopeStatus.COMPUTABLE:
        return HarnessState.INSPECT, ("progress-scope-not-computable",)
    if delivery_state is DeliveryState.BLOCKED:
        return HarnessState.FREEZE, ("delivery-blocked",)
    if gate_health is GateHealth.EVIDENCE_BLOCKED:
        return HarnessState.FREEZE, ("gate-evidence-blocked",)
    if delivery_state is DeliveryState.TARGET_REACHED:
        return HarnessState.COMPLETE, ("delivery-complete",)
    if delivery_state is DeliveryState.TARGET_PHASE_PENDING:
        return HarnessState.PLAN_GATE, ("delivery-target-phase-pending",)
    if _has_hint(combined, "freeze", "block", "next.resolve-blocker"):
        return HarnessState.FREEZE, ("snapshot-freeze-required",)
    if _has_hint(
        combined,
        "human-gate",
        "confirm",
        "owner-decision",
        "owner-authorization-pending",
        "next.provide-transaction-approval",
    ):
        return HarnessState.HUMAN_GATE, ("human-decision-required",)
    # Gate evidence is an ordering barrier.  It must win over an execution /
    # verification gap so a planner cannot recommend review or dispatch while
    # the transaction's required validation is still pending.
    if gate_health is GateHealth.EVIDENCE_PENDING or _has_hint(
        combined, "validate", "gate"
    ):
        return HarnessState.VALIDATE, ("validation-required",)
    if (
        (execution is not None and verified is not None and execution > verified)
        or _has_hint(
            combined,
            "independent-verify",
            "consolidation-evidence-pending",
            "next.review-final-result",
        )
    ):
        return HarnessState.INDEPENDENT_VERIFY, (
            "execution-ahead-of-verification",
        )
    if _has_hint(
        combined,
        "plan-gate",
        "plan",
        "recommendation-decision-pending",
        "next.decide-recommendation",
    ):
        return HarnessState.PLAN_GATE, ("plan-gate-required",)
    if _has_hint(combined, "progress", "next.compute-progress"):
        return HarnessState.PROGRESS, ("progress-projection-required",)
    if next_actions:
        return HarnessState.DISPATCH, ("next-actions-available",)
    return HarnessState.REPORT, ("status-report-required",)


def plan_continuous_delivery_harness(
    snapshot: ProgressSnapshot,
    loop_decision: LoopDecision | None = None,
    *,
    program_snapshot: ProgramProgressSnapshot | None = None,
) -> ContinuousDeliveryPlan:
    """Return a deterministic continuation plan without executing any work.

    ``dispatch_permitted`` is true only when the immediate primary state is
    ``DISPATCH``. A caller still needs to re-check exact transaction authority,
    scope, budget, rollback, and independent-review obligations before it can
    dispatch. Validation, review, and requeue may require existing authority,
    but do not themselves permit a new dispatch.
    """

    (
        scope_status,
        delivery_state,
        gate_health,
        execution,
        verified,
        next_actions,
        snapshot_reasons,
    ) = _validated_snapshot(snapshot)
    (
        program_scope,
        program_delivery,
        program_next_transaction,
        program_human_gate,
        program_reasons,
    ) = _validated_program_snapshot(program_snapshot)
    if loop_decision is not None and type(loop_decision) is not LoopDecision:
        raise ContinuousDeliveryHarnessError(
            "loop_decision must be a LoopDecision or null"
        )

    state, derived_reasons = _snapshot_state(
        scope_status=scope_status,
        delivery_state=delivery_state,
        gate_health=gate_health,
        execution=execution,
        verified=verified,
        next_actions=next_actions,
        reason_codes=snapshot_reasons,
    )
    loop_stop_state = None if loop_decision is None else loop_decision.stop_state
    if loop_stop_state is not None:
        if type(loop_stop_state) is not LoopStopState:
            raise ContinuousDeliveryHarnessError("loop decision has an invalid stop state")
        loop_reason = f"loop-{loop_stop_state.value}"
        if scope_status is ProgressScopeStatus.COMPUTABLE:
            if loop_stop_state in _FREEZE_LOOP_STATES:
                state, derived_reasons = HarnessState.FREEZE, (loop_reason,)
            elif loop_stop_state is LoopStopState.OWNER_DECISION_REQUIRED:
                state, derived_reasons = HarnessState.HUMAN_GATE, (loop_reason,)
            elif loop_stop_state is LoopStopState.COMPLETED and state not in {
                HarnessState.INSPECT,
                HarnessState.FREEZE,
                HarnessState.HUMAN_GATE,
                HarnessState.COMPLETE,
                HarnessState.PLAN_GATE,
            }:
                state, derived_reasons = HarnessState.REPORT, (loop_reason,)
            elif loop_stop_state is LoopStopState.CONTINUE and state not in {
                HarnessState.FREEZE,
                HarnessState.HUMAN_GATE,
                HarnessState.COMPLETE,
            }:
                state, derived_reasons = HarnessState.REQUEUE, (loop_reason,)

    reasons = tuple(sorted(set(snapshot_reasons + derived_reasons)))
    reasons = tuple(sorted(set(reasons + program_reasons)))
    # Program continuation is deliberately advisory and only takes over when
    # the bounded lifecycle itself has reached a stable completion.  Missing
    # or blocked program evidence freezes that continuation; it never grants
    # dispatch authority.
    if loop_decision is None or loop_decision.stop_state is None:
        if (
            program_scope is ProgramScopeStatus.NOT_COMPUTABLE
            and state is not HarnessState.INSPECT
        ):
            state = HarnessState.FREEZE
            reasons = tuple(sorted(set(reasons + ("program-progress-not-computable",))))
        elif (
            program_delivery is ProgramDeliveryState.BLOCKED
            and state is not HarnessState.INSPECT
        ):
            state = HarnessState.FREEZE
            reasons = tuple(sorted(set(reasons + ("program-delivery-blocked",))))
        elif program_human_gate is not None and state not in {
            HarnessState.INSPECT,
            HarnessState.FREEZE,
        }:
            state = HarnessState.HUMAN_GATE
            reasons = tuple(sorted(set(reasons + ("program-human-gate-required",))))
        elif program_next_transaction is not None and state is HarnessState.COMPLETE:
            state = HarnessState.PLAN_GATE
            reasons = tuple(sorted(set(reasons + ("program-successor-available",))))
    actions: list[HarnessAction] = [_primary_action(state, reasons)]
    if state is not HarnessState.REPORT:
        actions.append(
            _primary_action(
                HarnessState.REPORT,
                tuple(sorted(set(reasons + ("status-report-required",)))),
            )
        )
    return ContinuousDeliveryPlan(
        schema_version=CONTINUOUS_DELIVERY_HARNESS_SCHEMA_VERSION,
        snapshot=snapshot,
        loop_stop_state=loop_stop_state,
        state=state,
        actions=tuple(actions),
        reason_codes=reasons,
        dispatch_permitted=state in _DISPATCH_PERMITTED_STATES,
        human_gate_required=state is HarnessState.HUMAN_GATE,
        resume_condition=_RESUME_CONDITION_BY_STATE[state],
        execution_performed=False,
        program_snapshot=program_snapshot,
    )


def continuous_delivery_harness_mapping(value: ContinuousDeliveryPlan) -> dict[str, Any]:
    """Return the canonical JSON-compatible representation of a harness plan."""

    if type(value) is not ContinuousDeliveryPlan:
        raise ContinuousDeliveryHarnessError("value must be an exact ContinuousDeliveryPlan")
    snapshot_mapping = json.loads(render_progress_snapshot(value.snapshot).decode("utf-8"))
    program_snapshot = value.program_snapshot
    program_mapping = None
    if program_snapshot is not None:
        program_mapping = {
            "schema_version": program_snapshot.schema_version,
            "definition_id": program_snapshot.definition_id,
            "definition_sha256": program_snapshot.definition_sha256,
            "scope_status": program_snapshot.scope_status.value,
            "denominator_package_count": program_snapshot.denominator_package_count,
            "denominator_weight": program_snapshot.denominator_weight,
            "execution_progress_basis_points": program_snapshot.execution_progress_basis_points,
            "verified_progress_basis_points": program_snapshot.verified_progress_basis_points,
            "current_stage": program_snapshot.current_stage,
            "current_stage_package_ids": list(program_snapshot.current_stage_package_ids),
            "current_stage_total_weight": program_snapshot.current_stage_total_weight,
            "current_stage_execution_basis_points": program_snapshot.current_stage_execution_basis_points,
            "current_stage_verified_basis_points": program_snapshot.current_stage_verified_basis_points,
            "next_stage": program_snapshot.next_stage,
            "ordered_successor_transactions": [
                {
                    "transaction_id": item.transaction_id,
                    "label": item.label,
                    "stage_id": item.stage_id,
                    "authority_state": item.authority_state.value,
                    "gate_ref": item.gate_ref,
                    "rollback_ref": item.rollback_ref,
                    "depends_on": list(item.depends_on),
                    "package_state": item.package_state.value,
                }
                for item in program_snapshot.ordered_successor_transactions
            ],
            "next_transaction_id": program_snapshot.next_transaction_id,
            "human_gate_transaction_id": program_snapshot.human_gate_transaction_id,
            "delivery_state": program_snapshot.delivery_state.value,
            "excluded_historical_blocked_package_ids": list(
                program_snapshot.excluded_historical_blocked_package_ids
            ),
            "reason_codes": list(program_snapshot.reason_codes),
            "execution_performed": program_snapshot.execution_performed,
        }
    return {
        "actions": [
            {
                "action_code": action.action_code,
                "execution_performed": action.execution_performed,
                "owner": action.owner.value,
                "reason_codes": list(action.reason_codes),
                "requires_existing_authority": action.requires_existing_authority,
                "state": action.state.value,
            }
            for action in value.actions
        ],
        "dispatch_permitted": value.dispatch_permitted,
        "execution_performed": value.execution_performed,
        "human_gate_required": value.human_gate_required,
        "resume_condition": value.resume_condition,
        "loop_stop_state": (
            None if value.loop_stop_state is None else value.loop_stop_state.value
        ),
        "progress_snapshot": snapshot_mapping,
        "program_snapshot": program_mapping,
        "reason_codes": list(value.reason_codes),
        "schema_version": value.schema_version,
        "state": value.state.value,
    }


def render_continuous_delivery_harness(value: ContinuousDeliveryPlan) -> bytes:
    """Render a plan deterministically for receipts or a read-only status report."""

    return canonical_json_bytes(continuous_delivery_harness_mapping(value))


__all__ = [
    "CONTINUOUS_DELIVERY_HARNESS_SCHEMA_VERSION",
    "CONTINUOUS_DELIVERY_WORKFLOW",
    "ContinuousDeliveryHarnessError",
    "ContinuousDeliveryPlan",
    "HarnessAction",
    "HarnessOwner",
    "HarnessState",
    "continuous_delivery_harness_mapping",
    "plan_continuous_delivery_harness",
    "render_continuous_delivery_harness",
]
