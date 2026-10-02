"""Digest-bound local file transactions with cooperative recovery evidence.

The transaction layer is intentionally application-level protection. It checks
paths, links, identities, hashes, and evidence integrity, but it is not an OS
sandbox and cannot prevent a privileged process from changing the filesystem.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .safety import (
    SCHEMA_VERSION,
    _existing_casefold_alias,
    _is_reparse,
    _portable_key,
    _relative_parts,
    canonical_json,
    safe_target,
    sanitize_text,
    sha256_bytes,
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TRANSACTION_ID = re.compile(r"^tx-[0-9]{8}T[0-9]{6}[0-9]{6}Z-[0-9a-f]{12}$")
# Retained for compatibility with the historical fault probe; lock recovery no
# longer uses age as an ownership signal.
_LOCK_STALE_SECONDS = 300.0
_LOCK_NAME = ".transaction.lock"


class _WritePayload(bytes):
    """Bytes carrying a single-call pre-replace evidence callback."""

    def __new__(cls, content: bytes, before_replace=None):
        value = super().__new__(cls, content)
        value.before_replace = before_replace
        return value


class _LockBusy(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_hash(path: Path) -> str | None:
    try:
        if not path.exists() or path.is_symlink() or _is_reparse(path):
            return None
        return sha256_bytes(path.read_bytes())
    except OSError:
        return None


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        prepared = Path(temporary)
        prepared_identity = _identity(prepared)
        if prepared_identity is None:
            raise OSError("prepared postimage identity is unavailable")
        hook = getattr(content, "before_replace", None)
        if hook is not None:
            hook(path, prepared, prepared_identity)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    """Atomically replace evidence bytes without sharing target-write fault hooks."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _root(root: str | os.PathLike[str] | Path) -> Path:
    from .safety import _root_path

    return _root_path(root)


def _plan_core(root: Path, entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "root": str(root), "entries": entries}


def _plan_digest(plan_core: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json(plan_core))


def _identity(path: Path) -> list[int] | None:
    """普通**文件**的身份:拒绝符号链接/重解析点/硬链接(nlink>1)。"""
    try:
        info = os.stat(path, follow_symlinks=False)
    except (FileNotFoundError, OSError):
        return None
    if path.is_symlink() or _is_reparse(path) or getattr(info, "st_nlink", 1) > 1:
        return None
    return [int(getattr(info, "st_dev", 0)), int(getattr(info, "st_ino", 0))]


def _dir_identity(path: Path) -> list[int] | None:
    """**目录**的身份:拒绝符号链接/重解析点/非目录,链接计数不作为判据。

    W2(2026-09-30):目录的 ``st_nlink`` 语义与普通文件不同——POSIX 上一个
    空目录的 ``st_nlink`` 就是 2(自身 + 父目录里的条目),子目录再各加 1;
    硬链接规则只对普通文件有意义。此前把两者混用,导致 Linux 上"新建的父
    目录"一律被判为身份不可得,从缺父目录开始的新手流程被整体挡住。

    这里**不**删除任何链接/重解析/前像防护:目录链接仍然被拒,身份仍然绑定
    (dev, ino),事务回滚仍只移除"本事务创建且身份未变的空目录"。
    """
    try:
        info = os.stat(path, follow_symlinks=False)
    except (FileNotFoundError, OSError):
        return None
    if path.is_symlink() or _is_reparse(path):
        return None
    if not stat.S_ISDIR(int(getattr(info, "st_mode", 0))):
        return None
    return [int(getattr(info, "st_dev", 0)), int(getattr(info, "st_ino", 0))]


def _snapshot(path: Path) -> tuple[bytes | None, str | None, list[int] | None]:
    before_identity = _identity(path)
    if before_identity is None:
        if path.exists() or path.is_symlink():
            raise ValueError("target identity is unsafe")
        return None, None, None
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ValueError(f"target read failed: {sanitize_text(str(error))}") from error
    if before_identity != _identity(path):
        raise ValueError("target changed while it was being snapshotted")
    return content, sha256_bytes(content), before_identity


def _prepare_parents(root: Path, target: Path) -> list[tuple[str, list[int]]]:
    relative_parent = target.parent.relative_to(root)
    current = root
    created: list[tuple[str, list[int]]] = []
    for part in relative_parent.parts:
        alias = _existing_casefold_alias(current, part)
        if alias is not None:
            if alias.name != part or alias.is_symlink() or _is_reparse(alias) or not alias.is_dir():
                raise ValueError("target parent is an unsafe alias or link")
            current = alias
            continue
        current = current / part
        made = False
        try:
            current.mkdir()
            made = True
        except FileExistsError:
            pass
        if current.is_symlink() or _is_reparse(current) or not current.is_dir():
            raise ValueError("target parent is an unsafe link or non-directory")
        if made:
            identity = _dir_identity(current)
            if identity is None:
                raise ValueError("created parent identity is unavailable")
            created.append((current.relative_to(root).as_posix(), identity))
    return created


def _planned_parent_paths(root: Path, target: Path) -> list[str]:
    relative_parent = target.parent.relative_to(root)
    current = root
    planned: list[str] = []
    for part in relative_parent.parts:
        alias = _existing_casefold_alias(current, part)
        if alias is not None:
            if alias.name != part or alias.is_symlink() or _is_reparse(alias) or not alias.is_dir():
                raise ValueError("target parent is an unsafe alias or link")
            current = alias
            continue
        current = current / part
        planned.append(current.relative_to(root).as_posix())
    return planned


