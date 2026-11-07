from __future__ import annotations

import dataclasses
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .consistency_manifest import (
    ConsistencyManifestError,
    _is_link_or_reparse,
    _open_read_handle,
    _validate_opened_path,
)
from .model import CheckResult, Finding, Receipt
from .storage import (
    canonical_json_bytes,
    digest,
    load_receipt_json,
    load_receipt_mapping,
)


FEEDBACK_LOOP_INPUT_KEY = "feedback_loop"
FEEDBACK_LOOP_DECISION_OUTPUT_KEY = "feedback_loop_decision"


_RECEIPT_LEDGER_MAX_RECORDS = 10_000
_RECEIPT_LEDGER_MAX_FILE_BYTES = 1_048_576
_RECEIPT_LEDGER_MAX_INPUT_BYTES = 64 * 1_048_576
_RECEIPT_LEDGER_IDENTITY_FIELDS = (
    "st_dev",
    "st_ino",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)


@dataclasses.dataclass(frozen=True)
class ReceiptLedgerInventory:
    invalid_filenames: tuple[str, ...]
    canonical_records: tuple[tuple[str, Path, Receipt], ...]
    summary: Mapping[str, object]
    fingerprint: str
    fingerprint_input_bytes: bytes
    input_bytes: int


class ReceiptLedgerError(ValueError):
    """Raised when a Gate operation requires a canonical receipt ledger."""

    def __init__(self, message: str, inventory: ReceiptLedgerInventory):
        super().__init__(message)
        self.inventory = inventory


def _receipt_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _ledger_names(receipts: Path) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(receipts) as entries:
        for entry in entries:
            if entry.name.endswith(".json"):
                names.append(entry.name)
                if len(names) > _RECEIPT_LEDGER_MAX_RECORDS:
                    break
    return tuple(sorted(names))


def _identity_changed(before: os.stat_result, after: os.stat_result) -> bool:
    return any(
        getattr(before, field) != getattr(after, field)
        for field in _RECEIPT_LEDGER_IDENTITY_FIELDS
    )


def _invalid_ledger_inventory(filename: str) -> ReceiptLedgerInventory:
    fingerprint_input = canonical_json_bytes(
        {
            "schema_version": "1.0",
            "entries": ({"filename": filename, "status": "invalid-directory"},),
        }
    )
    return ReceiptLedgerInventory(
        invalid_filenames=(filename,),
        canonical_records=(),
        summary={
            "receipt_total": 1,
            "receipt_canonical": 0,
            "receipt_invalid": 1,
            "oldest_receipt_ref": "",
            "latest_receipt_ref": "",
        },
        fingerprint=digest(fingerprint_input),
        fingerprint_input_bytes=fingerprint_input,
        input_bytes=0,
    )


