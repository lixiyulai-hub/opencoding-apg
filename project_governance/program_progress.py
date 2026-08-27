"""Pure, source-bound program roadmap progress projections.

The lifecycle progress model intentionally measures one bounded delivery
lifecycle.  This module adds a separate, explicit program denominator for
the work that follows it.  It never executes work, grants authority, reads
files, or derives a percentage from anything except the declared package
weights and source-digest mapping supplied by its caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any
import unicodedata

from .storage import canonical_json_bytes


PROGRAM_ROADMAP_DEFINITION_SCHEMA_VERSION = "1.0"
PROGRAM_PROGRESS_SNAPSHOT_SCHEMA_VERSION = "1.0"
MAX_PROGRAM_ROADMAP_BYTES = 96 * 1024
MAX_PROGRAM_PACKAGES = 32
MAX_PROGRAM_EVIDENCE = 16
MAX_PROGRAM_SUCCESSORS = 24
MAX_PROGRAM_WEIGHT = 1_000_000
MAX_PROGRAM_TOTAL_WEIGHT = 10_000_000
MAX_BASIS_POINTS = 10_000

_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")
_SENSITIVE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}|\bghp_[A-Za-z0-9]{8,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.|"
    r"\bbearer\s+[A-Za-z0-9._~+/-]{8,}|"
    r"\b(?:api[_-]?key|token|password|secret)\s*[:=]|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)


class ProgramProgressError(ValueError):
    """Raised when a program roadmap is malformed or non-canonical."""


class ProgramScopeStatus(str, Enum):
    COMPUTABLE = "computable"
    NOT_COMPUTABLE = "not-computable"


class ProgramPackageState(str, Enum):
    COMPLETED = "completed"
    PENDING = "pending"
    BLOCKED = "blocked"
    HISTORICAL_BLOCKED = "historical-blocked"


class ProgramReviewState(str, Enum):
    NOT_REQUIRED = "not-required"
    PENDING = "pending"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"


class ProgramAuthorityState(str, Enum):
    NOT_REQUIRED = "not-required"
    PREPARABLE = "preparable"
    AUTHORIZED = "authorized"
    REQUIRES_CONFIRMATION = "requires-confirmation"
    UNAVAILABLE = "unavailable"


class ProgramDeliveryState(str, Enum):
    NOT_COMPUTABLE = "not-computable"
    WORK_IN_PROGRESS = "work-in-progress"
    BLOCKED = "blocked"
    COMPLETE = "complete"


def _text(value: object, label: str, maximum: int = 240) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ProgramProgressError(f"{label} must be bounded non-empty text")
    if value != unicodedata.normalize("NFC", value):
        raise ProgramProgressError(f"{label} must use NFC Unicode")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ProgramProgressError(f"{label} contains control characters")
    if _SENSITIVE.search(value):
        raise ProgramProgressError(f"{label} contains a sensitive-value pattern")
    return value


def _code(value: object, label: str) -> str:
    text = _text(value, label, 128)
    if not _CODE.fullmatch(text):
        raise ProgramProgressError(f"{label} must be a bounded stable code")
    return text


def _optional_code(value: object, label: str) -> str | None:
    return None if value is None else _code(value, label)


def _digest(value: object, label: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise ProgramProgressError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _path(value: object, label: str) -> str:
    text = _text(value, label, 240)
    if (
        "\\" in text
        or text.startswith("/")
        or _WINDOWS_DRIVE.match(text)
        or ":" in text
        or "?" in text
        or "#" in text
    ):
        raise ProgramProgressError(f"{label} must be a contained relative path")
    parts = text.split("/")
    if (
        any(part in ("", ".", "..") for part in parts)
        or any(part.endswith((".", " ")) for part in parts)
        or tuple(PurePosixPath(text).parts) != tuple(parts)
    ):
        raise ProgramProgressError(f"{label} must be a contained relative path")
    return text


def _reference(value: object, label: str) -> str:
    """Validate a path-like durable reference without reading it."""

    text = _text(value, label, 240)
    if "\\" in text or text.startswith("/") or _WINDOWS_DRIVE.match(text):
        raise ProgramProgressError(f"{label} must be a contained durable reference")
    if any(part in ("", ".", "..") for part in text.split("/")):
        raise ProgramProgressError(f"{label} must be a contained durable reference")
    return text


def _optional_reference(value: object, label: str) -> str | None:
    return None if value is None else _reference(value, label)


def _tuple(value: object, label: str, maximum: int) -> tuple[object, ...]:
    if type(value) is not tuple or len(value) > maximum:
        raise ProgramProgressError(f"{label} must be a bounded immutable tuple")
    return value


def _ordered_codes(value: object, label: str, maximum: int) -> tuple[str, ...]:
    result = tuple(
        _code(item, f"{label}[{index}]")
        for index, item in enumerate(_tuple(value, label, maximum))
    )
    if len(set(result)) != len(result):
        raise ProgramProgressError(f"{label} must contain unique codes")
    return result


def _canonical_codes(value: object, label: str, maximum: int) -> tuple[str, ...]:
    result = _ordered_codes(value, label, maximum)
    if result != tuple(sorted(result)):
        raise ProgramProgressError(f"{label} must use canonical unique order")
    return result


def _basis_points(numerator: int, denominator: int) -> int:
    if type(numerator) is not int or type(denominator) is not int:
        raise ProgramProgressError("program weights must be exact integers")
    if numerator < 0 or denominator <= 0 or numerator > denominator:
        raise ProgramProgressError("program weights are outside the declared denominator")
    return numerator * MAX_BASIS_POINTS // denominator


def _enum(value: object, enum_type: type[Enum], label: str) -> Enum:
    if type(value) is not str:
        raise ProgramProgressError(f"{label} must be a string enum")
    try:
        return enum_type(value)
    except ValueError as error:
        raise ProgramProgressError(f"{label} has an unsupported value") from error


@dataclass(frozen=True)
class ProgramEvidenceBinding:
    """A declared evidence path and the exact SHA-256 that must be observed."""

    path: str
    sha256: str

    def __post_init__(self) -> None:
        if type(self) is not ProgramEvidenceBinding:
            raise ProgramProgressError("ProgramEvidenceBinding subclasses are not accepted")
        _path(self.path, "evidence.path")
        _digest(self.sha256, "evidence.sha256")


@dataclass(frozen=True)
class ProgramRoadmapPackage:
    """One source-bound program package in explicit dependency order."""

    package_id: str
    stage_id: str
    weight: int
    depends_on: tuple[str, ...]
    include_in_denominator: bool
    execution_state: ProgramPackageState
    review_state: ProgramReviewState
    authority_state: ProgramAuthorityState
    evidence: tuple[ProgramEvidenceBinding, ...]
    gate_ref: str
    rollback_ref: str
    transaction_id: str | None
    transaction_label: str | None
    successor_transaction_id: str | None

    def __post_init__(self) -> None:
        if type(self) is not ProgramRoadmapPackage:
            raise ProgramProgressError("ProgramRoadmapPackage subclasses are not accepted")
        _code(self.package_id, "package.package_id")
        _code(self.stage_id, "package.stage_id")
        if type(self.weight) is not int or not 1 <= self.weight <= MAX_PROGRAM_WEIGHT:
            raise ProgramProgressError("package.weight must be a positive integer")
        _canonical_codes(self.depends_on, "package.depends_on", MAX_PROGRAM_PACKAGES)
        if type(self.include_in_denominator) is not bool:
            raise ProgramProgressError("package.include_in_denominator must be a boolean")
        if type(self.execution_state) is not ProgramPackageState:
            raise ProgramProgressError("package.execution_state must be ProgramPackageState")
        if type(self.review_state) is not ProgramReviewState:
            raise ProgramProgressError("package.review_state must be ProgramReviewState")
        if type(self.authority_state) is not ProgramAuthorityState:
            raise ProgramProgressError("package.authority_state must be ProgramAuthorityState")
        evidence = _tuple(self.evidence, "package.evidence", MAX_PROGRAM_EVIDENCE)
        if any(type(item) is not ProgramEvidenceBinding for item in evidence):
            raise ProgramProgressError("package.evidence contains invalid records")
        evidence_paths = tuple(item.path for item in evidence)
        if evidence_paths != tuple(sorted(set(evidence_paths))):
            raise ProgramProgressError("package.evidence must use canonical unique paths")
        _reference(self.gate_ref, "package.gate_ref")
        _reference(self.rollback_ref, "package.rollback_ref")
        transaction_id = _optional_code(self.transaction_id, "package.transaction_id")
        transaction_label = (
            None
            if self.transaction_label is None
            else _text(self.transaction_label, "package.transaction_label", 160)
        )
        successor_transaction_id = _optional_code(
            self.successor_transaction_id,
            "package.successor_transaction_id",
        )

        historical = self.execution_state is ProgramPackageState.HISTORICAL_BLOCKED
        completed = self.execution_state is ProgramPackageState.COMPLETED
        if historical:
            if self.include_in_denominator:
                raise ProgramProgressError(
                    "historical-blocked packages must be excluded from the denominator"
                )
            if self.review_state is not ProgramReviewState.BLOCKED:
                raise ProgramProgressError(
                    "historical-blocked packages must retain a blocked review state"
                )
            if self.authority_state is not ProgramAuthorityState.NOT_REQUIRED:
                raise ProgramProgressError(
                    "historical-blocked packages cannot create current authority"
                )
            if not evidence:
                raise ProgramProgressError(
                    "historical-blocked packages require immutable evidence bindings"
                )
            if transaction_id is not None or transaction_label is not None:
                raise ProgramProgressError(
                    "historical-blocked packages cannot be current transactions"
                )
            if successor_transaction_id is None:
                raise ProgramProgressError(
                    "historical-blocked packages must point to a fresh successor transaction"
                )
        elif not self.include_in_denominator:
            raise ProgramProgressError(
                "only historical-blocked packages may be excluded from the denominator"
            )
        elif completed:
            if self.authority_state is not ProgramAuthorityState.NOT_REQUIRED:
                raise ProgramProgressError("completed packages cannot create current authority")
            if self.review_state is ProgramReviewState.NOT_REQUIRED:
                raise ProgramProgressError("completed packages require a review state")
            if not evidence:
                raise ProgramProgressError("completed packages require evidence bindings")
            if transaction_id is not None or transaction_label is not None:
                raise ProgramProgressError("completed packages cannot be successor transactions")
            if successor_transaction_id is not None:
                raise ProgramProgressError("completed packages cannot point to a successor")
        else:
            if self.review_state is not ProgramReviewState.NOT_REQUIRED:
                raise ProgramProgressError(
                    "pending or blocked packages must not claim a review result"
                )
            if evidence:
                raise ProgramProgressError(
                    "pending or blocked packages cannot claim completion evidence"
                )
            if self.authority_state is ProgramAuthorityState.NOT_REQUIRED:
                raise ProgramProgressError(
                    "pending or blocked packages require an explicit authority state"
                )
            if transaction_id is None or transaction_label is None:
                raise ProgramProgressError(
                    "pending or blocked packages require a readable transaction"
                )
            if successor_transaction_id is not None:
                raise ProgramProgressError(
                    "pending or blocked packages cannot point past their own transaction"
                )


@dataclass(frozen=True)
class ProgramRoadmapDefinition:
    """Canonical program denominator and ordered future transaction sequence."""

    schema_version: str
    definition_id: str
    packages: tuple[ProgramRoadmapPackage, ...]
    total_weight: int
    successor_transaction_ids: tuple[str, ...]
    later_boundaries: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self) is not ProgramRoadmapDefinition:
            raise ProgramProgressError("ProgramRoadmapDefinition subclasses are not accepted")
        if self.schema_version != PROGRAM_ROADMAP_DEFINITION_SCHEMA_VERSION:
            raise ProgramProgressError("unsupported program-roadmap schema_version")
        _code(self.definition_id, "roadmap.definition_id")
        packages = _tuple(self.packages, "roadmap.packages", MAX_PROGRAM_PACKAGES)
        if not packages or any(type(item) is not ProgramRoadmapPackage for item in packages):
            raise ProgramProgressError("roadmap.packages must contain exact package records")
        package_ids = tuple(item.package_id for item in packages)
        if len(set(package_ids)) != len(package_ids):
            raise ProgramProgressError("roadmap.packages must contain unique package IDs")
        package_by_id = {item.package_id: item for item in packages}
        position_by_id = {item.package_id: index for index, item in enumerate(packages)}
        for package in packages:
            if any(dependency not in package_by_id for dependency in package.depends_on):
                raise ProgramProgressError("package.depends_on references an unknown package")
            if any(
                position_by_id[dependency] >= position_by_id[package.package_id]
                for dependency in package.depends_on
            ):
                raise ProgramProgressError(
                    "roadmap.packages must use dependency-topological order"
                )

        denominator = tuple(item for item in packages if item.include_in_denominator)
        if not denominator:
            raise ProgramProgressError("roadmap requires a non-empty program denominator")
        if type(self.total_weight) is not int or not 1 <= self.total_weight <= MAX_PROGRAM_TOTAL_WEIGHT:
            raise ProgramProgressError("roadmap.total_weight must be a positive integer")
        if sum(item.weight for item in denominator) != self.total_weight:
            raise ProgramProgressError(
                "roadmap.total_weight must equal the declared denominator weights"
            )

        seen_stages: set[str] = set()
        active_stage: str | None = None
        for package in denominator:
            if package.stage_id != active_stage:
                if package.stage_id in seen_stages:
                    raise ProgramProgressError(
                        "denominator packages must use contiguous stage order"
                    )
                seen_stages.add(package.stage_id)
                active_stage = package.stage_id

        successor_ids = _ordered_codes(
            self.successor_transaction_ids,
            "roadmap.successor_transaction_ids",
            MAX_PROGRAM_SUCCESSORS,
        )
        transaction_by_id = {
            item.transaction_id: item
            for item in denominator
            if item.transaction_id is not None
        }
        if set(successor_ids) != set(transaction_by_id):
            raise ProgramProgressError(
                "successor_transaction_ids must cover each incomplete denominator package"
            )
        transaction_positions = {item: index for index, item in enumerate(successor_ids)}
        for package in denominator:
            if package.transaction_id is None:
                continue
            for dependency in package.depends_on:
                prerequisite = package_by_id[dependency]
                if prerequisite.transaction_id is not None and (
                    transaction_positions[prerequisite.transaction_id]
                    >= transaction_positions[package.transaction_id]
                ):
                    raise ProgramProgressError(
                        "successor_transaction_ids must preserve package dependency order"
                    )
        for package in packages:
            if package.execution_state is ProgramPackageState.HISTORICAL_BLOCKED:
                if package.successor_transaction_id not in transaction_positions:
                    raise ProgramProgressError(
                        "historical-blocked package successor must name a fresh transaction"
                    )

        _ordered_codes(self.later_boundaries, "roadmap.later_boundaries", MAX_PROGRAM_SUCCESSORS)


@dataclass(frozen=True)
class ProgramSuccessorTransaction:
    """Read-only transaction information suitable for a Status Snapshot."""

    transaction_id: str
    label: str
    stage_id: str
    authority_state: ProgramAuthorityState
    gate_ref: str
    rollback_ref: str
    depends_on: tuple[str, ...]
    package_state: ProgramPackageState

    def __post_init__(self) -> None:
        if type(self) is not ProgramSuccessorTransaction:
            raise ProgramProgressError(
                "ProgramSuccessorTransaction subclasses are not accepted"
            )
        _code(self.transaction_id, "successor.transaction_id")
        _text(self.label, "successor.label", 160)
        _code(self.stage_id, "successor.stage_id")
        if type(self.authority_state) is not ProgramAuthorityState:
            raise ProgramProgressError(
                "successor.authority_state must be ProgramAuthorityState"
            )
        _reference(self.gate_ref, "successor.gate_ref")
        _reference(self.rollback_ref, "successor.rollback_ref")
        _canonical_codes(self.depends_on, "successor.depends_on", MAX_PROGRAM_PACKAGES)
        if self.package_state not in {ProgramPackageState.PENDING, ProgramPackageState.BLOCKED}:
            raise ProgramProgressError(
                "successor.package_state must be pending or blocked"
            )


@dataclass(frozen=True)
class ProgramProgressSnapshot:
    """Recomputable program-total and current-stage facts without side effects."""

    schema_version: str
    definition_id: str | None
    definition_sha256: str | None
    scope_status: ProgramScopeStatus
    denominator_package_count: int | None
    denominator_weight: int | None
    execution_progress_basis_points: int | None
    verified_progress_basis_points: int | None
    current_stage: str
    current_stage_package_ids: tuple[str, ...]
    current_stage_total_weight: int | None
    current_stage_execution_basis_points: int | None
    current_stage_verified_basis_points: int | None
    next_stage: str | None
    ordered_successor_transactions: tuple[ProgramSuccessorTransaction, ...]
    next_transaction_id: str | None
    human_gate_transaction_id: str | None
    delivery_state: ProgramDeliveryState
    excluded_historical_blocked_package_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    execution_performed: bool = False

    def __post_init__(self) -> None:
        if type(self) is not ProgramProgressSnapshot:
            raise ProgramProgressError("ProgramProgressSnapshot subclasses are not accepted")
        if self.schema_version != PROGRAM_PROGRESS_SNAPSHOT_SCHEMA_VERSION:
            raise ProgramProgressError("unsupported program-progress snapshot schema_version")
        _optional_code(self.definition_id, "snapshot.definition_id")
        if self.definition_sha256 is not None:
            _digest(self.definition_sha256, "snapshot.definition_sha256")
        if (self.definition_id is None) != (self.definition_sha256 is None):
            raise ProgramProgressError(
                "snapshot definition ID and digest must both be present or absent"
            )
        if type(self.scope_status) is not ProgramScopeStatus:
            raise ProgramProgressError("snapshot.scope_status must be ProgramScopeStatus")
        if type(self.delivery_state) is not ProgramDeliveryState:
            raise ProgramProgressError("snapshot.delivery_state must be ProgramDeliveryState")
        _code(self.current_stage, "snapshot.current_stage")
        package_ids = _canonical_codes(
            self.current_stage_package_ids,
            "snapshot.current_stage_package_ids",
            MAX_PROGRAM_PACKAGES,
        )
        _optional_code(self.next_stage, "snapshot.next_stage")
        transactions = _tuple(
            self.ordered_successor_transactions,
            "snapshot.ordered_successor_transactions",
            MAX_PROGRAM_SUCCESSORS,
        )
        if any(type(item) is not ProgramSuccessorTransaction for item in transactions):
            raise ProgramProgressError("snapshot successor transactions are invalid")
        transaction_ids = tuple(item.transaction_id for item in transactions)
        if len(set(transaction_ids)) != len(transaction_ids):
            raise ProgramProgressError("snapshot successor transactions must be unique")
        next_transaction_id = _optional_code(
            self.next_transaction_id,
            "snapshot.next_transaction_id",
        )
        human_gate_transaction_id = _optional_code(
            self.human_gate_transaction_id,
            "snapshot.human_gate_transaction_id",
        )
        if next_transaction_id is not None and (
            not transactions or next_transaction_id != transactions[0].transaction_id
        ):
            raise ProgramProgressError(
                "snapshot.next_transaction_id must be the first successor transaction"
            )
        if human_gate_transaction_id is not None:
            if human_gate_transaction_id != next_transaction_id:
                raise ProgramProgressError(
                    "snapshot may expose at most the current real human gate"
                )
            if not transactions or (
                transactions[0].authority_state
                is not ProgramAuthorityState.REQUIRES_CONFIRMATION
            ):
                raise ProgramProgressError(
                    "snapshot human gate must match a confirmation-bound next transaction"
                )
        _canonical_codes(
            self.excluded_historical_blocked_package_ids,
            "snapshot.excluded_historical_blocked_package_ids",
            MAX_PROGRAM_PACKAGES,
        )
        _canonical_codes(self.reason_codes, "snapshot.reason_codes", MAX_PROGRAM_PACKAGES * 4)
        if type(self.execution_performed) is not bool or self.execution_performed:
            raise ProgramProgressError("program projection cannot claim executor activity")

        computable = self.scope_status is ProgramScopeStatus.COMPUTABLE
        numeric_values = (
            self.denominator_package_count,
            self.denominator_weight,
            self.execution_progress_basis_points,
            self.verified_progress_basis_points,
        )
        if computable:
            if any(type(item) is not int or item < 0 for item in numeric_values):
                raise ProgramProgressError("computable program progress requires integer totals")
            if self.denominator_package_count is None or self.denominator_package_count <= 0:
                raise ProgramProgressError("computable program progress requires package denominator")
            if self.denominator_weight is None or self.denominator_weight <= 0:
                raise ProgramProgressError("computable program progress requires weight denominator")
            if (
                self.execution_progress_basis_points is None
                or self.execution_progress_basis_points > MAX_BASIS_POINTS
                or self.verified_progress_basis_points is None
                or self.verified_progress_basis_points > MAX_BASIS_POINTS
                or self.verified_progress_basis_points > self.execution_progress_basis_points
            ):
                raise ProgramProgressError("program basis points are outside the declared scope")
        elif any(item is not None for item in numeric_values):
            raise ProgramProgressError(
                "not-computable program progress must not contain numeric totals"
            )

        stage_values = (
            self.current_stage_total_weight,
            self.current_stage_execution_basis_points,
            self.current_stage_verified_basis_points,
        )
        if all(item is None for item in stage_values):
            if package_ids:
                raise ProgramProgressError("no active stage cannot contain package IDs")
        elif any(item is None for item in stage_values):
            raise ProgramProgressError("stage totals and basis points must be supplied together")
        else:
            if (
                type(self.current_stage_total_weight) is not int
                or self.current_stage_total_weight <= 0
                or type(self.current_stage_execution_basis_points) is not int
                or not 0 <= self.current_stage_execution_basis_points <= MAX_BASIS_POINTS
                or type(self.current_stage_verified_basis_points) is not int
                or not 0 <= self.current_stage_verified_basis_points <= MAX_BASIS_POINTS
                or self.current_stage_verified_basis_points
                > self.current_stage_execution_basis_points
                or not package_ids
            ):
                raise ProgramProgressError("current program stage is invalid")
        if not computable:
            if self.delivery_state is not ProgramDeliveryState.NOT_COMPUTABLE:
                raise ProgramProgressError(
                    "not-computable program progress must not claim a delivery state"
                )
            if transactions or next_transaction_id is not None or human_gate_transaction_id is not None:
                raise ProgramProgressError(
                    "not-computable program progress cannot recommend successor transactions"
                )


def _definition_mapping(value: ProgramRoadmapDefinition) -> dict[str, Any]:
    if type(value) is not ProgramRoadmapDefinition:
        raise TypeError("value must be an exact ProgramRoadmapDefinition")
    return {
        "definition_id": value.definition_id,
        "later_boundaries": list(value.later_boundaries),
        "packages": [
            {
                "authority_state": package.authority_state.value,
                "depends_on": list(package.depends_on),
                "evidence": [
                    {"path": evidence.path, "sha256": evidence.sha256}
                    for evidence in package.evidence
                ],
                "execution_state": package.execution_state.value,
                "gate_ref": package.gate_ref,
                "include_in_denominator": package.include_in_denominator,
                "package_id": package.package_id,
                "review_state": package.review_state.value,
                "rollback_ref": package.rollback_ref,
                "stage_id": package.stage_id,
                "successor_transaction_id": package.successor_transaction_id,
                "transaction_id": package.transaction_id,
                "transaction_label": package.transaction_label,
                "weight": package.weight,
            }
            for package in value.packages
        ],
        "schema_version": value.schema_version,
        "successor_transaction_ids": list(value.successor_transaction_ids),
        "total_weight": value.total_weight,
    }


def render_program_roadmap_definition(value: ProgramRoadmapDefinition) -> bytes:
    """Render a canonical roadmap definition suitable for content-addressing."""

    return canonical_json_bytes(_definition_mapping(value))


def _reject_constant(value: str) -> None:
    raise ProgramProgressError(f"program JSON cannot contain constant {value}")


def _payload(value: str | bytes) -> tuple[dict[str, Any], bytes]:
    if type(value) is str:
        raw = value.encode("utf-8")
    elif type(value) is bytes:
        raw = value
    else:
        raise TypeError("program roadmap JSON must be str or bytes")
    if not raw or len(raw) > MAX_PROGRAM_ROADMAP_BYTES:
        raise ProgramProgressError("program roadmap JSON exceeds the bounded size")
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=lambda pairs: _unique_object(pairs),
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        if isinstance(error, ProgramProgressError):
            raise
        raise ProgramProgressError("program roadmap JSON is invalid") from error
    if type(parsed) is not dict:
        raise ProgramProgressError("program roadmap JSON root must be an object")
    return parsed, raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProgramProgressError(f"program roadmap JSON has duplicate key {key}")
        result[key] = value
    return result


def _closed(value: object, fields: frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ProgramProgressError(f"{label} must be an object")
    actual = frozenset(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise ProgramProgressError(f"{label} has an invalid closed shape: {'; '.join(details)}")
    return value


def _array(value: object, label: str, maximum: int) -> tuple[object, ...]:
    if type(value) is not list or len(value) > maximum:
        raise ProgramProgressError(f"{label} must be a bounded JSON array")
    return tuple(value)


def _parse_evidence(value: object, label: str) -> ProgramEvidenceBinding:
    item = _closed(value, frozenset({"path", "sha256"}), label)
    return ProgramEvidenceBinding(
        path=_path(item["path"], f"{label}.path"),
        sha256=_digest(item["sha256"], f"{label}.sha256"),
    )


def _parse_package(value: object, label: str) -> ProgramRoadmapPackage:
    fields = frozenset(
        {
            "authority_state",
            "depends_on",
            "evidence",
            "execution_state",
            "gate_ref",
            "include_in_denominator",
            "package_id",
            "review_state",
            "rollback_ref",
            "stage_id",
            "successor_transaction_id",
            "transaction_id",
            "transaction_label",
            "weight",
        }
    )
    item = _closed(value, fields, label)
    return ProgramRoadmapPackage(
        package_id=_code(item["package_id"], f"{label}.package_id"),
        stage_id=_code(item["stage_id"], f"{label}.stage_id"),
        weight=item["weight"],
        depends_on=_canonical_codes(
            tuple(_array(item["depends_on"], f"{label}.depends_on", MAX_PROGRAM_PACKAGES)),
            f"{label}.depends_on",
            MAX_PROGRAM_PACKAGES,
        ),
        include_in_denominator=item["include_in_denominator"],
        execution_state=_enum(
            item["execution_state"], ProgramPackageState, f"{label}.execution_state"
        ),
        review_state=_enum(item["review_state"], ProgramReviewState, f"{label}.review_state"),
        authority_state=_enum(
            item["authority_state"],
            ProgramAuthorityState,
            f"{label}.authority_state",
        ),
        evidence=tuple(
            _parse_evidence(entry, f"{label}.evidence[{index}]")
            for index, entry in enumerate(
                _array(item["evidence"], f"{label}.evidence", MAX_PROGRAM_EVIDENCE)
            )
        ),
        gate_ref=_reference(item["gate_ref"], f"{label}.gate_ref"),
        rollback_ref=_reference(item["rollback_ref"], f"{label}.rollback_ref"),
        transaction_id=_optional_code(item["transaction_id"], f"{label}.transaction_id"),
        transaction_label=(
            None
            if item["transaction_label"] is None
            else _text(item["transaction_label"], f"{label}.transaction_label", 160)
        ),
        successor_transaction_id=_optional_code(
            item["successor_transaction_id"],
            f"{label}.successor_transaction_id",
        ),
    )


def parse_program_roadmap_definition(value: str | bytes) -> ProgramRoadmapDefinition:
    """Parse canonical roadmap bytes and reject unknown fields or driftable form."""

    mapping, raw = _payload(value)
    item = _closed(
        mapping,
        frozenset(
            {
                "definition_id",
                "later_boundaries",
                "packages",
                "schema_version",
                "successor_transaction_ids",
                "total_weight",
            }
        ),
        "program_roadmap_definition",
    )
    definition = ProgramRoadmapDefinition(
        schema_version=_text(item["schema_version"], "program_roadmap_definition.schema_version", 32),
        definition_id=_code(item["definition_id"], "program_roadmap_definition.definition_id"),
        packages=tuple(
            _parse_package(entry, f"program_roadmap_definition.packages[{index}]")
            for index, entry in enumerate(
                _array(item["packages"], "program_roadmap_definition.packages", MAX_PROGRAM_PACKAGES)
            )
        ),
        total_weight=item["total_weight"],
        successor_transaction_ids=_ordered_codes(
            tuple(
                _array(
                    item["successor_transaction_ids"],
                    "program_roadmap_definition.successor_transaction_ids",
                    MAX_PROGRAM_SUCCESSORS,
                )
            ),
            "program_roadmap_definition.successor_transaction_ids",
            MAX_PROGRAM_SUCCESSORS,
        ),
        later_boundaries=_ordered_codes(
            tuple(
                _array(
                    item["later_boundaries"],
                    "program_roadmap_definition.later_boundaries",
                    MAX_PROGRAM_SUCCESSORS,
                )
            ),
            "program_roadmap_definition.later_boundaries",
            MAX_PROGRAM_SUCCESSORS,
        ),
    )
    if raw != render_program_roadmap_definition(definition):
        raise ProgramProgressError("program roadmap definition JSON is not canonical")
    return definition


def _source_mapping_reasons(value: Mapping[str, str] | None) -> tuple[Mapping[str, str] | None, tuple[str, ...]]:
    if value is None:
        return None, ("program-source-digests-missing",)
    if not isinstance(value, Mapping):
        return None, ("program-source-digests-invalid",)
    result: dict[str, str] = {}
    try:
        for path, digest in value.items():
            parsed_path = _path(path, "source_digests.path")
            parsed_digest = _digest(digest, "source_digests.digest")
            if parsed_path in result:
                return None, ("program-source-digests-invalid",)
            result[parsed_path] = parsed_digest
    except ProgramProgressError:
        return None, ("program-source-digests-invalid",)
    return result, ()


def _definition_digest(value: ProgramRoadmapDefinition) -> str:
    return hashlib.sha256(render_program_roadmap_definition(value)).hexdigest()


def _not_computable_snapshot(
    definition: ProgramRoadmapDefinition | None,
    reason_codes: tuple[str, ...],
) -> ProgramProgressSnapshot:
    return ProgramProgressSnapshot(
        schema_version=PROGRAM_PROGRESS_SNAPSHOT_SCHEMA_VERSION,
        definition_id=None if definition is None else definition.definition_id,
        definition_sha256=None if definition is None else _definition_digest(definition),
        scope_status=ProgramScopeStatus.NOT_COMPUTABLE,
        denominator_package_count=None,
        denominator_weight=None,
        execution_progress_basis_points=None,
        verified_progress_basis_points=None,
        current_stage="stage.not-computable",
        current_stage_package_ids=(),
        current_stage_total_weight=None,
        current_stage_execution_basis_points=None,
        current_stage_verified_basis_points=None,
        next_stage=None,
        ordered_successor_transactions=(),
        next_transaction_id=None,
        human_gate_transaction_id=None,
        delivery_state=ProgramDeliveryState.NOT_COMPUTABLE,
        excluded_historical_blocked_package_ids=(
            ()
            if definition is None
            else tuple(
                sorted(
                    item.package_id
                    for item in definition.packages
                    if item.execution_state is ProgramPackageState.HISTORICAL_BLOCKED
                )
            )
        ),
        reason_codes=tuple(sorted(set(reason_codes))),
        execution_performed=False,
    )


def _successor_transaction(package: ProgramRoadmapPackage) -> ProgramSuccessorTransaction:
    if package.transaction_id is None or package.transaction_label is None:
        raise ProgramProgressError("incomplete denominator package lacks transaction details")
    return ProgramSuccessorTransaction(
        transaction_id=package.transaction_id,
        label=package.transaction_label,
        stage_id=package.stage_id,
        authority_state=package.authority_state,
        gate_ref=package.gate_ref,
        rollback_ref=package.rollback_ref,
        depends_on=package.depends_on,
        package_state=package.execution_state,
    )


def recompute_program_progress(
    definition: ProgramRoadmapDefinition | None,
    source_digests: Mapping[str, str] | None,
) -> ProgramProgressSnapshot:
    """Compute program progress from a canonical roadmap and supplied SHA-256 map.

    ``source_digests`` is deliberately a caller-supplied read-only mapping of
    declared evidence path to current SHA-256.  The projection performs no
    file I/O.  Any absent, malformed, missing, or drifted binding makes every
    percentage ``not-computable`` and withholds successor recommendations.
    """

    if definition is not None and type(definition) is not ProgramRoadmapDefinition:
        raise TypeError("definition must be an exact ProgramRoadmapDefinition or None")
    if definition is None:
        return _not_computable_snapshot(None, ("program-roadmap-definition-absent",))

    sources, source_reasons = _source_mapping_reasons(source_digests)
    reasons = list(source_reasons)
    if sources is not None:
        for package in definition.packages:
            if package.execution_state not in {
                ProgramPackageState.COMPLETED,
                ProgramPackageState.HISTORICAL_BLOCKED,
            }:
                continue
            for evidence in package.evidence:
                current = sources.get(evidence.path)
                if current is None:
                    reasons.append(f"program-evidence-missing.{package.package_id}")
                elif current != evidence.sha256:
                    reasons.append(f"program-evidence-drift.{package.package_id}")
    if reasons:
        return _not_computable_snapshot(definition, tuple(reasons))

    denominator = tuple(item for item in definition.packages if item.include_in_denominator)
    completed = tuple(
        item for item in denominator if item.execution_state is ProgramPackageState.COMPLETED
    )
    execution_weight = sum(item.weight for item in completed)
    verified_weight = sum(
        item.weight
        for item in completed
        if item.review_state is ProgramReviewState.ACCEPTED
    )
    active = next(
        (item for item in denominator if item.execution_state is not ProgramPackageState.COMPLETED),
        None,
    )
    if active is None:
        current_stage = "stage.completed-program"
        # A completed roadmap is a measured terminal stage, not a missing
        # stage. Keep its denominator and 100% values visible so the user can
        # distinguish completion from a source-bound ``not-computable`` state.
        stage_packages = denominator
        stage_total_weight = definition.total_weight
        stage_execution = _basis_points(definition.total_weight, definition.total_weight)
        stage_verified = _basis_points(definition.total_weight, definition.total_weight)
        next_stage = None
        delivery_state = ProgramDeliveryState.COMPLETE
    else:
        current_stage = active.stage_id
        stage_packages = tuple(
            item for item in denominator if item.stage_id == active.stage_id
        )
        stage_total_weight = sum(item.weight for item in stage_packages)
        stage_execution_weight = sum(
            item.weight
            for item in stage_packages
            if item.execution_state is ProgramPackageState.COMPLETED
        )
        stage_verified_weight = sum(
            item.weight
            for item in stage_packages
            if item.execution_state is ProgramPackageState.COMPLETED
            and item.review_state is ProgramReviewState.ACCEPTED
        )
        stage_execution = _basis_points(stage_execution_weight, stage_total_weight)
        stage_verified = _basis_points(stage_verified_weight, stage_total_weight)
        remaining_stage_ids = tuple(
            item.stage_id
            for item in denominator
            if item.stage_id != active.stage_id
            and item.execution_state is not ProgramPackageState.COMPLETED
        )
        next_stage = next(iter(remaining_stage_ids), None)
        if active.execution_state is ProgramPackageState.BLOCKED or (
            active.authority_state is ProgramAuthorityState.UNAVAILABLE
        ):
            delivery_state = ProgramDeliveryState.BLOCKED
            reasons.append(f"program-current-package-blocked.{active.package_id}")
        else:
            delivery_state = ProgramDeliveryState.WORK_IN_PROGRESS

    package_by_transaction = {
        item.transaction_id: item
        for item in denominator
        if item.transaction_id is not None
    }
    ordered_successors = tuple(
        _successor_transaction(package_by_transaction[transaction_id])
        for transaction_id in definition.successor_transaction_ids
        if package_by_transaction[transaction_id].execution_state
        is not ProgramPackageState.COMPLETED
    )
    # A completed package has no transaction by definition.  The filter above
    # remains intentionally defensive so a future schema extension cannot
    # turn a completed record into a recommended action.
    next_transaction_id = (
        None if not ordered_successors else ordered_successors[0].transaction_id
    )
    human_gate_transaction_id = (
        next_transaction_id
        if ordered_successors
        and ordered_successors[0].authority_state
        is ProgramAuthorityState.REQUIRES_CONFIRMATION
        else None
    )
    excluded_history = tuple(
        sorted(
            item.package_id
            for item in definition.packages
            if item.execution_state is ProgramPackageState.HISTORICAL_BLOCKED
        )
    )
    if human_gate_transaction_id is not None:
        reasons.append(f"program-human-gate.{human_gate_transaction_id}")
    if delivery_state is ProgramDeliveryState.BLOCKED:
        reasons.append("program-delivery-blocked")

    return ProgramProgressSnapshot(
        schema_version=PROGRAM_PROGRESS_SNAPSHOT_SCHEMA_VERSION,
        definition_id=definition.definition_id,
        definition_sha256=_definition_digest(definition),
        scope_status=ProgramScopeStatus.COMPUTABLE,
        denominator_package_count=len(denominator),
        denominator_weight=definition.total_weight,
        execution_progress_basis_points=_basis_points(execution_weight, definition.total_weight),
        verified_progress_basis_points=_basis_points(verified_weight, definition.total_weight),
        current_stage=current_stage,
        current_stage_package_ids=tuple(sorted(item.package_id for item in stage_packages)),
        current_stage_total_weight=stage_total_weight,
        current_stage_execution_basis_points=stage_execution,
        current_stage_verified_basis_points=stage_verified,
        next_stage=next_stage,
        ordered_successor_transactions=ordered_successors,
        next_transaction_id=next_transaction_id,
        human_gate_transaction_id=human_gate_transaction_id,
        delivery_state=delivery_state,
        excluded_historical_blocked_package_ids=excluded_history,
        reason_codes=tuple(sorted(set(reasons))),
        execution_performed=False,
    )


def _format_basis_points(value: int | None) -> str:
    if value is None:
        return "not-computable"
    return f"{value // 100}.{value % 100:02d}%"


def render_program_status_lines(
    value: ProgramProgressSnapshot,
) -> tuple[str, str, str, str, str]:
    """Return the fixed Program lines for a human Status Snapshot.

    The immediate transaction belongs to the current program stage.  The
    following stage is deliberately rendered separately so a terminal report
    cannot imply that a later stage is the work to run now.
    """

    if type(value) is not ProgramProgressSnapshot:
        raise TypeError("value must be an exact ProgramProgressSnapshot")
    definition_id = value.definition_id or "absent"
    if value.scope_status is ProgramScopeStatus.COMPUTABLE:
        package_count = str(value.denominator_package_count)
        denominator_weight = str(value.denominator_weight)
        stage_packages = str(len(value.current_stage_package_ids))
        stage_weight = (
            "not-computable"
            if value.current_stage_total_weight is None
            else str(value.current_stage_total_weight)
        )
    else:
        package_count = "not-computable"
        denominator_weight = "not-computable"
        stage_packages = "not-computable"
        stage_weight = "not-computable"
    reasons = ",".join(value.reason_codes) or "none"
    history = ",".join(value.excluded_historical_blocked_package_ids) or "none"
    human_gate = value.human_gate_transaction_id or "none"
    following_stage = value.next_stage or "none"
    if value.scope_status is ProgramScopeStatus.NOT_COMPUTABLE:
        roadmap = "unavailable/not-computable"
    elif not value.ordered_successor_transactions:
        roadmap = "completed"
    else:
        roadmap = " -> ".join(
            f"{transaction.label} [{transaction.transaction_id}; "
            f"stage={transaction.stage_id}; authority={transaction.authority_state.value}]"
            for transaction in value.ordered_successor_transactions
        )
    if value.scope_status is ProgramScopeStatus.NOT_COMPUTABLE:
        immediate = "unavailable/not-computable"
        following = "unavailable/not-computable"
    elif value.ordered_successor_transactions:
        transaction = value.ordered_successor_transactions[0]
        immediate = (
            f"{transaction.transaction_id} [{transaction.label}; "
            f"stage={transaction.stage_id}; authority={transaction.authority_state.value}; "
            f"gate={transaction.gate_ref}; rollback={transaction.rollback_ref}]"
        )
        following = following_stage
    else:
        immediate = "none"
        following = "none"
    return (
        "Program progress: "
        f"scope={value.scope_status.value}; definition_id={definition_id}; "
        f"denominator_packages={package_count}; denominator_weight={denominator_weight}; "
        f"execution={_format_basis_points(value.execution_progress_basis_points)} "
        f"verified={_format_basis_points(value.verified_progress_basis_points)}; "
        f"excluded_historical_blocked={history}; reasons={reasons}",
        "Program stage (current): "
        f"{value.current_stage}; packages={stage_packages}; denominator_weight={stage_weight}; "
        f"execution={_format_basis_points(value.current_stage_execution_basis_points)} "
        f"verified={_format_basis_points(value.current_stage_verified_basis_points)}",
        "Immediate program transaction: "
        f"{immediate}; human_gate={human_gate}; "
        f"delivery_state={value.delivery_state.value}",
        f"Following program stage: {following}",
        f"Roadmap: {roadmap}",
    )


__all__ = [
    "PROGRAM_ROADMAP_DEFINITION_SCHEMA_VERSION",
    "PROGRAM_PROGRESS_SNAPSHOT_SCHEMA_VERSION",
    "MAX_PROGRAM_ROADMAP_BYTES",
    "MAX_PROGRAM_PACKAGES",
    "MAX_PROGRAM_EVIDENCE",
    "MAX_PROGRAM_SUCCESSORS",
    "MAX_BASIS_POINTS",
    "ProgramProgressError",
    "ProgramScopeStatus",
    "ProgramPackageState",
    "ProgramReviewState",
    "ProgramAuthorityState",
    "ProgramDeliveryState",
    "ProgramEvidenceBinding",
    "ProgramRoadmapPackage",
    "ProgramRoadmapDefinition",
    "ProgramSuccessorTransaction",
    "ProgramProgressSnapshot",
    "render_program_roadmap_definition",
    "parse_program_roadmap_definition",
    "recompute_program_progress",
    "render_program_status_lines",
]
