"""Pure content validation for caller-supplied transaction evidence.

This module checks existing content rules. It does not collect evidence, prove
file identity/currentness, or attest that a transaction actually ran.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

from .safety import SCHEMA_VERSION, _relative_parts, canonical_json, sha256_bytes

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TRANSACTION_ID = re.compile(r"^tx-[0-9]{8}T[0-9]{6}[0-9]{6}Z-[0-9a-f]{12}$")


def _valid_hash(value: Any, *, allow_none: bool = False) -> bool:
    return (value is None and allow_none) or (isinstance(value, str) and bool(_HEX64.fullmatch(value)))


def _strict_json_bytes(raw: bytes, label: str) -> Any:
    """Load canonical JSON and reject duplicate keys or trailing bytes."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"{label} contains duplicate fields")
            result[key] = value
        return result

    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise ValueError(f"{label} is not canonical JSON")
    try:
        value = json.loads(raw[:-1].decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid JSON") from error
    if canonical_json(value) + b"\n" != raw:
        raise ValueError(f"{label} is not canonical JSON")
    return value


def _event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key not in {"event_sha256", "previous_event_sha256"}}


def _validate_event(event: Any, previous_bytes: bytes, transaction_id: str) -> None:
    if not isinstance(event, dict):
        raise ValueError("event is not an object")
    required = {"transaction_id", "status", "changed_paths", "at", "previous_event_sha256", "event_sha256"}
    allowed = required | {
        "path", "reason_codes", "before_sha256", "after_sha256", "before_identity",
        "after_identity", "parent_paths", "parent_identities",
    }
    if not required.issubset(event) or set(event) - allowed:
        raise ValueError("event fields are incompatible")
    if event["transaction_id"] != transaction_id or not isinstance(event["status"], str):
        raise ValueError("event identity is invalid")
    if event["status"] not in {"write_intent", "write_applied", "rollback_intent", "applied", "failed", "partial_failure", "rollback_item", "rolled_back", "blocked"}:
        raise ValueError("event status is invalid")
    if not isinstance(event["changed_paths"], list) or any(not isinstance(path, str) for path in event["changed_paths"]):
        raise ValueError("event paths are invalid")
    if len(set(event["changed_paths"])) != len(event["changed_paths"]):
        raise ValueError("event paths contain duplicates")
    for path in event["changed_paths"]:
        if "/".join(_relative_parts(path)) != path:
            raise ValueError("event path is not canonical")
    if not isinstance(event["at"], str):
        raise ValueError("event timestamp is invalid")
    expected_previous = sha256_bytes(previous_bytes) if previous_bytes else None
    if event["previous_event_sha256"] != expected_previous:
        raise ValueError("event chain is broken")
    if not _valid_hash(event.get("event_sha256")):
        raise ValueError("event digest is invalid")
    if sha256_bytes(canonical_json(_event_payload(event))) != event["event_sha256"]:
        raise ValueError("event digest mismatch")
    if "path" in event and (not isinstance(event["path"], str) or event["path"] not in event["changed_paths"]):
        raise ValueError("event item path is invalid")
    if "reason_codes" in event and (not isinstance(event["reason_codes"], list) or any(not isinstance(item, str) for item in event["reason_codes"])):
        raise ValueError("event reason codes are invalid")
    if event["status"] in {"write_intent", "rollback_intent"}:
        if not _valid_hash(event.get("before_sha256"), allow_none=True) or not _valid_hash(event.get("after_sha256")):
            raise ValueError("write intent hashes are invalid")
        identity = event.get("before_identity")
        if identity is not None and (not isinstance(identity, list) or len(identity) != 2 or any(not isinstance(item, int) for item in identity)):
            raise ValueError("write intent identity is invalid")
    if "after_identity" in event:
        identity = event["after_identity"]
        if not isinstance(identity, list) or len(identity) != 2 or any(not isinstance(item, int) for item in identity):
            raise ValueError("write result identity is invalid")
    if "parent_paths" in event:
        paths = event["parent_paths"]
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ValueError("parent paths are invalid")
        if len(set(paths)) != len(paths) or any("/".join(_relative_parts(path)) != path for path in paths):
            raise ValueError("parent paths are not canonical")
    if "parent_identities" in event:
        identities = event["parent_identities"]
        if not isinstance(identities, dict):
            raise ValueError("parent identities are invalid")
        if any(
            not isinstance(path, str)
            or "/".join(_relative_parts(path)) != path
            or not isinstance(identity, list)
            or len(identity) != 2
            or any(not isinstance(item, int) for item in identity)
            for path, identity in identities.items()
        ):
            raise ValueError("parent identities are invalid")