def inspect_receipt_ledger(root: str | Path) -> ReceiptLedgerInventory:
    """Inspect canonical receipt history without changing project files."""
    project_root = Path(root).resolve(strict=True)
    if not project_root.is_dir():
        raise ReceiptLedgerError(
            "receipt ledger project root must be a directory",
            _invalid_ledger_inventory("receipts"),
        )
    receipts = project_root / ".governance" / "receipts"
    if not os.path.lexists(receipts):
        fingerprint_input = canonical_json_bytes(
            {"schema_version": "1.0", "entries": ()}
        )
        return ReceiptLedgerInventory(
            invalid_filenames=(),
            canonical_records=(),
            summary={
                "receipt_total": 0,
                "receipt_canonical": 0,
                "receipt_invalid": 0,
                "oldest_receipt_ref": "",
                "latest_receipt_ref": "",
            },
            fingerprint=digest(fingerprint_input),
            fingerprint_input_bytes=fingerprint_input,
            input_bytes=0,
        )

    try:
        governance = receipts.parent
        if _is_link_or_reparse(governance) or _is_link_or_reparse(receipts):
            raise OSError("receipt directory contains a link or reparse point")
        resolved_receipts = receipts.resolve(strict=True)
        if (
            not resolved_receipts.is_relative_to(project_root)
            or os.path.normcase(str(resolved_receipts))
            != os.path.normcase(str(receipts))
            or not receipts.is_dir()
        ):
            raise OSError("receipt directory is not contained")
        directory_before = receipts.stat(follow_symlinks=False)
        if not stat.S_ISDIR(directory_before.st_mode):
            raise OSError("receipt ledger is not a directory")
        names_before = _ledger_names(receipts)
    except (ConsistencyManifestError, OSError, ValueError):
        return _invalid_ledger_inventory(receipts.name)

    invalid: set[str] = set()
    canonical: list[tuple[str, Path, Receipt]] = []
    fingerprint_entries: list[dict[str, object]] = []
    input_bytes = 0
    over_record_limit = len(names_before) > _RECEIPT_LEDGER_MAX_RECORDS
    if over_record_limit:
        invalid.add(receipts.name)
        names_to_read = names_before[:_RECEIPT_LEDGER_MAX_RECORDS]
    else:
        names_to_read = names_before

    for filename in names_to_read:
        path = receipts / filename
        reference = path.relative_to(project_root).as_posix()
        payload: bytes | None = None
        status = "invalid"
        try:
            if _is_link_or_reparse(path):
                raise OSError("receipt file is a link or reparse point")
            with _open_read_handle(path, "receipt ledger record") as handle:
                _validate_opened_path(
                    handle,
                    project_root=project_root,
                    expected=path,
                    expected_relative=reference,
                    label="receipt ledger record",
                    allow_manifest=True,
                )
                before = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(before.st_mode)
                    or getattr(before, "st_nlink", 1) != 1
                    or before.st_size < 0
                    or before.st_size > _RECEIPT_LEDGER_MAX_FILE_BYTES
                    or input_bytes + before.st_size
                    > _RECEIPT_LEDGER_MAX_INPUT_BYTES
                ):
                    raise OSError("receipt file violates ledger bounds")
                payload = handle.read(_RECEIPT_LEDGER_MAX_FILE_BYTES + 1)
                after = os.fstat(handle.fileno())
                if (
                    len(payload) > _RECEIPT_LEDGER_MAX_FILE_BYTES
                    or len(payload) != before.st_size
                    or _identity_changed(before, after)
                ):
                    raise OSError("receipt file changed while being read")
                _validate_opened_path(
                    handle,
                    project_root=project_root,
                    expected=path,
                    expected_relative=reference,
                    label="receipt ledger record",
                    allow_manifest=True,
                )
            receipt = load_receipt_json(payload, require_canonical=True)
            input_bytes += len(payload)
            canonical.append((reference, path, receipt))
            status = "canonical"
        except (ConsistencyManifestError, OSError, TypeError, ValueError):
            invalid.add(filename)
            if payload is not None:
                input_bytes += len(payload)
        fingerprint_entries.append(
            {
                "filename": filename,
                "sha256": digest(payload) if payload is not None else "",
                "size": len(payload) if payload is not None else 0,
                "status": status,
            }
        )

    try:
        names_after = _ledger_names(receipts)
        directory_after = receipts.stat(follow_symlinks=False)
        if _identity_changed(directory_before, directory_after):
            invalid.add(receipts.name)
        if names_after != names_before:
            invalid.update(set(names_before) ^ set(names_after))
            if len(names_after) > _RECEIPT_LEDGER_MAX_RECORDS:
                invalid.add(receipts.name)
    except OSError:
        names_after = ()
        invalid.add(receipts.name)

    ordered = tuple(
        sorted(
            canonical,
            key=lambda item: (_receipt_timestamp(item[2].timestamp_utc), item[0]),
        )
    )
    invalid_names = tuple(sorted(invalid))
    total = max(len(names_before), len(names_after))
    fingerprint_input = canonical_json_bytes(
        {
            "schema_version": "1.0",
            "entries": tuple(fingerprint_entries),
            "enumeration_before": names_before,
            "enumeration_after": names_after,
        }
    )
    return ReceiptLedgerInventory(
        invalid_filenames=invalid_names,
        canonical_records=ordered,
        summary={
            "receipt_total": total,
            "receipt_canonical": len(ordered),
            "receipt_invalid": len(invalid_names),
            "oldest_receipt_ref": ordered[0][0] if ordered else "",
            "latest_receipt_ref": ordered[-1][0] if ordered else "",
        },
        fingerprint=digest(fingerprint_input),
        fingerprint_input_bytes=fingerprint_input,
        input_bytes=input_bytes,
    )