def _remove_owned_empty_parents(
    root: Path,
    parent_identities: Mapping[str, list[int]],
) -> None:
    for relative in sorted(parent_identities, key=lambda value: (value.count("/"), _portable_key(value)), reverse=True):
        try:
            parent = _secure_dir(root, tuple(relative.split("/")), create=False)
        except (OSError, ValueError):
            continue
        if not parent.exists():
            continue
        if (
            parent.is_symlink()
            or _is_reparse(parent)
            or not parent.is_dir()
            or _dir_identity(parent) != parent_identities[relative]
        ):
            continue
        try:
            next(parent.iterdir())
        except StopIteration:
            parent.rmdir()
        except OSError:
            continue


def _cleanup_parent_paths(
    root: Path,
    parent_paths: set[str],
    parent_identities: Mapping[str, list[int]],
) -> list[str]:
    """Remove only verified empty parents and report every residual path."""

    def locate_existing(relative: str) -> Path | None:
        current = root.resolve(strict=True)
        for part in relative.split("/"):
            alias = _existing_casefold_alias(current, part)
            if alias is None:
                return None
            if alias.name != part or alias.is_symlink() or _is_reparse(alias) or not alias.is_dir():
                raise ValueError("parent path has an unsafe alias, link, reparse point, or non-directory")
            current = alias
        return current

    residual: list[str] = []
    for relative in sorted(parent_paths, key=lambda value: (value.count("/"), _portable_key(value)), reverse=True):
        try:
            parent = locate_existing(relative)
        except (OSError, ValueError):
            residual.append(relative)
            continue
        if parent is None:
            continue
        expected = parent_identities.get(relative)
        if expected is None or _dir_identity(parent) != expected:
            residual.append(relative)
            continue
        try:
            next(parent.iterdir())
            residual.append(relative)
        except StopIteration:
            try:
                parent.rmdir()
            except OSError:
                residual.append(relative)
        except OSError:
            residual.append(relative)
    return sorted(set(residual), key=_portable_key)


def preview_changes(root: Path, files: dict[str, str]) -> dict[str, Any]:
    """Build a read-only plan. This function never creates directories/files."""

    project_root = _root(root)
    if not isinstance(files, dict) or not files:
        raise ValueError("files must be a non-empty mapping")
    normalized: dict[str, tuple[str, Path, str]] = {}
    for original, content in files.items():
        if not isinstance(content, str):
            raise ValueError("file content must be text")
        relative = "/".join(_relative_parts(original))
        target = safe_target(project_root, relative, allow_missing=True)
        canonical = target.relative_to(project_root).as_posix() if target.exists() else relative
        key = _portable_key(canonical)
        if key in normalized:
            raise ValueError("files contain a path alias or duplicate")
        normalized[key] = (canonical, target, content)

    entries: list[dict[str, Any]] = []
    for _key, (relative, target, content) in sorted(normalized.items()):
        before = _read_hash(target)
        if target.exists() and before is None:
            raise ValueError(f"target cannot be safely read: {relative}")
        encoded = content.encode("utf-8")
        entries.append(
            {
                "path": relative,
                "operation": "update" if target.exists() else "create",
                "before_sha256": before,
                "after_sha256": sha256_bytes(encoded),
                "content": content,
            }
        )
    core = _plan_core(project_root, entries)
    return {**core, "plan_digest": _plan_digest(core), "status": "preview"}


def _valid_hash(value: Any, *, allow_none: bool = False) -> bool:
    return (value is None and allow_none) or (isinstance(value, str) and bool(_HEX64.fullmatch(value)))


def _validate_plan(root: Path, plan: Mapping[str, Any]) -> tuple[dict[str, Any], list[tuple[dict[str, Any], Path]]]:
    if not isinstance(plan, Mapping) or plan.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported or missing plan schema")
    if set(plan) - {"schema_version", "root", "entries", "plan_digest", "status", "commit_guard"}:
        raise ValueError("plan contains unknown fields")
    guard = plan.get("commit_guard")
    if guard is not None and not callable(guard):
        raise ValueError("plan commit guard is not callable")
    if plan.get("status") not in {None, "preview"}:
        raise ValueError("plan is not an immutable preview")
    if plan.get("root") != str(root):
        raise ValueError("plan root mismatch")
    entries = plan.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("plan entries must be a non-empty list")
    core = {"schema_version": plan["schema_version"], "root": plan["root"], "entries": entries}
    if plan.get("plan_digest") != _plan_digest(core):
        raise ValueError("plan digest mismatch")

    validated: list[tuple[dict[str, Any], Path]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"path", "operation", "before_sha256", "after_sha256", "content"}:
            raise ValueError("plan entry fields are incompatible")
        relative = entry["path"]
        if not isinstance(relative, str):
            raise ValueError("plan entry path must be a string")
        if "/".join(_relative_parts(relative)) != relative:
            raise ValueError("plan entry path is not canonical")
        target = safe_target(root, relative, allow_missing=True)
        key = _portable_key(relative)
        if key in seen:
            raise ValueError("plan contains duplicate or aliased paths")
        seen.add(key)
        if entry["operation"] not in {"create", "update"} or not isinstance(entry["content"], str):
            raise ValueError("plan entry operation or content is invalid")
        if not _valid_hash(entry["before_sha256"], allow_none=True) or not _valid_hash(entry["after_sha256"]):
            raise ValueError("plan entry hashes are invalid")
        actual_before = _read_hash(target)
        if actual_before != entry["before_sha256"]:
            raise ValueError(f"preimage drift: {relative}")
        if sha256_bytes(entry["content"].encode("utf-8")) != entry["after_sha256"]:
            raise ValueError(f"after image digest mismatch: {relative}")
        expected_operation = "update" if target.exists() else "create"
        if entry["operation"] != expected_operation:
            raise ValueError(f"operation drift: {relative}")
        validated.append((dict(entry), target))
    return dict(plan), validated


