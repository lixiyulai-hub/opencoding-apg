from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from typing import TYPE_CHECKING, Any, Iterable

from .storage import canonical_json_bytes

if TYPE_CHECKING:
    from .gates import GateDefinition


GATE_EXECUTION_EVIDENCE_SCHEMA_VERSION = "1.0"
GATE_EXECUTION_SELECTION_MODE = "phase"
GATE_EXECUTION_SELECTION_MODES = frozenset({"phase", "plan"})
GATE_EXECUTION_EVIDENCE_OUTPUT_KEY = "gate_execution_evidence"
GATE_EXECUTION_EVIDENCE_MAX_ENTRIES = 256

_GATE_CONTRACT_DOMAIN = b"adaptive-project-governance/gate-definition/v1\x00"
_CAPTURE_DOMAIN = b"adaptive-project-governance/redacted-gate-capture/v1\x00"
_DIGEST_LENGTH = 64
_MAX_GATE_ID_LENGTH = 256
_GATE_ID_CREDENTIAL_PATTERNS = (
    re.compile(r"ghp_[A-Za-z0-9_-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|client[_-]?secret|secret|token|password|credential|authorization)\b\s*[=:]\s*\S+"
    ),
)
_CREDENTIAL_KEY_PARTS = frozenset(
    {"api_key", "apikey", "authorization", "credential", "password", "private_key", "secret", "token"}
)
_PHASES = {
    "fast": frozenset({"fast"}),
    "full": frozenset({"fast", "full"}),
    "release": frozenset({"fast", "full", "release"}),
}
_GATE_KINDS = frozenset(
    {
        "command",
        "scope",
        "schema",
        "adapter",
        "baseline",
        "secret",
        "evidence",
        "forbidden",
        "metric",
    }
)
_STATUSES = frozenset({"pass", "warn", "fail", "inconclusive"})
_REASON_CODES = frozenset(
    {
        "process_exited",
        "process_timed_out",
        "process_spawn_failed",
        "command_context_invalid",
        "command_missing",
        "builtin_evaluated",
    }
)
_PROCESS_CAPTURE_REASONS = frozenset({"process_exited", "process_timed_out"})
_INCONCLUSIVE_REASONS = frozenset(
    {
        "process_timed_out",
        "process_spawn_failed",
        "command_context_invalid",
        "command_missing",
    }
)
_DOCUMENT_FIELDS = frozenset(
    {
        "schema_version",
        "selection_mode",
        "phase",
        "policy_sha256",
        "performed",
        "entry_count",
        "entries",
    }
)
_ENTRY_FIELDS = frozenset(
    {
        "check_index",
        "gate_id",
        "phase",
        "kind",
        "required",
        "status",
        "reason_code",
        "process_exit_code",
        "gate_contract_sha256",
        "stdout_capture_sha256",
        "stdout_captured_bytes",
        "stdout_observed_bytes",
        "stdout_truncated",
        "stderr_capture_sha256",
        "stderr_captured_bytes",
        "stderr_observed_bytes",
        "stderr_truncated",
        "duration_ms",
    }
)


