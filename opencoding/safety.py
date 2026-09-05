"""Offline safety primitives for OpenCoding transactions.

The module deliberately returns plain JSON-compatible values. It does not claim
to be an operating-system sandbox; it provides conservative, auditable checks
before the transaction layer performs local file changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "1.0"
ACTION_KINDS = {
    "preview",
    "local_write",
    "local_run",
    "network",
    "provider",
    "payment",
    "notification",
    "deploy",
    "git_publish",
}
_REQUIRED_ACTION_FIELDS = {
    "kind",
    "root",
    "plan_digest",
    "targets",
    "external",
    "cost_limit",
    "data_scope",
    "irreversible",
}
_OPTIONAL_ACTION_FIELDS = {"requested", "reason"}
_APPROVAL_FIELDS = {
    "action_digest",
    "root",
    "expires_at",
    "approved",
}
_RESERVED_PARTS = {".git", ".governance", ".opencoding"}
_WINDOWS_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_WINDOWS_FORBIDDEN_CHARS = set('<>:"|?*')


def canonical_json(value: Any) -> bytes:
    """Encode a public value using the repository's digest contract."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def _portable_key(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\\", "/")).casefold()


def _redaction_patterns() -> tuple[tuple[str, re.Pattern[str], str], ...]:
    return (
        (
            "private_key",
            re.compile(
                r"-----BEGIN(?: [A-Z0-9][A-Z0-9 -]*)? PRIVATE KEY-----.*?"
                r"-----END(?: [A-Z0-9][A-Z0-9 -]*)? PRIVATE KEY-----",
                re.I | re.S,
            ),
            "[REDACTED_PRIVATE_KEY]",
        ),
        (
            "pgp_private_key",
            re.compile(r"-----BEGIN PGP PRIVATE KEY BLOCK-----.*?-----END PGP PRIVATE KEY BLOCK-----", re.I | re.S),
            "[REDACTED_PRIVATE_KEY]",
        ),
        (
            "private_key_header",
            re.compile(r"-----BEGIN(?: [A-Z0-9][A-Z0-9 -]*)? PRIVATE KEY-----.*", re.I | re.S),
            "[REDACTED_PRIVATE_KEY]",
        ),
    ) + (
        (
            "bearer_token",
            re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.I),
            r"\1[REDACTED]",
        ),
        (
            "credential_assignment",
            re.compile(
                r"((?:api[_-]?key|access[_-]?key|secret|password|passwd|token|authorization)\s*[:=]\s*)([^\s,;]+)",
                re.I,
            ),
            r"\1[REDACTED]",
        ),
        (
            "jwt",
            re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
            "[REDACTED_JWT]",
        ),
        (
            "cloud_key",
            re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
            "[REDACTED_CLOUD_KEY]",
        ),
        (
            "secret_token",
            re.compile(r"\b(?:sk|ghp|xox[baprs]-)[A-Za-z0-9_-]{12,}\b", re.I),
            "[REDACTED_TOKEN]",
        ),
    )


def inspect_sensitive(text: str) -> dict[str, Any]:
    """Classify likely credentials without returning matched secret material."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    categories: list[str] = []
    match_count = 0
    for category, pattern, _replacement in _redaction_patterns():
        count = sum(1 for _match in pattern.finditer(text))
        if count:
            categories.append(category)
            match_count += count
    risk_level = "high" if categories else "none"
    return {
        "sensitive": bool(categories),
        "risk_level": risk_level,
        "categories": categories,
        "match_count": match_count,
    }


def sanitize_text(text: str) -> str:
    """Return text safe for receipts and persistent human-readable logs."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    sanitized = text
    for _category, pattern, replacement in _redaction_patterns():
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def _root_path(root: str | os.PathLike[str] | Path) -> Path:
    if not isinstance(root, (str, os.PathLike, Path)):
        raise ValueError("root must be a path")
    path = Path(root)
    if not path.is_absolute():
        path = path.absolute()
    _reject_linked_ancestors(path)
    if not path.exists() or not path.is_dir() or path.is_symlink() or _is_reparse(path):
        raise ValueError("root must be an existing regular directory")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink() or _is_reparse(resolved):
        raise ValueError("root must be an existing regular directory")
    return resolved


