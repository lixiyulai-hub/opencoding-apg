"""Closed validation and receipt evidence for plan-bound Gate execution."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Iterable

from .affected_gate_plan import plan_affected_gates
from .architecture_graph import load_architecture_graph
from .consistency_manifest import (
    _is_link_or_reparse,
    _open_read_handle,
    _validate_opened_path,
    consistency_manifest_impact,
    evaluate_consistency_manifest,
    load_consistency_manifest,
)
from .gates import GateDefinition
from .model import Receipt
from .receipts import load_receipt_json, receipt_digest
from .storage import canonical_json_bytes, digest


PLAN_BOUND_EXECUTION_SCHEMA_VERSION = "1.0"
PLAN_BOUND_EXECUTION_OUTPUT_KEY = "plan_bound_execution"
PLAN_BOUND_SELECTION_MODE = "plan"

_MAX_AUTHORITY_BYTES = 1_048_576
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_PHASE_ORDER = {"fast": 0, "full": 1, "release": 2}
_APPROVAL_FIELDS = frozenset({"id", "actor", "role", "timestamp_utc", "scope"})
_REQUEST_CHANGE_FIELDS = (
    "change_id",
    "problem",
    "outcome",
    "non_goals",
    "acceptance",
    "metric",
    "changed_paths",
    "surfaces",
    "rollout",
    "telemetry",
    "rollback",
)
_CHANGE_FIELDS = frozenset(
    {
        "change_id",
        "problem",
        "outcome",
        "non_goals",
        "acceptance",
        "metric",
        "changed_paths",
        "surfaces",
        "rollout",
        "telemetry",
        "rollback",
        "risk",
        "approval_refs",
    }
)
_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "policy_sha256",
        "architecture_graph_sha256",
        "consistency_manifest_sha256",
        "required_phase",
        "effective_phase",
        "changed_paths",
        "derived_consistency_paths",
        "planning_paths",
        "direct_node_ids",
        "affected_node_ids",
        "candidate_gate_ids",
        "eligible_policy_gate_ids",
        "eligible_candidate_gate_ids",
        "planned_gate_ids",
        "omitted_gate_ids",
        "unassigned_gate_ids",
        "unsafe_gate_ids",
        "nonpassing_consistency_relationship_ids",
        "fallback_reason_codes",
        "fallback_full",
        "execution_performed",
    }
)
_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "selection_mode",
        "plan_receipt_ref",
        "plan_receipt_sha256",
        "plan_receipt_digest",
        "change_id",
        "change_record_sha256",
        "mode",
        "effective_phase",
        "policy_sha256",
        "architecture_graph_sha256",
        "consistency_manifest_sha256",
        "planned_gate_ids",
        "executed_gate_ids",
        "omitted_gate_ids",
        "fallback_reason_codes",
        "execution_performed",
        "authority_status",
    }
)


class PlanBoundExecutionError(ValueError):
    """Raised before execution when plan authority is incomplete or stale."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanBoundExecutionError(f"{label} must be an object")
    return value


