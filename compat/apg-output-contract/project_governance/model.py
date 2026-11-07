from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any


class _FrozenList(Sequence[Any]):
    __slots__ = ("_items",)

    def __init__(self, values: Any) -> None:
        object.__setattr__(self, "_items", tuple(values))

    def __getitem__(self, index: Any) -> Any:
        return self._items[index]

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"_FrozenList({list(self._items)!r})"

    def __setattr__(self, name: str, value: Any) -> None:
        raise TypeError("frozen list is immutable")

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("frozen list is immutable")

    append = extend = insert = pop = remove = clear = sort = reverse = _immutable


def _freeze_mapping(value: Mapping[Any, Any], *, lists_as_tuples: bool) -> MappingProxyType:
    return MappingProxyType(
        {
            key: _freeze_value(item, lists_as_tuples=lists_as_tuples)
            for key, item in value.items()
        }
    )


def _freeze_value(value: Any, *, lists_as_tuples: bool) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value, lists_as_tuples=lists_as_tuples)
    if isinstance(value, list):
        frozen = [
            _freeze_value(item, lists_as_tuples=lists_as_tuples)
            for item in value
        ]
        return tuple(frozen) if lists_as_tuples else _FrozenList(frozen)
    if isinstance(value, tuple):
        return tuple(
            _freeze_value(item, lists_as_tuples=lists_as_tuples)
            for item in value
        )
    return value


def _freeze_sequence_field(value: Any, field_name: str) -> tuple[Any, ...] | _FrozenList:
    if isinstance(value, tuple):
        return tuple(
            _freeze_value(item, lists_as_tuples=True)
            for item in value
        )
    if isinstance(value, list):
        return _FrozenList(
            _freeze_value(item, lists_as_tuples=True)
            for item in value
        )
    raise TypeError(f"{field_name} must be a tuple or list")


def _freeze_sequence_fields(instance: Any, *field_names: str) -> None:
    for field_name in field_names:
        object.__setattr__(
            instance,
            field_name,
            _freeze_sequence_field(getattr(instance, field_name), field_name),
        )


def _freeze_mapping_field(value: Any, field_name: str) -> MappingProxyType:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    return _freeze_mapping(value, lists_as_tuples=True)


def _require_string_fields(instance: Any, *field_names: str) -> None:
    for field_name in field_names:
        if type(getattr(instance, field_name)) is not str:
            raise TypeError(f"{field_name} must be a string")


def _require_sequence_items(
    instance: Any,
    field_name: str,
    expected_type: type,
) -> None:
    if any(not isinstance(item, expected_type) for item in getattr(instance, field_name)):
        raise TypeError(
            f"{field_name} must contain only {expected_type.__name__} values"
        )


class CheckStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    NOT_APPLICABLE = "not-applicable"


class GovernanceLevel(str, Enum):
    G1 = "G1"
    G2 = "G2"
    G3 = "G3"
    G4 = "G4"


class RiskClass(str, Enum):
    ROUTINE = "routine"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Evidence:
    source: str
    kind: str
    detail: str
    confidence: str = "high"

    def __post_init__(self) -> None:
        _require_string_fields(self, "source", "kind", "detail", "confidence")


@dataclass(frozen=True)
class Finding:
    rule_id: str
    category: str
    severity: str
    confidence: str
    path: str
    message: str
    evidence_refs: tuple[str, ...]
    baselinable: bool = True

    def __post_init__(self) -> None:
        _require_string_fields(
            self,
            "rule_id",
            "category",
            "severity",
            "confidence",
            "path",
            "message",
        )
        if type(self.baselinable) is not bool:
            raise TypeError("baselinable must be a bool")
        _freeze_sequence_fields(self, "evidence_refs")
        _require_sequence_items(self, "evidence_refs", str)


@dataclass(frozen=True)
class CheckResult:
    gate_id: str
    phase: str
    status: CheckStatus
    message: str
    evidence_refs: tuple[str, ...] = ()
    duration_ms: int = 0

    def __post_init__(self) -> None:
        _require_string_fields(self, "gate_id", "phase", "message")
        if not isinstance(self.status, CheckStatus):
            raise TypeError("status must be a CheckStatus")
        if isinstance(self.duration_ms, bool) or not isinstance(self.duration_ms, int):
            raise TypeError("duration_ms must be an integer")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")
        _freeze_sequence_fields(self, "evidence_refs")
        _require_sequence_items(self, "evidence_refs", str)