def _reject_linked_ancestors(path: Path) -> None:
    current = path.absolute()
    while True:
        if current.exists() or current.is_symlink():
            if current.is_symlink() or _is_reparse(current):
                raise ValueError("path contains a link or reparse point")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _is_reparse(path: Path) -> bool:
    try:
        attributes = os.stat(path, follow_symlinks=False).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _relative_parts(relative: str) -> tuple[str, ...]:
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise ValueError("target path must be a non-empty string")
    normalized = unicodedata.normalize("NFC", relative.replace("\\", "/"))
    if normalized.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("target path must be relative")
    parts = tuple(normalized.split("/"))
    for part in parts:
        if part in ("", ".", ".."):
            raise ValueError("target path must not contain aliases or traversal")
        if part.casefold() in _RESERVED_PARTS:
            raise ValueError("target path uses a protected metadata directory")
        if any(ord(char) < 32 for char in part) or any(char in _WINDOWS_FORBIDDEN_CHARS for char in part):
            raise ValueError("target path uses a Windows-special name")
        if part.endswith((" ", ".")):
            raise ValueError("target path must not end with a space or dot")
        device_stem = part.rstrip(" .").split(".", 1)[0].casefold()
        if device_stem in _WINDOWS_DEVICE_NAMES:
            raise ValueError("target path uses a reserved Windows device name")
    return parts


def _existing_casefold_alias(parent: Path, name: str) -> Path | None:
    if not parent.is_dir():
        return None
    wanted = _portable_key(name)
    matches = [child for child in parent.iterdir() if _portable_key(child.name) == wanted]
    if len(matches) > 1:
        raise ValueError("target path has ambiguous case or Unicode aliases")
    return matches[0] if matches else None


def safe_target(root: Path, relative: str, *, allow_missing: bool = True) -> Path:
    """Resolve a relative target while rejecting links, aliases, and hardlinks."""

    project_root = _root_path(root)
    parts = _relative_parts(relative)
    current = project_root
    for index, part in enumerate(parts):
        alias = _existing_casefold_alias(current, part)
        if alias is not None:
            if alias.name != part:
                raise ValueError(f"target path is a case alias: {relative}")
            current = alias
        else:
            current = current / part
        is_last = index == len(parts) - 1
        if current.exists() or current.is_symlink():
            if current.is_symlink() or _is_reparse(current):
                raise ValueError(f"target path contains a link or reparse point: {relative}")
            info = os.stat(current, follow_symlinks=False)
            if not is_last and not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"target parent is not a directory: {relative}")
            if is_last and not stat.S_ISREG(info.st_mode):
                raise ValueError(f"target is not a regular file: {relative}")
            if is_last and getattr(info, "st_nlink", 1) > 1:
                raise ValueError(f"target is a hardlink: {relative}")
        elif not is_last:
            # Missing parents may be created by the transaction layer, but no
            # existing ancestor may be silently followed through a link.
            continue
    if not current.exists() and not allow_missing:
        raise ValueError(f"target does not exist: {relative}")
    resolved = current.resolve(strict=False)
    if not resolved.is_relative_to(project_root):
        raise ValueError(f"target escapes root: {relative}")
    return current


