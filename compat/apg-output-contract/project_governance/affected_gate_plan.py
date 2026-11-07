"""Pure, bounded, execution-free planning for architecture-affected Gates."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
import re
from typing import Any
import unicodedata

from .architecture_graph import ArchitectureGraph, architecture_graph_impact
from .gates import GateDefinition
from .receipts import redact
from .storage import digest


AFFECTED_GATE_PLAN_SCHEMA_VERSION = "1.0"

_PHASE_ORDER = {"fast": 0, "full": 1, "release": 2}
_MAX_DECLARED_PATHS = 1024
_MAX_DERIVED_PATHS = 512
_MAX_PLANNING_PATHS = 1024
_MAX_PROJECTED_GATE_IDS = 256
_MAX_PATH_CHARS = 256
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_GATE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_AUTHORITY_PATHS = {
    "policy": ".governance/policy.toml",
    "architecture_graph": ".governance/architecture.graph.json",
    "consistency_manifest": ".governance/consistency.manifest.json",
}


class AffectedGatePlanError(ValueError):
    """Raised when planner inputs violate the closed planning contract."""


def _sequence(value: object, label: str, maximum: int) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AffectedGatePlanError(f"{label} must be an array")
    if len(value) > maximum:
        raise AffectedGatePlanError(f"{label} exceeds its {maximum}-item bound")
    return value


def _path(value: object, label: str) -> str:
    if type(value) is not str or not value or len(value) > _MAX_PATH_CHARS:
        raise AffectedGatePlanError(f"{label} must be a bounded non-empty path")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise AffectedGatePlanError(f"{label} contains control characters")
    normalized = value.replace("\\", "/")
    if (
        normalized.startswith("/")
        or normalized.endswith("/")
        or "//" in normalized
        or ":" in normalized
        or "?" in normalized
        or "#" in normalized
    ):
        raise AffectedGatePlanError(f"{label} must be a safe project-relative path")
    parts = normalized.split("/")
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise AffectedGatePlanError(f"{label} must be traversal-free")
    return candidate.as_posix()


def _paths(value: object, label: str, maximum: int) -> tuple[str, ...]:
    items = _sequence(value, label, maximum)
    return tuple(
        sorted({_path(item, f"{label}[{index}]") for index, item in enumerate(items)})
    )


def _digest(value: object, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise AffectedGatePlanError(f"{label} must be lowercase SHA-256")
    return value


def _phase(value: object, label: str) -> str:
    if type(value) is not str or value not in _PHASE_ORDER:
        raise AffectedGatePlanError(f"{label} must be fast, full, or release")
    return value


def _highest_phase(*values: str) -> str:
    return max(values, key=_PHASE_ORDER.__getitem__)


def _visible_gate_id(value: str) -> str:
    if redact(value) == value:
        return value
    return f"redacted-gate-{digest(value)[:12]}"


def _visible_gate_ids(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(_visible_gate_id(value) for value in values))


def _changed_authorities(paths: Sequence[str]) -> tuple[str, ...]:
    changed = []
    for name, authority_path in _AUTHORITY_PATHS.items():
        authority_key = unicodedata.normalize("NFC", authority_path).casefold()
        if any(
            unicodedata.normalize("NFC", path).casefold() == authority_key
            or authority_key.startswith(
                unicodedata.normalize("NFC", path).casefold() + "/"
            )
            for path in paths
        ):
            changed.append(name)
    return tuple(sorted(changed))


def _consistency_inputs(
    consistency_impact: Mapping[str, object] | None,
) -> tuple[str | None, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    if consistency_impact is None:
        return None, (), (), ()
    if not isinstance(consistency_impact, Mapping):
        raise AffectedGatePlanError("consistency_impact must be an object")
    manifest_sha256 = _digest(
        consistency_impact.get("manifest_sha256"),
        "consistency_impact.manifest_sha256",
    )
    endpoints = _paths(
        consistency_impact.get("affected_endpoints", ()),
        "consistency_impact.affected_endpoints",
        _MAX_DERIVED_PATHS,
    )
    relationships = _sequence(
        consistency_impact.get("relationships", ()),
        "consistency_impact.relationships",
        128,
    )
    nonpassing: list[str] = []
    reason_codes: set[str] = set()
    for index, raw in enumerate(relationships):
        if not isinstance(raw, Mapping):
            raise AffectedGatePlanError(
                f"consistency_impact.relationships[{index}] must be an object"
            )
        relationship_id = raw.get("relationship_id")
        status = raw.get("status")
        if type(relationship_id) is not str or not relationship_id:
            raise AffectedGatePlanError(
                f"consistency_impact.relationships[{index}].relationship_id is required"
            )
        if status not in {"pass", "drift", "missing", "not_evaluated"}:
            raise AffectedGatePlanError(
                f"consistency_impact.relationships[{index}].status is unsupported"
            )
        if status != "pass":
            nonpassing.append(relationship_id)
            reason_codes.add(
                "consistency.missing"
                if status == "missing"
                else "consistency.drift"
                if status == "drift"
                else "consistency.not_evaluated"
            )
    return (
        manifest_sha256,
        endpoints,
        tuple(sorted(set(nonpassing))),
        tuple(sorted(reason_codes)),
    )


def plan_affected_gates(
    graph: ArchitectureGraph | None,
    *,
    changed_paths: Sequence[str],
    gates: Sequence[GateDefinition],
    required_phase: str,
    policy_sha256: str | None,
    consistency_impact: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    """Return a conservative Gate plan without selecting or executing any Gate."""
    if graph is not None and not isinstance(graph, ArchitectureGraph):
        raise TypeError("graph must be an ArchitectureGraph or None")
    declared_paths = _paths(changed_paths, "changed_paths", _MAX_DECLARED_PATHS)
    if not declared_paths:
        raise AffectedGatePlanError("changed_paths must not be empty")
    required = _phase(required_phase, "required_phase")
    policy_digest = _digest(policy_sha256, "policy_sha256", optional=True)
    gate_items = _sequence(gates, "gates", 4096)
    if any(not isinstance(gate, GateDefinition) for gate in gate_items):
        raise TypeError("gates must contain GateDefinition values")
    if len({gate.gate_id for gate in gate_items}) != len(gate_items):
        raise AffectedGatePlanError("gates contains duplicate Gate IDs")
    gate_by_id = {gate.gate_id: gate for gate in gate_items}
    policy_gate_ids = tuple(sorted(gate_by_id))
    gate_projection_exhausted = len(policy_gate_ids) > _MAX_PROJECTED_GATE_IDS

    (
        manifest_sha256,
        derived_paths,
        nonpassing_relationships,
        consistency_reasons,
    ) = _consistency_inputs(consistency_impact)
    combined_paths = tuple(sorted(set(declared_paths).union(derived_paths)))
    reasons: set[str] = set(consistency_reasons)
    changed_authorities = _changed_authorities(declared_paths)
    reasons.update(f"authority.{name}_changed" for name in changed_authorities)
    if gate_projection_exhausted:
        reasons.add("planning.bounds_exhausted")

    projected: Mapping[str, object] | None = None
    if graph is None:
        reasons.add("architecture.graph_absent")
        planning_paths = combined_paths
        candidate_gate_ids = ()
    elif len(combined_paths) > _MAX_PLANNING_PATHS:
        reasons.add("planning.bounds_exhausted")
        planning_paths: tuple[str, ...] = ()
        candidate_gate_ids: tuple[str, ...] = ()
    else:
        planning_paths = combined_paths
        projected = architecture_graph_impact(
            graph,
            planning_paths,
            policy_gate_ids=policy_gate_ids,
        )
        candidate_gate_ids = tuple(projected["candidate_gate_ids"])
        if len(candidate_gate_ids) > _MAX_PROJECTED_GATE_IDS:
            reasons.add("planning.bounds_exhausted")
        if not projected["direct_node_ids"]:
            reasons.add("architecture.no_direct_node")
        if projected["unmapped_paths"]:
            reasons.add("architecture.unmapped_path")
        if projected["ambiguous_paths"]:
            reasons.add("architecture.ambiguous_path")
        if projected["cycle_detected"]:
            reasons.add("architecture.cycle")
        if projected["unknown_gate_ids"]:
            reasons.add("architecture.unknown_gate")
        if projected["traversal_exhausted"]:
            reasons.add("architecture.traversal_exhausted")

    if policy_digest is None:
        reasons.add("policy.absent")
    unsafe_gate_ids = tuple(
        sorted(
            gate_id
            for gate_id in policy_gate_ids
            if not _SAFE_GATE_ID.fullmatch(gate_id)
        )
    )
    if unsafe_gate_ids:
        reasons.add("policy.unsafe_gate_id")
    if graph is not None and not candidate_gate_ids:
        reasons.add("architecture.empty_candidate")

    known_candidates = tuple(
        gate_id for gate_id in candidate_gate_ids if gate_id in gate_by_id
    )
    candidate_phases = tuple(gate_by_id[gate_id].phase for gate_id in known_candidates)
    effective = _highest_phase(required, *candidate_phases) if candidate_phases else required
    if reasons:
        effective = _highest_phase(effective, "full")

    def cumulative_gate_ids(phase: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                gate.gate_id
                for gate in gate_items
                if _PHASE_ORDER[gate.phase] <= _PHASE_ORDER[phase]
            )
        )

    eligible_gate_ids = cumulative_gate_ids(effective)
    unassigned_gate_ids = (
        tuple(sorted(set(eligible_gate_ids) - set(graph.referenced_gate_ids)))
        if graph is not None
        else ()
    )
    if unassigned_gate_ids:
        reasons.add("policy.unassigned_gate")
        promoted = _highest_phase(effective, "full")
        if promoted != effective:
            effective = promoted
            eligible_gate_ids = cumulative_gate_ids(effective)
            unassigned_gate_ids = (
                tuple(sorted(set(eligible_gate_ids) - set(graph.referenced_gate_ids)))
                if graph is not None
                else ()
            )
    eligible_candidates = tuple(
        gate_id for gate_id in candidate_gate_ids if gate_id in set(eligible_gate_ids)
    )
    if not eligible_gate_ids:
        reasons.add("policy.no_eligible_gate")
    if graph is not None and not eligible_candidates:
        reasons.add("policy.no_eligible_candidate")

    if (
        "policy.absent" in reasons
        or "policy.no_eligible_gate" in reasons
        or "policy.unsafe_gate_id" in reasons
        or bool(changed_authorities)
        or gate_projection_exhausted
        or len(candidate_gate_ids) > _MAX_PROJECTED_GATE_IDS
        or len(eligible_gate_ids) > _MAX_PROJECTED_GATE_IDS
    ):
        mode = "inconclusive"
        planned_gate_ids: tuple[str, ...] = ()
    elif reasons:
        mode = "fallback_full"
        planned_gate_ids = eligible_gate_ids
    else:
        mode = "affected"
        planned_gate_ids = eligible_candidates
    omitted_gate_ids = tuple(sorted(set(eligible_gate_ids) - set(planned_gate_ids)))
    if mode == "inconclusive" and "planning.bounds_exhausted" in reasons:
        candidate_gate_ids = ()
        eligible_gate_ids = ()
        eligible_candidates = ()
        planned_gate_ids = ()
        omitted_gate_ids = ()
        unassigned_gate_ids = ()
        unsafe_gate_ids = ()

    return {
        "schema_version": AFFECTED_GATE_PLAN_SCHEMA_VERSION,
        "mode": mode,
        "policy_sha256": policy_digest,
        "architecture_graph_sha256": graph.digest if graph is not None else None,
        "consistency_manifest_sha256": manifest_sha256,
        "required_phase": required,
        "effective_phase": effective,
        "changed_paths": declared_paths,
        "derived_consistency_paths": derived_paths,
        "planning_paths": planning_paths,
        "direct_node_ids": tuple(projected["direct_node_ids"]) if projected else (),
        "affected_node_ids": tuple(projected["affected_node_ids"]) if projected else (),
        "candidate_gate_ids": _visible_gate_ids(candidate_gate_ids),
        "eligible_policy_gate_ids": _visible_gate_ids(eligible_gate_ids),
        "eligible_candidate_gate_ids": _visible_gate_ids(eligible_candidates),
        "planned_gate_ids": _visible_gate_ids(planned_gate_ids),
        "omitted_gate_ids": _visible_gate_ids(omitted_gate_ids),
        "unassigned_gate_ids": _visible_gate_ids(unassigned_gate_ids),
        "unsafe_gate_ids": _visible_gate_ids(unsafe_gate_ids),
        "nonpassing_consistency_relationship_ids": tuple(
            sorted(
                _visible_gate_id(relationship_id)
                for relationship_id in nonpassing_relationships
            )
        ),
        "fallback_reason_codes": tuple(sorted(reasons)),
        "fallback_full": mode == "fallback_full",
        "execution_performed": False,
    }


__all__ = [
    "AFFECTED_GATE_PLAN_SCHEMA_VERSION",
    "AffectedGatePlanError",
    "plan_affected_gates",
]
