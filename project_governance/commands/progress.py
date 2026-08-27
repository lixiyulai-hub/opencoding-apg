"""Read-only source-bound project progress reporting."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any

from ..audit_contract import audit_proof_contract, snapshot_for_audit
from ..continuous_delivery_harness import (
    ContinuousDeliveryPlan,
    continuous_delivery_harness_mapping,
    plan_continuous_delivery_harness,
)
from ..goal_delivery_lifecycle import (
    MAX_GOAL_DELIVERY_LIFECYCLE_BYTES,
    GoalDeliveryLifecycle,
    LifecyclePhase,
    parse_goal_delivery_lifecycle,
)
from ..model import Receipt
from ..path_guard import PathViolation, WorkspaceGuard
from ..progress_projection import (
    MAX_PROGRESS_DEFINITION_BYTES,
    ProgressDefinition,
    ProgressProjectionError,
    ProgressSnapshot,
    parse_progress_definition,
    recompute_progress_snapshot,
    render_progress_snapshot,
    render_progress_status,
)
from ..receipts import build_receipt
from ..storage import digest


DEFAULT_PROGRESS_DEFINITION = ".governance/progress/active.json"


@dataclass(frozen=True)
class ProgressOutcome:
    ok: bool
    exit_code: int
    receipt: Receipt
    status_snapshot: str
    snapshot: ProgressSnapshot | None = None
    continuation: ContinuousDeliveryPlan | None = None


def _project_relative(value: str | Path, label: str) -> str:
    if isinstance(value, Path):
        value = value.as_posix()
    if type(value) is not str or not value or "\\" in value:
        raise ValueError(f"{label} must be a normalized project-relative path")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"{label} must remain project-relative")
    if candidate.as_posix() != value:
        raise ValueError(f"{label} must be normalized")
    return value


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        info = os.lstat(path)
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(info, "st_file_attributes", 0) & reparse)


def _read_project_file(
    guard: WorkspaceGuard,
    relative: str,
    *,
    maximum: int,
    label: str,
) -> bytes:
    current = guard.root
    for part in PurePosixPath(relative).parts:
        current /= part
        if not os.path.lexists(current):
            raise FileNotFoundError(f"{label} is missing")
        if _is_link_or_reparse(current):
            raise ValueError(f"{label} must be a regular contained project file")
    try:
        resolved = current.resolve(strict=True)
        if not resolved.is_relative_to(guard.root) or not resolved.is_file():
            raise ValueError(f"{label} must be a regular contained project file")
        with resolved.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size < 0:
                raise ValueError(f"{label} must be a regular file")
            if before.st_size > maximum:
                raise ValueError(f"{label} exceeds the fixed byte limit")
            payload = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
    except OSError as error:
        raise ValueError(f"{label} cannot be read safely") from error
    if (
        len(payload) > maximum
        or len(payload) != before.st_size
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
    ):
        raise ValueError(f"{label} changed while being read")
    return payload


def _not_computable_status(*reasons: str) -> str:
    reason_text = ",".join(reasons) or "progress-source-unavailable"
    return "\n".join(
        (
            "Status Snapshot",
            "Scope: not-computable",
            "Completed work: unavailable",
            "Total progress: execution=not-computable verified=not-computable",
            "Current phase: unavailable/not-computable",
            "Current stage: unavailable; execution=not-computable verified=not-computable",
            "Next phase: unavailable/not-computable",
            "Delivery and Gates: target=none; state=not-computable; gate_health=unavailable",
            "Next automatic work: inspect-progress-scope",
            "Human gate: none",
            f"Blockers and review: reasons={reason_text}; independent_review=unavailable",
            "Later boundaries: unavailable/not-computable",
            "Continuation: state=inspect; action=inspect-progress-scope; dispatch_permitted=false",
        )
    )


def _invalid_status(error_type: str) -> str:
    return _not_computable_status(f"progress-input-invalid-{error_type.casefold()}")


def _read_only_proof(changed_paths: tuple[str, ...]) -> dict[str, object]:
    return {
        "passed": not changed_paths,
        "changed_paths": changed_paths,
        **audit_proof_contract(),
    }


def _snapshot_mapping(snapshot: ProgressSnapshot) -> dict[str, Any]:
    return json.loads(render_progress_snapshot(snapshot).decode("utf-8"))


def _render_status_snapshot(
    snapshot: ProgressSnapshot,
    continuation: ContinuousDeliveryPlan,
    *,
    current_phase: str | None = None,
    next_phase: str | None = None,
) -> str:
    primary = continuation.actions[0]
    human_gate = (
        primary.action_code
        if continuation.human_gate_required
        else "none"
    )
    base = render_progress_status(
        snapshot,
        current_phase=current_phase,
        next_phase=next_phase,
    )
    lines = base.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("Human gate: "):
            lines[index] = f"Human gate: {human_gate}"
            break
    requires_authority = "true" if primary.requires_existing_authority else "false"
    dispatch_permitted = "true" if continuation.dispatch_permitted else "false"
    lines.append(
        "Continuation: "
        f"state={continuation.state.value}; action={primary.action_code}; "
        f"owner={primary.owner.value}; requires_existing_authority={requires_authority}; "
        f"dispatch_permitted={dispatch_permitted}"
    )
    return "\n".join(lines)


def _receipt(
    *,
    fingerprint: str,
    classification: str,
    definition_ref: str,
    definition_sha256: str | None,
    lifecycle_ref: str | None,
    lifecycle_sha256: str | None,
    status_snapshot: str,
    changed_paths: tuple[str, ...],
    snapshot: ProgressSnapshot | None = None,
    continuation: ContinuousDeliveryPlan | None = None,
    error_type: str | None = None,
) -> Receipt:
    outputs: dict[str, object] = {
        "definition_ref": definition_ref,
        "definition_sha256": definition_sha256,
        "lifecycle_ref": lifecycle_ref,
        "lifecycle_sha256": lifecycle_sha256,
        "progress_snapshot": (
            None if snapshot is None else _snapshot_mapping(snapshot)
        ),
        "continuation": (
            None
            if continuation is None
            else continuous_delivery_harness_mapping(continuation)
        ),
        "status_snapshot": status_snapshot,
        "execution_performed": False,
        "read_only_proof": _read_only_proof(changed_paths),
    }
    if error_type is not None:
        outputs["error_type"] = error_type
    evidence_refs = tuple(
        item for item in (definition_ref, lifecycle_ref) if item is not None
    )
    return build_receipt(
        command="progress",
        target_fingerprint=fingerprint,
        authorized_scope=(".",),
        inputs={"definition_ref": definition_ref},
        outputs=outputs,
        classification=classification,
        evidence_refs=evidence_refs,
    )


def _missing_definition_outcome(
    *,
    fingerprint: str,
    definition_ref: str,
    reason_code: str,
    definition_sha256: str | None = None,
    lifecycle_ref: str | None = None,
    changed_paths: tuple[str, ...] = (),
) -> ProgressOutcome:
    status_snapshot = _not_computable_status(reason_code)
    receipt = _receipt(
        fingerprint=fingerprint,
        classification="scope-violation" if changed_paths else "not-computable",
        definition_ref=definition_ref,
        definition_sha256=definition_sha256,
        lifecycle_ref=lifecycle_ref,
        lifecycle_sha256=None,
        status_snapshot=status_snapshot,
        changed_paths=changed_paths,
    )
    return ProgressOutcome(
        ok=not changed_paths,
        exit_code=4 if changed_paths else 0,
        receipt=receipt,
        status_snapshot=status_snapshot,
    )


def run_progress(
    target: str | Path,
    *,
    definition: str | Path | None = None,
) -> ProgressOutcome:
    """Report source-bound status without altering target files or lifecycle state."""

    definition_ref = DEFAULT_PROGRESS_DEFINITION
    try:
        guard = WorkspaceGuard(Path(target))
        before = snapshot_for_audit(guard)
        definition_ref = _project_relative(
            DEFAULT_PROGRESS_DEFINITION if definition is None else definition,
            "definition",
        )
        definition_path = guard.root / definition_ref
        if not os.path.lexists(definition_path):
            if definition is None:
                changed = guard.changed_paths(before)
                return _missing_definition_outcome(
                    fingerprint=digest(before),
                    definition_ref=definition_ref,
                    reason_code="progress-definition-absent",
                    changed_paths=changed,
                )
            raise FileNotFoundError("progress definition is missing")

        definition_payload = _read_project_file(
            guard,
            definition_ref,
            maximum=MAX_PROGRESS_DEFINITION_BYTES,
            label="progress definition",
        )
        parsed_definition: ProgressDefinition = parse_progress_definition(
            definition_payload
        )
        definition_sha256 = digest(definition_payload)
        if parsed_definition.lifecycle_ref is None:
            changed = guard.changed_paths(before)
            return _missing_definition_outcome(
                fingerprint=digest(before),
                definition_ref=definition_ref,
                reason_code="progress-source-ref-missing",
                definition_sha256=definition_sha256,
                changed_paths=changed,
            )

        lifecycle_ref = _project_relative(
            parsed_definition.lifecycle_ref,
            "progress definition lifecycle_ref",
        )
        lifecycle_payload = _read_project_file(
            guard,
            lifecycle_ref,
            maximum=MAX_GOAL_DELIVERY_LIFECYCLE_BYTES,
            label="progress lifecycle source",
        )
        lifecycle: GoalDeliveryLifecycle = parse_goal_delivery_lifecycle(
            lifecycle_payload
        )
        snapshot = recompute_progress_snapshot(parsed_definition, lifecycle)
        continuation = plan_continuous_delivery_harness(snapshot)
        changed = guard.changed_paths(before)
        route_by_id = {route.task_id: route for route in lifecycle.plan.routes}
        next_phase = None
        if lifecycle.next_task_ids:
            phase = route_by_id[lifecycle.next_task_ids[0]].phase
            next_phase = getattr(phase, "value", phase)
        elif lifecycle.state.value == "complete":
            phases = tuple(LifecyclePhase)
            current_index = phases.index(lifecycle.phase)
            if current_index + 1 < len(phases):
                next_phase = phases[current_index + 1].value
        status_snapshot = _render_status_snapshot(
            snapshot,
            continuation,
            current_phase=lifecycle.phase.value,
            next_phase=next_phase,
        )
        receipt = _receipt(
            fingerprint=digest(before),
            classification=(
                "scope-violation"
                if changed
                else (
                    "pass"
                    if snapshot.scope_status.value == "computable"
                    else "not-computable"
                )
            ),
            definition_ref=definition_ref,
            definition_sha256=definition_sha256,
            lifecycle_ref=lifecycle_ref,
            lifecycle_sha256=digest(lifecycle_payload),
            status_snapshot=status_snapshot,
            changed_paths=changed,
            snapshot=snapshot,
            continuation=continuation,
        )
        return ProgressOutcome(
            ok=not changed,
            exit_code=4 if changed else 0,
            receipt=receipt,
            status_snapshot=status_snapshot,
            snapshot=snapshot,
            continuation=continuation,
        )
    except (
        OSError,
        TypeError,
        ValueError,
        PathViolation,
        ProgressProjectionError,
    ) as error:
        try:
            changed = guard.changed_paths(before)
            fingerprint = digest(before)
        except (NameError, OSError, ValueError, PathViolation):
            changed = ()
            fingerprint = ""
        status_snapshot = _invalid_status(type(error).__name__)
        receipt = _receipt(
            fingerprint=fingerprint,
            classification="scope-violation" if changed else "invalid",
            definition_ref=definition_ref,
            definition_sha256=None,
            lifecycle_ref=None,
            lifecycle_sha256=None,
            status_snapshot=status_snapshot,
            changed_paths=changed,
            error_type=type(error).__name__,
        )
        return ProgressOutcome(
            ok=False,
            exit_code=4 if changed else 2,
            receipt=receipt,
            status_snapshot=status_snapshot,
        )


__all__ = [
    "DEFAULT_PROGRESS_DEFINITION",
    "ProgressOutcome",
    "run_progress",
]