@dataclass(frozen=True)
class ProjectProfile:
    project_id: str
    root: str
    project_types: tuple[str, ...]
    lifecycle: str
    public_surfaces: tuple[str, ...]
    data_risk: str
    user_exposure: str
    release_model: str
    test_burden: str
    operational_dependencies: tuple[str, ...]
    owners: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_string_fields(
            self,
            "project_id",
            "root",
            "lifecycle",
            "data_risk",
            "user_exposure",
            "release_model",
            "test_burden",
        )
        _freeze_sequence_fields(
            self,
            "project_types",
            "public_surfaces",
            "operational_dependencies",
            "owners",
            "evidence_refs",
        )
        for field_name in (
            "project_types",
            "public_surfaces",
            "operational_dependencies",
            "owners",
            "evidence_refs",
        ):
            _require_sequence_items(self, field_name, str)


@dataclass(frozen=True)
class Policy:
    schema_version: str
    policy_version: str
    level: GovernanceLevel
    reasons: tuple[str, ...]
    required_documents: tuple[str, ...]
    adapters: tuple[str, ...]
    gates: tuple[dict[str, Any], ...]
    non_baselinable_rules: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_string_fields(self, "schema_version", "policy_version")
        if not isinstance(self.level, GovernanceLevel):
            raise TypeError("level must be a GovernanceLevel")
        _freeze_sequence_fields(
            self,
            "reasons",
            "required_documents",
            "adapters",
            "non_baselinable_rules",
        )
        for field_name in (
            "reasons",
            "required_documents",
            "adapters",
            "non_baselinable_rules",
        ):
            _require_sequence_items(self, field_name, str)
        if isinstance(self.gates, tuple):
            frozen_gates = tuple(
                _freeze_value(gate, lists_as_tuples=False)
                for gate in self.gates
            )
        elif isinstance(self.gates, list):
            frozen_gates = _FrozenList(
                _freeze_value(gate, lists_as_tuples=False)
                for gate in self.gates
            )
        else:
            raise TypeError("gates must be a tuple or list")
        object.__setattr__(
            self,
            "gates",
            frozen_gates,
        )


@dataclass(frozen=True)
class ChangeRecord:
    change_id: str
    problem: str
    outcome: str
    non_goals: tuple[str, ...]
    acceptance: tuple[str, ...]
    metric: str
    changed_paths: tuple[str, ...]
    surfaces: tuple[str, ...]
    rollout: str
    telemetry: tuple[str, ...]
    rollback: str
    risk: RiskClass
    approval_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_string_fields(
            self,
            "change_id",
            "problem",
            "outcome",
            "metric",
            "rollout",
            "rollback",
        )
        if not isinstance(self.risk, RiskClass):
            raise TypeError("risk must be a RiskClass")
        _freeze_sequence_fields(
            self,
            "non_goals",
            "acceptance",
            "changed_paths",
            "surfaces",
            "telemetry",
            "approval_refs",
        )
        for field_name in (
            "non_goals",
            "acceptance",
            "changed_paths",
            "surfaces",
            "telemetry",
            "approval_refs",
        ):
            _require_sequence_items(self, field_name, str)


@dataclass(frozen=True)
class Receipt:
    schema_version: str
    command: str
    policy_digest: str
    target_fingerprint: str
    actor: str
    timestamp_utc: str
    authorized_scope: tuple[str, ...]
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    findings: tuple[Finding, ...]
    checks: tuple[CheckResult, ...]
    approvals: tuple[str, ...]
    classification: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_string_fields(
            self,
            "schema_version",
            "command",
            "policy_digest",
            "target_fingerprint",
            "actor",
            "timestamp_utc",
            "classification",
        )
        _freeze_sequence_fields(
            self,
            "authorized_scope",
            "findings",
            "checks",
            "approvals",
            "evidence_refs",
        )
        for field_name in (
            "authorized_scope",
            "approvals",
            "evidence_refs",
        ):
            _require_sequence_items(self, field_name, str)
        _require_sequence_items(self, "findings", Finding)
        _require_sequence_items(self, "checks", CheckResult)
        object.__setattr__(
            self,
            "inputs",
            _freeze_mapping_field(self.inputs, "inputs"),
        )
        object.__setattr__(
            self,
            "outputs",
            _freeze_mapping_field(self.outputs, "outputs"),
        )
