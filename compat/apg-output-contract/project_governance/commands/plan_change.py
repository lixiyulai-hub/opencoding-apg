"""Canonical approval-gated change planning."""
from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import os
import re
from pathlib import Path
from typing import Any, Mapping
import unicodedata
from ..affected_gate_plan import plan_affected_gates
from ..architecture_graph import (
    ARCHITECTURE_GRAPH_RELATIVE_PATH,
    architecture_graph_impact,
    load_architecture_graph,
)
from ..consistency_manifest import (
    CONSISTENCY_MANIFEST_RELATIVE_PATH,
    consistency_manifest_impact,
    evaluate_consistency_manifest,
    load_consistency_manifest,
)
from ..audit_contract import snapshot_for_audit
from ..feedback_loops import (
    feedback_loop_sidecar_bytes,
    feedback_loop_sidecar_path,
    parse_feedback_loop_sidecar,
)
from ..gates import GateDefinition, parse_gate_definitions
from ..model import ChangeRecord, RiskClass
from ..path_guard import PathViolation, WorkspaceGuard, WorkspaceTransaction
from ..policy import classify_change
from ..receipts import build_receipt, receipt_digest, redact_contract
from ..regression_ledger import REGRESSION_UPDATE_KEY, prepare_regression_update
from ..storage import canonical_json_bytes, digest, dump_policy_toml, load_policy_toml

REQUIRED_FIELDS = ("change_id", "problem", "outcome", "non_goals", "acceptance", "metric", "changed_paths", "surfaces", "rollout", "telemetry", "rollback")
_CHANGE_ID = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_APPROVAL_FIELDS = {"id", "actor", "role", "timestamp_utc", "scope"}


