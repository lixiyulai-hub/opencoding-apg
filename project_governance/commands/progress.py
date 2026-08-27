"""Read-only source-bound project progress reporting."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any

from ..audit_contract import AUDIT_CONTENT_HASH_LIMIT, audit_proof_contract, snapshot_for_audit
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
from ..program_progress import (
    MAX_PROGRAM_ROADMAP_BYTES,
    ProgramProgressSnapshot,
    ProgramScopeStatus,
    parse_program_roadmap_definition,
    recompute_program_progress,
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
    program_snapshot: ProgramProgressSnapshot | None = None


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
            "Progress basis: definition_id=absent; denominator_tasks=not-computable; "
            "denominator_weight=not-computable; lifecycle_ref=not-computable; "
            "plan_id=not-computable",
            "Completed work: unavailable",
            "Total progress: execution=not-computable verified=not-computable",
            "Program progress: scope=not-computable; definition_id=absent; "
            "execution=not-computable verified=not-computable; "
            "reason=program-roadmap-unavailable",
            "Current phase: unavailable/not-computable",
            "Lifecycle stage: unavailable; execution=not-computable verified=not-computable",
            "Program stage (current): unavailable/not-computable; packages=not-computable; "
            "execution=not-computable verified=not-computable",
            "Next lifecycle boundary: unavailable/not-computable",
            "Immediate program transaction: unavailable/not-computable",
            "Following program stage: unavailable/not-computable",
            "Roadmap: unavailable/not-computable",
            "Delivery and Gates: target=none; state=not-computable; gate_health=unavailable",
            "Next automatic work: inspect-progress-scope",
            "Human gate: none",
            f"Blockers and review: reasons={reason_text}; independent_review=unavailable",
            "Later boundaries: unavailable/not-computable",
            "Continuation: state=inspect; action=inspect-progress-scope; "
            "owner=harness-controller; requires_existing_authority=false; "
            "dispatch_permitted=false; "
            "resume_condition=resume.after-progress-source-is-readable",
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


def _program_snapshot_mapping(snapshot: ProgramProgressSnapshot) -> dict[str, Any]:
    """Return a bounded receipt mapping without adding a second file format."""

    return {
        "schema_version": snapshot.schema_version,
        "definition_id": snapshot.definition_id,
        "definition_sha256": snapshot.definition_sha256,
        "scope_status": snapshot.scope_status.value,
        "denominator_package_count": snapshot.denominator_package_count,
        "denominator_weight": snapshot.denominator_weight,
        "execution_progress_basis_points": snapshot.execution_progress_basis_points,
        "verified_progress_basis_points": snapshot.verified_progress_basis_points,
        "current_stage": snapshot.current_stage,
        "current_stage_package_ids": list(snapshot.current_stage_package_ids),
        "current_stage_total_weight": snapshot.current_stage_total_weight,
        "current_stage_execution_basis_points": snapshot.current_stage_execution_basis_points,
        "current_stage_verified_basis_points": snapshot.current_stage_verified_basis_points,
        "next_stage": snapshot.next_stage,
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
            for item in snapshot.ordered_successor_transactions
        ],
        "next_transaction_id": snapshot.next_transaction_id,
        "human_gate_transaction_id": snapshot.human_gate_transaction_id,
        "delivery_state": snapshot.delivery_state.value,
        "excluded_historical_blocked_package_ids": list(
            snapshot.excluded_historical_blocked_package_ids
        ),
        "reason_codes": list(snapshot.reason_codes),
        "execution_performed": snapshot.execution_performed,
    }


def _read_program_snapshot(
    guard: WorkspaceGuard,
    parsed_definition: ProgressDefinition,
) -> tuple[ProgramProgressSnapshot, str | None, str | None]:
    """Read and verify the optional roadmap and its declared evidence.

    The program projection owns all percentage and source-drift decisions.  A
    missing roadmap/evidence is therefore represented as a not-computable
    snapshot rather than turning a valid lifecycle report into an estimate.
    """

    roadmap_ref = parsed_definition.program_roadmap_ref
    if roadmap_ref is None:
        return recompute_program_progress(None, None), None, None
    try:
        roadmap_payload = _read_project_file(
            guard,
            _project_relative(roadmap_ref, "progress definition program_roadmap_ref"),
            maximum=MAX_PROGRAM_ROADMAP_BYTES,
            label="program roadmap",
        )
        roadmap = parse_program_roadmap_definition(roadmap_payload)
    except (OSError, TypeError, ValueError):
        return recompute_program_progress(None, None), roadmap_ref, None

    source_digests: dict[str, str] = {}
    evidence_paths = sorted(
        {
            evidence.path
            for package in roadmap.packages
            for evidence in package.evidence
        }
    )
    for evidence_path in evidence_paths:
        try:
            evidence_payload = _read_project_file(
                guard,
                _project_relative(evidence_path, "program evidence path"),
                maximum=AUDIT_CONTENT_HASH_LIMIT,
                label="program evidence",
            )
        except (OSError, TypeError, ValueError):
            # Omission is intentional: recompute_program_progress then emits
            # a source-bound missing/drift reason and withholds successors.
            continue
        source_digests[evidence_path] = digest(evidence_payload)
    return (
        recompute_program_progress(roadmap, source_digests),
        roadmap_ref,
        digest(roadmap_payload),
    )


def _lifecycle_human_gate(snapshot: ProgressSnapshot) -> bool:
    """Return whether the lifecycle itself, rather than its successor, is gated."""

    hints = tuple(snapshot.next_actions) + tuple(snapshot.reason_codes)
    prefixes = (
        "human-gate",
        "confirm",
        "owner-decision",
        "owner-authorization-pending",
        "next.provide-transaction-approval",
    )
    return any(
        item == prefix or item.startswith(f"{prefix}-") or item.startswith(f"{prefix}.")
        for item in hints
        for prefix in prefixes
    )


def _render_status_snapshot(
    snapshot: ProgressSnapshot,
    continuation: ContinuousDeliveryPlan,
    *,
    current_phase: str | None = None,
    next_phase: str | None = None,
    definition_total_weight: int | None = None,
    program_snapshot: ProgramProgressSnapshot | None = None,
) -> str:
    primary = continuation.actions[0]
    lifecycle_gate = _lifecycle_human_gate(snapshot)
    human_gate = (
        primary.action_code
        if continuation.human_gate_required and lifecycle_gate
        else program_snapshot.human_gate_transaction_id
        if continuation.human_gate_required
        and program_snapshot is not None
        and program_snapshot.human_gate_transaction_id is not None
        else primary.action_code
        if continuation.human_gate_required
        else "none"
    )
    requires_authority = "true" if primary.requires_existing_authority else "false"
    dispatch_permitted = "true" if continuation.dispatch_permitted else "false"
    continuation_text = (
        f"state={continuation.state.value}; action={primary.action_code}; "
        f"owner={primary.owner.value}; "
        f"requires_existing_authority={requires_authority}; "
        f"dispatch_permitted={dispatch_permitted}; "
        f"resume_condition={continuation.resume_condition}"
    )
    base = render_progress_status(
        snapshot,
        current_phase=current_phase,
        next_phase=next_phase,
        definition_total_weight=definition_total_weight,
        continuation=continuation_text,
        program_snapshot=program_snapshot,
    )
    lines = base.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("Next automatic work: "):
            source_sequence = ",".join(snapshot.next_actions)
            program_transaction = (
                None
                if program_snapshot is None
                or program_snapshot.scope_status is not ProgramScopeStatus.COMPUTABLE
                or not program_snapshot.ordered_successor_transactions
                else program_snapshot.ordered_successor_transactions[0]
            )
            if program_transaction is None:
                lines[index] = f"Next automatic work: {primary.action_code}"
            else:
                lines[index] = (
                    f"Next automatic work: {primary.action_code} for program transaction "
                    f"{program_transaction.label} [{program_transaction.transaction_id}]"
                )
            if (
                source_sequence
                and primary.action_code not in snapshot.next_actions
                and snapshot.delivery_state.value != "target-reached"
            ):
                lines[index] += f"; source_sequence={source_sequence}"
            break
    status_reasons = tuple(
        sorted(
            set(
                snapshot.reason_codes
                + (() if program_snapshot is None else program_snapshot.reason_codes)
                + (
                    continuation.reason_codes
                    if continuation.state.value in {"freeze", "human-gate"}
                    else ()
                )
            )
        )
    )
    for index, line in enumerate(lines):
        if line.startswith("Blockers and review: "):
            prefix = "Blockers and review: reasons="
            if line.startswith(prefix):
                _, separator, remainder = line[len(prefix) :].partition("; ")
                lines[index] = prefix + (",".join(status_reasons) or "none")
                if separator:
                    lines[index] += "; " + remainder
            break
    for index, line in enumerate(lines):
        if line.startswith("Human gate: "):
            lines[index] = f"Human gate: {human_gate}"
            break
    return "\n".join(lines)


def _receipt(
    *,
    fingerprint: str,
    classification: str,
    definition_ref: str,
    definition_sha256: str | None,
    lifecycle_ref: str | None,
    lifecycle_sha256: str | None,
    program_roadmap_ref: str | None = None,
    program_roadmap_sha256: str | None = None,
    status_snapshot: str,
    changed_paths: tuple[str, ...],
    snapshot: ProgressSnapshot | None = None,
    program_snapshot: ProgramProgressSnapshot | None = None,
    continuation: ContinuousDeliveryPlan | None = None,
    error_type: str | None = None,
) -> Receipt:
    outputs: dict[str, object] = {
        "definition_ref": definition_ref,
        "definition_sha256": definition_sha256,
        "lifecycle_ref": lifecycle_ref,
        "lifecycle_sha256": lifecycle_sha256,
        "program_roadmap_ref": program_roadmap_ref,
        "program_roadmap_sha256": program_roadmap_sha256,
        "progress_snapshot": (
            None if snapshot is None else _snapshot_mapping(snapshot)
        ),
        "program_snapshot": (
            None
            if program_snapshot is None
            else _program_snapshot_mapping(program_snapshot)
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
        item
        for item in (definition_ref, lifecycle_ref, program_roadmap_ref)
        if item is not None
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
        program_snapshot, program_roadmap_ref, program_roadmap_sha256 = _read_program_snapshot(
            guard,
            parsed_definition,
        )
        continuation = plan_continuous_delivery_harness(
            snapshot,
            program_snapshot=program_snapshot,
        )
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
            definition_total_weight=parsed_definition.total_weight,
            program_snapshot=program_snapshot,
        )
        receipt = _receipt(
            fingerprint=digest(before),
            classification=(
                "scope-violation"
                if changed
                else (
                    "pass"
                    if snapshot.scope_status.value == "computable"
                    and (
                        program_roadmap_ref is None
                        or program_snapshot.scope_status is ProgramScopeStatus.COMPUTABLE
                    )
                    else "not-computable"
                )
            ),
            definition_ref=definition_ref,
            definition_sha256=definition_sha256,
            lifecycle_ref=lifecycle_ref,
            lifecycle_sha256=digest(lifecycle_payload),
            program_roadmap_ref=program_roadmap_ref,
            program_roadmap_sha256=program_roadmap_sha256,
            status_snapshot=status_snapshot,
            changed_paths=changed,
            snapshot=snapshot,
            program_snapshot=program_snapshot,
            continuation=continuation,
        )
        return ProgressOutcome(
            ok=not changed,
            exit_code=4 if changed else 0,
            receipt=receipt,
            status_snapshot=status_snapshot,
            snapshot=snapshot,
            program_snapshot=program_snapshot,
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
