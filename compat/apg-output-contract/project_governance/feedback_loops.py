"""Closed feedback-loop records, receipt chains, budgets, and stop decisions."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .gates import GateDefinition
from .model import CheckResult, CheckStatus, Receipt
from .regression_identity import (
    REGRESSION_GATE_CONTRACTS_OUTPUT_KEY,
    candidate_proposal,
    parse_regression_proposal,
    symptom_code_for_check,
    validate_proposal_against_receipt,
)
from .receipts import (
    FEEDBACK_LOOP_DECISION_OUTPUT_KEY,
    FEEDBACK_LOOP_INPUT_KEY,
    load_receipt_json,
)
from .storage import canonical_json_bytes, digest


EXTENSION_SCHEMA_VERSION = "1.0"
PROGRESS_SCHEMA_VERSION = "feedback-progress-v1"
_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_SYMPTOM = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REFERENCE_COUNT = 32
_MAX_REFERENCE_CHARS = 256
_MAX_RECORD_BYTES = 1_048_576
_MAX_COUNTER = 2**63 - 1
_MAX_CHAIN_RECEIPTS = 10_000
_MAX_PROGRESS_ITEMS = 128
_ACCEPTANCE_STATUSES = frozenset({"pass", "fail", "inconclusive", "not_applicable"})
_OWNER_DECISION_CODES = frozenset({"none", "continue", "revise", "defer", "stop"})
_SENSITIVE_CODE_PARTS = (
    "token",
    "password",
    "secret",
    "credential",
    "cookie",
    "authorization",
    "private_key",
)


class FeedbackLoopType(str, Enum):
    AGENTIC_CODING = "agentic_coding"
    DEVELOPER_REVIEW = "developer_review"
    EXTERNAL_FEEDBACK = "external_feedback"


class LoopStopState(str, Enum):
    CONTINUE = "continue"
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILURE_THRESHOLD = "failure_threshold"
    NO_PROGRESS = "no_progress"
    BLOCKED = "blocked"
    OWNER_DECISION_REQUIRED = "owner_decision_required"


@dataclass(frozen=True)
class LoopBudget:
    max_iterations: int
    max_elapsed_seconds: int
    max_cost_units: int
    max_failures: int
    max_consecutive_no_progress: int

    def __post_init__(self) -> None:
        positive = (
            "max_iterations",
            "max_elapsed_seconds",
            "max_consecutive_no_progress",
        )
        nonnegative = ("max_cost_units", "max_failures")
        for name in positive + nonnegative:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if name in positive and value <= 0:
                raise ValueError(f"{name} must be positive")
            if name in nonnegative and value < 0:
                raise ValueError(f"{name} must be non-negative")
            if value > _MAX_COUNTER:
                raise ValueError(f"{name} exceeds the supported integer range")


@dataclass(frozen=True)
class FeedbackLoopSidecar:
    extension_schema_version: str
    loop_id: str
    loop_type: FeedbackLoopType
    change_id: str
    input_evidence_refs: tuple[str, ...]
    budget: LoopBudget


@dataclass(frozen=True)
class ProgressEvidence:
    metrics: tuple[tuple[str, int | float], ...] = ()
    acceptance: tuple[tuple[str, str], ...] = ()
    incidents: tuple[str, ...] = ()
    owner_decision_code: str = "none"


@dataclass(frozen=True)
class LoopRun:
    change_id: str
    loop_id: str
    cost_units: int
    input_evidence_refs: tuple[str, ...]
    previous_receipt_ref: str | None = None
    progress_evidence: ProgressEvidence = ProgressEvidence()


@dataclass(frozen=True)
class LoopUsage:
    iterations: int
    elapsed_seconds: int
    cost_units: int
    failures: int
    consecutive_no_progress: int

    def __post_init__(self) -> None:
        for name in (
            "iterations",
            "elapsed_seconds",
            "cost_units",
            "failures",
            "consecutive_no_progress",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
            if value > _MAX_COUNTER:
                raise ValueError(f"{name} exceeds the supported integer range")


@dataclass(frozen=True)
class PreparedLoopRun:
    sidecar: FeedbackLoopSidecar
    run: LoopRun
    sidecar_digest: str
    previous_usage: LoopUsage
    previous_fingerprint: str
    previous_stop_state: LoopStopState | None


@dataclass(frozen=True)
class LoopDecision:
    sidecar: FeedbackLoopSidecar
    usage: LoopUsage
    progress_fingerprint: str
    stop_state: LoopStopState
    decision_code: str
    next_gate: str
    proposed_regression_delta: Mapping[str, Any]


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise TypeError(f"{label} must be a string-keyed object")
    return value


def _fields(
    value: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    label: str,
) -> None:
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ValueError(f"{label} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")


def _code(value: object, label: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError(f"{label} has invalid syntax")
    normalized = value.casefold().replace("-", "_")
    if any(part in normalized for part in _SENSITIVE_CODE_PARTS):
        raise ValueError(f"{label} contains a reserved sensitive label")
    return value


def _integer(value: object, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if positive and value <= 0:
        raise ValueError(f"{label} must be positive")
    if not positive and value < 0:
        raise ValueError(f"{label} must be non-negative")
    if value > _MAX_COUNTER:
        raise ValueError(f"{label} exceeds the supported integer range")
    return value


def _bounded_array(value: object, label: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise TypeError(f"{label} must be an array")
    if len(value) > _MAX_PROGRESS_ITEMS:
        raise ValueError(f"{label} exceeds the bounded item limit")
    return tuple(value)


def _normalized_number(value: object, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be numeric")
    if isinstance(value, int):
        if abs(value) > _MAX_COUNTER:
            raise ValueError(f"{label} exceeds the supported numeric range")
        return value
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    if abs(value) > _MAX_COUNTER:
        raise ValueError(f"{label} exceeds the supported numeric range")
    if value == 0 or value.is_integer():
        return int(value)
    return value


def _parse_progress_evidence(
    value: object,
    *,
    label: str = "progress_evidence",
) -> ProgressEvidence:
    item = _mapping(value, label)
    _fields(
        item,
        required=frozenset({"metrics", "acceptance", "incidents", "owner_decision_code"}),
        label=label,
    )

    metrics: list[tuple[str, int | float]] = []
    metric_ids: set[str] = set()
    for index, raw in enumerate(_bounded_array(item["metrics"], f"{label}.metrics")):
        metric = _mapping(raw, f"{label}.metrics[{index}]")
        _fields(
            metric,
            required=frozenset({"id", "value"}),
            label=f"{label}.metrics[{index}]",
        )
        metric_id = _code(metric["id"], f"{label}.metrics[{index}].id")
        if metric_id in metric_ids:
            raise ValueError(f"{label}.metrics contains duplicate IDs")
        metric_ids.add(metric_id)
        metrics.append(
            (
                metric_id,
                _normalized_number(metric["value"], f"{label}.metrics[{index}].value"),
            )
        )

    acceptance: list[tuple[str, str]] = []
    acceptance_ids: set[str] = set()
    for index, raw in enumerate(_bounded_array(item["acceptance"], f"{label}.acceptance")):
        record = _mapping(raw, f"{label}.acceptance[{index}]")
        _fields(
            record,
            required=frozenset({"id", "status"}),
            label=f"{label}.acceptance[{index}]",
        )
        acceptance_id = _code(record["id"], f"{label}.acceptance[{index}].id")
        if acceptance_id in acceptance_ids:
            raise ValueError(f"{label}.acceptance contains duplicate IDs")
        acceptance_ids.add(acceptance_id)
        status = record["status"]
        if type(status) is not str or status not in _ACCEPTANCE_STATUSES:
            raise ValueError(f"{label}.acceptance[{index}].status must be a closed status code")
        acceptance.append((acceptance_id, status))

    incidents: list[str] = []
    incident_ids: set[str] = set()
    for index, raw in enumerate(_bounded_array(item["incidents"], f"{label}.incidents")):
        incident_id = _code(raw, f"{label}.incidents[{index}]")
        if incident_id in incident_ids:
            raise ValueError(f"{label}.incidents contains duplicate IDs")
        incident_ids.add(incident_id)
        incidents.append(incident_id)

    owner_decision_code = item["owner_decision_code"]
    if type(owner_decision_code) is not str or owner_decision_code not in _OWNER_DECISION_CODES:
        raise ValueError(f"{label}.owner_decision_code must be a closed decision code")
    return ProgressEvidence(
        metrics=tuple(sorted(metrics)),
        acceptance=tuple(sorted(acceptance)),
        incidents=tuple(sorted(incidents)),
        owner_decision_code=owner_decision_code,
    )


def _progress_evidence_mapping(value: ProgressEvidence) -> dict[str, Any]:
    return {
        "metrics": tuple({"id": key, "value": metric} for key, metric in value.metrics),
        "acceptance": tuple(
            {"id": key, "status": status} for key, status in value.acceptance
        ),
        "incidents": value.incidents,
        "owner_decision_code": value.owner_decision_code,
    }


def _has_progress_evidence(value: ProgressEvidence) -> bool:
    return bool(
        value.metrics
        or value.acceptance
        or value.incidents
        or value.owner_decision_code != "none"
    )


def _root(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    resolved = Path(value).resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("feedback-loop root must be a directory")
    return resolved


def _reference(value: object, label: str, root: Path | None) -> str:
    if type(value) is not str or not value or len(value) > _MAX_REFERENCE_CHARS:
        raise ValueError(f"{label} must be a bounded non-empty string")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be ASCII") from error
    if "\\" in value or "?" in value or "#" in value or "://" in value:
        raise ValueError(f"{label} must be a canonical project reference")
    normalized = value.casefold().replace("-", "_")
    if any(part in normalized for part in _SENSITIVE_CODE_PARTS):
        raise ValueError(f"{label} contains a reserved sensitive label")
    path = Path(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"{label} escapes the project root")
    if root is not None:
        candidate = (root / path).resolve(strict=False)
        if not candidate.is_relative_to(root):
            raise ValueError(f"{label} escapes the project root")
        if not candidate.exists():
            raise ValueError(f"{label} does not exist")
    return value


def _references(value: object, label: str, root: Path | None) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise TypeError(f"{label} must be an array")
    if not value or len(value) > _MAX_REFERENCE_COUNT:
        raise ValueError(f"{label} must contain 1 to {_MAX_REFERENCE_COUNT} items")
    result = tuple(_reference(item, f"{label}[{index}]", root) for index, item in enumerate(value))
    if len(set(result)) != len(result):
        raise ValueError(f"{label} contains duplicates")
    return result


def _previous_receipt_ref(value: object, root: Path | None) -> str:
    result = _reference(value, "previous_receipt_ref", root)
    parts = Path(result).parts
    if len(parts) != 3 or parts[:2] != (".governance", "receipts") or not result.endswith(".json"):
        raise ValueError("previous_receipt_ref must name one governance receipt")
    return result


def parse_feedback_loop_sidecar(
    value: object,
    *,
    root: str | Path | None = None,
    expected_change_id: str | None = None,
) -> FeedbackLoopSidecar:
    item = _mapping(value, "feedback_loop")
    _fields(
        item,
        required=frozenset(
            {
                "extension_schema_version",
                "loop_id",
                "loop_type",
                "change_id",
                "input_evidence_refs",
                "budget",
            }
        ),
        label="feedback_loop",
    )
    if item["extension_schema_version"] != EXTENSION_SCHEMA_VERSION:
        raise ValueError("unsupported feedback-loop schema version")
    try:
        loop_type = FeedbackLoopType(item["loop_type"])
    except (TypeError, ValueError) as error:
        raise ValueError("unsupported feedback-loop type") from error
    change_id = _code(item["change_id"], "change_id")
    if expected_change_id is not None and change_id != expected_change_id:
        raise ValueError("feedback-loop change_id does not match the change request")
    budget = _mapping(item["budget"], "budget")
    _fields(
        budget,
        required=frozenset(
            {
                "max_iterations",
                "max_elapsed_seconds",
                "max_cost_units",
                "max_failures",
                "max_consecutive_no_progress",
            }
        ),
        label="budget",
    )
    resolved_root = _root(root)
    return FeedbackLoopSidecar(
        extension_schema_version=EXTENSION_SCHEMA_VERSION,
        loop_id=_code(item["loop_id"], "loop_id"),
        loop_type=loop_type,
        change_id=change_id,
        input_evidence_refs=_references(
            item["input_evidence_refs"], "input_evidence_refs", resolved_root
        ),
        budget=LoopBudget(
            max_iterations=_integer(budget["max_iterations"], "max_iterations", positive=True),
            max_elapsed_seconds=_integer(
                budget["max_elapsed_seconds"], "max_elapsed_seconds", positive=True
            ),
            max_cost_units=_integer(budget["max_cost_units"], "max_cost_units"),
            max_failures=_integer(budget["max_failures"], "max_failures"),
            max_consecutive_no_progress=_integer(
                budget["max_consecutive_no_progress"],
                "max_consecutive_no_progress",
                positive=True,
            ),
        ),
    )


def parse_loop_run(value: object, *, root: str | Path | None = None) -> LoopRun:
    item = _mapping(value, "loop_run")
    _fields(
        item,
        required=frozenset({"change_id", "loop_id", "cost_units", "input_evidence_refs"}),
        optional=frozenset({"previous_receipt_ref", "progress_evidence"}),
        label="loop_run",
    )
    resolved_root = _root(root)
    previous = item.get("previous_receipt_ref")
    return LoopRun(
        change_id=_code(item["change_id"], "change_id"),
        loop_id=_code(item["loop_id"], "loop_id"),
        cost_units=_integer(item["cost_units"], "cost_units"),
        input_evidence_refs=_references(
            item["input_evidence_refs"], "input_evidence_refs", resolved_root
        ),
        previous_receipt_ref=(
            None if previous is None else _previous_receipt_ref(previous, resolved_root)
        ),
        progress_evidence=(
            ProgressEvidence()
            if "progress_evidence" not in item
            else _parse_progress_evidence(item["progress_evidence"])
        ),
    )


def feedback_loop_sidecar_path(change_id: str) -> str:
    return f".governance/changes/{_code(change_id, 'change_id')}.feedback-loop.json"


def feedback_loop_sidecar_bytes(sidecar: FeedbackLoopSidecar) -> bytes:
    if not isinstance(sidecar, FeedbackLoopSidecar):
        raise TypeError("sidecar must be a FeedbackLoopSidecar")
    return canonical_json_bytes(sidecar)


def _read_json(path: Path) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_RECORD_BYTES:
        raise ValueError(f"invalid feedback-loop record: {path.name}")
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid feedback-loop record: {path.name}") from error
    result = _mapping(value, path.name)
    if canonical_json_bytes(result) != payload:
        raise ValueError(f"feedback-loop record is not canonical: {path.name}")
    return result


def _usage(value: object) -> LoopUsage:
    item = _mapping(value, "usage")
    fields = frozenset(
        {
            "iterations",
            "elapsed_seconds",
            "cost_units",
            "failures",
            "consecutive_no_progress",
        }
    )
    _fields(item, required=fields, label="usage")
    return LoopUsage(**{name: _integer(item[name], name) for name in fields})


def _decision(value: object, sidecar: FeedbackLoopSidecar) -> tuple[LoopUsage, str, LoopStopState]:
    item = _mapping(value, "feedback_loop_decision")
    _fields(
        item,
        required=frozenset(
            {
                "extension_schema_version",
                "loop_id",
                "loop_type",
                "change_id",
                "usage",
                "progress_fingerprint",
                "stop_state",
                "decision_code",
                "next_gate",
                "proposed_regression_delta",
            }
        ),
        label="feedback_loop_decision",
    )
    if item["extension_schema_version"] != EXTENSION_SCHEMA_VERSION:
        raise ValueError("previous receipt has an unsupported loop schema")
    if (
        item["loop_id"] != sidecar.loop_id
        or item["change_id"] != sidecar.change_id
        or item["loop_type"] != sidecar.loop_type.value
    ):
        raise ValueError("previous receipt belongs to another loop")
    fingerprint = item["progress_fingerprint"]
    if type(fingerprint) is not str or not _HEX_DIGEST.fullmatch(fingerprint):
        raise ValueError("previous receipt has an invalid progress fingerprint")
    try:
        stop_state = LoopStopState(item["stop_state"])
    except (TypeError, ValueError) as error:
        raise ValueError("previous receipt has an invalid stop state") from error
    if item["decision_code"] not in {
        "pass",
        "fail",
        "inconclusive",
        "scope_violation",
        "blocked",
    }:
        raise ValueError("previous receipt has an invalid decision code")
    expected_next = {
        LoopStopState.CONTINUE: "check",
        LoopStopState.COMPLETED: "complete",
        LoopStopState.BLOCKED: "resolve_blocker",
    }.get(stop_state, "owner_review")
    if item["next_gate"] != expected_next:
        raise ValueError("previous receipt has an inconsistent next gate")
    parse_regression_proposal(item["proposed_regression_delta"])
    return _usage(item["usage"]), fingerprint, stop_state


def _receipt_context(
    value: object,
    sidecar: FeedbackLoopSidecar,
    sidecar_digest: str,
    root: Path,
) -> str | None:
    item = _mapping(value, "feedback_loop receipt input")
    _fields(
        item,
        required=frozenset(
            {
                "extension_schema_version",
                "change_id",
                "loop_id",
                "loop_type",
                "sidecar_sha256",
                "previous_receipt_ref",
                "cost_units",
                "input_evidence_refs",
            }
        ),
        optional=frozenset({"progress_evidence"}),
        label="feedback_loop receipt input",
    )
    if item["extension_schema_version"] != EXTENSION_SCHEMA_VERSION:
        raise ValueError("previous receipt has an unsupported loop schema")
    if (
        item["change_id"] != sidecar.change_id
        or item["loop_id"] != sidecar.loop_id
        or item["loop_type"] != sidecar.loop_type.value
    ):
        raise ValueError("previous receipt belongs to another loop")
    if item["sidecar_sha256"] != sidecar_digest:
        raise ValueError("feedback-loop sidecar drifted from the receipt chain")
    _integer(item["cost_units"], "cost_units")
    _references(item["input_evidence_refs"], "input_evidence_refs", root)
    if "progress_evidence" in item:
        _parse_progress_evidence(
            item["progress_evidence"],
            label="feedback_loop receipt input.progress_evidence",
        )
    previous = item["previous_receipt_ref"]
    return None if previous is None else _previous_receipt_ref(previous, root)


def _receipt_progress_evidence(value: Mapping[str, Any]) -> ProgressEvidence:
    if "progress_evidence" not in value:
        return ProgressEvidence()
    return _parse_progress_evidence(
        value["progress_evidence"],
        label="feedback_loop receipt input.progress_evidence",
    )


def _validated_receipt_decision(
    receipt: Receipt,
    sidecar: FeedbackLoopSidecar,
) -> tuple[LoopUsage, str, LoopStopState]:
    context = receipt.inputs.get(FEEDBACK_LOOP_INPUT_KEY)
    if not isinstance(context, Mapping):
        raise ValueError("previous receipt is missing feedback-loop input")
    evidence = _receipt_progress_evidence(context)
    usage, stored_fingerprint, stop_state = _decision(
        receipt.outputs.get(FEEDBACK_LOOP_DECISION_OUTPUT_KEY), sidecar
    )
    recomputed_fingerprint = progress_fingerprint(
        sidecar.loop_type,
        receipt.checks,
        metrics=evidence.metrics,
        acceptance=evidence.acceptance,
        incidents=evidence.incidents,
        owner_decision_code=evidence.owner_decision_code,
    )
    if stored_fingerprint != recomputed_fingerprint:
        raise ValueError(
            "previous receipt progress fingerprint does not match its recorded evidence"
        )
    proposal = receipt.outputs.get(FEEDBACK_LOOP_DECISION_OUTPUT_KEY)
    decision = _mapping(proposal, "feedback_loop_decision")
    parsed_proposal = parse_regression_proposal(
        decision.get("proposed_regression_delta")
    )
    if not parsed_proposal.legacy:
        exit_code = receipt.outputs.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code < 0:
            raise ValueError("previous receipt has an invalid exit code")
        governance_refs = receipt.outputs.get("changed_paths", ())
        if isinstance(governance_refs, (str, bytes)) or not isinstance(
            governance_refs, (list, tuple)
        ):
            raise TypeError("previous receipt changed_paths must be an array")
        validate_proposal_against_receipt(
            decision["proposed_regression_delta"],
            receipt.checks,
            exit_code=exit_code,
            governance_evidence_refs=governance_refs,
            gate_contracts=receipt.outputs.get(
                REGRESSION_GATE_CONTRACTS_OUTPUT_KEY
            ),
        )
    return usage, recomputed_fingerprint, stop_state


def _loop_receipts(
    root: Path,
    sidecar: FeedbackLoopSidecar,
    sidecar_digest: str,
) -> dict[str, Receipt]:
    receipt_root = root / ".governance" / "receipts"
    if not receipt_root.is_dir():
        return {}
    result: dict[str, Receipt] = {}
    for path in sorted(receipt_root.glob("*.json")):
        if len(result) >= _MAX_CHAIN_RECEIPTS:
            raise ValueError("feedback-loop receipt chain exceeds the bounded limit")
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_RECORD_BYTES:
            raise ValueError(f"invalid feedback-loop record: {path.name}")
        try:
            receipt = load_receipt_json(path.read_bytes(), require_canonical=True)
        except OSError as error:
            raise ValueError(f"invalid feedback-loop record: {path.name}") from error
        if receipt.command != "check":
            continue
        context = receipt.inputs.get(FEEDBACK_LOOP_INPUT_KEY)
        if not isinstance(context, Mapping):
            continue
        if context.get("change_id") != sidecar.change_id or context.get("loop_id") != sidecar.loop_id:
            continue
        _receipt_context(context, sidecar, sidecar_digest, root)
        _validated_receipt_decision(receipt, sidecar)
        result[path.relative_to(root).as_posix()] = receipt
    return result


def prepare_loop_run(root: str | Path, value: object) -> PreparedLoopRun:
    project_root = Path(root).resolve(strict=True)
    run = parse_loop_run(value, root=project_root)
    sidecar_path = project_root / feedback_loop_sidecar_path(run.change_id)
    sidecar = parse_feedback_loop_sidecar(
        _read_json(sidecar_path), root=project_root, expected_change_id=run.change_id
    )
    if sidecar.loop_id != run.loop_id:
        raise ValueError("loop_run does not select the sidecar loop_id")
    sidecar_digest = digest(feedback_loop_sidecar_bytes(sidecar))
    records = _loop_receipts(project_root, sidecar, sidecar_digest)
    if not records:
        if run.previous_receipt_ref is not None:
            raise ValueError("first loop run must not reference a previous receipt")
        return PreparedLoopRun(
            sidecar,
            run,
            sidecar_digest,
            LoopUsage(0, 0, 0, 0, 0),
            "",
            None,
        )
    if run.previous_receipt_ref is None:
        raise ValueError("loop_run must reference the current receipt-chain tip")
    referenced: set[str] = set()
    roots: list[str] = []
    for ref, payload in records.items():
        context = payload.inputs[FEEDBACK_LOOP_INPUT_KEY]
        previous = _receipt_context(context, sidecar, sidecar_digest, project_root)
        if previous is None:
            roots.append(ref)
        elif previous in records:
            referenced.add(previous)
        else:
            raise ValueError("feedback-loop receipt chain is broken")
    tips = set(records) - referenced
    if len(roots) != 1 or len(tips) != 1:
        raise ValueError("feedback-loop receipt chain is branched")
    tip = next(iter(tips))
    seen: set[str] = set()
    cursor: str | None = tip
    while cursor is not None:
        if cursor in seen:
            raise ValueError("feedback-loop receipt chain is cyclic")
        seen.add(cursor)
        cursor = _receipt_context(
            records[cursor].inputs[FEEDBACK_LOOP_INPUT_KEY],
            sidecar,
            sidecar_digest,
            project_root,
        )
    if seen != set(records) or run.previous_receipt_ref != tip:
        raise ValueError("loop_run does not reference the current receipt-chain tip")
    usage, fingerprint, stop_state = _validated_receipt_decision(records[tip], sidecar)
    if stop_state is not LoopStopState.CONTINUE:
        raise ValueError("previous loop receipt is terminal and requires a new owner decision")
    return PreparedLoopRun(
        sidecar,
        run,
        sidecar_digest,
        usage,
        fingerprint,
        stop_state,
    )


def _normalized_metrics(values: Iterable[tuple[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen: set[str] = set()
    for key, value in values:
        metric_id = _code(key, "metric_id")
        if metric_id in seen:
            raise ValueError("metrics contain duplicate IDs")
        seen.add(metric_id)
        result.append(
            {"id": metric_id, "value": _normalized_number(value, "metric_value")}
        )
    return sorted(result, key=lambda item: item["id"])


def _normalized_acceptance(values: Iterable[tuple[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen: set[str] = set()
    for key, value in values:
        acceptance_id = _code(key, "acceptance_id")
        if acceptance_id in seen:
            raise ValueError("acceptance contains duplicate IDs")
        seen.add(acceptance_id)
        if type(value) is not str or value not in _ACCEPTANCE_STATUSES:
            raise ValueError("acceptance values must be closed status codes")
        result.append({"id": acceptance_id, "value": value})
    return sorted(result, key=lambda item: item["id"])


def validate_progress_gate_ids(values: Iterable[str]) -> None:
    seen: set[str] = set()
    for value in values:
        gate_id = _code(value, "gate_id")
        if gate_id in seen:
            raise ValueError("gate IDs contain duplicates")
        seen.add(gate_id)


def progress_fingerprint(
    loop_type: FeedbackLoopType,
    checks: Iterable[CheckResult],
    *,
    metrics: Iterable[tuple[str, Any]] = (),
    acceptance: Iterable[tuple[str, Any]] = (),
    incidents: Iterable[str] = (),
    owner_decision_code: str = "none",
) -> str:
    if not isinstance(loop_type, FeedbackLoopType):
        raise TypeError("loop_type must be a FeedbackLoopType")
    gates = []
    gate_ids: set[str] = set()
    for check in checks:
        if not isinstance(check, CheckResult):
            raise TypeError("checks must contain CheckResult records")
        gate_id = _code(check.gate_id, "gate_id")
        if gate_id in gate_ids:
            raise ValueError("checks contain duplicate gate IDs")
        gate_ids.add(gate_id)
        gates.append({"gate_id": gate_id, "status": check.status.value})
    incident_ids = [_code(value, "incident_id") for value in incidents]
    if len(set(incident_ids)) != len(incident_ids):
        raise ValueError("incidents contain duplicate IDs")
    if type(owner_decision_code) is not str or owner_decision_code not in _OWNER_DECISION_CODES:
        raise ValueError("owner_decision_code must be a closed decision code")
    payload = {
        "schema": PROGRESS_SCHEMA_VERSION,
        "loop_type": loop_type.value,
        "gates": sorted(gates, key=lambda item: (item["gate_id"], item["status"])),
        "metrics": _normalized_metrics(metrics),
        "acceptance": _normalized_acceptance(acceptance),
        "incidents": sorted(incident_ids),
        "owner_decision_code": owner_decision_code,
    }
    return digest(payload)


def _symptom_code(check: CheckResult) -> str:
    return symptom_code_for_check(check)


def evaluate_loop(
    prepared: PreparedLoopRun,
    checks: Iterable[CheckResult],
    *,
    exit_code: int,
    elapsed_seconds: int | float,
    gate_definitions: Iterable[GateDefinition] | None = None,
    governance_evidence_refs: Iterable[str] = (),
) -> LoopDecision:
    if not isinstance(prepared, PreparedLoopRun):
        raise TypeError("prepared must be a PreparedLoopRun")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code < 0:
        raise TypeError("exit_code must be a non-negative integer")
    if isinstance(elapsed_seconds, bool) or not isinstance(elapsed_seconds, (int, float)):
        raise TypeError("elapsed_seconds must be numeric")
    if not math.isfinite(float(elapsed_seconds)) or elapsed_seconds < 0:
        raise ValueError("elapsed_seconds must be finite and non-negative")
    check_items = tuple(checks)
    evidence = prepared.run.progress_evidence
    fingerprint = progress_fingerprint(
        prepared.sidecar.loop_type,
        check_items,
        metrics=evidence.metrics,
        acceptance=evidence.acceptance,
        incidents=evidence.incidents,
        owner_decision_code=evidence.owner_decision_code,
    )
    same_result = bool(prepared.previous_fingerprint) and fingerprint == prepared.previous_fingerprint
    previous = prepared.previous_usage
    usage = LoopUsage(
        iterations=previous.iterations + 1,
        elapsed_seconds=previous.elapsed_seconds + max(1, math.ceil(float(elapsed_seconds))),
        cost_units=previous.cost_units + prepared.run.cost_units,
        failures=previous.failures + (1 if exit_code != 0 else 0),
        consecutive_no_progress=(previous.consecutive_no_progress + 1 if same_result else 0),
    )
    budget = prepared.sidecar.budget
    if usage.consecutive_no_progress >= budget.max_consecutive_no_progress:
        stop = LoopStopState.NO_PROGRESS
    elif exit_code != 0 and usage.failures >= budget.max_failures:
        stop = LoopStopState.FAILURE_THRESHOLD
    elif (
        usage.iterations >= budget.max_iterations
        or usage.elapsed_seconds >= budget.max_elapsed_seconds
        or usage.cost_units > budget.max_cost_units
    ):
        stop = LoopStopState.BUDGET_EXHAUSTED
    elif exit_code == 0 and prepared.sidecar.loop_type is FeedbackLoopType.EXTERNAL_FEEDBACK:
        stop = LoopStopState.OWNER_DECISION_REQUIRED
    elif exit_code == 0:
        stop = LoopStopState.COMPLETED
    elif exit_code == 1:
        stop = LoopStopState.CONTINUE
    else:
        stop = LoopStopState.BLOCKED
    decision_code = {0: "pass", 1: "fail", 3: "inconclusive", 4: "scope_violation"}.get(
        exit_code, "blocked"
    )
    next_gate = {
        LoopStopState.CONTINUE: "check",
        LoopStopState.COMPLETED: "complete",
        LoopStopState.BLOCKED: "resolve_blocker",
    }.get(stop, "owner_review")
    symptoms = tuple(
        sorted(
            {
                _symptom_code(check)
                for check in check_items
                if check.status in (CheckStatus.FAIL, CheckStatus.INCONCLUSIVE)
            }
        )
    )
    if exit_code == 4:
        symptoms = tuple(sorted(set(symptoms) | {"governance.scope_violation"}))
    elif exit_code == 3 and not symptoms:
        symptoms = ("governance.inconclusive",)
    proposal: Mapping[str, Any]
    if gate_definitions is None:
        proposal = MappingProxyType(
            {
                "status": "candidate" if symptoms else "none",
                "symptom_codes": symptoms,
            }
        )
    else:
        proposal = candidate_proposal(
            gate_definitions,
            check_items,
            exit_code=exit_code,
            governance_evidence_refs=governance_evidence_refs,
        )
    return LoopDecision(
        prepared.sidecar,
        usage,
        fingerprint,
        stop,
        decision_code,
        next_gate,
        proposal,
    )


def feedback_loop_receipt_input(prepared: PreparedLoopRun) -> dict[str, Any]:
    result = {
        "extension_schema_version": EXTENSION_SCHEMA_VERSION,
        "change_id": prepared.sidecar.change_id,
        "loop_id": prepared.sidecar.loop_id,
        "loop_type": prepared.sidecar.loop_type.value,
        "sidecar_sha256": prepared.sidecar_digest,
        "previous_receipt_ref": prepared.run.previous_receipt_ref,
        "cost_units": prepared.run.cost_units,
        "input_evidence_refs": prepared.run.input_evidence_refs,
    }
    if _has_progress_evidence(prepared.run.progress_evidence):
        result["progress_evidence"] = _progress_evidence_mapping(
            prepared.run.progress_evidence
        )
    return result


def feedback_loop_receipt_output(decision: LoopDecision) -> dict[str, Any]:
    return {
        "extension_schema_version": EXTENSION_SCHEMA_VERSION,
        "change_id": decision.sidecar.change_id,
        "loop_id": decision.sidecar.loop_id,
        "loop_type": decision.sidecar.loop_type.value,
        "usage": decision.usage,
        "progress_fingerprint": decision.progress_fingerprint,
        "stop_state": decision.stop_state.value,
        "decision_code": decision.decision_code,
        "next_gate": decision.next_gate,
        "proposed_regression_delta": decision.proposed_regression_delta,
    }


__all__ = [
    "EXTENSION_SCHEMA_VERSION",
    "FEEDBACK_LOOP_DECISION_OUTPUT_KEY",
    "FEEDBACK_LOOP_INPUT_KEY",
    "FeedbackLoopSidecar",
    "FeedbackLoopType",
    "LoopBudget",
    "LoopDecision",
    "LoopRun",
    "LoopStopState",
    "LoopUsage",
    "ProgressEvidence",
    "PreparedLoopRun",
    "evaluate_loop",
    "feedback_loop_receipt_input",
    "feedback_loop_receipt_output",
    "feedback_loop_sidecar_bytes",
    "feedback_loop_sidecar_path",
    "parse_feedback_loop_sidecar",
    "parse_loop_run",
    "prepare_loop_run",
    "progress_fingerprint",
    "validate_progress_gate_ids",
]
