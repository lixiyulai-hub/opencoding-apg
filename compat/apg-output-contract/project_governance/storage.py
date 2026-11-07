from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
import tomllib
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from .model import (
    CheckResult,
    CheckStatus,
    Finding,
    GovernanceLevel,
    Policy,
    ProjectProfile,
    Receipt,
    _FrozenList,
)


PROJECT_SCHEMA_VERSION = "1.0"
_SUPPORTED_SCHEMA_MAJOR = 1
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_PROJECT_STRING_FIELDS = (
    "project_id",
    "root",
    "lifecycle",
    "data_risk",
    "user_exposure",
    "release_model",
    "test_burden",
)
_PROJECT_ARRAY_FIELDS = (
    "project_types",
    "public_surfaces",
    "operational_dependencies",
    "owners",
    "evidence_refs",
)
_PROJECT_FIELDS = (
    "project_id",
    "root",
    "project_types",
    "lifecycle",
    "public_surfaces",
    "data_risk",
    "user_exposure",
    "release_model",
    "test_burden",
    "operational_dependencies",
    "owners",
    "evidence_refs",
)
_POLICY_FIELDS = (
    "schema_version",
    "policy_version",
    "level",
    "reasons",
    "required_documents",
    "adapters",
    "gates",
    "non_baselinable_rules",
)
_RECEIPT_FIELDS = (
    "schema_version",
    "command",
    "policy_digest",
    "target_fingerprint",
    "actor",
    "timestamp_utc",
    "authorized_scope",
    "inputs",
    "outputs",
    "findings",
    "checks",
    "approvals",
    "classification",
    "evidence_refs",
)
_FINDING_FIELDS = (
    "rule_id",
    "category",
    "severity",
    "confidence",
    "path",
    "message",
    "evidence_refs",
    "baselinable",
)
_CHECK_FIELDS = (
    "gate_id",
    "phase",
    "status",
    "message",
    "evidence_refs",
    "duration_ms",
)
_RECEIPT_COMMANDS = frozenset(
    {"audit", "init", "adopt", "plan-change", "check", "doctor"}
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$"
)
_CURRENT_STATE_MAX_FILES = 128


@dataclasses.dataclass(frozen=True)
class CurrentStateFile:
    path: str
    sha256: str


@dataclasses.dataclass(frozen=True)
class CurrentStateProjection:
    schema_version: str
    source_receipt: str
    source_receipt_sha256: str
    files: tuple[CurrentStateFile, ...]


class SchemaError(ValueError):
    pass