def require_canonical_receipt_ledger(root: str | Path) -> ReceiptLedgerInventory:
    inventory = inspect_receipt_ledger(root)
    if inventory.invalid_filenames:
        names = ", ".join(inventory.invalid_filenames)
        raise ReceiptLedgerError(f"receipt ledger is invalid: {names}", inventory)
    return inventory


_SECRET_KEY_PARTS = (
    "token",
    "password",
    "secret",
    "credential",
    "cookie",
    "authorization",
    "private_key",
)
_ENVIRONMENT_KEYS = frozenset({"env", "environment"})
_OPAQUE_PAYLOAD_KEYS = frozenset(
    {
        "args",
        "arguments",
        "argv",
        "command",
        "command_line",
        "digest",
        "error",
        "exception",
        "log",
        "logs",
        "path",
        "stderr",
        "stdout",
        "traceback",
    }
)
_CAPTURE_PAYLOAD_KEYS = frozenset({"log", "logs", "stderr", "stdout"})
_MAX_CAPTURE_CHARS = 4096
_TRUNCATION_MARKER = "...[TRUNCATED]"
_REDACTED = "[REDACTED]"
_URL_USERINFO = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@")
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+\S+")

_SECRET_SCALAR_PATTERNS = (
    re.compile(r"ghp_[A-Za-z0-9_-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@"),
    re.compile(r"(?i)\b(?:api[_-]?key|client[_-]?secret|secret|token|password|credential|authorization)\b\s*[=:]\s*[^\s]+"),
)


def _normalized_key(value: object) -> str:
    return str(value).casefold().replace("-", "_")


def _is_secret_key(value: object) -> bool:
    normalized = _normalized_key(value)
    return any(part in normalized for part in _SECRET_KEY_PARTS)


def _bounded_text(value: str) -> str:
    if len(value) <= _MAX_CAPTURE_CHARS:
        return value
    keep = _MAX_CAPTURE_CHARS - len(_TRUNCATION_MARKER)
    return value[:keep] + _TRUNCATION_MARKER


def _safe_scalar(value: object, *, allow_secret_label: bool = False) -> str:
    text = _bounded_text(str(value))
    normalized = _normalized_key(text)
    if any(pattern.search(text) for pattern in _SECRET_SCALAR_PATTERNS):
        return _REDACTED
    if not allow_secret_label and any(part in normalized for part in _SECRET_KEY_PARTS):
        return _REDACTED
    return text

def _environment_summary(value: object) -> object:
    if not isinstance(value, Mapping):
        return _REDACTED
    return {"keys": tuple(sorted(_safe_scalar(key) for key in value))}


def _opaque_summary(value: object, key: str) -> str:
    if key not in _CAPTURE_PAYLOAD_KEYS:
        return _REDACTED
    try:
        length = len(value)  # type: ignore[arg-type]
    except TypeError:
        length = len(str(value))
    return f"[REDACTED {key.upper()} length={length} TRUNCATED]"


def _safe_mapping(
    value: Mapping[Any, Any], *, allow_secret_labels: bool = False
) -> dict[str, Any]:
    if any(not isinstance(key, str) for key in value):
        raise TypeError("receipt mapping keys must be strings")
    result: dict[str, Any] = {}
    redacted_index = 0
    items = sorted(
        value.items(),
        key=lambda item: (item[0].casefold(), item[0]),
    )
    for original_key, item in items:
        safe_key = _safe_scalar(original_key)
        if safe_key == _REDACTED:
            redacted_index += 1
            safe_key = f"[REDACTED_KEY_{redacted_index}]"
        candidate = safe_key
        suffix = 1
        while candidate in result:
            suffix += 1
            candidate = f"{safe_key}_{suffix}"
        result[candidate] = redact(
            item,
            _key=original_key,
            _allow_secret_labels=allow_secret_labels,
        )
    return result