def _validate_receipt_fields(receipt: Any, manifest_doc: Any, transaction_id: str) -> None:
    if not isinstance(receipt, dict) or not isinstance(manifest_doc, dict):
        raise ValueError("transaction evidence is not an object")
    receipt_fields = {
        "schema_version", "transaction_id", "plan_digest", "status", "changed_paths", "planned_paths",
        "uncertain_paths", "started_at", "finished_at", "reason_codes", "manifest_sha256", "rollback_status",
        "rollback_changed_paths", "rollback_reason_codes", "rollback_residual_paths",
    }
    required_receipt = {"schema_version", "transaction_id", "plan_digest", "status", "changed_paths", "planned_paths", "started_at", "manifest_sha256", "rollback_status", "rollback_changed_paths"}
    if set(receipt) - receipt_fields or not required_receipt.issubset(receipt):
        raise ValueError("receipt fields are incompatible")
    if receipt["schema_version"] != SCHEMA_VERSION or receipt["transaction_id"] != transaction_id:
        raise ValueError("receipt identity is invalid")
    if not _valid_hash(receipt["plan_digest"]) or not isinstance(receipt["status"], str):
        raise ValueError("receipt digest or status is invalid")
    if receipt["status"] not in {"started", "applied", "partial_failure", "failed"}:
        raise ValueError("receipt status is invalid")
    if not isinstance(receipt["planned_paths"], list) or not isinstance(receipt["changed_paths"], list):
        raise ValueError("receipt paths are invalid")
    if "uncertain_paths" in receipt and not isinstance(receipt["uncertain_paths"], list):
        raise ValueError("receipt uncertain paths are invalid")
    receipt_uncertain = receipt.get("uncertain_paths", [])
    if any(not isinstance(path, str) for path in receipt["planned_paths"] + receipt["changed_paths"] + receipt_uncertain):
        raise ValueError("receipt path type is invalid")
    for path in receipt["planned_paths"] + receipt["changed_paths"] + receipt_uncertain:
        if "/".join(_relative_parts(path)) != path:
            raise ValueError("receipt path is not canonical")
    if len(set(receipt["planned_paths"])) != len(receipt["planned_paths"]):
        raise ValueError("receipt planned paths contain duplicates")
    if len(set(receipt["changed_paths"])) != len(receipt["changed_paths"]):
        raise ValueError("receipt changed paths contain duplicates")
    if len(set(receipt_uncertain)) != len(receipt_uncertain):
        raise ValueError("receipt uncertain paths contain duplicates")
    if not isinstance(receipt["rollback_status"], (str, type(None))) or not isinstance(receipt["rollback_changed_paths"], list):
        raise ValueError("rollback receipt state is invalid")
    if any(not isinstance(path, str) for path in receipt["rollback_changed_paths"]):
        raise ValueError("rollback receipt paths are invalid")
    if any(path not in receipt["changed_paths"] for path in receipt["rollback_changed_paths"]):
        raise ValueError("rollback receipt paths disagree")
    residual_paths = receipt.get("rollback_residual_paths", [])
    if not isinstance(residual_paths, list) or any(not isinstance(path, str) for path in residual_paths):
        raise ValueError("rollback residual paths are invalid")
    if len(set(residual_paths)) != len(residual_paths):
        raise ValueError("rollback residual paths contain duplicates")
    for path in residual_paths:
        if "/".join(_relative_parts(path)) != path:
            raise ValueError("rollback residual path is not canonical")
    for field in ("started_at", "finished_at"):
        if field in receipt and receipt[field] is not None and not isinstance(receipt[field], str):
            raise ValueError("receipt timestamp is invalid")
    for field in ("reason_codes", "rollback_reason_codes"):
        if field in receipt and (not isinstance(receipt[field], list) or any(not isinstance(item, str) for item in receipt[field])):
            raise ValueError("receipt reason codes are invalid")
    if not _valid_hash(receipt["manifest_sha256"]):
        raise ValueError("manifest digest is invalid")


def _validate_manifest_digest(raw: bytes, expected_sha256: str) -> None:
    if sha256_bytes(raw.rstrip(b"\n")) != expected_sha256:
        raise ValueError("manifest digest mismatch")