def _closed(value: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    missing = sorted(fields - set(value))
    unknown = sorted(set(value) - fields)
    if missing or unknown:
        raise PlanBoundExecutionError(f"{label} fields do not match the closed contract")


def _sha256(value: object, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise PlanBoundExecutionError(f"{label} must be a lowercase SHA-256 value")
    return value


def _string(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise PlanBoundExecutionError(f"{label} must be a non-empty string")
    return value


def _string_array(value: object, label: str, *, maximum: int = 1024) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PlanBoundExecutionError(f"{label} must be an array")
    if len(value) > maximum or any(type(item) is not str or not item for item in value):
        raise PlanBoundExecutionError(f"{label} must contain bounded non-empty strings")
    return tuple(value)


def _authority_ref(value: str) -> str:
    if type(value) is not str or not value or any(ord(char) < 0x20 for char in value):
        raise PlanBoundExecutionError("plan receipt path is invalid")
    normalized = value.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if (
        candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or len(candidate.parts) != 3
        or candidate.parts[:2] != (".governance", "receipts")
        or not candidate.name.endswith(".json")
    ):
        raise PlanBoundExecutionError(
            "plan receipt must be one project-relative .governance/receipts JSON file"
        )
    return candidate.as_posix()


def _read_stable_file(
    project_root: Path,
    relative: str,
    *,
    label: str,
) -> tuple[bytes, str]:
    expected = project_root.joinpath(*PurePosixPath(relative).parts)
    current = project_root
    for part in PurePosixPath(relative).parts:
        current /= part
        if not os.path.lexists(current):
            raise PlanBoundExecutionError(f"{label} is missing")
        if _is_link_or_reparse(current):
            raise PlanBoundExecutionError(f"{label} path contains a link or reparse point")
    try:
        with _open_read_handle(expected, label) as handle:
            _validate_opened_path(
                handle,
                project_root=project_root,
                expected=expected,
                expected_relative=relative,
                label=label,
                allow_manifest=True,
            )
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or getattr(before, "st_nlink", 1) != 1:
                raise PlanBoundExecutionError(f"{label} must be one regular non-hardlinked file")
            if before.st_size <= 0 or before.st_size > _MAX_AUTHORITY_BYTES:
                raise PlanBoundExecutionError(f"{label} exceeds its bounded size")
            payload = handle.read(_MAX_AUTHORITY_BYTES + 1)
            after = os.fstat(handle.fileno())
            identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if len(payload) > _MAX_AUTHORITY_BYTES or any(
                getattr(before, field) != getattr(after, field) for field in identity
            ):
                raise PlanBoundExecutionError(f"{label} changed while being read")
            _validate_opened_path(
                handle,
                project_root=project_root,
                expected=expected,
                expected_relative=relative,
                label=label,
                allow_manifest=True,
            )
    except PlanBoundExecutionError:
        raise
    except (OSError, ValueError) as error:
        raise PlanBoundExecutionError(f"{label} cannot be read safely") from error
    return payload, digest(payload)


def _normalized_change(value: object) -> dict[str, Any]:
    change = dict(_mapping(value, "plan receipt change"))
    _closed(change, _CHANGE_FIELDS, "plan receipt change")
    change_id = _string(change["change_id"], "change_id")
    if not _SAFE_ID.fullmatch(change_id):
        raise PlanBoundExecutionError("change_id is unsafe")
    for field in ("problem", "outcome", "metric", "rollout", "rollback"):
        _string(change[field], f"change.{field}")
    for field in (
        "non_goals",
        "acceptance",
        "changed_paths",
        "surfaces",
        "telemetry",
        "approval_refs",
    ):
        change[field] = _string_array(change[field], f"change.{field}")
    raw_risk = _string(change["risk"], "change.risk")
    risk = raw_risk.removeprefix("RiskClass.").lower()
    if risk not in {"routine", "moderate", "high", "critical"}:
        raise PlanBoundExecutionError("change.risk is unsupported")
    change["risk"] = risk
    return change


def _validate_receipt_inputs(
    value: object,
    change: Mapping[str, Any],
    receipt_approval_ids: Sequence[str],
) -> None:
    inputs = _mapping(value, "plan receipt inputs")
    sequence_fields = {
        "non_goals",
        "acceptance",
        "changed_paths",
        "surfaces",
        "telemetry",
    }
    for field in _REQUEST_CHANGE_FIELDS:
        if field not in inputs:
            raise PlanBoundExecutionError("plan receipt inputs are incomplete")
        actual = (
            _string_array(inputs[field], f"inputs.{field}")
            if field in sequence_fields
            else _string(inputs[field], f"inputs.{field}")
        )
        if actual != change[field]:
            raise PlanBoundExecutionError(
                f"plan receipt input {field} does not match the ChangeRecord"
            )

    raw_approvals = inputs.get("approvals", ())
    if isinstance(raw_approvals, (str, bytes, Mapping)) or not isinstance(
        raw_approvals, Sequence
    ):
        raise PlanBoundExecutionError("plan receipt approvals must be an array")
    approvals: list[dict[str, Any]] = []
    changed_paths = tuple(change["changed_paths"])
    for raw in raw_approvals:
        approval = dict(_mapping(raw, "plan receipt approval"))
        _closed(approval, _APPROVAL_FIELDS, "plan receipt approval")
        for field in ("id", "actor", "role", "timestamp_utc"):
            approval[field] = _string(
                approval[field], f"plan receipt approval {field}"
            )
        try:
            approved_at = datetime.fromisoformat(
                approval["timestamp_utc"].replace("Z", "+00:00")
            )
        except ValueError as error:
            raise PlanBoundExecutionError(
                "plan receipt approval timestamp is invalid"
            ) from error
        if (
            approved_at.tzinfo is None
            or approved_at.utcoffset() != timezone.utc.utcoffset(approved_at)
            or approved_at > datetime.now(timezone.utc)
        ):
            raise PlanBoundExecutionError(
                "plan receipt approval timestamp must be current or past UTC"
            )
        scope = _string_array(approval["scope"], "plan receipt approval scope")
        for item in scope:
            if item == ".":
                continue
            normalized = item.replace("\\", "/")
            candidate = PurePosixPath(normalized)
            if (
                candidate.is_absolute()
                or any(part in {"", ".", ".."} for part in candidate.parts)
                or not any(
                    normalized == path
                    or normalized.startswith(path.rstrip("/") + "/")
                    for path in changed_paths
                )
            ):
                raise PlanBoundExecutionError(
                    "plan receipt approval scope is outside changed_paths"
                )
        approval["scope"] = scope
        approvals.append(approval)

    approval_ids = tuple(item["id"] for item in approvals)
    if approval_ids != tuple(receipt_approval_ids) or approval_ids != tuple(
        change["approval_refs"]
    ):
        raise PlanBoundExecutionError(
            "plan receipt approval inputs do not match recorded approval references"
        )
    if len(set(approval_ids)) != len(approval_ids) or len(
        {item["actor"] for item in approvals}
    ) != len(approvals):
        raise PlanBoundExecutionError("plan receipt approvals are not unique")
    if change["risk"] in {"high", "critical"} and not any(
        item["role"] == "owner" for item in approvals
    ):
        raise PlanBoundExecutionError("high-risk plan receipt lacks owner approval")
    if change["risk"] == "critical":
        owners = [item for item in approvals if item["role"] == "owner"]
        verifiers = [
            item for item in approvals if item["role"] == "independent-verifier"
        ]
        if (
            not owners
            or not verifiers
            or owners[0]["actor"] == verifiers[0]["actor"]
        ):
            raise PlanBoundExecutionError(
                "critical plan receipt lacks a distinct independent verifier"
            )


@dataclass(frozen=True)
class PlanBoundSelection:
    plan_receipt_ref: str
    plan_receipt_sha256: str
    plan_receipt_digest: str
    change_id: str
    change_record_sha256: str
    mode: str
    effective_phase: str
    policy_sha256: str
    architecture_graph_sha256: str | None
    consistency_manifest_sha256: str | None
    planned_gate_ids: tuple[str, ...]
    omitted_gate_ids: tuple[str, ...]
    fallback_reason_codes: tuple[str, ...]


def prepare_plan_bound_selection(
    root: str | Path,
    plan_receipt: str,
    gates: Iterable[GateDefinition],
    *,
    policy_sha256: str | None,
) -> PlanBoundSelection:
    """Authenticate and recompute one P1-D plan without executing a Gate."""
    project_root = Path(root).resolve(strict=True)
    if not project_root.is_dir():
        raise PlanBoundExecutionError("project root must be a directory")
    receipt_ref = _authority_ref(plan_receipt)
    receipt_payload, receipt_sha256 = _read_stable_file(
        project_root, receipt_ref, label="plan receipt"
    )
    try:
        receipt = load_receipt_json(receipt_payload, require_canonical=True)
    except (TypeError, ValueError) as error:
        raise PlanBoundExecutionError("plan receipt is not canonical") from error
    if receipt.schema_version != "1.0" or receipt.command != "plan-change":
        raise PlanBoundExecutionError("plan receipt must be a schema 1.0 plan-change receipt")
    if receipt.checks or receipt.findings:
        raise PlanBoundExecutionError("plan receipt must not contain check or finding records")

    outputs = _mapping(receipt.outputs, "plan receipt outputs")
    allowed_outputs = {
        "risk",
        "required_phase",
        "impact",
        "change",
        "feedback_loop",
        "regression_record",
    }
    if not {"risk", "required_phase", "impact", "change"}.issubset(outputs) or (
        set(outputs) - allowed_outputs
    ):
        raise PlanBoundExecutionError("plan receipt outputs are incomplete or unknown")
    change = _normalized_change(outputs["change"])
    change_id = change["change_id"]
    plan_digest = receipt_digest(receipt)
    timestamp = receipt.timestamp_utc.replace(":", "").replace("-", "")
    expected_name = f"{timestamp}-plan-change-{change_id}-{plan_digest[:12]}.json"
    if PurePosixPath(receipt_ref).name != expected_name:
        raise PlanBoundExecutionError("plan receipt filename does not match its canonical digest")

    change_ref = f".governance/changes/{change_id}.json"
    change_payload, change_sha256 = _read_stable_file(
        project_root, change_ref, label="plan ChangeRecord"
    )
    if change_payload != canonical_json_bytes(change):
        raise PlanBoundExecutionError("plan ChangeRecord does not match the plan receipt")
    changed_paths = tuple(change["changed_paths"])
    if receipt.inputs.get("change_id") != change_id:
        raise PlanBoundExecutionError("plan receipt input change_id does not match")
    if tuple(receipt.evidence_refs) != changed_paths:
        raise PlanBoundExecutionError("plan receipt evidence scope does not match changed_paths")
    if change_ref not in receipt.authorized_scope or ".governance/receipts" not in receipt.authorized_scope:
        raise PlanBoundExecutionError("plan receipt authorized_scope is incomplete")
    if tuple(receipt.approvals) != tuple(change["approval_refs"]):
        raise PlanBoundExecutionError("plan receipt approvals do not match the ChangeRecord")
    _validate_receipt_inputs(receipt.inputs, change, receipt.approvals)
    if receipt.classification != change["risk"] or outputs["risk"] != change["risk"]:
        raise PlanBoundExecutionError("plan receipt risk fields do not match")
    required_phase = _string(outputs["required_phase"], "required_phase")
    if required_phase not in _PHASE_ORDER:
        raise PlanBoundExecutionError("required_phase is unsupported")

    policy_digest = _sha256(policy_sha256, "policy_sha256")
    if receipt.policy_digest != policy_digest:
        raise PlanBoundExecutionError("plan receipt policy authority is stale")
    gate_definitions = tuple(gates)
    if any(not isinstance(gate, GateDefinition) for gate in gate_definitions):
        raise PlanBoundExecutionError("gates must contain GateDefinition values")
    gate_by_id = {gate.gate_id: gate for gate in gate_definitions}
    if len(gate_by_id) != len(gate_definitions):
        raise PlanBoundExecutionError("current policy contains duplicate Gate IDs")

    try:
        graph = load_architecture_graph(project_root, policy_gate_ids=tuple(gate_by_id))
        manifest = load_consistency_manifest(project_root)
        consistency_impact = None
        if manifest is not None:
            evaluation = evaluate_consistency_manifest(project_root, manifest)
            consistency_impact = consistency_manifest_impact(
                manifest, changed_paths, evaluation=evaluation
            )
        recomputed = plan_affected_gates(
            graph,
            changed_paths=changed_paths,
            gates=gate_definitions,
            required_phase=required_phase,
            policy_sha256=policy_digest,
            consistency_impact=consistency_impact,
        )
    except PlanBoundExecutionError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise PlanBoundExecutionError("current plan authority cannot be evaluated") from error

    impact = _mapping(outputs["impact"], "plan receipt impact")
    raw_plan = _mapping(impact.get("affected_gate_plan"), "affected Gate plan")
    _closed(raw_plan, _PLAN_FIELDS, "affected Gate plan")
    if canonical_json_bytes(raw_plan) != canonical_json_bytes(recomputed):
        raise PlanBoundExecutionError("affected Gate plan does not match current authority")
    if raw_plan["execution_performed"] is not False:
        raise PlanBoundExecutionError("plan receipt already claims execution")

    mode = recomputed["mode"]
    effective_phase = recomputed["effective_phase"]
    planned_gate_ids = tuple(recomputed["planned_gate_ids"])
    omitted_gate_ids = tuple(recomputed["omitted_gate_ids"])
    if mode in {"affected", "fallback_full"}:
        if not planned_gate_ids or any(
            not _SAFE_ID.fullmatch(gate_id) or gate_id not in gate_by_id
            for gate_id in planned_gate_ids
        ):
            raise PlanBoundExecutionError("executable plan contains unsafe or unknown Gate IDs")
    elif mode == "inconclusive":
        if planned_gate_ids:
            raise PlanBoundExecutionError("inconclusive plan must not select Gates")
    else:
        raise PlanBoundExecutionError("plan mode is unsupported")

    return PlanBoundSelection(
        plan_receipt_ref=receipt_ref,
        plan_receipt_sha256=receipt_sha256,
        plan_receipt_digest=plan_digest,
        change_id=change_id,
        change_record_sha256=change_sha256,
        mode=mode,
        effective_phase=effective_phase,
        policy_sha256=policy_digest,
        architecture_graph_sha256=graph.digest if graph is not None else None,
        consistency_manifest_sha256=manifest.digest if manifest is not None else None,
        planned_gate_ids=planned_gate_ids,
        omitted_gate_ids=omitted_gate_ids,
        fallback_reason_codes=tuple(recomputed["fallback_reason_codes"]),
    )


def selected_gate_definitions(
    selection: PlanBoundSelection, gates: Iterable[GateDefinition]
) -> tuple[GateDefinition, ...]:
    if not isinstance(selection, PlanBoundSelection):
        raise TypeError("selection must be a PlanBoundSelection")
    gate_values = tuple(gates)
    by_id = {gate.gate_id: gate for gate in gate_values}
    if len(by_id) != len(gate_values):
        raise PlanBoundExecutionError("current policy contains duplicate Gate IDs")
    try:
        selected = tuple(by_id[gate_id] for gate_id in selection.planned_gate_ids)
    except KeyError as error:
        raise PlanBoundExecutionError("planned Gate is absent from current policy") from error
    return tuple(
        sorted(selected, key=lambda gate: (_PHASE_ORDER[gate.phase], gate.gate_id))
    )


@dataclass(frozen=True)
class PlanBoundExecutionEvidence:
    schema_version: str
    selection_mode: str
    plan_receipt_ref: str
    plan_receipt_sha256: str
    plan_receipt_digest: str
    change_id: str
    change_record_sha256: str
    mode: str
    effective_phase: str
    policy_sha256: str
    architecture_graph_sha256: str | None
    consistency_manifest_sha256: str | None
    planned_gate_ids: tuple[str, ...]
    executed_gate_ids: tuple[str, ...]
    omitted_gate_ids: tuple[str, ...]
    fallback_reason_codes: tuple[str, ...]
    execution_performed: bool
    authority_status: str

    def __post_init__(self) -> None:
        if self.schema_version != PLAN_BOUND_EXECUTION_SCHEMA_VERSION:
            raise PlanBoundExecutionError("unsupported plan-bound execution schema_version")
        if self.selection_mode != PLAN_BOUND_SELECTION_MODE:
            raise PlanBoundExecutionError("unsupported plan-bound selection_mode")
        _authority_ref(self.plan_receipt_ref)
        for field in (
            "plan_receipt_sha256",
            "plan_receipt_digest",
            "change_record_sha256",
            "policy_sha256",
        ):
            _sha256(getattr(self, field), field)
        _sha256(
            self.architecture_graph_sha256,
            "architecture_graph_sha256",
            optional=True,
        )
        _sha256(
            self.consistency_manifest_sha256,
            "consistency_manifest_sha256",
            optional=True,
        )
        if not _SAFE_ID.fullmatch(self.change_id):
            raise PlanBoundExecutionError("change_id is unsafe")
        if self.mode not in {"affected", "fallback_full", "inconclusive"}:
            raise PlanBoundExecutionError("mode is unsupported")
        if self.effective_phase not in _PHASE_ORDER:
            raise PlanBoundExecutionError("effective_phase is unsupported")
        if self.authority_status not in {"stable", "changed"}:
            raise PlanBoundExecutionError("authority_status is unsupported")
        for field in (
            "planned_gate_ids",
            "executed_gate_ids",
            "omitted_gate_ids",
            "fallback_reason_codes",
        ):
            values = _string_array(getattr(self, field), field, maximum=256)
            if len(values) != len(set(values)):
                raise PlanBoundExecutionError(f"{field} contains duplicates")
            object.__setattr__(self, field, values)
        if any(not _SAFE_ID.fullmatch(item) for item in self.planned_gate_ids):
            raise PlanBoundExecutionError("planned_gate_ids contains unsafe identities")
        if any(not _SAFE_ID.fullmatch(item) for item in self.executed_gate_ids):
            raise PlanBoundExecutionError("executed_gate_ids contains unsafe identities")
        if set(self.planned_gate_ids) & set(self.omitted_gate_ids):
            raise PlanBoundExecutionError("planned and omitted Gate sets overlap")
        if self.mode == "inconclusive":
            if self.planned_gate_ids or self.executed_gate_ids:
                raise PlanBoundExecutionError("inconclusive execution must not run Gates")
        elif set(self.executed_gate_ids) != set(self.planned_gate_ids):
            raise PlanBoundExecutionError("executed Gate set does not match the plan")
        if self.mode == "fallback_full" and self.omitted_gate_ids:
            raise PlanBoundExecutionError("fallback_full must not omit eligible Gates")
        if type(self.execution_performed) is not bool or self.execution_performed != bool(
            self.executed_gate_ids
        ):
            raise PlanBoundExecutionError("execution_performed does not match executed Gates")


def plan_bound_execution_evidence(
    selection: PlanBoundSelection,
    executed_gate_ids: Iterable[str],
    *,
    authority_status: str = "stable",
) -> dict[str, Any]:
    if not isinstance(selection, PlanBoundSelection):
        raise TypeError("selection must be a PlanBoundSelection")
    executed = tuple(executed_gate_ids)
    document = PlanBoundExecutionEvidence(
        schema_version=PLAN_BOUND_EXECUTION_SCHEMA_VERSION,
        selection_mode=PLAN_BOUND_SELECTION_MODE,
        plan_receipt_ref=selection.plan_receipt_ref,
        plan_receipt_sha256=selection.plan_receipt_sha256,
        plan_receipt_digest=selection.plan_receipt_digest,
        change_id=selection.change_id,
        change_record_sha256=selection.change_record_sha256,
        mode=selection.mode,
        effective_phase=selection.effective_phase,
        policy_sha256=selection.policy_sha256,
        architecture_graph_sha256=selection.architecture_graph_sha256,
        consistency_manifest_sha256=selection.consistency_manifest_sha256,
        planned_gate_ids=selection.planned_gate_ids,
        executed_gate_ids=executed,
        omitted_gate_ids=selection.omitted_gate_ids,
        fallback_reason_codes=selection.fallback_reason_codes,
        execution_performed=bool(executed),
        authority_status=authority_status,
    )
    return {
        field: getattr(document, field)
        for field in (
            "schema_version",
            "selection_mode",
            "plan_receipt_ref",
            "plan_receipt_sha256",
            "plan_receipt_digest",
            "change_id",
            "change_record_sha256",
            "mode",
            "effective_phase",
            "policy_sha256",
            "architecture_graph_sha256",
            "consistency_manifest_sha256",
            "planned_gate_ids",
            "executed_gate_ids",
            "omitted_gate_ids",
            "fallback_reason_codes",
            "execution_performed",
            "authority_status",
        )
    }


def parse_plan_bound_execution(value: object) -> PlanBoundExecutionEvidence:
    mapping = _mapping(value, "plan-bound execution evidence")
    _closed(mapping, _EVIDENCE_FIELDS, "plan-bound execution evidence")
    return PlanBoundExecutionEvidence(**dict(mapping))


__all__ = [
    "PLAN_BOUND_EXECUTION_OUTPUT_KEY",
    "PLAN_BOUND_EXECUTION_SCHEMA_VERSION",
    "PLAN_BOUND_SELECTION_MODE",
    "PlanBoundExecutionError",
    "PlanBoundExecutionEvidence",
    "PlanBoundSelection",
    "parse_plan_bound_execution",
    "plan_bound_execution_evidence",
    "prepare_plan_bound_selection",
    "selected_gate_definitions",
]