def redact(
    value: Any,
    *,
    _key: object | None = None,
    _allow_secret_labels: bool = False,
) -> Any:
    if _key is not None:
        if _is_secret_key(_key):
            return _REDACTED
        if _normalized_key(_key) in _ENVIRONMENT_KEYS:
            return _environment_summary(value)
        normalized_key = _normalized_key(_key)
        if normalized_key in _OPAQUE_PAYLOAD_KEYS:
            return _opaque_summary(value, normalized_key)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: redact(
                getattr(value, field.name),
                _key=field.name,
                _allow_secret_labels=_allow_secret_labels,
            )
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return _safe_mapping(
            value, allow_secret_labels=_allow_secret_labels
        )
    if isinstance(value, tuple):
        return tuple(
            redact(item, _allow_secret_labels=_allow_secret_labels)
            for item in value
        )
    if isinstance(value, list):
        return [
            redact(item, _allow_secret_labels=_allow_secret_labels)
            for item in value
        ]
    if isinstance(value, (set, frozenset)):
        return f"[REDACTED SET count={len(value)}]"
    if _allow_secret_labels and isinstance(value, Enum):
        safe_value = _safe_scalar(value.value, allow_secret_label=True)
        return value if safe_value == value.value else safe_value
    if isinstance(value, str):
        return _safe_scalar(value, allow_secret_label=_allow_secret_labels)
    return value


def redact_contract(value: Any) -> Any:
    """Preserve benign governance labels while filtering value-shaped secrets."""
    return redact(value, _allow_secret_labels=True)


def _project_policy_gate_commands(
    original_outputs: Mapping[str, Any], safe_outputs: dict[str, Any]
) -> dict[str, Any]:
    original_policy = original_outputs.get("policy")
    safe_policy = safe_outputs.get("policy")
    if not isinstance(original_policy, Mapping) or not isinstance(safe_policy, Mapping):
        return safe_outputs
    original_gates = original_policy.get("gates")
    safe_gates = safe_policy.get("gates")
    if (
        isinstance(original_gates, (str, bytes))
        or not isinstance(original_gates, Sequence)
        or isinstance(safe_gates, (str, bytes))
        or not isinstance(safe_gates, Sequence)
        or len(original_gates) != len(safe_gates)
    ):
        return safe_outputs
    projected_gates: list[Any] = []
    for original_gate, safe_gate in zip(original_gates, safe_gates):
        if not isinstance(original_gate, Mapping) or not isinstance(safe_gate, Mapping):
            projected_gates.append(safe_gate)
            continue
        command = original_gate.get("command")
        if (
            original_gate.get("kind") != "command"
            or isinstance(command, (str, bytes))
            or not isinstance(command, Sequence)
            or any(type(item) is not str for item in command)
        ):
            projected_gates.append(safe_gate)
            continue
        projected = dict(safe_gate)
        projected["command"] = tuple(redact(item) for item in command)
        projected_gates.append(projected)
    projected_policy = dict(safe_policy)
    projected_policy["gates"] = tuple(projected_gates)
    result = dict(safe_outputs)
    result["policy"] = projected_policy
    return result


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _safe_finding(value: Finding) -> Finding:
    if not isinstance(value, Finding):
        raise TypeError("findings must contain Finding records")
    return dataclasses.replace(
        value,
        rule_id=_safe_scalar(value.rule_id, allow_secret_label=True),
        category=_safe_scalar(value.category, allow_secret_label=True),
        severity=_safe_scalar(value.severity, allow_secret_label=True),
        confidence=_safe_scalar(value.confidence, allow_secret_label=True),
        path=_safe_scalar(value.path),
        message=_REDACTED,
        evidence_refs=tuple(_safe_scalar(item) for item in value.evidence_refs),
    )