def _secure_dir(root: Path, parts: tuple[str, ...], *, create: bool) -> Path:
    root = root.resolve(strict=True)
    current = root
    for part in parts:
        alias = _existing_casefold_alias(current, part)
        if alias is not None:
            if alias.name != part or alias.is_symlink() or _is_reparse(alias) or not alias.is_dir():
                raise ValueError("internal evidence path has an unsafe alias or link")
            current = alias
            continue
        candidate = current / part
        if not create:
            raise ValueError("internal evidence path is missing")
        try:
            candidate.mkdir()
        except FileExistsError:
            pass
        if candidate.is_symlink() or _is_reparse(candidate) or not candidate.is_dir():
            raise ValueError("internal evidence path has an unsafe link")
        current = candidate
    resolved = current.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("internal evidence path escapes root")
    return current


def _evidence_base(root: Path, *, create: bool) -> Path:
    return _secure_dir(root, (".opencoding", "transactions"), create=create)


def _evidence_dir(root: Path, transaction_id: str, *, create: bool = False) -> Path:
    if not _TRANSACTION_ID.fullmatch(transaction_id):
        raise ValueError("invalid transaction id")
    return _secure_dir(root, (".opencoding", "transactions", transaction_id), create=create)


def _write_bytes(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Replace JSON through a sibling directory so the old receipt survives faults."""

    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    temporary_dir.mkdir()
    temporary = temporary_dir / path.name
    try:
        _write_bytes(temporary, canonical_json(value) + b"\n")
        os.replace(temporary, path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        finally:
            try:
                temporary_dir.rmdir()
            except OSError:
                pass


def _safe_strings(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [_safe_strings(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _safe_strings(item) for key, item in value.items()}
    return value


def _safe_receipt_paths(value: Mapping[str, Any]) -> dict[str, Any]:
    """Sanitize a receipt without changing its JSON-compatible shape."""

    sanitized = _safe_strings(dict(value))
    if not isinstance(sanitized, dict):
        raise ValueError("receipt is not a JSON object")
    return sanitized


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


def _safe_evidence_file(path: Path, evidence: Path) -> None:
    """Require an ordinary, unaliased file contained by the evidence dir."""

    if not path.is_file() or path.is_symlink() or _is_reparse(path):
        raise ValueError("transaction evidence file is missing or unsafe")
    try:
        if not path.resolve(strict=True).is_relative_to(evidence.resolve(strict=True)):
            raise ValueError("transaction evidence file escapes evidence root")
        if os.stat(path, follow_symlinks=False).st_nlink > 1:
            raise ValueError("transaction evidence file is a hardlink")
    except OSError as error:
        raise ValueError("transaction evidence file is unavailable") from error


def _event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key not in {"event_sha256", "previous_event_sha256"}}


def _append_event(directory: Path, event: Mapping[str, Any]) -> str:
    path = directory / "events.jsonl"
    if path.exists():
        _safe_evidence_file(path, directory)
    existing = path.read_bytes() if path.exists() else b""
    payload = dict(_safe_strings(dict(event)))
    payload["previous_event_sha256"] = sha256_bytes(existing) if existing else None
    payload["event_sha256"] = sha256_bytes(canonical_json(_event_payload(payload)))
    _atomic_replace_bytes(path, existing + canonical_json(payload) + b"\n")
    return str(path)


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


def _load_events(evidence: Path, transaction_id: str) -> list[dict[str, Any]]:
    path = evidence / "events.jsonl"
    _safe_evidence_file(path, evidence)
    events: list[dict[str, Any]] = []
    previous = b""
    raw = path.read_bytes()
    for line in raw.splitlines(keepends=True):
        if not line.strip():
            raise ValueError("events evidence contains an empty line")
        if not line.endswith(b"\n"):
            raise ValueError("events evidence is not newline terminated")
        try:
            event = _strict_json_bytes(line, "event")
        except ValueError:
            raise
        _validate_event(event, previous, transaction_id)
        events.append(event)
        previous += line
    if not events:
        raise ValueError("events evidence is empty")
    return events


def _load_receipt_manifest(root: Path, transaction_id: str) -> tuple[Path, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    evidence = _evidence_dir(root, transaction_id, create=False)
    receipt_path = evidence / "receipt.json"
    manifest_path = evidence / "manifest.json"
    _safe_evidence_file(receipt_path, evidence)
    _safe_evidence_file(manifest_path, evidence)
    receipt = _strict_json_bytes(receipt_path.read_bytes(), "receipt")
    manifest_doc = _strict_json_bytes(manifest_path.read_bytes(), "manifest")
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
    manifest_bytes = manifest_path.read_bytes()
    if sha256_bytes(manifest_bytes.rstrip(b"\n")) != receipt["manifest_sha256"]:
        raise ValueError("manifest digest mismatch")
    legacy_manifest_fields = {"schema_version", "transaction_id", "plan_digest", "entries"}
    if set(manifest_doc) not in (legacy_manifest_fields, legacy_manifest_fields | {"root"}):
        raise ValueError("manifest fields are incompatible")
    # New receipts retain the canonical root from the reviewed plan. A moved
    # tree can retain every inode, so file identities alone cannot bind a
    # rollback to its original project path. Legacy manifests remain readable
    # but their missing root binding is explicitly reported to callers.
    if "root" in manifest_doc and manifest_doc["root"] != str(root):
        raise ValueError("transaction root mismatch")
    if manifest_doc["schema_version"] != SCHEMA_VERSION or manifest_doc["transaction_id"] != transaction_id or manifest_doc["plan_digest"] != receipt["plan_digest"]:
        raise ValueError("manifest identity is invalid")
    entries = manifest_doc["entries"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("manifest entries are invalid")
    manifest_paths: set[str] = set()
    for index, item in enumerate(entries):
        required = {"path", "before_exists", "before_sha256", "after_sha256", "preimage_file", "before_identity"}
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("manifest entry fields are incompatible")
        if not isinstance(item["path"], str) or item["path"] in manifest_paths:
            raise ValueError("manifest path is invalid")
        if "/".join(_relative_parts(item["path"])) != item["path"]:
            raise ValueError("manifest path is not canonical")
        safe_target(root, item["path"], allow_missing=True)
        manifest_paths.add(item["path"])
        if type(item["before_exists"]) is not bool or not _valid_hash(item["before_sha256"], allow_none=True) or not _valid_hash(item["after_sha256"]):
            raise ValueError("manifest hashes are invalid")
        expected_preimage = f"preimage/{index}.bin" if item["before_exists"] else None
        if item["preimage_file"] != expected_preimage:
            raise ValueError("manifest preimage path is invalid")
        if item["before_exists"] and (not isinstance(item["before_identity"], list) or len(item["before_identity"]) != 2):
            raise ValueError("manifest identity is invalid")
        if not item["before_exists"] and item["before_identity"] is not None:
            raise ValueError("manifest identity is invalid")
        if item["before_exists"]:
            preimage = evidence / item["preimage_file"]
            _safe_evidence_file(preimage, evidence)
            if sha256_bytes(preimage.read_bytes()) != item["before_sha256"]:
                raise ValueError("preimage digest mismatch")
    preimage_dir = evidence / "preimage"
    if not preimage_dir.is_dir() or preimage_dir.is_symlink() or _is_reparse(preimage_dir):
        raise ValueError("transaction preimage directory is missing or unsafe")
    expected_preimages = {item["preimage_file"] for item in entries if item["preimage_file"] is not None}
    actual_preimages = set()
    for candidate in preimage_dir.iterdir():
        if candidate.is_dir() or candidate.is_symlink() or _is_reparse(candidate):
            raise ValueError("transaction preimage directory contains unsafe entry")
        relative = candidate.relative_to(evidence).as_posix()
        _safe_evidence_file(candidate, evidence)
        actual_preimages.add(relative)
    if actual_preimages != expected_preimages:
        raise ValueError("transaction preimage inventory disagrees")
    if set(receipt["planned_paths"]) != manifest_paths or not set(receipt["changed_paths"]).issubset(manifest_paths):
        raise ValueError("receipt and manifest paths disagree")
    return evidence, receipt, manifest_doc, entries


@contextmanager
def _cooperative_lock(root: Path) -> Iterator[None]:
    base = _evidence_base(root, create=True)
    lock = base / _LOCK_NAME
    payload = canonical_json({"pid": os.getpid(), "created_at": _now(), "nonce": uuid.uuid4().hex})
    if lock.is_symlink() or _is_reparse(lock):
        raise _LockBusy("transaction lock is an unsafe link")
    if lock.exists():
        try:
            if not lock.is_file() or os.stat(lock, follow_symlinks=False).st_nlink > 1:
                raise _LockBusy("transaction lock is not an owned regular file")
        except OSError as error:
            raise _LockBusy("transaction lock is unavailable") from error
    handle = None
    try:
        handle = open(lock, "a+b")
        handle.seek(0)
        if handle.read(1) == b"":
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                handle.close()
                handle = None
                raise _LockBusy("transaction lock is held by another process") from error
        else:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                handle.close()
                handle = None
                raise _LockBusy("transaction lock is held by another process") from error
        handle.seek(0)
        handle.truncate()
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    except _LockBusy:
        raise
    except OSError as error:
        if handle is not None:
            handle.close()
        raise _LockBusy("transaction lock could not be acquired") from error
    try:
        yield
    finally:
        try:
            handle.flush()
            handle.seek(0)
            owned = handle.read() == payload
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            if owned and lock.is_file() and not lock.is_symlink() and not _is_reparse(lock):
                lock.unlink()
        except (FileNotFoundError, OSError):
            try:
                handle.close()
            except OSError:
                pass


def _new_transaction_id() -> str:
    return f"tx-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:12]}"


def _blocked(error: Exception, *, transaction_id: str | None = None, evidence: Path | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "blocked",
        "reason_codes": [sanitize_text(str(error))],
        "changed_paths": [],
        "transaction_id": transaction_id,
        "receipt_path": None,
        "rollback_ref": str(evidence) if evidence else None,
    }


def _entry_state(root: Path, item: Mapping[str, Any], after_identity: list[int] | None = None) -> str:
    target = safe_target(root, item["path"], allow_missing=True)
    current_hash = _read_hash(target)
    current_identity = _identity(target)
    before_state = (
        current_hash == item["before_sha256"] and current_identity == item["before_identity"]
        if item["before_exists"]
        else current_hash is None
    )
    if before_state:
        return "before"
    if current_hash == item["after_sha256"]:
        if after_identity is None:
            return "uncertain"
        if current_identity == after_identity:
            return "after"
    return "drift"


def _observed_paths(root: Path, manifest_entries: list[dict[str, Any]], candidates: set[str], after_identities: Mapping[str, list[int]] | None = None) -> tuple[list[str], list[str]]:
    changed: list[str] = []
    uncertain: list[str] = []
    for item in manifest_entries:
        if item["path"] not in candidates:
            continue
        state = _entry_state(root, item, (after_identities or {}).get(item["path"]))
        if state == "after":
            changed.append(item["path"])
        elif state in {"drift", "uncertain"}:
            uncertain.append(item["path"])
    return changed, uncertain


def _event_candidate_paths(receipt: Mapping[str, Any], events: list[dict[str, Any]]) -> set[str]:
    candidates = set(receipt["changed_paths"])
    for event in events:
        if event.get("status") in {"write_intent", "write_applied"}:
            candidates.update(event.get("changed_paths", []))
    return candidates


def _event_after_identities(events: list[dict[str, Any]]) -> dict[str, list[int]]:
    identities: dict[str, list[int]] = {}
    for event in events:
        if event.get("status") not in {"write_intent", "write_applied"}:
            continue
        path = event.get("path")
        identity = event.get("after_identity")
        if isinstance(path, str) and isinstance(identity, list):
            identities[path] = identity
    return identities


def _event_parent_identities(events: list[dict[str, Any]]) -> dict[str, list[int]]:
    identities: dict[str, list[int]] = {}
    for event in events:
        if event.get("status") not in {"write_intent", "write_applied"}:
            continue
        parent_identities = event.get("parent_identities")
        if isinstance(parent_identities, dict):
            for path, identity in parent_identities.items():
                if isinstance(path, str) and isinstance(identity, list):
                    identities[path] = identity
    return identities


def _event_parent_paths(events: list[dict[str, Any]]) -> set[str]:
    paths: set[str] = set()
    for event in events:
        if event.get("status") not in {"write_intent", "write_applied"}:
            continue
        parent_paths = event.get("parent_paths")
        if isinstance(parent_paths, list):
            paths.update(path for path in parent_paths if isinstance(path, str))
    return paths


def _event_rollback_identities(events: list[dict[str, Any]]) -> dict[str, list[int]]:
    identities: dict[str, list[int]] = {}
    for event in events:
        if event.get("status") != "rollback_intent":
            continue
        path = event.get("path")
        identity = event.get("after_identity")
        if isinstance(path, str) and isinstance(identity, list):
            identities[path] = identity
    return identities


def _event_restored_paths(events: list[dict[str, Any]]) -> set[str]:
    restored: set[str] = set()
    for event in events:
        if event.get("status") != "rollback_item" or not isinstance(event.get("path"), str):
            continue
        reasons = event.get("reason_codes", [])
        if not reasons or "already_restored" in reasons:
            restored.add(event["path"])
    return restored


def _failure_result(
    error: Exception,
    *,
    project_root: Path,
    transaction_id: str,
    evidence: Path,
    manifest_entries: list[dict[str, Any]],
    candidates: set[str],
    receipt_path: Path,
    after_identities: Mapping[str, list[int]] | None = None,
) -> dict[str, Any]:
    try:
        changed, uncertain = _observed_paths(project_root, manifest_entries, candidates, after_identities)
    except Exception:
        changed, uncertain = [], sorted(candidates, key=_portable_key)
    status = "partial_failure" if changed else ("blocked" if uncertain else "failed")
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "transaction_id": transaction_id,
        "changed_paths": changed,
        "uncertain_paths": uncertain,
        "receipt_path": str(receipt_path) if receipt_path.exists() else None,
        "rollback_ref": str(evidence),
        "reason_codes": [sanitize_text(str(error))],
    }
    receipt_status = status if status in {"partial_failure", "failed"} else "failed"
    terminal_receipt = dict(
        _safe_receipt_paths(
            {
                "status": receipt_status,
                "changed_paths": changed,
                "uncertain_paths": uncertain,
                "finished_at": _now(),
                "reason_codes": result["reason_codes"],
            }
        )
    )
    try:
        current = _strict_json_bytes(receipt_path.read_bytes(), "receipt")
        if not isinstance(current, dict):
            raise ValueError("receipt is not an object")
        current.update(terminal_receipt)
        _write_json(receipt_path, current)
        result["receipt_path"] = str(receipt_path)
    except Exception as receipt_error:
        result["reason_codes"].append(
            f"terminal receipt persistence unavailable: {sanitize_text(str(receipt_error))}"
        )
    try:
        _append_event(
            evidence,
            {
                "transaction_id": transaction_id,
                "status": receipt_status,
                "changed_paths": list(changed),
                "at": _now(),
                "reason_codes": result["reason_codes"],
            },
        )
    except Exception as event_error:
        result["reason_codes"].append(
            f"terminal event persistence unavailable: {sanitize_text(str(event_error))}"
        )
    return result


def apply_changes(root: Path, plan: dict[str, Any], *, approved_digest: str) -> dict[str, Any]:
    """Apply one unchanged preview under a cooperative lock."""

    transaction_id: str | None = None
    evidence: Path | None = None
    try:
        project_root = _root(root)
        if not isinstance(approved_digest, str) or approved_digest != plan.get("plan_digest"):
            raise ValueError("approved digest mismatch")
        _validate_plan(project_root, plan)
        transaction_id = _new_transaction_id()
        with _cooperative_lock(project_root):
            _validated_plan, entries = _validate_plan(project_root, plan)
            evidence = _evidence_dir(project_root, transaction_id, create=True)
            preimage_dir = evidence / "preimage"
            preimage_dir.mkdir()
            manifest_entries: list[dict[str, Any]] = []
            for index, (entry, target) in enumerate(entries):
                content, current_hash, identity_before = _snapshot(target)
                if current_hash != entry["before_sha256"]:
                    raise ValueError(f"preimage drift: {entry['path']}")
                if content is not None:
                    _write_bytes(preimage_dir / f"{index}.bin", content)
                manifest_entries.append(
                    {
                        "path": entry["path"],
                        "before_exists": content is not None,
                        "before_sha256": entry["before_sha256"],
                        "after_sha256": entry["after_sha256"],
                        "preimage_file": f"preimage/{index}.bin" if content is not None else None,
                        "before_identity": identity_before,
                    }
                )
            manifest = {"schema_version": SCHEMA_VERSION, "transaction_id": transaction_id, "plan_digest": plan["plan_digest"], "root": str(project_root), "entries": manifest_entries}
            manifest_bytes = canonical_json(manifest) + b"\n"
            _write_bytes(evidence / "manifest.json", manifest_bytes)
            receipt = {
                "schema_version": SCHEMA_VERSION,
                "transaction_id": transaction_id,
                "plan_digest": plan["plan_digest"],
                "status": "started",
                "planned_paths": [entry["path"] for entry, _target in entries],
                "changed_paths": [],
                "uncertain_paths": [],
                "started_at": _now(),
                "manifest_sha256": sha256_bytes(manifest_bytes.rstrip(b"\n")),
                "rollback_status": None,
                "rollback_changed_paths": [],
                "rollback_residual_paths": [],
            }
            receipt_path = evidence / "receipt.json"
            _write_json(receipt_path, receipt)
            changed: list[str] = []
            candidates: set[str] = set()
            after_identities: dict[str, list[int]] = {}
            try:
                # S02：受控提交点。取消/撤销/预算核对由调用方以 commit_guard 注入，
                # 在真正落盘前与每次写入前各核对一次，消除“检查完—开始应用”的无协调窗口。
                guard = plan.get("commit_guard")
                if callable(guard):
                    guard()
                for entry, _original_target in entries:
                    if callable(guard):
                        guard()
                    target = safe_target(project_root, entry["path"], allow_missing=True)
                    current_identity = _identity(target)
                    current_hash = _read_hash(target)
                    expected_identity = next(item["before_identity"] for item in manifest_entries if item["path"] == entry["path"])
                    if current_identity != expected_identity or current_hash != entry["before_sha256"]:
                        raise ValueError(f"preimage drift before write: {entry['path']}")
                    planned_parent_paths = _planned_parent_paths(project_root, target)
                    candidates.add(entry["path"])
                    _append_event(
                        evidence,
                        {
                            "transaction_id": transaction_id,
                            "status": "write_intent",
                            "path": entry["path"],
                            "changed_paths": [entry["path"]],
                            "before_sha256": entry["before_sha256"],
                            "after_sha256": entry["after_sha256"],
                            "before_identity": expected_identity,
                            "parent_paths": planned_parent_paths,
                            "at": _now(),
                        },
                    )
                    created_parents = _prepare_parents(project_root, target)
                    parent_identities = {path: identity for path, identity in created_parents}
                    pending_after_identity: list[list[int]] = []

                    def before_target_replace(
                        replacement_target: Path,
                        _temporary: Path,
                        prepared_identity: list[int],
                    ) -> None:
                        if replacement_target != target:
                            return
                        pending_after_identity.append(prepared_identity)
                        _append_event(
                            evidence,
                            {
                                "transaction_id": transaction_id,
                                "status": "write_intent",
                                "path": entry["path"],
                                "changed_paths": [entry["path"]],
                                "before_sha256": entry["before_sha256"],
                                "after_sha256": entry["after_sha256"],
                                "before_identity": expected_identity,
                                "after_identity": prepared_identity,
                                "parent_paths": planned_parent_paths,
                                "parent_identities": parent_identities,
                                "at": _now(),
                            },
                        )

                    _atomic_write(
                        target,
                        _WritePayload(entry["content"].encode("utf-8"), before_target_replace),
                    )
                    if _read_hash(target) != entry["after_sha256"]:
                        raise ValueError(f"postimage verification failed: {entry['path']}")
                    after_identity = pending_after_identity[-1] if pending_after_identity else _identity(target)
                    if after_identity is None or _identity(target) != after_identity:
                        raise ValueError(f"postimage identity unavailable: {entry['path']}")
                    changed.append(entry["path"])
                    after_identities[entry["path"]] = after_identity
                    _append_event(evidence, {"transaction_id": transaction_id, "status": "write_applied", "path": entry["path"], "changed_paths": [entry["path"]], "after_identity": after_identity, "parent_paths": planned_parent_paths, "parent_identities": parent_identities, "at": _now()})
                    receipt["changed_paths"] = list(changed)
                    _write_json(receipt_path, receipt)
            except Exception as error:
                return _failure_result(error, project_root=project_root, transaction_id=transaction_id, evidence=evidence, manifest_entries=manifest_entries, candidates=candidates, receipt_path=receipt_path, after_identities=after_identities)
            try:
                receipt.update({"status": "applied", "finished_at": _now()})
                _write_json(receipt_path, receipt)
                _append_event(evidence, {"transaction_id": transaction_id, "status": "applied", "changed_paths": changed, "at": _now()})
            except Exception as error:
                return _failure_result(error, project_root=project_root, transaction_id=transaction_id, evidence=evidence, manifest_entries=manifest_entries, candidates=candidates, receipt_path=receipt_path, after_identities=after_identities)
            return {"schema_version": SCHEMA_VERSION, "status": "applied", "transaction_id": transaction_id, "changed_paths": changed, "uncertain_paths": [], "receipt_path": str(receipt_path), "rollback_ref": str(evidence)}
    except (_LockBusy, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return _blocked(error, transaction_id=transaction_id, evidence=evidence)


def rollback_changes(root: Path, transaction_id: str) -> dict[str, Any]:
    """Restore each safe item independently and resume after interruptions."""

    try:
        project_root = _root(root)
        evidence, receipt, _manifest_doc, manifest_entries = _load_receipt_manifest(project_root, transaction_id)
        events = _load_events(evidence, transaction_id)
        manifest_by_path = {item["path"]: item for item in manifest_entries}
        applied_paths = _event_candidate_paths(receipt, events)
        after_identities = _event_after_identities(events)
        if not applied_paths.issubset(manifest_by_path):
            raise ValueError("receipt/events contain unknown changed paths")
        with _cooperative_lock(project_root):
            evidence, receipt, _manifest_doc, manifest_entries = _load_receipt_manifest(project_root, transaction_id)
            events = _load_events(evidence, transaction_id)
            manifest_by_path = {item["path"]: item for item in manifest_entries}
            applied_paths = _event_candidate_paths(receipt, events)
            after_identities = _event_after_identities(events)
            parent_identities = _event_parent_identities(events)
            parent_paths = _event_parent_paths(events)
            rollback_identities = _event_rollback_identities(events)
            prior_restored = _event_restored_paths(events)
            restored: list[str] = []
            blocked: list[str] = []
            failures: list[str] = []
            observed_changed, observed_uncertain = _observed_paths(
                project_root, manifest_entries, applied_paths, after_identities
            )
            for relative in [item["path"] for item in manifest_entries if item["path"] in applied_paths]:
                item = manifest_by_path[relative]
                target = safe_target(project_root, relative, allow_missing=True)
                state = _entry_state(project_root, item, after_identities.get(relative))
                current_identity = _identity(target)
                already_restored = (
                    (
                        state == "before"
                        and relative in prior_restored
                    )
                    or (
                        item["before_exists"]
                        and _read_hash(target) == item["before_sha256"]
                        and rollback_identities.get(relative) == current_identity
                    )
                )
                if already_restored:
                    restored.append(relative)
                    _append_event(evidence, {"transaction_id": transaction_id, "status": "rollback_item", "path": relative, "changed_paths": [relative], "at": _now(), "reason_codes": ["already_restored"]})
                    continue
                if state == "before":
                    _append_event(evidence, {"transaction_id": transaction_id, "status": "rollback_item", "path": relative, "changed_paths": [relative], "at": _now(), "reason_codes": ["not_applied"]})
                    continue
                if state != "after":
                    blocked.append(relative)
                    _append_event(evidence, {"transaction_id": transaction_id, "status": "rollback_item", "path": relative, "changed_paths": [relative], "at": _now(), "reason_codes": ["postimage_drift"]})
                    continue
                try:
                    target = safe_target(project_root, relative, allow_missing=True)
                    if _entry_state(project_root, item, after_identities.get(relative)) != "after":
                        blocked.append(relative)
                        _append_event(evidence, {"transaction_id": transaction_id, "status": "rollback_item", "path": relative, "changed_paths": [relative], "at": _now(), "reason_codes": ["postimage_drift"]})
                        continue
                    if item["before_exists"]:
                        before_identity = _identity(target)
                        if before_identity is None:
                            raise OSError("rollback postimage identity is unavailable")

                        def before_restore_replace(
                            replacement_target: Path,
                            _temporary: Path,
                            prepared_identity: list[int],
                        ) -> None:
                            if replacement_target != target:
                                return
                            _append_event(
                                evidence,
                                {
                                    "transaction_id": transaction_id,
                                    "status": "rollback_intent",
                                    "path": relative,
                                    "changed_paths": [relative],
                                    "before_sha256": item["after_sha256"],
                                    "after_sha256": item["before_sha256"],
                                    "before_identity": before_identity,
                                    "after_identity": prepared_identity,
                                    "at": _now(),
                                },
                            )

                        _atomic_write(
                            target,
                            _WritePayload(
                                (evidence / item["preimage_file"]).read_bytes(),
                                before_restore_replace,
                            ),
                        )
                    else:
                        target.unlink()
                    expected = item["before_sha256"] if item["before_exists"] else None
                    if _read_hash(target) != expected:
                        raise OSError("rollback postcondition failed")
                    restored.append(relative)
                    _append_event(evidence, {"transaction_id": transaction_id, "status": "rollback_item", "path": relative, "changed_paths": [relative], "at": _now()})
                except Exception as error:
                    failures.append(relative)
                    _append_event(evidence, {"transaction_id": transaction_id, "status": "rollback_item", "path": relative, "changed_paths": [relative], "at": _now(), "reason_codes": [sanitize_text(str(error))]})
            residual_paths = _cleanup_parent_paths(project_root, parent_paths, parent_identities)
            verified_changed = set(observed_changed) | set(restored)
            receipt["changed_paths"] = sorted(verified_changed, key=_portable_key)
            receipt["uncertain_paths"] = sorted(set(observed_uncertain) | set(blocked), key=_portable_key)
            unresolved = set(blocked) | set(failures)
            if not unresolved and not residual_paths:
                status = "rolled_back"
                reasons: list[str] = []
            else:
                status = "partial_failure" if restored else "blocked"
                reasons = (
                    ["postimage_drift"] if blocked else []
                ) + (
                    ["rollback_write_failed"] if failures else []
                ) + (
                    ["rollback_residual_paths"] if residual_paths else []
                )
            receipt["rollback_status"] = status
            receipt["rollback_changed_paths"] = restored
            receipt["rollback_residual_paths"] = residual_paths
            if reasons:
                receipt["rollback_reason_codes"] = reasons
            else:
                receipt.pop("rollback_reason_codes", None)
            _write_json(evidence / "receipt.json", _safe_receipt_paths(receipt))
            _append_event(evidence, {"transaction_id": transaction_id, "status": status, "changed_paths": restored, "at": _now(), **({"reason_codes": reasons} if reasons else {})})
            return {"schema_version": SCHEMA_VERSION, "status": status, "changed_paths": restored, "rollback_residual_paths": residual_paths, "rollback_ref": str(evidence), **({"reason_codes": reasons} if reasons else {})}
    except (_LockBusy, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return {"schema_version": SCHEMA_VERSION, "status": "blocked", "reason_codes": [sanitize_text(str(error))], "changed_paths": []}


_RECEIPT_MAX_BYTES = 4 * 1024 * 1024


def read_receipt_state(root: str | os.PathLike[str] | Path, transaction_id: str) -> dict[str, Any]:
    """F10：只读读取某次事务的权威回执与清单，供状态核验使用。

    只解析受控证据目录内的 receipt/manifest，先检查文件身份与大小，再校验
    摘要与路径一致性；不读取无关数据，不修改任何证据。
    """

    project_root = _root(root)
    if not isinstance(transaction_id, str) or not _TRANSACTION_ID.fullmatch(transaction_id):
        raise ValueError("invalid transaction id")
    evidence = _secure_dir(project_root, (".opencoding", "transactions", transaction_id), create=False)
    receipt_path = evidence / "receipt.json"
    manifest_path = evidence / "manifest.json"
    _safe_evidence_file(receipt_path, evidence)
    _safe_evidence_file(manifest_path, evidence)
    receipt_size = os.path.getsize(receipt_path)
    manifest_size = os.path.getsize(manifest_path)
    if receipt_size > _RECEIPT_MAX_BYTES or manifest_size > _RECEIPT_MAX_BYTES:
        raise ValueError("transaction receipt is oversized")
    _evidence, receipt, manifest_doc, entries = _load_receipt_manifest(project_root, transaction_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "root_binding": "verified" if "root" in manifest_doc else "legacy_unbound",
        "recorded_root": manifest_doc.get("root"),
        "status": str(receipt["status"]),
        "rollback_status": receipt.get("rollback_status"),
        "planned_paths": list(receipt["planned_paths"]),
        "changed_paths": list(receipt["changed_paths"]),
        "uncertain_paths": list(receipt.get("uncertain_paths", [])),
        "rollback_changed_paths": list(receipt.get("rollback_changed_paths", [])),
        "residual_paths": list(receipt.get("rollback_residual_paths", [])),
        "entries": [
            {
                "path": str(item["path"]),
                "before_exists": bool(item["before_exists"]),
                "before_sha256": item["before_sha256"],
                "after_sha256": item["after_sha256"],
            }
            for item in entries
        ],
        "receipt_bytes": receipt_size,
        "manifest_bytes": manifest_size,
        "plan_digest": str(receipt["plan_digest"]),
    }


__all__ = ["apply_changes", "preview_changes", "read_receipt_state", "rollback_changes"]
