"""Closed consistency manifest parsing, read-only evaluation, and impact evidence."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any
import unicodedata

from .storage import canonical_json_bytes, digest


CONSISTENCY_MANIFEST_RELATIVE_PATH = ".governance/consistency.manifest.json"
CONSISTENCY_MANIFEST_SCHEMA_VERSION = "1.0"

_MAX_CANONICAL_BYTES = 1_048_576
_MAX_RELATIONSHIPS = 128
_MIN_RELATIONSHIP_MEMBERS = 2
_MAX_RELATIONSHIP_MEMBERS = 16
_MAX_UNIQUE_PATHS = 512
_MAX_CHANGED_PATHS = 1024
_MAX_PATH_CHARS = 256
_MAX_MEMBER_BYTES = 8 * 1024 * 1024
_MAX_AGGREGATE_BYTES = 64 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024

_TOP_LEVEL_FIELDS = frozenset({"schema_version", "relationships"})
_SOURCE_GENERATED_FIELDS = frozenset(
    {
        "relationship_id",
        "kind",
        "comparison",
        "source_path",
        "generated_paths",
    }
)
_CROSS_SURFACE_FIELDS = frozenset(
    {"relationship_id", "kind", "comparison", "paths"}
)
_STABLE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_FORBIDDEN_PREFIX_KEYS = (
    ".git",
    ".governance/receipts",
    ".governance/changes",
    ".governance/regressions",
    ".governance/.recovery",
)


class ConsistencyManifestError(ValueError):
    """Raised when a consistency manifest or its members violate the contract."""


@dataclass(frozen=True)
class ConsistencyRelationship:
    relationship_id: str
    kind: str
    comparison: str
    member_paths: tuple[str, ...]
    source_path: str | None = None
    generated_paths: tuple[str, ...] = ()

    @property
    def paths(self) -> tuple[str, ...]:
        """Return all relationship endpoints in their canonical order."""
        return self.member_paths

    @property
    def member_count(self) -> int:
        return len(self.member_paths)


@dataclass(frozen=True)
class ConsistencyManifest:
    schema_version: str
    relationships: tuple[ConsistencyRelationship, ...]
    manifest_sha256: str

    @property
    def digest(self) -> str:
        return self.manifest_sha256

    @property
    def relationship_count(self) -> int:
        return len(self.relationships)

    @property
    def member_count(self) -> int:
        return sum(relationship.member_count for relationship in self.relationships)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                path
                for relationship in self.relationships
                for path in relationship.member_paths
            )
        )


@dataclass(frozen=True)
class ConsistencyMemberEvaluation:
    path: str
    status: str
    size_bytes: int | None = None
    sha256: str | None = None

    @property
    def present(self) -> bool:
        return self.status == "present"


@dataclass(frozen=True)
class ConsistencyRelationshipEvaluation:
    relationship_id: str
    kind: str
    comparison: str
    status: str
    members: tuple[ConsistencyMemberEvaluation, ...]

    @property
    def member_count(self) -> int:
        return len(self.members)

    @property
    def missing_paths(self) -> tuple[str, ...]:
        return tuple(member.path for member in self.members if not member.present)

    @property
    def distinct_content_count(self) -> int:
        return len(
            {
                (member.size_bytes, member.sha256)
                for member in self.members
                if member.present
            }
        )

    @property
    def is_consistent(self) -> bool:
        return self.status == "pass"


@dataclass(frozen=True)
class ConsistencyManifestEvaluation:
    manifest_sha256: str
    relationships: tuple[ConsistencyRelationshipEvaluation, ...]
    aggregate_bytes: int

    @property
    def digest(self) -> str:
        return self.manifest_sha256

    @property
    def status(self) -> str:
        statuses = {relationship.status for relationship in self.relationships}
        return "pass" if statuses <= {"pass"} else "fail"

    @property
    def relationship_count(self) -> int:
        return len(self.relationships)

    @property
    def pass_count(self) -> int:
        return sum(item.status == "pass" for item in self.relationships)

    @property
    def missing_count(self) -> int:
        return sum(item.status == "missing" for item in self.relationships)

    @property
    def drift_count(self) -> int:
        return sum(item.status == "drift" for item in self.relationships)

    @property
    def failing_relationship_ids(self) -> tuple[str, ...]:
        return tuple(
            item.relationship_id
            for item in self.relationships
            if item.status != "pass"
        )

    @property
    def is_consistent(self) -> bool:
        return self.status == "pass"

    def relationship(self, relationship_id: str) -> ConsistencyRelationshipEvaluation:
        for item in self.relationships:
            if item.relationship_id == relationship_id:
                return item
        raise KeyError(relationship_id)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ConsistencyManifestError(f"{label} must be a string-keyed object")
    return value


def _closed(value: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    missing = sorted(fields - set(value))
    unknown = sorted(set(value) - fields)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise ConsistencyManifestError(
            f"{label} fields are invalid: {'; '.join(details)}"
        )


def _sequence(
    value: object,
    label: str,
    maximum: int,
    *,
    minimum: int = 0,
) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ConsistencyManifestError(f"{label} must be an array")
    if len(value) < minimum:
        raise ConsistencyManifestError(
            f"{label} must contain at least {minimum} items"
        )
    if len(value) > maximum:
        raise ConsistencyManifestError(f"{label} exceeds its {maximum}-item bound")
    return value


def _stable_id(value: object, label: str) -> str:
    if type(value) is not str or not _STABLE_ID.fullmatch(value):
        raise ConsistencyManifestError(f"{label} must be a stable lowercase ID")
    return value


def _portable_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _safe_relative(
    value: object,
    label: str,
    *,
    normalize_separator: bool = False,
) -> str:
    if type(value) is not str or not value or len(value) > _MAX_PATH_CHARS:
        raise ConsistencyManifestError(f"{label} must be a bounded non-empty path")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ConsistencyManifestError(f"{label} contains control characters")
    if not normalize_separator and "\\" in value:
        raise ConsistencyManifestError(f"{label} must use POSIX separators")
    normalized = value.replace("\\", "/") if normalize_separator else value
    if unicodedata.normalize("NFC", normalized) != normalized:
        raise ConsistencyManifestError(f"{label} must use NFC Unicode")
    if (
        normalized in {".", ".."}
        or normalized.startswith("/")
        or normalized.endswith("/")
        or "//" in normalized
        or ":" in normalized
        or "?" in normalized
        or "#" in normalized
    ):
        raise ConsistencyManifestError(f"{label} must be a safe project-relative path")
    parts = normalized.split("/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ConsistencyManifestError(f"{label} must be traversal-free")
    if any(part.endswith((".", " ")) for part in parts):
        raise ConsistencyManifestError(
            f"{label} must not contain trailing dots or spaces"
        )
    return path.as_posix()


def _member_paths(
    value: object,
    label: str,
    *,
    maximum: int,
    minimum: int,
) -> tuple[str, ...]:
    items = _sequence(value, label, maximum, minimum=minimum)
    paths = tuple(
        _safe_relative(item, f"{label}[{index}]")
        for index, item in enumerate(items)
    )
    keys = tuple(_portable_key(path) for path in paths)
    if len(set(keys)) != len(keys):
        raise ConsistencyManifestError(f"{label} contains portable path aliases")
    return tuple(sorted(paths, key=lambda path: (_portable_key(path), path)))


def _is_same_or_child(path_key: str, prefix_key: str) -> bool:
    return path_key == prefix_key or path_key.startswith(prefix_key + "/")


def _is_protected_path(path: str) -> bool:
    key = _portable_key(path)
    return any(
        _is_same_or_child(key, _portable_key(prefix))
        for prefix in _FORBIDDEN_PREFIX_KEYS
    )


def _validate_owned_path(path: str, owner: str, ownership: dict[str, str]) -> None:
    key = _portable_key(path)
    previous = ownership.get(key)
    if previous is not None:
        raise ConsistencyManifestError(
            f"path ownership is duplicated by {previous} and {owner}"
        )
    manifest_key = _portable_key(CONSISTENCY_MANIFEST_RELATIVE_PATH)
    if key == manifest_key:
        raise ConsistencyManifestError("manifest cannot reference itself")
    if _is_protected_path(path):
        raise ConsistencyManifestError(
            f"{path} is inside protected append-only or VCS evidence"
        )
    ownership[key] = owner


def _canonical_manifest_mapping(
    relationships: tuple[ConsistencyRelationship, ...],
) -> dict[str, object]:
    values: list[dict[str, object]] = []
    for relationship in relationships:
        if relationship.kind == "source_generated":
            values.append(
                {
                    "relationship_id": relationship.relationship_id,
                    "kind": relationship.kind,
                    "comparison": relationship.comparison,
                    "source_path": relationship.source_path,
                    "generated_paths": list(relationship.generated_paths),
                }
            )
        else:
            values.append(
                {
                    "relationship_id": relationship.relationship_id,
                    "kind": relationship.kind,
                    "comparison": relationship.comparison,
                    "paths": list(relationship.member_paths),
                }
            )
    return {
        "schema_version": CONSISTENCY_MANIFEST_SCHEMA_VERSION,
        "relationships": values,
    }


def parse_consistency_manifest(value: object) -> ConsistencyManifest:
    """Parse one in-memory manifest through the closed Version 1 contract."""
    mapping = _mapping(value, "consistency manifest")
    try:
        encoded = canonical_json_bytes(mapping)
    except (TypeError, ValueError, RecursionError) as error:
        raise ConsistencyManifestError(
            "consistency manifest is not canonical JSON data"
        ) from error
    if len(encoded) > _MAX_CANONICAL_BYTES:
        raise ConsistencyManifestError("consistency manifest exceeds the 1 MiB bound")
    _closed(mapping, _TOP_LEVEL_FIELDS, "consistency manifest")
    if mapping["schema_version"] != CONSISTENCY_MANIFEST_SCHEMA_VERSION:
        raise ConsistencyManifestError(
            "unsupported consistency manifest schema version"
        )

    raw_relationships = _sequence(
        mapping["relationships"],
        "relationships",
        _MAX_RELATIONSHIPS,
        minimum=1,
    )
    relationships: list[ConsistencyRelationship] = []
    relationship_ids: set[str] = set()
    ownership: dict[str, str] = {}
    for index, raw_relationship in enumerate(raw_relationships):
        label = f"relationships[{index}]"
        item = _mapping(raw_relationship, label)
        kind = item.get("kind")
        if kind == "source_generated":
            _closed(item, _SOURCE_GENERATED_FIELDS, label)
        elif kind == "cross_surface":
            _closed(item, _CROSS_SURFACE_FIELDS, label)
        else:
            raise ConsistencyManifestError(f"{label}.kind is unsupported")
        relationship_id = _stable_id(
            item["relationship_id"], f"{label}.relationship_id"
        )
        if relationship_id in relationship_ids:
            raise ConsistencyManifestError(
                "relationships contains duplicate relationship_id values"
            )
        relationship_ids.add(relationship_id)
        if item["comparison"] != "exact_bytes":
            raise ConsistencyManifestError(
                f"{label}.comparison must be exact_bytes"
            )

        if kind == "source_generated":
            source_path = _safe_relative(item["source_path"], f"{label}.source_path")
            generated_paths = _member_paths(
                item["generated_paths"],
                f"{label}.generated_paths",
                maximum=_MAX_RELATIONSHIP_MEMBERS - 1,
                minimum=_MIN_RELATIONSHIP_MEMBERS - 1,
            )
            member_paths = (source_path, *generated_paths)
            if len({_portable_key(path) for path in member_paths}) != len(member_paths):
                raise ConsistencyManifestError(
                    f"{label} contains portable path aliases"
                )
            relationship = ConsistencyRelationship(
                relationship_id=relationship_id,
                kind=kind,
                comparison="exact_bytes",
                member_paths=member_paths,
                source_path=source_path,
                generated_paths=generated_paths,
            )
        else:
            member_paths = _member_paths(
                item["paths"],
                f"{label}.paths",
                maximum=_MAX_RELATIONSHIP_MEMBERS,
                minimum=_MIN_RELATIONSHIP_MEMBERS,
            )
            relationship = ConsistencyRelationship(
                relationship_id=relationship_id,
                kind=kind,
                comparison="exact_bytes",
                member_paths=member_paths,
            )

        for path in relationship.member_paths:
            _validate_owned_path(path, relationship_id, ownership)
        relationships.append(relationship)

    if len(ownership) > _MAX_UNIQUE_PATHS:
        raise ConsistencyManifestError(
            f"consistency manifest exceeds its {_MAX_UNIQUE_PATHS}-path bound"
        )
    normalized_relationships = tuple(
        sorted(relationships, key=lambda relationship: relationship.relationship_id)
    )
    canonical_mapping = _canonical_manifest_mapping(normalized_relationships)
    return ConsistencyManifest(
        schema_version=CONSISTENCY_MANIFEST_SCHEMA_VERSION,
        relationships=normalized_relationships,
        manifest_sha256=digest(canonical_mapping),
    )


def consistency_manifest_bytes(manifest: ConsistencyManifest) -> bytes:
    """Serialize one parsed manifest to its unique canonical Version 1 bytes."""
    if not isinstance(manifest, ConsistencyManifest):
        raise TypeError("manifest must be a ConsistencyManifest")
    return canonical_json_bytes(_canonical_manifest_mapping(manifest.relationships))


def _reject_duplicate_object_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConsistencyManifestError(
                "consistency manifest contains duplicate object fields"
            )
        result[key] = value
    return result


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        info = os.lstat(path)
    except OSError as error:
        raise ConsistencyManifestError(
            "consistency member metadata cannot be inspected"
        ) from error
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse)


def _opened_final_path(handle, label: str) -> Path:
    """Resolve the final path bound to an already-open file descriptor."""
    if os.name == "nt":
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.GetFinalPathNameByHandleW
        function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        function.restype = ctypes.c_uint32
        native_handle = msvcrt.get_osfhandle(handle.fileno())
        required = function(native_handle, None, 0, 0)
        if required == 0:
            raise ConsistencyManifestError(f"{label} final path cannot be resolved")
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = function(native_handle, buffer, len(buffer), 0)
        if written == 0 or written >= len(buffer):
            raise ConsistencyManifestError(f"{label} final path cannot be resolved")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)

    descriptor = handle.fileno()
    for link in (Path("/proc/self/fd") / str(descriptor), Path("/dev/fd") / str(descriptor)):
        try:
            if os.path.lexists(link):
                return Path(os.readlink(link)).resolve(strict=True)
        except OSError:
            continue
    raise ConsistencyManifestError(f"{label} final path cannot be resolved")


def _validate_opened_path(
    handle,
    *,
    project_root: Path,
    expected: Path,
    expected_relative: str,
    label: str,
    allow_manifest: bool = False,
) -> Path:
    final_path = _opened_final_path(handle, label)
    try:
        final_path = final_path.resolve(strict=True)
    except OSError as error:
        raise ConsistencyManifestError(f"{label} final path cannot be resolved") from error
    if not final_path.is_relative_to(project_root):
        raise ConsistencyManifestError(f"{label} escapes the project root")
    if os.path.normcase(str(final_path)) != os.path.normcase(str(expected)):
        raise ConsistencyManifestError(f"{label} uses a symlink, reparse point, or filesystem alias")
    final_relative = final_path.relative_to(project_root).as_posix()
    if _portable_key(final_relative) != _portable_key(expected_relative):
        raise ConsistencyManifestError(f"{label} final path does not match its declaration")
    if not allow_manifest and _is_protected_path(final_relative):
        raise ConsistencyManifestError(f"{label} resolves inside protected append-only or VCS evidence")
    return final_path


def _open_read_handle(path: Path, label: str):
    flags = os.O_RDONLY
    for name in ("O_BINARY", "O_CLOEXEC", "O_NONBLOCK", "O_NOFOLLOW"):
        flags |= getattr(os, name, 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ConsistencyManifestError(f"{label} cannot be opened safely") from error
    try:
        return os.fdopen(descriptor, "rb", closefd=True)
    except Exception:
        os.close(descriptor)
        raise


def _regular_manifest_path(project_root: Path) -> Path:
    path = project_root / CONSISTENCY_MANIFEST_RELATIVE_PATH
    if not os.path.lexists(path):
        return path
    current = project_root
    for part in PurePosixPath(CONSISTENCY_MANIFEST_RELATIVE_PATH).parts:
        current = current / part
        if not os.path.lexists(current):
            break
        if _is_link_or_reparse(current):
            raise ConsistencyManifestError(
                "consistency manifest path contains a symlink or reparse point"
            )
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ConsistencyManifestError(
            "consistency manifest path cannot be resolved"
        ) from error
    if not resolved.is_relative_to(project_root) or not path.is_file():
        raise ConsistencyManifestError(
            "consistency manifest must be a regular project file"
        )
    return path


def load_consistency_manifest(root: str | Path) -> ConsistencyManifest | None:
    """Load the optional canonical manifest without modifying the project."""
    try:
        project_root = Path(root).resolve(strict=True)
    except OSError as error:
        raise ConsistencyManifestError(
            "consistency manifest root cannot be resolved"
        ) from error
    if not project_root.is_dir():
        raise ConsistencyManifestError(
            "consistency manifest root must be a directory"
        )
    path = _regular_manifest_path(project_root)
    if not os.path.lexists(path):
        return None
    try:
        with _open_read_handle(path, "consistency manifest") as handle:
            _validate_opened_path(
                handle,
                project_root=project_root,
                expected=path,
                expected_relative=CONSISTENCY_MANIFEST_RELATIVE_PATH,
                label="consistency manifest",
                allow_manifest=True,
            )
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ConsistencyManifestError(
                    "consistency manifest must be a regular project file"
                )
            if opened.st_size > _MAX_CANONICAL_BYTES:
                raise ConsistencyManifestError(
                    "consistency manifest exceeds the 1 MiB bound"
                )
            if opened.st_size <= 0:
                raise ConsistencyManifestError(
                    "consistency manifest has invalid bounded bytes"
                )
            payload = handle.read(_MAX_CANONICAL_BYTES + 1)
            if len(payload) > _MAX_CANONICAL_BYTES:
                raise ConsistencyManifestError(
                    "consistency manifest exceeds the 1 MiB bound"
                )
            after = os.fstat(handle.fileno())
            if len(payload) != after.st_size or any(
                getattr(opened, field, None) != getattr(after, field, None)
                for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            ):
                raise ConsistencyManifestError(
                    "consistency manifest changed during evaluation"
                )
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_object_fields
        )
    except ConsistencyManifestError:
        raise
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise ConsistencyManifestError(
            "consistency manifest is not valid UTF-8 JSON"
        ) from error
    manifest = parse_consistency_manifest(value)
    try:
        if consistency_manifest_bytes(manifest) != payload:
            raise ConsistencyManifestError(
                "consistency manifest JSON is not canonical"
            )
    except (TypeError, ValueError) as error:
        raise ConsistencyManifestError(
            "consistency manifest JSON is not canonical"
        ) from error
    return manifest


def _member_candidate(project_root: Path, relative: str) -> Path | None:
    candidate = project_root.joinpath(*PurePosixPath(relative).parts)
    current = project_root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        if not os.path.lexists(current):
            return None
        if _is_link_or_reparse(current):
            raise ConsistencyManifestError(
                f"consistency member path is a symlink or reparse traversal: {relative}"
            )
        try:
            info = current.stat(follow_symlinks=False)
        except OSError as error:
            raise ConsistencyManifestError(
                f"consistency member metadata cannot be read: {relative}"
            ) from error
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ConsistencyManifestError(
                f"consistency member parent is not a directory: {relative}"
            )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ConsistencyManifestError(
            f"consistency member path cannot be resolved: {relative}"
        ) from error
    if not resolved.is_relative_to(project_root):
        raise ConsistencyManifestError(
            f"consistency member escapes the project root: {relative}"
        )
    return candidate


def _read_member(
    path: Path,
    relative: str,
    *,
    project_root: Path,
    aggregate_before: int,
) -> tuple[ConsistencyMemberEvaluation, os.stat_result, int]:
    content_hash = hashlib.sha256()
    size = 0
    try:
        with _open_read_handle(path, f"consistency member {relative}") as handle:
            _validate_opened_path(
                handle,
                project_root=project_root,
                expected=path,
                expected_relative=relative,
                label=f"consistency member {relative}",
            )
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ConsistencyManifestError(
                    f"consistency member is not a regular file: {relative}"
                )
            if opened.st_size > _MAX_MEMBER_BYTES:
                raise ConsistencyManifestError(
                    f"consistency member exceeds the 8 MiB bound: {relative}"
                )
            if aggregate_before + opened.st_size > _MAX_AGGREGATE_BYTES:
                raise ConsistencyManifestError(
                    "consistency members exceed the 64 MiB aggregate bound"
                )
            for chunk in iter(lambda: handle.read(_READ_CHUNK_BYTES), b""):
                size += len(chunk)
                if size > _MAX_MEMBER_BYTES:
                    raise ConsistencyManifestError(
                        f"consistency member exceeds the 8 MiB bound: {relative}"
                    )
                if aggregate_before + size > _MAX_AGGREGATE_BYTES:
                    raise ConsistencyManifestError(
                        "consistency members exceed the 64 MiB aggregate bound"
                    )
                content_hash.update(chunk)
    except ConsistencyManifestError:
        raise
    except OSError as error:
        raise ConsistencyManifestError(
            f"consistency member cannot be read: {relative}"
        ) from error
    try:
        after = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ConsistencyManifestError(
            f"consistency member changed during evaluation: {relative}"
        ) from error
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if size != opened.st_size or any(
        getattr(opened, field, None) != getattr(after, field, None)
        for field in stable_fields
    ):
        raise ConsistencyManifestError(
            f"consistency member changed during evaluation: {relative}"
        )
    return (
        ConsistencyMemberEvaluation(
            path=relative,
            status="present",
            size_bytes=size,
            sha256=content_hash.hexdigest(),
        ),
        after,
        size,
    )


def evaluate_consistency_manifest(
    root: str | Path,
    manifest: ConsistencyManifest,
) -> ConsistencyManifestEvaluation:
    """Evaluate all manifest members read-only using bounded streaming hashes."""
    if not isinstance(manifest, ConsistencyManifest):
        raise TypeError("manifest must be a ConsistencyManifest")
    try:
        project_root = Path(root).resolve(strict=True)
    except OSError as error:
        raise ConsistencyManifestError(
            "consistency manifest root cannot be resolved"
        ) from error
    if not project_root.is_dir():
        raise ConsistencyManifestError(
            "consistency manifest root must be a directory"
        )

    aggregate_bytes = 0
    identities: dict[tuple[int, int], str] = {}
    evaluated: dict[str, ConsistencyMemberEvaluation] = {}
    for relative in manifest.paths:
        candidate = _member_candidate(project_root, relative)
        if candidate is None:
            evaluated[relative] = ConsistencyMemberEvaluation(
                path=relative,
                status="missing",
            )
            continue
        member, info, size = _read_member(
            candidate,
            relative,
            project_root=project_root,
            aggregate_before=aggregate_bytes,
        )
        identity = (info.st_dev, info.st_ino)
        previous = identities.get(identity)
        if previous is not None:
            raise ConsistencyManifestError(
                f"consistency members are same-file hardlink aliases: {previous}, {relative}"
            )
        identities[identity] = relative
        evaluated[relative] = member
        aggregate_bytes += size

    results: list[ConsistencyRelationshipEvaluation] = []
    for relationship in manifest.relationships:
        members = tuple(evaluated[path] for path in relationship.member_paths)
        missing = any(not member.present for member in members)
        distinct = {
            (member.size_bytes, member.sha256)
            for member in members
            if member.present
        }
        status_value = "missing" if missing else "pass" if len(distinct) == 1 else "drift"
        results.append(
            ConsistencyRelationshipEvaluation(
                relationship_id=relationship.relationship_id,
                kind=relationship.kind,
                comparison=relationship.comparison,
                status=status_value,
                members=members,
            )
        )
    return ConsistencyManifestEvaluation(
        manifest_sha256=manifest.digest,
        relationships=tuple(results),
        aggregate_bytes=aggregate_bytes,
    )


def _path_contains(path: str, member: str) -> bool:
    path_key = _portable_key(path)
    member_key = _portable_key(member)
    return path_key == member_key or member_key.startswith(path_key + "/")


def consistency_manifest_impact(
    manifest: ConsistencyManifest,
    changed_paths: Sequence[str],
    *,
    evaluation: ConsistencyManifestEvaluation | None = None,
) -> Mapping[str, object]:
    """Return evidence for relationships touched by exact or ancestor paths."""
    if not isinstance(manifest, ConsistencyManifest):
        raise TypeError("manifest must be a ConsistencyManifest")
    if evaluation is not None:
        if not isinstance(evaluation, ConsistencyManifestEvaluation):
            raise TypeError("evaluation must be a ConsistencyManifestEvaluation")
        if evaluation.manifest_sha256 != manifest.digest:
            raise ConsistencyManifestError(
                "consistency evaluation does not match the manifest"
            )
    raw_paths = _sequence(changed_paths, "changed_paths", _MAX_CHANGED_PATHS)
    paths = tuple(
        sorted(
            {
                _safe_relative(
                    value,
                    f"changed_paths[{index}]",
                    normalize_separator=True,
                )
                for index, value in enumerate(raw_paths)
            },
            key=lambda path: (_portable_key(path), path),
        )
    )
    status_by_id = (
        {
            relationship.relationship_id: relationship.status
            for relationship in evaluation.relationships
        }
        if evaluation is not None
        else {}
    )
    affected: list[dict[str, object]] = []
    all_endpoints: set[str] = set()
    all_omitted: set[str] = set()
    for relationship in manifest.relationships:
        endpoint_matches = {
            endpoint: tuple(
                path for path in paths if _path_contains(path, endpoint)
            )
            for endpoint in relationship.member_paths
        }
        endpoints = tuple(
            endpoint for endpoint, matches in endpoint_matches.items() if matches
        )
        if not endpoints:
            continue
        matched_paths = endpoints
        omitted = tuple(
            endpoint
            for endpoint in relationship.member_paths
            if endpoint not in endpoints
        )
        all_endpoints.update(relationship.member_paths)
        all_omitted.update(omitted)
        affected.append(
            {
                "relationship_id": relationship.relationship_id,
                "kind": relationship.kind,
                "comparison": relationship.comparison,
                "matched_paths": matched_paths,
                "endpoints": endpoints,
                "omitted_counterparts": omitted,
                "status": status_by_id.get(
                    relationship.relationship_id, "not_evaluated"
                ),
            }
        )
    return {
        "manifest_sha256": manifest.digest,
        "relationship_count": manifest.relationship_count,
        "affected_relationship_ids": tuple(
            item["relationship_id"] for item in affected
        ),
        "affected_endpoints": tuple(
            sorted(all_endpoints, key=lambda path: (_portable_key(path), path))
        ),
        "omitted_counterparts": tuple(
            sorted(all_omitted, key=lambda path: (_portable_key(path), path))
        ),
        "relationships": tuple(affected),
    }


__all__ = [
    "CONSISTENCY_MANIFEST_RELATIVE_PATH",
    "CONSISTENCY_MANIFEST_SCHEMA_VERSION",
    "ConsistencyManifest",
    "ConsistencyManifestError",
    "ConsistencyManifestEvaluation",
    "ConsistencyMemberEvaluation",
    "ConsistencyRelationship",
    "ConsistencyRelationshipEvaluation",
    "consistency_manifest_bytes",
    "consistency_manifest_impact",
    "evaluate_consistency_manifest",
    "load_consistency_manifest",
    "parse_consistency_manifest",
]