def _safe_check(value: CheckResult) -> CheckResult:
    if not isinstance(value, CheckResult):
        raise TypeError("checks must contain CheckResult records")
    return dataclasses.replace(
        value,
        gate_id=_safe_scalar(value.gate_id, allow_secret_label=True),
        phase=_safe_scalar(value.phase, allow_secret_label=True),
        message=_REDACTED,
        evidence_refs=tuple(_safe_scalar(item) for item in value.evidence_refs),
    )


def _safe_string_items(values: Iterable[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field_name} must be an iterable of strings")
    try:
        raw = tuple(values)
    except TypeError as error:
        raise TypeError(f"{field_name} must be an iterable of strings") from error
    if any(not isinstance(item, str) for item in raw):
        raise TypeError(f"{field_name} must contain only strings")
    result = tuple(_safe_scalar(item) for item in raw)
    if isinstance(values, (set, frozenset)):
        return tuple(sorted(result))
    return result


def _safe_findings(values: Iterable[Finding]) -> tuple[Finding, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("findings must be an iterable of Finding records")
    result = tuple(_safe_finding(item) for item in values)
    if isinstance(values, (set, frozenset)):
        return tuple(
            sorted(result, key=lambda item: (item.path, item.rule_id, item.message))
        )
    return result


def _safe_checks(values: Iterable[CheckResult]) -> tuple[CheckResult, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("checks must be an iterable of CheckResult records")
    result = tuple(_safe_check(item) for item in values)
    if isinstance(values, (set, frozenset)):
        return tuple(
            sorted(
                result,
                key=lambda item: (item.phase, item.gate_id, item.status.value),
            )
        )
    return result


def build_receipt(
    *,
    command: str,
    policy_digest: str = "",
    target_fingerprint: str = "",
    actor: str = "controller",
    timestamp_utc: str | None = None,
    authorized_scope: Iterable[str] = (),
    inputs: Mapping[str, Any] | None = None,
    outputs: Mapping[str, Any] | None = None,
    findings: Iterable[Finding] = (),
    checks: Iterable[CheckResult] = (),
    approvals: Iterable[str] = (),
    classification: str = "unknown",
    evidence_refs: Iterable[str] = (),
) -> Receipt:
    if not isinstance(command, str) or not command:
        raise TypeError("command must be a non-empty string")
    raw_outputs = dict(outputs or {})
    safe_inputs = redact(dict(inputs or {}))
    safe_outputs = _project_policy_gate_commands(raw_outputs, redact(raw_outputs))
    return Receipt(
        schema_version="1.0",
        command=_safe_scalar(command),
        policy_digest=_safe_scalar(policy_digest),
        target_fingerprint=_safe_scalar(target_fingerprint),
        actor=_safe_scalar(actor),
        timestamp_utc=_safe_scalar(timestamp_utc or _utc_now()),
        authorized_scope=_safe_string_items(authorized_scope, "authorized_scope"),
        inputs=safe_inputs,
        outputs=safe_outputs,
        findings=_safe_findings(findings),
        checks=_safe_checks(checks),
        approvals=_safe_string_items(approvals, "approvals"),
        classification=_safe_scalar(classification),
        evidence_refs=_safe_string_items(evidence_refs, "evidence_refs"),
    )


def receipt_digest(receipt: Receipt) -> str:
    if not isinstance(receipt, Receipt):
        raise TypeError("receipt must be a Receipt")
    payload = json.loads(canonical_json_bytes(receipt))
    payload.pop("timestamp_utc", None)
    return digest(payload)


__all__ = [
    "FEEDBACK_LOOP_DECISION_OUTPUT_KEY",
    "FEEDBACK_LOOP_INPUT_KEY",
    "ReceiptLedgerError",
    "ReceiptLedgerInventory",
    "build_receipt",
    "inspect_receipt_ledger",
    "load_receipt_json",
    "load_receipt_mapping",
    "receipt_digest",
    "redact",
    "redact_contract",
    "require_canonical_receipt_ledger",
]
