"""Dependency-free APG contracts for a separately operated LangChain project."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import math
from pathlib import Path, PurePosixPath
import re
from typing import Mapping, Sequence
import unicodedata

from .gates import GateDefinition


LANGCHAIN_CONTROL_SCHEMA_VERSION = "1.0"

_MAX_TEXT = 512
_MAX_TAGS = 16
_MAX_METADATA_ITEMS = 32
_MAX_METADATA_DEPTH = 4
_MAX_CALLBACKS = 16

_STABLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,191}\Z")
_EXACT_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:api[-_.]?key|access[-_.]?token|refresh[-_.]?token|secret|"
    r"password|credential|authorization)\s*[=:]\s*\S+|"
    r"\bbearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}|\bghp_[A-Za-z0-9]{8,}"
)
_RESERVED_METADATA = frozenset(
    {"project_id", "change_id", "gate_id", "receipt_ref"}
)


class LangChainControlError(ValueError):
    """Raised when a LangChain/APG binding violates the closed contract."""


class ModelOperation(str, Enum):
    CHAT = "chat"
    EMBEDDING = "embedding"


class NetworkDisposition(str, Enum):
    OFFLINE = "offline"


class ToolRisk(str, Enum):
    READ_ONLY = "read-only"
    REVERSIBLE_WRITE = "reversible-write"
    IRREVERSIBLE_WRITE = "irreversible-write"


class OwnerDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _text(value: object, label: str, *, maximum: int = _MAX_TEXT) -> str:
    if type(value) is not str or not value:
        raise LangChainControlError(f"{label} must be a non-empty string")
    if len(value) > maximum:
        raise LangChainControlError(f"{label} exceeds its {maximum}-character bound")
    if unicodedata.normalize("NFC", value) != value:
        raise LangChainControlError(f"{label} must use NFC Unicode")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise LangChainControlError(f"{label} contains control characters")
    if _SENSITIVE_VALUE.search(value):
        raise LangChainControlError(f"{label} contains a sensitive value")
    return value


def _stable_id(value: object, label: str) -> str:
    text = _text(value, label, maximum=128)
    if not _STABLE_ID.fullmatch(text):
        raise LangChainControlError(f"{label} must be a bounded stable ID")
    return text


def _model_id(value: object, label: str) -> str:
    text = _text(value, label, maximum=192)
    if not _MODEL_ID.fullmatch(text):
        raise LangChainControlError(f"{label} must be a bounded model ID")
    return text


def _exact_version(value: object, label: str) -> str:
    text = _text(value, label, maximum=64)
    if not _EXACT_VERSION.fullmatch(text):
        raise LangChainControlError(f"{label} must be an exact package version")
    return text


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise LangChainControlError(f"{label} must be a lowercase SHA-256 value")
    return value


def _enum(value: object, enum_type: type[Enum], label: str) -> None:
    if not isinstance(value, enum_type):
        raise LangChainControlError(f"{label} must be a {enum_type.__name__}")


def _receipt_ref(value: object, label: str) -> str:
    text = _text(value, label, maximum=240)
    if "\\" in text:
        raise LangChainControlError(f"{label} must use project-relative POSIX form")
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or len(path.parts) != 3
        or path.parts[:2] != (".governance", "receipts")
        or path.suffix != ".json"
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise LangChainControlError(f"{label} must name one governance receipt")
    return text


def _timestamp(value: object, label: str) -> datetime:
    text = _text(value, label, maximum=32)
    if not text.endswith("Z"):
        raise LangChainControlError(f"{label} must be an explicit UTC timestamp")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise LangChainControlError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise LangChainControlError(f"{label} must be UTC")
    return parsed


def _relative_path(value: object, label: str) -> str:
    text = _text(value, label, maximum=240)
    if "\\" in text:
        raise LangChainControlError(f"{label} must use POSIX separators")
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise LangChainControlError(f"{label} must remain project-relative")
    return text


def _sequence(value: object, label: str, maximum: int) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise LangChainControlError(f"{label} must be a bounded sequence")
    items = tuple(value)
    if len(items) > maximum:
        raise LangChainControlError(f"{label} exceeds its {maximum}-item bound")
    return items


@dataclass(frozen=True)
class ModelAdapterBinding:
    schema_version: str
    adapter_id: str
    operation: ModelOperation
    provider_id: str
    model_id: str
    package_name: str
    package_version: str
    fake_provider_id: str
    network_disposition: NetworkDisposition = NetworkDisposition.OFFLINE

    def __post_init__(self) -> None:
        if self.schema_version != LANGCHAIN_CONTROL_SCHEMA_VERSION:
            raise LangChainControlError("unsupported LangChain control schema")
        _stable_id(self.adapter_id, "adapter_id")
        _enum(self.operation, ModelOperation, "operation")
        _stable_id(self.provider_id, "provider_id")
        _model_id(self.model_id, "model_id")
        _stable_id(self.package_name, "package_name")
        _exact_version(self.package_version, "package_version")
        _stable_id(self.fake_provider_id, "fake_provider_id")
        _enum(self.network_disposition, NetworkDisposition, "network_disposition")
        if self.provider_id == self.fake_provider_id:
            raise LangChainControlError("fake_provider_id must be distinct from provider_id")

    @property
    def entrypoint(self) -> str:
        return (
            "init_chat_model"
            if self.operation is ModelOperation.CHAT
            else "init_embeddings"
        )


@dataclass(frozen=True)
class ToolProposal:
    schema_version: str
    proposal_id: str
    project_id: str
    change_id: str
    gate_id: str
    plan_receipt_ref: str
    tool_name: str
    tool_schema_sha256: str
    input_sha256: str
    risk: ToolRisk

    def __post_init__(self) -> None:
        if self.schema_version != LANGCHAIN_CONTROL_SCHEMA_VERSION:
            raise LangChainControlError("unsupported LangChain control schema")
        for field_name in (
            "proposal_id",
            "project_id",
            "change_id",
            "gate_id",
            "tool_name",
        ):
            _stable_id(getattr(self, field_name), field_name)
        _receipt_ref(self.plan_receipt_ref, "plan_receipt_ref")
        _sha256(self.tool_schema_sha256, "tool_schema_sha256")
        _sha256(self.input_sha256, "input_sha256")
        _enum(self.risk, ToolRisk, "risk")


@dataclass(frozen=True)
class OwnerDecisionRecord:
    decision_id: str
    proposal_id: str
    project_id: str
    change_id: str
    gate_id: str
    tool_name: str
    tool_schema_sha256: str
    input_sha256: str
    decision: OwnerDecision
    decided_at_utc: str
    expires_at_utc: str

    def __post_init__(self) -> None:
        for field_name in (
            "decision_id",
            "proposal_id",
            "project_id",
            "change_id",
            "gate_id",
            "tool_name",
        ):
            _stable_id(getattr(self, field_name), field_name)
        _sha256(self.tool_schema_sha256, "tool_schema_sha256")
        _sha256(self.input_sha256, "input_sha256")
        _enum(self.decision, OwnerDecision, "decision")
        decided = _timestamp(self.decided_at_utc, "decided_at_utc")
        expires = _timestamp(self.expires_at_utc, "expires_at_utc")
        if expires <= decided:
            raise LangChainControlError("expires_at_utc must follow decided_at_utc")


def _require_decision_match(
    proposal: ToolProposal,
    decision: OwnerDecisionRecord,
) -> None:
    pairs = (
        ("proposal_id", proposal.proposal_id, decision.proposal_id),
        ("project_id", proposal.project_id, decision.project_id),
        ("change_id", proposal.change_id, decision.change_id),
        ("gate_id", proposal.gate_id, decision.gate_id),
        ("tool_name", proposal.tool_name, decision.tool_name),
        (
            "tool_schema_sha256",
            proposal.tool_schema_sha256,
            decision.tool_schema_sha256,
        ),
        ("input_sha256", proposal.input_sha256, decision.input_sha256),
    )
    mismatches = tuple(label for label, left, right in pairs if left != right)
    if mismatches:
        raise LangChainControlError(
            "owner decision does not match proposal fields: " + ", ".join(mismatches)
        )


@dataclass(frozen=True)
class GovernedToolExecution:
    execution_id: str
    proposal: ToolProposal
    owner_decision: OwnerDecisionRecord
    started_at_utc: str
    attempt: int = 1

    def __post_init__(self) -> None:
        _stable_id(self.execution_id, "execution_id")
        if not isinstance(self.proposal, ToolProposal):
            raise LangChainControlError("proposal must be a ToolProposal")
        if not isinstance(self.owner_decision, OwnerDecisionRecord):
            raise LangChainControlError("owner_decision must be an OwnerDecisionRecord")
        _require_decision_match(self.proposal, self.owner_decision)
        if self.owner_decision.decision is not OwnerDecision.APPROVE:
            raise LangChainControlError("tool execution requires an approved decision")
        started = _timestamp(self.started_at_utc, "started_at_utc")
        decided = _timestamp(self.owner_decision.decided_at_utc, "decided_at_utc")
        expires = _timestamp(self.owner_decision.expires_at_utc, "expires_at_utc")
        if started < decided:
            raise LangChainControlError("tool execution cannot predate its decision")
        if started > expires:
            raise LangChainControlError("owner decision expired before execution")
        if type(self.attempt) is not int or self.attempt != 1:
            raise LangChainControlError("the initial contract permits exactly one attempt")


def authorize_tool_execution(
    proposal: ToolProposal,
    owner_decision: OwnerDecisionRecord,
    *,
    execution_id: str,
    started_at_utc: str,
) -> GovernedToolExecution:
    return GovernedToolExecution(
        execution_id=execution_id,
        proposal=proposal,
        owner_decision=owner_decision,
        started_at_utc=started_at_utc,
    )


@dataclass(frozen=True)
class ToolExecutionEvidence:
    execution: GovernedToolExecution
    status: ExecutionStatus
    finished_at_utc: str
    receipt_ref: str
    output_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution, GovernedToolExecution):
            raise LangChainControlError("execution must be a GovernedToolExecution")
        _enum(self.status, ExecutionStatus, "status")
        finished = _timestamp(self.finished_at_utc, "finished_at_utc")
        started = _timestamp(self.execution.started_at_utc, "started_at_utc")
        if finished < started:
            raise LangChainControlError("completion cannot predate execution")
        _receipt_ref(self.receipt_ref, "receipt_ref")
        _sha256(self.output_sha256, "output_sha256")

    @property
    def retry_count(self) -> int:
        return 0


def complete_tool_execution(
    execution: GovernedToolExecution,
    *,
    status: ExecutionStatus,
    finished_at_utc: str,
    receipt_ref: str,
    output_sha256: str,
) -> ToolExecutionEvidence:
    return ToolExecutionEvidence(
        execution=execution,
        status=status,
        finished_at_utc=finished_at_utc,
        receipt_ref=receipt_ref,
        output_sha256=output_sha256,
    )


@dataclass(frozen=True)
class RunnableCorrelation:
    project_id: str
    change_id: str
    gate_id: str
    receipt_ref: str

    def __post_init__(self) -> None:
        _stable_id(self.project_id, "project_id")
        _stable_id(self.change_id, "change_id")
        _stable_id(self.gate_id, "gate_id")
        _receipt_ref(self.receipt_ref, "receipt_ref")


def _metadata_value(value: object, label: str, *, depth: int = 0) -> object:
    if depth > _MAX_METADATA_DEPTH:
        raise LangChainControlError(f"{label} exceeds metadata depth")
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise LangChainControlError(f"{label} must be finite")
        return value
    if type(value) is str:
        return _text(value, label)
    if isinstance(value, Mapping):
        if len(value) > _MAX_METADATA_ITEMS:
            raise LangChainControlError(f"{label} exceeds metadata item bound")
        result: dict[str, object] = {}
        for key, item in value.items():
            safe_key = _stable_id(key, f"{label}.key")
            result[safe_key] = _metadata_value(
                item, f"{label}.{safe_key}", depth=depth + 1
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = _sequence(value, label, _MAX_METADATA_ITEMS)
        return [
            _metadata_value(item, f"{label}[{index}]", depth=depth + 1)
            for index, item in enumerate(items)
        ]
    raise LangChainControlError(f"{label} contains an unsupported metadata value")


def build_runnable_config(
    correlation: RunnableCorrelation,
    *,
    tags: Sequence[str] = (),
    metadata: Mapping[str, object] | None = None,
    callbacks: Sequence[object] = (),
) -> dict[str, object]:
    if not isinstance(correlation, RunnableCorrelation):
        raise LangChainControlError("correlation must be a RunnableCorrelation")

    user_tags = tuple(
        _text(item, f"tags[{index}]", maximum=128)
        for index, item in enumerate(_sequence(tags, "tags", _MAX_TAGS))
    )
    if len(set(user_tags)) != len(user_tags):
        raise LangChainControlError("tags must not contain duplicates")
    if any(tag.startswith("apg:") for tag in user_tags):
        raise LangChainControlError("caller tags must not use the reserved apg namespace")

    raw_metadata = {} if metadata is None else metadata
    if not isinstance(raw_metadata, Mapping):
        raise LangChainControlError("metadata must be a mapping")
    if any(key in _RESERVED_METADATA for key in raw_metadata):
        raise LangChainControlError("metadata contains a reserved correlation key")
    copied_metadata = _metadata_value(raw_metadata, "metadata")
    assert isinstance(copied_metadata, dict)
    copied_metadata.update(
        {
            "project_id": correlation.project_id,
            "change_id": correlation.change_id,
            "gate_id": correlation.gate_id,
            "receipt_ref": correlation.receipt_ref,
        }
    )

    callback_items = _sequence(callbacks, "callbacks", _MAX_CALLBACKS)
    correlation_tags = (
        f"apg:project:{correlation.project_id}",
        f"apg:change:{correlation.change_id}",
        f"apg:gate:{correlation.gate_id}",
    )
    return {
        "tags": list(user_tags + correlation_tags),
        "metadata": copied_metadata,
        "callbacks": list(callback_items),
    }


@dataclass(frozen=True)
class LangChainPackageGateSpec:
    gate_prefix: str
    package_path: str
    source_path: str
    tests_path: str
    external_environment: str
    external_pytest_cache: str
    test_group: str = "test"
    timeout_seconds: int = 900

    def __post_init__(self) -> None:
        _stable_id(self.gate_prefix, "gate_prefix")
        _relative_path(self.package_path, "package_path")
        _relative_path(self.source_path, "source_path")
        _relative_path(self.tests_path, "tests_path")
        _stable_id(self.test_group, "test_group")
        if type(self.external_environment) is not str or not self.external_environment:
            raise LangChainControlError("external_environment must be an absolute path")
        if type(self.external_pytest_cache) is not str or not self.external_pytest_cache:
            raise LangChainControlError("external_pytest_cache must be an absolute path")
        if (
            type(self.timeout_seconds) is not int
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise LangChainControlError("timeout_seconds must be a positive integer")


def _external_path(value: str, label: str, project_root: Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        raise LangChainControlError(f"{label} must be absolute")
    resolved = candidate.resolve(strict=False)
    if resolved == project_root or project_root in resolved.parents:
        raise LangChainControlError(f"{label} must remain outside the project root")
    return resolved


def build_langchain_package_gates(
    spec: LangChainPackageGateSpec,
    *,
    project_root: str | Path,
) -> tuple[GateDefinition, GateDefinition]:
    if not isinstance(spec, LangChainPackageGateSpec):
        raise LangChainControlError("spec must be a LangChainPackageGateSpec")
    root = Path(project_root)
    if not root.is_absolute():
        raise LangChainControlError("project_root must be absolute")
    root = root.resolve(strict=False)
    environment = _external_path(
        spec.external_environment, "external_environment", root
    )
    cache = _external_path(
        spec.external_pytest_cache, "external_pytest_cache", root
    )
    if (
        environment == cache
        or environment in cache.parents
        or cache in environment.parents
    ):
        raise LangChainControlError(
            "external environment and pytest cache must be disjoint"
        )

    compile_code = (
        "from pathlib import Path; "
        f"files=sorted(Path({spec.source_path!r}).rglob('*.py')); "
        "assert files, 'no Python sources found'; "
        "[compile(path.read_bytes(), str(path), 'exec', dont_inherit=True) "
        "for path in files]"
    )
    common_env = {"PYTHONDONTWRITEBYTECODE": "1"}
    compile_gate = GateDefinition(
        gate_id=f"{spec.gate_prefix}-compile",
        phase="fast",
        command=("python", "-B", "-X", "utf8", "-c", compile_code),
        timeout_seconds=min(spec.timeout_seconds, 180),
        options={"cwd": spec.package_path, "env": common_env},
    )
    unit_gate = GateDefinition(
        gate_id=f"{spec.gate_prefix}-unit",
        phase="full",
        command=(
            "uv",
            "run",
            "--frozen",
            "--offline",
            "--group",
            spec.test_group,
            "pytest",
            "-o",
            f"cache_dir={cache.as_posix()}",
            spec.tests_path,
            "-q",
        ),
        timeout_seconds=spec.timeout_seconds,
        options={
            "cwd": spec.package_path,
            "env": {
                **common_env,
                "UV_PROJECT_ENVIRONMENT": environment.as_posix(),
            },
        },
    )
    return compile_gate, unit_gate


__all__ = [
    "LANGCHAIN_CONTROL_SCHEMA_VERSION",
    "ExecutionStatus",
    "GovernedToolExecution",
    "LangChainControlError",
    "LangChainPackageGateSpec",
    "ModelAdapterBinding",
    "ModelOperation",
    "NetworkDisposition",
    "OwnerDecision",
    "OwnerDecisionRecord",
    "RunnableCorrelation",
    "ToolExecutionEvidence",
    "ToolProposal",
    "ToolRisk",
    "authorize_tool_execution",
    "build_langchain_package_gates",
    "build_runnable_config",
    "complete_tool_execution",
]