def _json_value(value: Any, path: str = "$") -> Any:
    if isinstance(value, Enum):
        return _json_value(value.value, path)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name), f"{path}.{field.name}")
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SchemaError(f"JSON mapping key at {path} must be a string")
            result[key] = _json_value(item, f"{path}.{key}")
        return result
    if isinstance(value, (tuple, list, _FrozenList)):
        return [_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SchemaError(f"non-finite JSON number at {path}")
        return value
    raise SchemaError(f"unsupported JSON value at {path}: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            _json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except SchemaError:
        raise
    except (TypeError, ValueError, RecursionError) as error:
        raise SchemaError(f"cannot encode canonical JSON: {error}") from error
    return encoded.encode("utf-8") + b"\n"


def digest(value: Any) -> str:
    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, bytearray):
        payload = bytes(value)
    elif isinstance(value, memoryview):
        payload = value.tobytes()
    else:
        payload = canonical_json_bytes(value)
    return hashlib.sha256(payload).hexdigest()


def _toml_string(value: str) -> str:
    pieces = ['"']
    for character in value:
        if character == "\\":
            pieces.append("\\\\")
        elif character == '"':
            pieces.append('\\"')
        elif character == "\n":
            pieces.append("\\n")
        elif character == "\t":
            pieces.append("\\t")
        elif character == "\r":
            pieces.append("\\r")
        elif character == "\b":
            pieces.append("\\b")
        elif character == "\f":
            pieces.append("\\f")
        elif ord(character) < 0x20 or ord(character) == 0x7F:
            pieces.append(f"\\u{ord(character):04X}")
        else:
            pieces.append(character)
    pieces.append('"')
    return "".join(pieces)


def _toml_key(value: str) -> str:
    return value if _BARE_KEY.fullmatch(value) else _toml_string(value)


def _toml_value(value: Any, path: str) -> str:
    if isinstance(value, Enum):
        raise SchemaError(f"unsupported TOML enum at {path}: {type(value).__name__}")
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if not -(2**63) <= value <= 2**63 - 1:
            raise SchemaError(f"TOML integer at {path} is outside signed 64-bit range")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SchemaError(f"non-finite TOML number at {path}")
        return repr(value)
    if isinstance(value, list):
        raise SchemaError(
            f"mutable TOML array at {path} must use tuple for stable round trip"
        )
    if isinstance(value, tuple):
        return "[" + ", ".join(
            _toml_value(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ) + "]"
    if isinstance(value, Mapping):
        pairs: list[str] = []
        keys = list(value)
        if any(not isinstance(key, str) for key in keys):
            raise SchemaError(f"TOML mapping key at {path} must be a string")
        for key in sorted(keys):
            pairs.append(
                f"{_toml_key(key)} = {_toml_value(value[key], f'{path}.{key}')}"
            )
        return "{ " + ", ".join(pairs) + " }" if pairs else "{}"
    raise SchemaError(f"unsupported TOML value at {path}: {type(value).__name__}")


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"field {field} must be a string")
    return value


def _require_string_tuple(value: Any, field: str, *, source: str) -> tuple[str, ...]:
    expected = tuple if source == "model" else list
    if not isinstance(value, expected):
        raise SchemaError(f"field {field} must be an array of strings")
    result = tuple(value)
    if any(not isinstance(item, str) for item in result):
        raise SchemaError(f"field {field} must contain only strings")
    return result


def _validate_schema_version(value: Any) -> str:
    version = _require_string(value, "schema_version")
    major_text = version.split(".", 1)[0]
    if not major_text.isdigit():
        raise SchemaError(f"invalid schema_version: {version}")
    if int(major_text) != _SUPPORTED_SCHEMA_MAJOR:
        raise SchemaError(f"unsupported schema major: {major_text}")
    return version


def _parse_toml(text: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise SchemaError("TOML input must be text")
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise SchemaError(f"invalid TOML: {error}") from error


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SchemaError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise SchemaError(f"invalid JSON constant: {value}")


def _parse_json(value: str | bytes) -> tuple[dict[str, Any], bytes]:
    if isinstance(value, bytes):
        payload = value
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SchemaError("JSON input must be UTF-8") from error
    elif isinstance(value, str):
        text = value
        payload = value.encode("utf-8")
    else:
        raise SchemaError("JSON input must be text or bytes")
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except SchemaError:
        raise
    except (json.JSONDecodeError, UnicodeError) as error:
        raise SchemaError(f"invalid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise SchemaError("receipt JSON must be an object")
    return parsed, payload


def _validate_keys(
    data: Mapping[str, Any],
    allowed: tuple[str, ...],
    *,
    kind: str,
) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise SchemaError(f"unknown {kind} fields: {', '.join(unknown)}")
    missing = [field for field in allowed if field not in data]
    if missing:
        raise SchemaError(f"missing {kind} fields: {', '.join(missing)}")


def dump_project_toml(profile: ProjectProfile) -> str:
    if not isinstance(profile, ProjectProfile):
        raise SchemaError("project value must be a ProjectProfile")
    for field in _PROJECT_STRING_FIELDS:
        _require_string(getattr(profile, field), field)
    for field in _PROJECT_ARRAY_FIELDS:
        _require_string_tuple(getattr(profile, field), field, source="model")

    lines = [f"schema_version = {_toml_string(PROJECT_SCHEMA_VERSION)}"]
    for field in _PROJECT_FIELDS:
        lines.append(f"{field} = {_toml_value(getattr(profile, field), field)}")
    return "\n".join(lines) + "\n"


def load_project_toml(text: str) -> ProjectProfile:
    data = _parse_toml(text)
    if "schema_version" not in data:
        raise SchemaError("missing project fields: schema_version")
    _validate_schema_version(data["schema_version"])
    allowed = ("schema_version",) + _PROJECT_FIELDS
    _validate_keys(data, allowed, kind="project")

    values: dict[str, Any] = {}
    for field in _PROJECT_STRING_FIELDS:
        values[field] = _require_string(data[field], field)
    for field in _PROJECT_ARRAY_FIELDS:
        values[field] = _require_string_tuple(data[field], field, source="toml")
    return ProjectProfile(**{field: values[field] for field in _PROJECT_FIELDS})


def _normalize_gate_value(value: Any, path: str) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SchemaError(f"gate mapping key at {path} must be a string")
            result[key] = _normalize_gate_value(item, f"{path}.{key}")
        return result
    if isinstance(value, list):
        return tuple(
            _normalize_gate_value(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    if isinstance(value, (str, bool)) or value is None:
        if value is None:
            raise SchemaError(f"unsupported gate value at {path}: null")
        return value
    if isinstance(value, int):
        if not -(2**63) <= value <= 2**63 - 1:
            raise SchemaError(
                f"gate integer at {path} is outside signed 64-bit range"
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SchemaError(f"non-finite gate number at {path}")
        return value
    raise SchemaError(f"unsupported gate value at {path}: {type(value).__name__}")


def _validate_policy_model(policy: Policy) -> None:
    _validate_schema_version(policy.schema_version)
    _require_string(policy.policy_version, "policy_version")
    if not isinstance(policy.level, GovernanceLevel):
        raise SchemaError("field level must be a GovernanceLevel")
    for field in (
        "reasons",
        "required_documents",
        "adapters",
        "non_baselinable_rules",
    ):
        _require_string_tuple(getattr(policy, field), field, source="model")
    if not isinstance(policy.gates, tuple):
        raise SchemaError("field gates must be an array of mappings")
    for index, gate in enumerate(policy.gates):
        if not isinstance(gate, Mapping):
            raise SchemaError(f"field gates[{index}] must be a mapping")
        _toml_value(gate, f"gates[{index}]")


def dump_policy_toml(policy: Policy) -> str:
    if not isinstance(policy, Policy):
        raise SchemaError("policy value must be a Policy")
    _validate_policy_model(policy)
    lines = [
        f"schema_version = {_toml_string(policy.schema_version)}",
        f"policy_version = {_toml_string(policy.policy_version)}",
        f"level = {_toml_string(policy.level.value)}",
        f"reasons = {_toml_value(policy.reasons, 'reasons')}",
        "required_documents = "
        + _toml_value(policy.required_documents, "required_documents"),
        f"adapters = {_toml_value(policy.adapters, 'adapters')}",
        "non_baselinable_rules = "
        + _toml_value(policy.non_baselinable_rules, "non_baselinable_rules"),
    ]
    if not policy.gates:
        lines.append("gates = []")
    else:
        for index, gate in enumerate(policy.gates):
            lines.extend(("", "[[gates]]"))
            keys = list(gate)
            if any(not isinstance(key, str) for key in keys):
                raise SchemaError(
                    f"TOML mapping key at gates[{index}] must be a string"
                )
            for key in sorted(keys):
                lines.append(
                    f"{_toml_key(key)} = "
                    + _toml_value(gate[key], f"gates[{index}].{key}")
                )
    return "\n".join(lines) + "\n"


def load_policy_toml(text: str) -> Policy:
    data = _parse_toml(text)
    if "schema_version" not in data:
        raise SchemaError("missing policy fields: schema_version")
    schema_version = _validate_schema_version(data["schema_version"])
    _validate_keys(data, _POLICY_FIELDS, kind="policy")

    policy_version = _require_string(data["policy_version"], "policy_version")
    level_value = _require_string(data["level"], "level")
    try:
        level = GovernanceLevel(level_value)
    except ValueError as error:
        raise SchemaError(f"field level has unknown GovernanceLevel value: {level_value}") from error

    gates_value = data["gates"]
    if not isinstance(gates_value, list):
        raise SchemaError("field gates must be an array of mappings")
    gates: list[dict[str, Any]] = []
    for index, gate in enumerate(gates_value):
        if not isinstance(gate, dict):
            raise SchemaError(f"field gates[{index}] must be a mapping")
        gates.append(_normalize_gate_value(gate, f"gates[{index}]"))

    return Policy(
        schema_version=schema_version,
        policy_version=policy_version,
        level=level,
        reasons=_require_string_tuple(data["reasons"], "reasons", source="toml"),
        required_documents=_require_string_tuple(
            data["required_documents"],
            "required_documents",
            source="toml",
        ),
        adapters=_require_string_tuple(data["adapters"], "adapters", source="toml"),
        gates=tuple(gates),
        non_baselinable_rules=_require_string_tuple(
            data["non_baselinable_rules"],
            "non_baselinable_rules",
            source="toml",
        ),
    )


def _receipt_strings(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise SchemaError(f"field {field} must be an array of strings")
    result = tuple(value)
    if any(type(item) is not str for item in result):
        raise SchemaError(f"field {field} must contain only strings")
    return result


def _receipt_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError(f"field {field} must be a mapping")
    if any(type(key) is not str for key in value):
        raise SchemaError(f"field {field} keys must be strings")
    result = dict(value)
    canonical_json_bytes(result)
    return result


def _receipt_timestamp(value: Any) -> str:
    timestamp = _require_string(value, "timestamp_utc")
    if not _UTC_TIMESTAMP.fullmatch(timestamp):
        raise SchemaError("field timestamp_utc must be an ISO UTC timestamp with a Z suffix")
    try:
        parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError as error:
        raise SchemaError("field timestamp_utc is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise SchemaError("field timestamp_utc must be UTC")
    return timestamp


def _receipt_finding(value: Any, index: int) -> Finding:
    if not isinstance(value, Mapping):
        raise SchemaError(f"findings[{index}] must be a mapping")
    _validate_keys(value, _FINDING_FIELDS, kind=f"findings[{index}]")
    if type(value["baselinable"]) is not bool:
        raise SchemaError(f"findings[{index}].baselinable must be a bool")
    return Finding(
        rule_id=_require_string(value["rule_id"], f"findings[{index}].rule_id"),
        category=_require_string(value["category"], f"findings[{index}].category"),
        severity=_require_string(value["severity"], f"findings[{index}].severity"),
        confidence=_require_string(value["confidence"], f"findings[{index}].confidence"),
        path=_require_string(value["path"], f"findings[{index}].path"),
        message=_require_string(value["message"], f"findings[{index}].message"),
        evidence_refs=_receipt_strings(
            value["evidence_refs"], f"findings[{index}].evidence_refs"
        ),
        baselinable=value["baselinable"],
    )


def _receipt_check(value: Any, index: int) -> CheckResult:
    if not isinstance(value, Mapping):
        raise SchemaError(f"checks[{index}] must be a mapping")
    _validate_keys(value, _CHECK_FIELDS, kind=f"checks[{index}]")
    try:
        status = CheckStatus(value["status"])
    except (TypeError, ValueError) as error:
        raise SchemaError(f"checks[{index}].status is invalid") from error
    duration = value["duration_ms"]
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
        raise SchemaError(f"checks[{index}].duration_ms must be non-negative")
    return CheckResult(
        gate_id=_require_string(value["gate_id"], f"checks[{index}].gate_id"),
        phase=_require_string(value["phase"], f"checks[{index}].phase"),
        status=status,
        message=_require_string(value["message"], f"checks[{index}].message"),
        evidence_refs=_receipt_strings(
            value["evidence_refs"], f"checks[{index}].evidence_refs"
        ),
        duration_ms=duration,
    )


def load_receipt_mapping(value: Mapping[str, Any]) -> Receipt:
    if not isinstance(value, Mapping):
        raise SchemaError("receipt value must be a mapping")
    _validate_keys(value, _RECEIPT_FIELDS, kind="receipt")
    schema_version = _validate_schema_version(value["schema_version"])
    command = _require_string(value["command"], "command")
    if command not in _RECEIPT_COMMANDS:
        raise SchemaError(f"unknown receipt command: {command}")
    findings_value = value["findings"]
    checks_value = value["checks"]
    if not isinstance(findings_value, (list, tuple)):
        raise SchemaError("field findings must be an array")
    if not isinstance(checks_value, (list, tuple)):
        raise SchemaError("field checks must be an array")
    receipt = Receipt(
        schema_version=schema_version,
        command=command,
        policy_digest=_require_string(value["policy_digest"], "policy_digest"),
        target_fingerprint=_require_string(
            value["target_fingerprint"], "target_fingerprint"
        ),
        actor=_require_string(value["actor"], "actor"),
        timestamp_utc=_receipt_timestamp(value["timestamp_utc"]),
        authorized_scope=_receipt_strings(
            value["authorized_scope"], "authorized_scope"
        ),
        inputs=_receipt_mapping(value["inputs"], "inputs"),
        outputs=_receipt_mapping(value["outputs"], "outputs"),
        findings=tuple(
            _receipt_finding(item, index)
            for index, item in enumerate(findings_value)
        ),
        checks=tuple(
            _receipt_check(item, index) for index, item in enumerate(checks_value)
        ),
        approvals=_receipt_strings(value["approvals"], "approvals"),
        classification=_require_string(value["classification"], "classification"),
        evidence_refs=_receipt_strings(value["evidence_refs"], "evidence_refs"),
    )
    canonical_json_bytes(receipt)
    return receipt


def load_receipt_json(
    value: str | bytes, *, require_canonical: bool = False
) -> Receipt:
    mapping, payload = _parse_json(value)
    receipt = load_receipt_mapping(mapping)
    if require_canonical and payload != canonical_json_bytes(receipt):
        raise SchemaError("receipt JSON bytes are not canonical")
    return receipt


def _project_relative_path(value: Any, field: str) -> str:
    path = _require_string(value, field)
    if not path or "\\" in path:
        raise SchemaError(f"field {field} must be a normalized project-relative path")
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise SchemaError(f"field {field} must remain project-relative")
    normalized = candidate.as_posix()
    if normalized != path:
        raise SchemaError(f"field {field} must be normalized")
    return normalized


def _sha256(value: Any, field: str) -> str:
    text = _require_string(value, field)
    if not _SHA256.fullmatch(text):
        raise SchemaError(f"field {field} must be a lowercase SHA-256 digest")
    return text


def load_current_state_toml(text: str) -> CurrentStateProjection:
    data = _parse_toml(text)
    _validate_keys(
        data,
        ("schema_version", "source_receipt", "source_receipt_sha256", "files"),
        kind="current-state",
    )
    schema_version = _validate_schema_version(data["schema_version"])
    source_receipt = _project_relative_path(data["source_receipt"], "source_receipt")
    if not source_receipt.startswith(".governance/receipts/") or not source_receipt.endswith(".json"):
        raise SchemaError("field source_receipt must name a governance receipt JSON file")
    files_value = data["files"]
    if not isinstance(files_value, list) or not files_value:
        raise SchemaError("field files must be a non-empty array of tables")
    if len(files_value) > _CURRENT_STATE_MAX_FILES:
        raise SchemaError("field files exceeds the current-state file limit")
    files: list[CurrentStateFile] = []
    seen: set[str] = set()
    for index, item in enumerate(files_value):
        if not isinstance(item, Mapping):
            raise SchemaError(f"files[{index}] must be a mapping")
        _validate_keys(item, ("path", "sha256"), kind=f"files[{index}]")
        path = _project_relative_path(item["path"], f"files[{index}].path")
        if not path.startswith(".governance/") or path == ".governance/current-state.md":
            raise SchemaError(f"files[{index}].path must name governance evidence")
        if path in seen:
            raise SchemaError(f"files[{index}].path is duplicated")
        seen.add(path)
        files.append(
            CurrentStateFile(
                path=path,
                sha256=_sha256(item["sha256"], f"files[{index}].sha256"),
            )
        )
    return CurrentStateProjection(
        schema_version=schema_version,
        source_receipt=source_receipt,
        source_receipt_sha256=_sha256(
            data["source_receipt_sha256"], "source_receipt_sha256"
        ),
        files=tuple(files),
    )