def _validate_manifest_header(manifest_doc: dict[str, Any], receipt: dict[str, Any], transaction_id: str) -> None:
    if set(manifest_doc) != {"schema_version", "transaction_id", "plan_digest", "entries"}:
        raise ValueError("manifest fields are incompatible")
    if manifest_doc["schema_version"] != SCHEMA_VERSION or manifest_doc["transaction_id"] != transaction_id or manifest_doc["plan_digest"] != receipt["plan_digest"]:
        raise ValueError("manifest identity is invalid")
    entries = manifest_doc["entries"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("manifest entries are invalid")


def _validate_manifest_entry_path(item: Any, manifest_paths: set[str]) -> str:
    required = {"path", "before_exists", "before_sha256", "after_sha256", "preimage_file", "before_identity"}
    if not isinstance(item, dict) or set(item) != required:
        raise ValueError("manifest entry fields are incompatible")
    if not isinstance(item["path"], str) or item["path"] in manifest_paths:
        raise ValueError("manifest path is invalid")
    if "/".join(_relative_parts(item["path"])) != item["path"]:
        raise ValueError("manifest path is not canonical")
    return item["path"]


def _validate_manifest_entry_metadata(item: dict[str, Any], index: int) -> None:
    if type(item["before_exists"]) is not bool or not _valid_hash(item["before_sha256"], allow_none=True) or not _valid_hash(item["after_sha256"]):
        raise ValueError("manifest hashes are invalid")
    expected_preimage = f"preimage/{index}.bin" if item["before_exists"] else None
    if item["preimage_file"] != expected_preimage:
        raise ValueError("manifest preimage path is invalid")
    if item["before_exists"] and (not isinstance(item["before_identity"], list) or len(item["before_identity"]) != 2):
        raise ValueError("manifest identity is invalid")
    if not item["before_exists"] and item["before_identity"] is not None:
        raise ValueError("manifest identity is invalid")


def _validate_preimage_bytes(raw: bytes, expected_hash: str) -> None:
    if sha256_bytes(raw) != expected_hash:
        raise ValueError("preimage digest mismatch")


def _expected_preimages(entries: list[dict[str, Any]]) -> set[str]:
    return {item["preimage_file"] for item in entries if item["preimage_file"] is not None}


def _validate_preimage_inventory(expected: set[str], actual: set[str]) -> None:
    if actual != expected:
        raise ValueError("transaction preimage inventory disagrees")


def _validate_receipt_manifest_paths(receipt: dict[str, Any], manifest_paths: set[str]) -> None:
    if set(receipt["planned_paths"]) != manifest_paths or not set(receipt["changed_paths"]).issubset(manifest_paths):
        raise ValueError("receipt and manifest paths disagree")


def _event_rows(raw: bytes) -> Iterable[tuple[bytes, Any]]:
    seen = False
    for line in raw.splitlines(keepends=True):
        if not line.strip():
            raise ValueError("events evidence contains an empty line")
        if not line.endswith(b"\n"):
            raise ValueError("events evidence is not newline terminated")
        event = _strict_json_bytes(line, "event")
        seen = True
        yield line, event
    if not seen:
        raise ValueError("events evidence is empty")


def _validate_event_rows(rows: Iterable[tuple[bytes, Any]], transaction_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    previous = b""
    for line, event in rows:
        _validate_event(event, previous, transaction_id)
        events.append(event)
        previous += line
    return events


def _parse_events_bytes(raw: bytes, transaction_id: str) -> list[dict[str, Any]]:
    return _validate_event_rows(_event_rows(raw), transaction_id)


# These limits apply only to the new public bytes API, never to legacy readers.
_MAX_BYTES = 16 * 1024 * 1024
_MAX_DEPTH = 64
_MAX_NODES = 100000
_MAX_CONTAINER = 4096
_LIMIT_ERROR = "transaction evidence exceeds resource limits"


def _check_input_bytes(receipt: bytes, manifest: bytes, events: bytes, preimages: dict[str, bytes]) -> None:
    if any(type(raw) is not bytes for raw in (receipt, manifest, events)) or type(preimages) is not dict:
        raise ValueError("transaction evidence requires plain bytes and a plain preimage dict")
    total = len(receipt) + len(manifest) + len(events)
    if total > _MAX_BYTES or len(preimages) > _MAX_CONTAINER:
        raise ValueError(_LIMIT_ERROR)
    for key, raw in preimages.items():
        if type(key) is not str or type(raw) is not bytes:
            raise ValueError("preimages require plain string keys and plain bytes values")
        # A UTF-8 key cannot be shorter than its code-point count. Check before
        # allocating its encoded form, then account for its exact byte length.
        if total + len(key) + len(raw) > _MAX_BYTES:
            raise ValueError(_LIMIT_ERROR)
        try:
            total += len(key.encode("utf-8")) + len(raw)
        except UnicodeEncodeError as error:
            raise ValueError("preimage key is not valid UTF-8") from error
        if total > _MAX_BYTES:
            raise ValueError(_LIMIT_ERROR)


def _structure_nodes(value: Any, used: int = 0) -> int:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        used += 1
        if used > _MAX_NODES or depth > _MAX_DEPTH:
            raise ValueError(_LIMIT_ERROR)
        if type(item) in (dict, list):
            if len(item) > _MAX_CONTAINER:
                raise ValueError(_LIMIT_ERROR)
            if type(item) is dict:
                for key, child in item.items():
                    pending.append((key, depth + 1))
                    pending.append((child, depth + 1))
            else:
                pending.extend((child, depth + 1) for child in item)
    return used


def _check_event_count(raw: bytes) -> None:
    # bytes.splitlines recognizes CR, LF and CRLF. Bound its allocation before
    # parsing, including a final unterminated line (which the parser rejects).
    count = raw.count(b"\n") + raw.count(b"\r") - raw.count(b"\r\n")
    if raw and raw[-1] not in (10, 13):
        count += 1
    if count > _MAX_CONTAINER:
        raise ValueError(_LIMIT_ERROR)


def validate_transaction_bytes(
    *,
    transaction_id: str,
    receipt_bytes: bytes,
    manifest_bytes: bytes,
    events_bytes: bytes,
    preimage_bytes: dict[str, bytes],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate supplied content without IO or any claim of execution/currentness.

    Inputs must be exact builtin str/bytes/dict, with str-to-bytes preimages.
    The 16 MiB aggregate counts all three buffers, preimage bytes and UTF-8 keys.
    Structure limits are depth 64 (root 0), 100000 aggregate nodes (keys count),
    and 4096 members per list/dict, including the events list and preimage dict.

    ID/type/raw bounds precede parsing. Receipt and manifest are parsed, budgeted
    together with preimages, then checked against the legacy content rules.
    Events are subsequently bounded, parsed, budgeted and checked; their parsing
    errors can precede their content errors in this new API. RecursionError is
    converted to a resource-limit ValueError only here. Legacy readers retain
    their own interleaved IO and validation order, without these new limits.

    Returns parsed receipt, manifest, its entries list, and events. The raw
    status values and caller-provided bytes are not attestations or safe files.
    """
    if type(transaction_id) is not str or _TRANSACTION_ID.fullmatch(transaction_id) is None:
        raise ValueError("invalid transaction id")
    _check_input_bytes(receipt_bytes, manifest_bytes, events_bytes, preimage_bytes)
    try:
        receipt = _strict_json_bytes(receipt_bytes, "receipt")
        manifest = _strict_json_bytes(manifest_bytes, "manifest")
        used = _structure_nodes(receipt)
        used = _structure_nodes(manifest, used)
        used = _structure_nodes(preimage_bytes, used)
        _validate_receipt_fields(receipt, manifest, transaction_id)
        _validate_manifest_digest(manifest_bytes, receipt["manifest_sha256"])
        _validate_manifest_header(manifest, receipt, transaction_id)
        entries = manifest["entries"]
        paths: set[str] = set()
        for index, item in enumerate(entries):
            paths.add(_validate_manifest_entry_path(item, paths))
            _validate_manifest_entry_metadata(item, index)
            if item["before_exists"]:
                name = item["preimage_file"]
                if name not in preimage_bytes:
                    raise ValueError("transaction preimage inventory disagrees")
                _validate_preimage_bytes(preimage_bytes[name], item["before_sha256"])
        _validate_preimage_inventory(_expected_preimages(entries), set(preimage_bytes))
        _validate_receipt_manifest_paths(receipt, paths)
        _check_event_count(events_bytes)
        rows = list(_event_rows(events_bytes))
        _structure_nodes([event for _line, event in rows], used)
        events = _validate_event_rows(rows, transaction_id)
        return receipt, manifest, entries, events
    except RecursionError as error:
        raise ValueError(_LIMIT_ERROR) from error


__all__ = ["validate_transaction_bytes"]