def _closed_mapping(value: object, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    if any(type(key) is not str for key in value):
        raise TypeError(f"{label} keys must be strings")
    actual = frozenset(value)
    missing = fields - actual
    unknown = actual - fields
    if missing:
        raise ValueError(f"{label} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{label} has unsupported fields: {', '.join(sorted(unknown))}")
    return value


def _nonempty_string(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise TypeError(f"{field} must be a non-empty string")
    return value


def _gate_id(value: object) -> str:
    gate_id = _nonempty_string(value, "gate_id")
    if len(gate_id) > _MAX_GATE_ID_LENGTH:
        raise ValueError("gate_id exceeds the 256-character limit")
    if any(
        unicodedata.category(character) == "Cc" or character in {"\u2028", "\u2029"}
        for character in gate_id
    ):
        raise ValueError("gate_id cannot contain control or line-separator characters")
    if any(pattern.search(gate_id) for pattern in _GATE_ID_CREDENTIAL_PATTERNS):
        raise ValueError("gate_id cannot contain credential-like material")
    return gate_id


def _credential_key(value: str) -> bool:
    normalized = value.casefold().replace("-", "_").lstrip("/_")
    parts = frozenset(part for part in normalized.split("_") if part)
    return normalized in _CREDENTIAL_KEY_PARTS or bool(parts & _CREDENTIAL_KEY_PARTS)


def _validate_contract_secret_free(value: object) -> None:
    if type(value) is str:
        if any(pattern.search(value) for pattern in _GATE_ID_CREDENTIAL_PATTERNS):
            raise ValueError("Gate contract cannot contain credential-like material")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is str and _credential_key(key):
                raise ValueError("Gate contract cannot contain credential-bearing fields")
            _validate_contract_secret_free(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _validate_contract_secret_free(item)
        return
    if is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            _validate_contract_secret_free(getattr(value, field.name))


def _capture_shape(payload: bytes) -> bytes:
    text = payload.decode("utf-8", errors="replace")
    shape: list[str] = []
    for character in text:
        if character.isspace():
            shape.append(character if character in {" ", "\t", "\r", "\n"} else " ")
        else:
            category = unicodedata.category(character)
            shape.append("L" if category.startswith("L") else "N" if category.startswith("N") else "X")
    return "".join(shape).encode("ascii")


def _enum_string(value: object, allowed: frozenset[str], field: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be a string")
    if value not in allowed:
        raise ValueError(f"unsupported {field}: {value}")
    return value


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must be non-negative")
    return value


def _process_exit_code(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("process_exit_code must be an integer or null")
    return value


def _sha256(value: object, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str:
        raise TypeError(f"{field} must be a SHA-256 string")
    if len(value) != _DIGEST_LENGTH or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field} must be 64 lowercase hexadecimal characters")
    return value


def _capture_fields(
    *,
    stream: str,
    capture_sha256: object,
    captured_bytes: object,
    observed_bytes: object,
    truncated: object,
) -> tuple[str | None, int, int, bool]:
    digest_value = _sha256(capture_sha256, f"{stream}_capture_sha256", optional=True)
    captured = _nonnegative_integer(captured_bytes, f"{stream}_captured_bytes")
    observed = _nonnegative_integer(observed_bytes, f"{stream}_observed_bytes")
    if type(truncated) is not bool:
        raise TypeError(f"{stream}_truncated must be bool")
    if digest_value is None:
        if captured != 0 or observed != 0 or truncated:
            raise ValueError(f"absent {stream} capture must have zero counts and no truncation")
        return None, 0, 0, False
    if captured > observed:
        raise ValueError(f"{stream}_captured_bytes cannot exceed observed bytes")
    if truncated != (captured < observed):
        raise ValueError(f"{stream}_truncated must exactly reflect omitted observed bytes")
    return digest_value, captured, observed, truncated


def gate_contract_sha256(gate: GateDefinition) -> str:
    """Hash the complete immutable GateDefinition without exposing its raw contract."""
    from .gates import GateDefinition as RuntimeGateDefinition

    if not isinstance(gate, RuntimeGateDefinition):
        raise TypeError("gate must be a GateDefinition")
    _gate_id(gate.gate_id)
    payload = {
        "gate_id": gate.gate_id,
        "phase": gate.phase,
        "kind": gate.kind,
        "command": gate.command,
        "timeout_seconds": gate.timeout_seconds,
        "required": gate.required,
        "warning_exit_codes": gate.warning_exit_codes,
        "options": gate.options,
    }
    for index, argument in enumerate(gate.command[:-1]):
        if _credential_key(argument):
            raise ValueError("Gate contract cannot contain credential arguments")
    _validate_contract_secret_free(payload)
    return hashlib.sha256(_GATE_CONTRACT_DOMAIN + canonical_json_bytes(payload)).hexdigest()


def capture_digest(redacted_capture: str | bytes, *, stream: str) -> str:
    """Hash the character-class shape of an already-redacted capture."""
    if stream not in {"stdout", "stderr"}:
        raise ValueError("stream must be stdout or stderr")
    if type(redacted_capture) is str:
        payload = redacted_capture.encode("utf-8")
    elif type(redacted_capture) is bytes:
        payload = redacted_capture
    else:
        raise TypeError("redacted_capture must be str or bytes")
    return hashlib.sha256(
        _CAPTURE_DOMAIN + stream.encode("ascii") + b"\x00" + _capture_shape(payload)
    ).hexdigest()


@dataclass(frozen=True)
class GateExecutionEvidence:
    check_index: int
    gate_id: str
    phase: str
    kind: str
    required: bool
    status: str
    reason_code: str
    process_exit_code: int | None
    gate_contract_sha256: str
    stdout_capture_sha256: str | None
    stdout_captured_bytes: int
    stdout_observed_bytes: int
    stdout_truncated: bool
    stderr_capture_sha256: str | None
    stderr_captured_bytes: int
    stderr_observed_bytes: int
    stderr_truncated: bool
    duration_ms: int

    def __post_init__(self) -> None:
        _nonnegative_integer(self.check_index, "check_index")
        _gate_id(self.gate_id)
        _enum_string(self.phase, frozenset(_PHASES), "phase")
        _enum_string(self.kind, _GATE_KINDS, "kind")
        if type(self.required) is not bool:
            raise TypeError("required must be bool")
        _enum_string(self.status, _STATUSES, "status")
        _enum_string(self.reason_code, _REASON_CODES, "reason_code")
        exit_code = _process_exit_code(self.process_exit_code)
        _sha256(self.gate_contract_sha256, "gate_contract_sha256")
        stdout = _capture_fields(
            stream="stdout",
            capture_sha256=self.stdout_capture_sha256,
            captured_bytes=self.stdout_captured_bytes,
            observed_bytes=self.stdout_observed_bytes,
            truncated=self.stdout_truncated,
        )
        stderr = _capture_fields(
            stream="stderr",
            capture_sha256=self.stderr_capture_sha256,
            captured_bytes=self.stderr_captured_bytes,
            observed_bytes=self.stderr_observed_bytes,
            truncated=self.stderr_truncated,
        )
        _nonnegative_integer(self.duration_ms, "duration_ms")

        has_process_capture = stdout[0] is not None and stderr[0] is not None
        if self.reason_code in _PROCESS_CAPTURE_REASONS:
            if not has_process_capture:
                raise ValueError("completed process execution requires stdout and stderr capture digests")
        elif stdout[0] is not None or stderr[0] is not None:
            raise ValueError("non-process evidence cannot contain process capture digests")
        if self.reason_code in {"process_exited", "process_timed_out"}:
            if exit_code is None:
                raise ValueError(f"{self.reason_code} requires process_exit_code")
        if self.reason_code == "process_exited":
            if self.status == "inconclusive" and self.kind != "metric":
                raise ValueError("process_exited cannot be inconclusive")
        elif self.reason_code != "process_timed_out" and exit_code is not None:
            raise ValueError("process_exit_code is only valid for a started process")
        if self.reason_code in _INCONCLUSIVE_REASONS and self.status != "inconclusive":
            raise ValueError(f"{self.reason_code} must be inconclusive")
        if self.reason_code == "builtin_evaluated" and self.status == "warn":
            raise ValueError("builtin_evaluated cannot produce warn")
        process_kinds = {"command", "metric"}
        if self.reason_code == "builtin_evaluated" and self.kind == "command":
            raise ValueError("command Gates cannot use builtin_evaluated")
        if self.reason_code != "builtin_evaluated" and self.kind not in process_kinds:
            raise ValueError("non-process Gates must use builtin_evaluated")


@dataclass(frozen=True)
class GateExecutionEvidenceDocument:
    schema_version: str
    selection_mode: str
    phase: str
    policy_sha256: str | None
    performed: bool
    entry_count: int
    entries: tuple[GateExecutionEvidence, ...]

    def __post_init__(self) -> None:
        if self.schema_version != GATE_EXECUTION_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("unsupported gate execution evidence schema_version")
        if self.selection_mode not in GATE_EXECUTION_SELECTION_MODES:
            raise ValueError("unsupported gate execution selection_mode")
        _enum_string(self.phase, frozenset(_PHASES), "phase")
        _sha256(self.policy_sha256, "policy_sha256", optional=True)
        if type(self.performed) is not bool:
            raise TypeError("performed must be bool")
        count = _nonnegative_integer(self.entry_count, "entry_count")
        if type(self.entries) not in (tuple, list):
            raise TypeError("entries must be a tuple or list")
        entries = tuple(self.entries)
        if len(entries) > GATE_EXECUTION_EVIDENCE_MAX_ENTRIES:
            raise ValueError("gate execution evidence exceeds the 256-entry limit")
        if any(not isinstance(entry, GateExecutionEvidence) for entry in entries):
            raise TypeError("entries must contain GateExecutionEvidence values")
        if count != len(entries):
            raise ValueError("entry_count does not match entries")
        if self.performed != bool(entries):
            raise ValueError("performed must exactly reflect whether Gate entries exist")
        allowed_phases = _PHASES[self.phase]
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            if entry.check_index != index:
                raise ValueError("check_index values must be contiguous and ordered from zero")
            if entry.phase not in allowed_phases:
                raise ValueError("entry phase is outside the selected phase")
            if entry.gate_id in seen:
                raise ValueError("gate execution evidence contains a duplicate Gate identity")
            seen.add(entry.gate_id)
        canonical = tuple(
            sorted(
                entries,
                key=lambda entry: (("fast", "full", "release").index(entry.phase), entry.gate_id),
            )
        )
        if entries != canonical:
            raise ValueError("gate execution evidence entries are not in canonical Gate order")
        object.__setattr__(self, "entries", entries)


def build_gate_execution_evidence(
    *,
    check_index: int,
    gate: GateDefinition,
    status: str,
    reason_code: str,
    process_exit_code: int | None = None,
    stdout_capture_sha256: str | None = None,
    stdout_captured_bytes: int = 0,
    stdout_observed_bytes: int = 0,
    stdout_truncated: bool = False,
    stderr_capture_sha256: str | None = None,
    stderr_captured_bytes: int = 0,
    stderr_observed_bytes: int = 0,
    stderr_truncated: bool = False,
    duration_ms: int = 0,
) -> GateExecutionEvidence:
    from .gates import GateDefinition as RuntimeGateDefinition

    if not isinstance(gate, RuntimeGateDefinition):
        raise TypeError("gate must be a GateDefinition")
    return GateExecutionEvidence(
        check_index=check_index,
        gate_id=gate.gate_id,
        phase=gate.phase,
        kind=gate.kind,
        required=gate.required,
        status=status,
        reason_code=reason_code,
        process_exit_code=process_exit_code,
        gate_contract_sha256=gate_contract_sha256(gate),
        stdout_capture_sha256=stdout_capture_sha256,
        stdout_captured_bytes=stdout_captured_bytes,
        stdout_observed_bytes=stdout_observed_bytes,
        stdout_truncated=stdout_truncated,
        stderr_capture_sha256=stderr_capture_sha256,
        stderr_captured_bytes=stderr_captured_bytes,
        stderr_observed_bytes=stderr_observed_bytes,
        stderr_truncated=stderr_truncated,
        duration_ms=duration_ms,
    )


def build_gate_execution_evidence_document(
    *,
    phase: str,
    policy_sha256: str | None,
    selection_mode: str = GATE_EXECUTION_SELECTION_MODE,
    performed: bool | None = None,
    entries: Iterable[GateExecutionEvidence] = (),
) -> GateExecutionEvidenceDocument:
    if isinstance(entries, (str, bytes, Mapping)):
        raise TypeError("entries must be an iterable of GateExecutionEvidence values")
    values = tuple(entries)
    return GateExecutionEvidenceDocument(
        schema_version=GATE_EXECUTION_EVIDENCE_SCHEMA_VERSION,
        selection_mode=selection_mode,
        phase=phase,
        policy_sha256=policy_sha256,
        performed=bool(values) if performed is None else performed,
        entry_count=len(values),
        entries=values,
    )


def gate_execution_entry_mapping(entry: GateExecutionEvidence) -> dict[str, Any]:
    if not isinstance(entry, GateExecutionEvidence):
        raise TypeError("entry must be GateExecutionEvidence")
    return {
        "check_index": entry.check_index,
        "gate_id": entry.gate_id,
        "phase": entry.phase,
        "kind": entry.kind,
        "required": entry.required,
        "status": entry.status,
        "reason_code": entry.reason_code,
        "process_exit_code": entry.process_exit_code,
        "gate_contract_sha256": entry.gate_contract_sha256,
        "stdout_capture_sha256": entry.stdout_capture_sha256,
        "stdout_captured_bytes": entry.stdout_captured_bytes,
        "stdout_observed_bytes": entry.stdout_observed_bytes,
        "stdout_truncated": entry.stdout_truncated,
        "stderr_capture_sha256": entry.stderr_capture_sha256,
        "stderr_captured_bytes": entry.stderr_captured_bytes,
        "stderr_observed_bytes": entry.stderr_observed_bytes,
        "stderr_truncated": entry.stderr_truncated,
        "duration_ms": entry.duration_ms,
    }


def gate_execution_evidence_mapping(document: GateExecutionEvidenceDocument) -> dict[str, Any]:
    if not isinstance(document, GateExecutionEvidenceDocument):
        raise TypeError("document must be GateExecutionEvidenceDocument")
    return {
        "schema_version": document.schema_version,
        "selection_mode": document.selection_mode,
        "phase": document.phase,
        "policy_sha256": document.policy_sha256,
        "performed": document.performed,
        "entry_count": document.entry_count,
        "entries": tuple(gate_execution_entry_mapping(entry) for entry in document.entries),
    }


def gate_execution_evidence_document(
    entries: Iterable[GateExecutionEvidence],
    *,
    phase: str,
    policy_sha256: str | None,
    selection_mode: str = GATE_EXECUTION_SELECTION_MODE,
    performed: bool | None = None,
) -> dict[str, Any]:
    """Build the exact receipt output mapping for one phase- or plan-selected run."""
    return gate_execution_evidence_mapping(
        build_gate_execution_evidence_document(
            phase=phase,
            policy_sha256=policy_sha256,
            selection_mode=selection_mode,
            performed=performed,
            entries=entries,
        )
    )


def parse_gate_execution_entry(value: object) -> GateExecutionEvidence:
    mapping = _closed_mapping(value, _ENTRY_FIELDS, "gate execution evidence entry")
    return GateExecutionEvidence(
        check_index=mapping["check_index"],
        gate_id=mapping["gate_id"],
        phase=mapping["phase"],
        kind=mapping["kind"],
        required=mapping["required"],
        status=mapping["status"],
        reason_code=mapping["reason_code"],
        process_exit_code=mapping["process_exit_code"],
        gate_contract_sha256=mapping["gate_contract_sha256"],
        stdout_capture_sha256=mapping["stdout_capture_sha256"],
        stdout_captured_bytes=mapping["stdout_captured_bytes"],
        stdout_observed_bytes=mapping["stdout_observed_bytes"],
        stdout_truncated=mapping["stdout_truncated"],
        stderr_capture_sha256=mapping["stderr_capture_sha256"],
        stderr_captured_bytes=mapping["stderr_captured_bytes"],
        stderr_observed_bytes=mapping["stderr_observed_bytes"],
        stderr_truncated=mapping["stderr_truncated"],
        duration_ms=mapping["duration_ms"],
    )


def parse_gate_execution_evidence(value: object) -> GateExecutionEvidenceDocument:
    mapping = _closed_mapping(value, _DOCUMENT_FIELDS, "gate execution evidence")
    raw_entries = mapping["entries"]
    if isinstance(raw_entries, (str, bytes, Mapping)) or not isinstance(raw_entries, Sequence):
        raise TypeError("gate execution evidence entries must be an array")
    entries = tuple(parse_gate_execution_entry(entry) for entry in raw_entries)
    return GateExecutionEvidenceDocument(
        schema_version=mapping["schema_version"],
        selection_mode=mapping["selection_mode"],
        phase=mapping["phase"],
        policy_sha256=mapping["policy_sha256"],
        performed=mapping["performed"],
        entry_count=mapping["entry_count"],
        entries=entries,
    )


__all__ = [
    "GATE_EXECUTION_EVIDENCE_SCHEMA_VERSION",
    "GATE_EXECUTION_EVIDENCE_MAX_ENTRIES",
    "GATE_EXECUTION_EVIDENCE_OUTPUT_KEY",
    "GATE_EXECUTION_SELECTION_MODE",
    "GATE_EXECUTION_SELECTION_MODES",
    "GateExecutionEvidence",
    "GateExecutionEvidenceDocument",
    "build_gate_execution_evidence",
    "build_gate_execution_evidence_document",
    "capture_digest",
    "gate_contract_sha256",
    "gate_execution_entry_mapping",
    "gate_execution_evidence_document",
    "gate_execution_evidence_mapping",
    "parse_gate_execution_entry",
    "parse_gate_execution_evidence",
]