def _normal_action(action: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(action, Mapping):
        raise ValueError("action must be an object")
    keys = set(action)
    missing = _REQUIRED_ACTION_FIELDS - keys
    unknown = keys - _REQUIRED_ACTION_FIELDS - _OPTIONAL_ACTION_FIELDS
    if missing:
        raise ValueError("missing action fields: " + ", ".join(sorted(missing)))
    if unknown:
        raise ValueError("unknown action fields: " + ", ".join(sorted(unknown)))
    normalized = dict(action)
    kind = normalized["kind"]
    if not isinstance(kind, str) or kind not in ACTION_KINDS:
        raise ValueError("unknown action kind")
    normalized["root"] = str(Path(normalized["root"]).absolute())
    targets = normalized["targets"]
    if isinstance(targets, (str, bytes)) or not isinstance(targets, (list, tuple)):
        raise ValueError("targets must be a sequence")
    normalized_targets = []
    seen = set()
    for target in targets:
        parts = _relative_parts(target)
        canonical = "/".join(parts)
        key = _portable_key(canonical)
        if key in seen:
            raise ValueError("targets contain an alias or duplicate")
        seen.add(key)
        normalized_targets.append(canonical)
    normalized["targets"] = sorted(normalized_targets, key=_portable_key)
    if not isinstance(normalized["plan_digest"], str) or not normalized["plan_digest"]:
        raise ValueError("plan_digest must be a non-empty string")
    if type(normalized["external"]) is not bool or type(normalized["irreversible"]) is not bool:
        raise ValueError("external and irreversible must be booleans")
    if isinstance(normalized["cost_limit"], bool) or not isinstance(normalized["cost_limit"], (int, float)) or normalized["cost_limit"] < 0:
        raise ValueError("cost_limit must be a non-negative number")
    if not isinstance(normalized["data_scope"], (str, list, tuple, dict)):
        raise ValueError("data_scope must be structured or textual")
    if "requested" in normalized and type(normalized["requested"]) is not bool:
        raise ValueError("requested must be boolean")
    return normalized


def action_digest(action: Mapping[str, Any]) -> str:
    """Digest an action after conservative normalization."""

    return sha256_bytes(canonical_json(_normal_action(action)))


def _valid_approval(approval: Mapping[str, Any] | None, digest: str, root: str) -> tuple[bool, str]:
    if approval is None:
        return False, "approval_missing"
    if not isinstance(approval, Mapping) or set(approval) != _APPROVAL_FIELDS:
        return False, "approval_fields_invalid"
    if approval["action_digest"] != digest:
        return False, "approval_digest_mismatch"
    if approval["root"] != root:
        return False, "approval_root_mismatch"
    if type(approval["approved"]) is not bool or not approval["approved"]:
        return False, "approval_not_granted"
    if not isinstance(approval["expires_at"], str):
        return False, "approval_expiry_invalid"
    try:
        from datetime import datetime, timezone

        expiry = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            return False, "approval_expired"
    except ValueError:
        return False, "approval_expiry_invalid"
    return True, "approval_valid"


def evaluate_action(action: dict[str, Any], approval: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify whether an action is safe, needs confirmation, or is blocked."""

    try:
        normalized = _normal_action(action)
        digest = sha256_bytes(canonical_json(normalized))
    except (TypeError, ValueError) as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "decision": "block",
            "reason_codes": ["invalid_action", sanitize_text(str(error))],
            "action_digest": None,
        }
    if normalized.get("requested", True) is False:
        return {
            "schema_version": SCHEMA_VERSION,
            "decision": "block",
            "reason_codes": ["action_not_requested"],
            "action_digest": digest,
        }
    risky = normalized["kind"] != "preview" or normalized["external"] or normalized["irreversible"] or normalized["cost_limit"] > 0
    if not risky:
        return {
            "schema_version": SCHEMA_VERSION,
            "decision": "allow",
            "reason_codes": ["offline_preview"],
            "action_digest": digest,
        }
    valid, reason = _valid_approval(approval, digest, normalized["root"])
    return {
        "schema_version": SCHEMA_VERSION,
        "decision": "allow" if valid else ("block" if approval is not None else "confirm"),
        "reason_codes": [reason if not valid else "approval_bound"],
        "action_digest": digest,
    }


__all__ = [
    "ACTION_KINDS",
    "SCHEMA_VERSION",
    "action_digest",
    "canonical_json",
    "evaluate_action",
    "inspect_sensitive",
    "safe_target",
    "sanitize_text",
    "sha256_bytes",
    "sha256_text",
]