def _portable_path_key(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\\", "/")).casefold()


def _reject_graph_authority_alias(
    guard: WorkspaceGuard, normalized: str, resolved: Path
) -> None:
    graph_path = guard.root / ARCHITECTURE_GRAPH_RELATIVE_PATH
    if not os.path.lexists(graph_path):
        return
    resolved_relative = resolved.relative_to(guard.root).as_posix()
    if _portable_path_key(normalized) != _portable_path_key(resolved_relative):
        raise ValueError(f"changed path aliases another project path: {normalized}")
    if not os.path.lexists(resolved):
        return
    authority_paths = (
        ".governance/policy.toml",
        ARCHITECTURE_GRAPH_RELATIVE_PATH,
        CONSISTENCY_MANIFEST_RELATIVE_PATH,
    )
    for authority_relative in authority_paths:
        authority = guard.root / authority_relative
        if (
            os.path.lexists(authority)
            and os.path.samefile(resolved, authority)
            and _portable_path_key(normalized)
            != _portable_path_key(authority_relative)
        ):
            raise ValueError(
                f"changed path aliases governance authority: {normalized}"
            )

@dataclass(frozen=True)
class PlanChangeResult:
    ok: bool
    message: str
    risk: RiskClass | None = None
    required_phase: str = ""
    planned_paths: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    receipt: Any = None

def _failure(message: str, risk: RiskClass | None = None) -> PlanChangeResult:
    return PlanChangeResult(False, message, risk=risk)


def _contract_projection(value: object, label: str) -> object:
    projected = redact_contract(value)
    if canonical_json_bytes(projected) != canonical_json_bytes(value):
        raise ValueError(f"{label} contains non-recordable sensitive material")
    return projected

def _timestamp(value: object, label: str) -> datetime:
    if type(value) is not str:
        raise ValueError(f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be a valid UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed) or parsed > datetime.now(timezone.utc):
        raise ValueError(f"{label} must be current or past UTC")
    return parsed.astimezone(timezone.utc)

def _approval(value: object, guard: WorkspaceGuard, allowed: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _APPROVAL_FIELDS:
        raise ValueError("approvals must be structured mappings")
    item = dict(value)
    for field in ("id", "actor", "role"):
        if type(item[field]) is not str or not item[field].strip():
            raise ValueError(f"approval {field} must be a non-empty string")
    _timestamp(item["timestamp_utc"], f"approval {item['id']} timestamp_utc")
    scope = item["scope"]
    if isinstance(scope, str) or not isinstance(scope, (list, tuple)) or not scope:
        raise ValueError("approval scope must be a non-empty sequence")
    normalized = []
    for path in scope:
        if type(path) is not str or not path:
            raise ValueError("approval scope must contain strings")
        if path == ".":
            normalized.append(".")
            continue
        normalized_path = path.replace("\\", "/")
        if Path(path).is_absolute() or any(part in ("", ".", "..") for part in normalized_path.split("/")):
            raise ValueError("approval scope must be relative and traversal-free")
        relative = guard.resolve_write(path).relative_to(guard.root).as_posix()
        if not any(relative == prefix or relative.startswith(prefix.rstrip("/") + "/") for prefix in allowed):
            raise ValueError("approval scope is outside the authorized change")
        normalized.append(relative)
    item["scope"] = tuple(sorted(set(normalized)))
    return item

def _validate(request: Mapping[str, Any], guard: WorkspaceGuard):
    if not isinstance(request, Mapping):
        raise ValueError("request must be an object")
    missing = [field for field in REQUIRED_FIELDS if field not in request]
    if missing:
        raise ValueError(f"missing product intent: {', '.join(missing)}")
    sequence_fields = {"non_goals", "acceptance", "changed_paths", "surfaces", "telemetry"}
    for field in REQUIRED_FIELDS:
        value = request[field]
        if field in sequence_fields:
            if not isinstance(value, (list, tuple)) or not value or any(type(item) is not str or not item.strip() for item in value):
                raise ValueError(f"{field} must be a non-empty sequence of strings")
        elif type(value) is not str or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
    change_id = request["change_id"]
    if not _CHANGE_ID.fullmatch(change_id) or "/" in change_id or "\\" in change_id:
        raise ValueError("change_id must start with lowercase ASCII alnum and contain only ._-")
    changed_paths = []
    for path in request["changed_paths"]:
        normalized = path.replace("\\", "/")
        if Path(path).is_absolute() or any(part in ("", ".", "..") for part in normalized.split("/")):
            raise ValueError(f"changed path is not relative and traversal-free: {path}")
        resolved = guard.resolve_write(path)
        _reject_graph_authority_alias(guard, normalized, resolved)
        changed_paths.append(normalized)
    decision = classify_change(changed_paths, surfaces=request["surfaces"])
    relative = f".governance/changes/{change_id}.json"
    raw_approvals = request.get("approvals", ())
    if isinstance(raw_approvals, (str, bytes)) or not isinstance(raw_approvals, (list, tuple)):
        raise ValueError("approvals must be a sequence of structured mappings")
    approvals = tuple(_approval(item, guard, tuple(changed_paths)) for item in raw_approvals)
    if len({item["id"] for item in approvals}) != len(approvals) or len({item["actor"] for item in approvals}) != len(approvals):
        raise ValueError("approval IDs and actors must be distinct")
    record = ChangeRecord(change_id=change_id, problem=request["problem"], outcome=request["outcome"], non_goals=tuple(request["non_goals"]), acceptance=tuple(request["acceptance"]), metric=request["metric"], changed_paths=tuple(changed_paths), surfaces=tuple(request["surfaces"]), rollout=request["rollout"], telemetry=tuple(request["telemetry"]), rollback=request["rollback"], risk=decision.risk, approval_refs=tuple(item["id"] for item in approvals))
    loop_value = request.get("feedback_loop")
    feedback_loop = (
        None
        if loop_value is None
        else parse_feedback_loop_sidecar(
            loop_value,
            root=guard.root,
            expected_change_id=change_id,
        )
    )
    regression_value = request.get(REGRESSION_UPDATE_KEY)
    regression_update = (
        None
        if regression_value is None
        else prepare_regression_update(guard.root, regression_value)
    )
    if regression_update is not None and regression_update.relative_path not in changed_paths:
        raise ValueError("regression update path must be included in changed_paths")
    return record, decision, approvals, relative, feedback_loop, regression_update

def _policy_gates(root: Path) -> tuple[tuple[GateDefinition, ...], str | None]:
    policy_path = root / ".governance" / "policy.toml"
    if policy_path.is_symlink():
        raise ValueError("Gate policy must be a regular project file")
    if not policy_path.is_file():
        return (), None
    resolved = policy_path.resolve(strict=True)
    if (
        resolved != policy_path
        or not resolved.is_relative_to(root)
    ):
        raise ValueError("Gate policy must be a regular project file")
    policy = load_policy_toml(policy_path.read_text(encoding="utf-8"))
    gates = parse_gate_definitions(policy.gates)
    policy_sha256 = digest(dump_policy_toml(policy).encode("utf-8"))
    return gates, policy_sha256


def _impact(
    record: ChangeRecord, root: Path, *, required_phase: str
) -> tuple[dict[str, object], str]:
    paths = tuple(sorted(record.changed_paths))
    result: dict[str, object] = {
        "modules": tuple(path for path in paths if path.endswith((".py", ".js", ".ts", ".go", ".rs"))),
        "contracts": tuple(path for path in paths if any(token in path.lower() for token in ("api", "schema", "contract", "openapi"))),
        "data": tuple(path for path in paths if any(token in path.lower() for token in ("data", "migration", "model", "database"))),
        "tests": tuple(path for path in paths if path.startswith("tests/") or "/tests/" in path),
        "budgets": tuple(path for path in paths if any(token in path.lower() for token in ("budget", "perf", "performance"))),
        "adapters": tuple(path for path in paths if path.startswith((".github/", ".cursor/", ".git/")) or path in ("AGENTS.md", "CLAUDE.md")),
    }
    graph = load_architecture_graph(root)
    gates, policy_sha256 = _policy_gates(root)
    if graph is not None:
        policy_gate_ids = tuple(gate.gate_id for gate in gates)
        result["architecture_graph"] = architecture_graph_impact(
            graph,
            paths,
            policy_gate_ids=policy_gate_ids,
        )
    consistency_manifest = load_consistency_manifest(root)
    if consistency_manifest is not None:
        consistency_evaluation = evaluate_consistency_manifest(
            root, consistency_manifest
        )
        result["consistency_manifest"] = consistency_manifest_impact(
            consistency_manifest,
            paths,
            evaluation=consistency_evaluation,
        )
    result["affected_gate_plan"] = plan_affected_gates(
        graph,
        changed_paths=paths,
        gates=gates,
        required_phase=required_phase,
        policy_sha256=policy_sha256,
        consistency_impact=result.get("consistency_manifest"),
    )
    return result, policy_sha256 or ""

def run_plan_change(target: str | Path, request: Mapping[str, Any], *, apply: bool = False) -> PlanChangeResult:
    try:
        guard = WorkspaceGuard(Path(target))
        record, decision, approvals, relative, feedback_loop, regression_update = _validate(request, guard)
        if apply and decision.risk in (RiskClass.HIGH, RiskClass.CRITICAL) and not any(item["role"] == "owner" for item in approvals):
            return _failure("high and critical changes require an authorized owner approval", decision.risk)
        if apply and decision.risk is RiskClass.CRITICAL:
            owners = [item for item in approvals if item["role"] == "owner"]
            verifiers = [item for item in approvals if item["role"] == "independent-verifier"]
            if not owners or not verifiers or owners[0]["actor"] == verifiers[0]["actor"]:
                return _failure("critical changes require a distinct independent-verifier actor", decision.risk)
        payload = canonical_json_bytes(record)
        existing = guard.resolve_write(relative)
        record_preexisting = existing.exists()
        if record_preexisting and existing.read_bytes() != payload:
            return _failure("duplicate change ID has different content", decision.risk)
        loop_relative = (
            feedback_loop_sidecar_path(record.change_id)
            if feedback_loop is not None
            else None
        )
        loop_payload = (
            feedback_loop_sidecar_bytes(feedback_loop)
            if feedback_loop is not None
            else None
        )
        loop_existing = (
            guard.resolve_write(loop_relative) if loop_relative is not None else None
        )
        implicit_loop = guard.resolve_write(feedback_loop_sidecar_path(record.change_id))
        if feedback_loop is None and implicit_loop.exists():
            return _failure("existing loop-enabled change requires its feedback_loop input", decision.risk)
        loop_preexisting = loop_existing is not None and loop_existing.exists()
        if loop_preexisting and loop_existing.read_bytes() != loop_payload:
            return _failure("duplicate feedback-loop sidecar has different content", decision.risk)
        if record_preexisting and loop_existing is not None and not loop_preexisting:
            return _failure("existing loop-enabled change is missing its immutable sidecar", decision.risk)
        regression_relative = (
            regression_update.relative_path if regression_update is not None else None
        )
        regression_existing = (
            guard.resolve_write(regression_relative)
            if regression_relative is not None
            else None
        )
        regression_preexisting = (
            regression_existing is not None and regression_existing.is_file()
        )
        if record_preexisting and regression_existing is not None and not regression_preexisting:
            return _failure("existing regression-enabled change is missing its ledger record", decision.risk)
        if record_preexisting:
            receipts = sorted((guard.root / ".governance" / "receipts").glob(f"*-plan-change-{record.change_id}-*.json"))
            if receipts:
                planned = (relative,) + ((loop_relative,) if loop_relative else ()) + ((regression_relative,) if regression_relative else ())
                return PlanChangeResult(True, "already applied" if apply else "already planned", decision.risk, decision.required_phase, planned, (), planned, None)
        planned_records = (relative,) + ((loop_relative,) if loop_relative else ()) + ((regression_relative,) if regression_relative else ())
        impact, policy_digest = _impact(
            record, guard.root, required_phase=decision.required_phase
        )
        receipt_outputs = {"risk": decision.risk.value, "required_phase": decision.required_phase, "impact": impact, "change": record}
        if feedback_loop is not None:
            receipt_outputs["feedback_loop"] = feedback_loop
        if regression_update is not None:
            receipt_outputs["regression_record"] = regression_update.record
        record_projection = _contract_projection(record, "ChangeRecord")
        impact_projection = _contract_projection(impact, "plan impact")
        approval_projection = _contract_projection(approvals, "plan approvals")
        scope_projection = _contract_projection(
            planned_records + (".governance/receipts",),
            "plan authorized scope",
        )
        evidence_projection = _contract_projection(
            record.changed_paths, "plan evidence scope"
        )
        receipt = build_receipt(command="plan-change", policy_digest=policy_digest, target_fingerprint=digest(snapshot_for_audit(guard)), authorized_scope=planned_records + (".governance/receipts",), inputs=dict(request), outputs=receipt_outputs, approvals=tuple(item["id"] for item in approvals), classification=decision.risk.value, evidence_refs=tuple(record.changed_paths))
        receipt_inputs = dict(receipt.inputs)
        for field in REQUIRED_FIELDS:
            receipt_inputs[field] = record_projection[field]
        if "approvals" in request:
            receipt_inputs["approvals"] = approval_projection
        receipt_outputs = dict(receipt.outputs)
        receipt_outputs["change"] = record_projection
        receipt_outputs["impact"] = impact_projection
        receipt = replace(
            receipt,
            authorized_scope=scope_projection,
            inputs=receipt_inputs,
            outputs=receipt_outputs,
            approvals=tuple(record_projection["approval_refs"]),
            evidence_refs=evidence_projection,
        )
        receipt_relative = f".governance/receipts/{receipt.timestamp_utc.replace(':', '').replace('-', '')}-plan-change-{record.change_id}-{receipt_digest(receipt)[:12]}.json"
        transaction = WorkspaceTransaction(guard, planned_records + (receipt_relative,), apply=apply)
        if not record_preexisting:
            transaction.stage_bytes(relative, payload)
        if loop_existing is not None and not loop_preexisting and loop_payload is not None:
            transaction.stage_bytes(loop_relative, loop_payload)
        if regression_update is not None and (
            not regression_preexisting
            or regression_existing.read_bytes() != regression_update.payload
        ):
            transaction.stage_bytes(regression_relative, regression_update.payload)
        transaction.stage_bytes(receipt_relative, canonical_json_bytes(receipt))
        committed = transaction.commit()
        conflicts = tuple(
            item
            for item, was_present in (
                (relative, record_preexisting),
                (loop_relative, loop_preexisting),
                (regression_relative, regression_preexisting),
            )
            if item is not None and was_present
        )
        return PlanChangeResult(True, "planned" if not apply else "applied", decision.risk, decision.required_phase, committed.planned_paths, committed.changed_paths, conflicts, receipt)
    except (OSError, ValueError, PathViolation, TypeError) as error:
        return _failure(str(error))

__all__ = ["PlanChangeResult", "run_plan_change"]
