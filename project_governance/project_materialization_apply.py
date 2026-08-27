"""Bounded P3-E materialization apply transactions.

P3-D remains an immutable preview. This module adds a separate transaction
controller that can classify an action, verify a supplied physical root and
manifest payload, capture compare-and-swap pre-state, apply bounded files, and
roll back only when post-state still matches the transaction record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .authorization_session import AuthorizationSession
from .project_materialization import (
    PreviewState,
    ProjectMaterializationError,
    ProjectMaterializationPreview,
    parse_project_materialization_preview,
    render_project_materialization_preview,
)
from .storage import SchemaError, canonical_json_bytes, digest


P3E_SCHEMA_VERSION = "1.0"
MAX_TRANSACTION_FILES = 256
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
_CODE_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SENSITIVE_RE = re.compile(
    rb"(?:sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|"
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?" + rb"PRIVATE " + rb"KEY-----)",
    re.IGNORECASE,
)
_REPARSE_POINT = 0x400


class MaterializationApplyError(ValueError):
    """Raised for malformed or unsafe P3-E input."""


class AuthorizationClass(Enum):
    AUTO = "auto"
    RECOMMEND = "recommend"
    CONFIRM = "confirm"
    BLOCK = "block"


class ApplyState(Enum):
    READY = "ready"
    RECOMMEND = "recommend"
    PENDING_USER_INPUT = "pending-user-input"
    BLOCK = "block"
    APPLIED = "applied"
    ROLLED_BACK = "rolled-back"


@dataclass(frozen=True)
class ActionContext:
    """Facts used to decide whether a transaction may self-authorize."""

    policy_sha256: str
    evidence_refs: tuple[str, ...]
    bounded_scope: bool = True
    reversible: bool = True
    no_secret_values: bool = True
    no_network: bool = True
    no_cost: bool = True
    no_credentials: bool = True
    no_real_data: bool = True
    public_delivery: bool = False
    irreversible: bool = False
    security_change: bool = False
    privacy_change: bool = False
    materially_ambiguous: bool = False
    recommendation_only: bool = False
    runtime_launch: bool = False
    deployment: bool = False

    def __post_init__(self) -> None:
        if type(self) is not ActionContext:
            raise MaterializationApplyError("ActionContext subclasses are not accepted")
        _digest(self.policy_sha256, "policy_sha256")
        if type(self.evidence_refs) is not tuple or not self.evidence_refs:
            raise MaterializationApplyError("evidence_refs must be a non-empty tuple")
        evidence = tuple(_code(item, "evidence_ref") for item in self.evidence_refs)
        if evidence != tuple(sorted(set(evidence))):
            raise MaterializationApplyError("evidence_refs must be canonical")
        for name, value in vars(self).items():
            if name in ("policy_sha256", "evidence_refs"):
                continue
            if type(value) is not bool:
                raise MaterializationApplyError(f"{name} must be a boolean")


@dataclass(frozen=True)
class AuthorizationAssessment:
    classification: AuthorizationClass
    reason_codes: tuple[str, ...]
    requires_owner_approval: bool

    def __post_init__(self) -> None:
        if type(self) is not AuthorizationAssessment:
            raise MaterializationApplyError(
                "AuthorizationAssessment subclasses are not accepted"
            )
        if type(self.classification) is not AuthorizationClass:
            raise MaterializationApplyError("classification must be an AuthorizationClass")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise MaterializationApplyError("reason_codes must be canonical")
        if type(self.requires_owner_approval) is not bool:
            raise MaterializationApplyError("requires_owner_approval must be boolean")


@dataclass(frozen=True)
class TransactionApproval:
    approval_id: str
    actor: str
    role: str
    transaction_id: str
    preview_sha256: str
    physical_root_sha256: str
    scope: tuple[str, ...]
    timestamp_utc: str

    def __post_init__(self) -> None:
        _code(self.approval_id, "approval_id")
        _text(self.actor, "actor", 128)
        _code(self.role, "role")
        _code(self.transaction_id, "transaction_id")
        _digest(self.preview_sha256, "preview_sha256")
        _digest(self.physical_root_sha256, "physical_root_sha256")
        if type(self.scope) is not tuple or not self.scope:
            raise MaterializationApplyError("scope must be a non-empty tuple")
        normalized = tuple(_path(item, "scope entry") for item in self.scope if item != ".")
        if "." in self.scope:
            normalized = (".",) + normalized
        if self.scope != tuple(sorted(set(normalized))):
            raise MaterializationApplyError("scope must be canonical")
        _timestamp(self.timestamp_utc, "timestamp_utc")


@dataclass(frozen=True)
class MaterializationFile:
    path: str
    content: bytes

    def __post_init__(self) -> None:
        _path(self.path, "file.path")
        if not isinstance(self.content, bytes):
            raise MaterializationApplyError("file.content must be bytes")
        if len(self.content) > MAX_FILE_BYTES:
            raise MaterializationApplyError("file.content exceeds the bounded byte limit")
        if _SENSITIVE_RE.search(self.content):
            raise MaterializationApplyError("file.content contains a sensitive-value pattern")

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class PreStateEntry:
    path: str
    expected_sha256: str | None
    observed_sha256: str | None
    existed: bool

    def __post_init__(self) -> None:
        _path(self.path, "pre_state.path")
        if self.expected_sha256 is not None:
            _digest(self.expected_sha256, "pre_state.expected_sha256")
        if self.observed_sha256 is not None:
            _digest(self.observed_sha256, "pre_state.observed_sha256")
        if type(self.existed) is not bool:
            raise MaterializationApplyError("pre_state.existed must be boolean")
        if self.existed != (self.observed_sha256 is not None):
            raise MaterializationApplyError("pre_state existence does not match observed hash")


@dataclass(frozen=True)
class MaterializationApplyPlan:
    transaction_id: str
    preview_id: str
    preview_sha256: str
    preview: ProjectMaterializationPreview
    physical_root: Path | None
    physical_root_sha256: str | None
    files: tuple[MaterializationFile, ...]
    assessment: AuthorizationAssessment
    state: ApplyState
    blocker_codes: tuple[str, ...]
    pre_state: tuple[PreStateEntry, ...]

    def __post_init__(self) -> None:
        _code(self.transaction_id, "transaction_id")
        _code(self.preview_id, "preview_id")
        _digest(self.preview_sha256, "preview_sha256")
        if type(self.preview) is not ProjectMaterializationPreview:
            raise MaterializationApplyError("preview must be an exact P3-D preview")
        if self.preview_id != self.preview.preview_id:
            raise MaterializationApplyError("preview_id does not bind preview")
        if self.physical_root is not None and not isinstance(self.physical_root, Path):
            raise MaterializationApplyError("physical_root must be a Path or null")
        if self.physical_root_sha256 is not None:
            _digest(self.physical_root_sha256, "physical_root_sha256")
        if (self.physical_root is None) != (self.physical_root_sha256 is None):
            raise MaterializationApplyError("physical_root and its digest must agree")
        if (
            self.physical_root is not None
            and self.physical_root_sha256 != _root_fingerprint(self.physical_root)
        ):
            raise MaterializationApplyError("physical_root digest does not bind root")
        _canonical_files(self.files)
        if self.blocker_codes != tuple(sorted(set(self.blocker_codes))):
            raise MaterializationApplyError("blocker_codes must be canonical")
        if tuple(item.path for item in self.pre_state) != tuple(
            item.path for item in sorted(self.pre_state, key=lambda value: value.path)
        ):
            raise MaterializationApplyError("pre_state must be canonical")


@dataclass(frozen=True)
class MaterializationApplyResult:
    transaction_id: str
    preview_id: str
    preview_sha256: str
    physical_root_sha256: str | None
    state: ApplyState
    authorization: AuthorizationAssessment
    blocker_codes: tuple[str, ...]
    changed_paths: tuple[str, ...]
    pre_state: tuple[PreStateEntry, ...]
    post_state: tuple[PreStateEntry, ...]
    snapshot_ref: str | None
    rollback_id: str
    physical_root: Path | None = None

    def __post_init__(self) -> None:
        _code(self.transaction_id, "transaction_id")
        _code(self.preview_id, "preview_id")
        _digest(self.preview_sha256, "preview_sha256")
        if self.physical_root_sha256 is not None:
            _digest(self.physical_root_sha256, "physical_root_sha256")
        if (self.physical_root is None) != (self.physical_root_sha256 is None):
            raise MaterializationApplyError("physical_root and its digest must agree")
        if (
            self.physical_root is not None
            and self.physical_root_sha256 != _root_fingerprint(self.physical_root)
        ):
            raise MaterializationApplyError("physical_root digest does not bind root")
        if type(self.state) is not ApplyState:
            raise MaterializationApplyError("state must be an ApplyState")
        if self.changed_paths != tuple(sorted(set(self.changed_paths))):
            raise MaterializationApplyError("changed_paths must be canonical")
        _code(self.rollback_id, "rollback_id")
        if self.snapshot_ref is not None:
            _text(self.snapshot_ref, "snapshot_ref", 1024)


@dataclass(frozen=True)
class MaterializationRollbackResult:
    transaction_id: str
    rollback_id: str
    state: ApplyState
    blocker_codes: tuple[str, ...]
    restored_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        _code(self.transaction_id, "transaction_id")
        _code(self.rollback_id, "rollback_id")
        if self.state not in (ApplyState.ROLLED_BACK, ApplyState.BLOCK):
            raise MaterializationApplyError("rollback state must be rolled-back or block")
        if self.restored_paths != tuple(sorted(set(self.restored_paths))):
            raise MaterializationApplyError("restored_paths must be canonical")


def _text(value: object, label: str, maximum: int = 256) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise MaterializationApplyError(f"{label} must be bounded non-empty text")
    if value != unicodedata.normalize("NFC", value):
        raise MaterializationApplyError(f"{label} must use NFC Unicode")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise MaterializationApplyError(f"{label} contains control characters")
    if _SENSITIVE_RE.search(value.encode("utf-8")):
        raise MaterializationApplyError(f"{label} contains a sensitive-value pattern")
    return value


def _code(value: object, label: str) -> str:
    value = _text(value, label, 128)
    if not _CODE_RE.fullmatch(value):
        raise MaterializationApplyError(f"{label} must be a bounded stable code")
    return value


def _digest(value: object, label: str) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise MaterializationApplyError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _path(value: object, label: str) -> str:
    value = _text(value, label, 240)
    if "\\" in value or value.startswith("/") or ":" in value:
        raise MaterializationApplyError(f"{label} must be relative slash-separated")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise MaterializationApplyError(f"{label} contains an unsafe path segment")
    return value


def _timestamp(value: object, label: str) -> str:
    if type(value) is not str:
        raise MaterializationApplyError(f"{label} must be a UTC timestamp")
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MaterializationApplyError(f"{label} must be a valid UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise MaterializationApplyError(f"{label} must use UTC")
    if parsed > datetime.now(timezone.utc):
        raise MaterializationApplyError(f"{label} cannot be in the future")
    return value


def assess_action(context: ActionContext) -> AuthorizationAssessment:
    """Classify a path without asking the user to approve safe routine work."""

    blockers: list[str] = []
    if not context.bounded_scope:
        blockers.append("scope-unbounded")
    if not context.no_secret_values:
        blockers.append("secret-safety-required")
    if not context.reversible and not context.irreversible:
        blockers.append("reversibility-unknown")
    if blockers:
        return AuthorizationAssessment(AuthorizationClass.BLOCK, tuple(sorted(blockers)), False)

    confirmation_reasons: list[str] = []
    if not context.no_network:
        confirmation_reasons.append("network-access")
    if not context.no_cost:
        confirmation_reasons.append("cost-or-quota")
    if not context.no_credentials:
        confirmation_reasons.append("credentials-or-provider")
    if not context.no_real_data:
        confirmation_reasons.append("real-or-production-data")
    if context.public_delivery:
        confirmation_reasons.append("public-delivery")
    if context.irreversible:
        confirmation_reasons.append("irreversible-change")
    if context.security_change:
        confirmation_reasons.append("security-posture-change")
    if context.privacy_change:
        confirmation_reasons.append("privacy-posture-change")
    if context.materially_ambiguous:
        confirmation_reasons.append("materially-ambiguous-direction")
    if context.runtime_launch:
        confirmation_reasons.append("runtime-launch")
    if context.deployment:
        confirmation_reasons.append("deployment")
    if confirmation_reasons:
        reasons = tuple(sorted(set(confirmation_reasons)))
        return AuthorizationAssessment(AuthorizationClass.CONFIRM, reasons, True)
    if context.recommendation_only:
        return AuthorizationAssessment(
            AuthorizationClass.RECOMMEND, ("recommendation-only",), False
        )
    return AuthorizationAssessment(AuthorizationClass.AUTO, ("bounded-safe-path",), False)


def _canonical_files(files: Sequence[MaterializationFile]) -> tuple[MaterializationFile, ...]:
    if not isinstance(files, (tuple, list)) or len(files) > MAX_TRANSACTION_FILES:
        raise MaterializationApplyError("files must be a bounded sequence")
    values = tuple(files)
    if any(type(item) is not MaterializationFile for item in values):
        raise MaterializationApplyError("files must contain exact MaterializationFile records")
    paths = tuple(item.path for item in values)
    if paths != tuple(sorted(set(paths))):
        raise MaterializationApplyError("files must use canonical unique path order")
    total = sum(len(item.content) for item in values)
    if total > MAX_TOTAL_BYTES:
        raise MaterializationApplyError("files exceed the total byte limit")
    return values


def _is_link_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _physical_root(value: str | Path | None) -> Path:
    if value is None:
        raise MaterializationApplyError("physical_root is required")
    root = Path(value)
    if not root.is_absolute():
        raise MaterializationApplyError("physical_root must be absolute")
    resolved = root.resolve(strict=True)
    if not resolved.is_dir() or _is_link_or_reparse(root):
        raise MaterializationApplyError("physical_root must be a regular directory")
    # Windows may expose the same directory through an 8.3 short name.
    try:
        same_directory = os.path.samefile(root, resolved)
    except OSError as error:
        raise MaterializationApplyError("physical_root identity cannot be verified") from error
    if not same_directory:
        raise MaterializationApplyError("physical_root must not resolve through a link")
    return resolved


def _resolve_child(root: Path, relative: str) -> Path:
    _path(relative, "path")
    candidate = root.joinpath(*relative.split("/"))
    current = root
    for part in relative.split("/"):
        current = current / part
        if os.path.lexists(current) and _is_link_or_reparse(current):
            raise MaterializationApplyError(f"path traverses a link or reparse point: {relative}")
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise MaterializationApplyError(f"path escapes physical_root: {relative}")
    return resolved


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_pre_state(
    root: Path,
    baseline: Sequence[Mapping[str, object]],
) -> tuple[tuple[PreStateEntry, ...], dict[str, bytes | None], tuple[str, ...]]:
    entries: list[PreStateEntry] = []
    originals: dict[str, bytes | None] = {}
    mismatches: list[str] = []
    for item in baseline:
        path = _path(item["path"], "baseline.path")
        expected = item["expected_sha256"]
        if expected is not None:
            _digest(expected, "baseline.expected_sha256")
        target = _resolve_child(root, path)
        if os.path.lexists(target):
            if not target.is_file() or _is_link_or_reparse(target):
                raise MaterializationApplyError(f"baseline path is not a regular file: {path}")
            content = target.read_bytes()
            observed = _sha256_bytes(content)
            originals[path] = content
        else:
            observed = None
            originals[path] = None
        entries.append(PreStateEntry(path, expected, observed, observed is not None))
        if observed != expected:
            mismatches.append(path)
    return tuple(sorted(entries, key=lambda item: item.path)), originals, tuple(sorted(mismatches))


def _approval_covers(
    approval: TransactionApproval,
    paths: Sequence[str],
    *,
    transaction_id: str,
    preview_sha256: str,
    physical_root_sha256: str,
) -> bool:
    if (
        approval.role != "owner"
        or approval.transaction_id != transaction_id
        or approval.preview_sha256 != preview_sha256
        or approval.physical_root_sha256 != physical_root_sha256
    ):
        return False
    if "." in approval.scope:
        return True
    return all(
        any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in approval.scope)
        for path in paths
    )


def _preview_sha256(preview: ProjectMaterializationPreview) -> str:
    return digest(render_project_materialization_preview(preview))


def _root_fingerprint(root: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()


def plan_materialization_apply(
    preview_payload: bytes | bytearray | memoryview,
    physical_root: str | Path | None,
    files: Sequence[MaterializationFile],
    *,
    transaction_id: str,
    context: ActionContext | None = None,
    approval: TransactionApproval | None = None,
    authorization_session: AuthorizationSession | None = None,
) -> MaterializationApplyPlan:
    """Validate an apply without writing the root or snapshot evidence."""

    _code(transaction_id, "transaction_id")
    try:
        preview = parse_project_materialization_preview(preview_payload)
    except (ProjectMaterializationError, TypeError, ValueError) as error:
        raise MaterializationApplyError("P3-D preview is invalid") from error
    values = _canonical_files(files)
    preview_digest = _preview_sha256(preview)
    if context is None:
        assessment = AuthorizationAssessment(
            AuthorizationClass.BLOCK, ("action-context-required",), False
        )
    else:
        assessment = assess_action(context)
    root: Path | None = None
    root_sha256: str | None = None
    blockers: list[str] = []
    pre_state: tuple[PreStateEntry, ...] = ()
    if preview.state is not PreviewState.PREVIEW_READY:
        blockers.append("preview-not-ready")
    manifest = tuple(item.path for item in preview.proposal.manifest_entries)
    if tuple(item.path for item in values) != manifest:
        blockers.append("manifest-content-set-mismatch")
    for item, manifest_item in zip(values, preview.proposal.manifest_entries):
        if item.content_sha256 != manifest_item.content_sha256:
            blockers.append(f"content-digest-mismatch:{item.path}")
    if assessment.classification is AuthorizationClass.BLOCK:
        blockers.extend(assessment.reason_codes)
    if context is not None and context.policy_sha256 != preview.proposal.policy_sha256:
        blockers.append("policy-digest-mismatch")
    if assessment.classification is AuthorizationClass.RECOMMEND:
        blockers.append("recommendation-only")
    if not blockers:
        try:
            root = _physical_root(physical_root)
            root_sha256 = _root_fingerprint(root)
            baseline = tuple(
                {"path": item.path, "expected_sha256": item.expected_sha256}
                for item in preview.proposal.baseline_entries
            )
            pre_state, _, mismatches = _read_pre_state(root, baseline)
            if mismatches:
                blockers.append("pre-state-mismatch")
        except (MaterializationApplyError, OSError) as error:
            blockers.append(_reason_code(str(error)))
    if authorization_session is not None and type(authorization_session) is not AuthorizationSession:
        raise TypeError("authorization_session must be an exact AuthorizationSession")
    if assessment.classification is AuthorizationClass.CONFIRM:
        if approval is not None:
            if root_sha256 is None or not _approval_covers(
                approval,
                manifest,
                transaction_id=transaction_id,
                preview_sha256=preview_digest,
                physical_root_sha256=root_sha256,
            ):
                blockers.append("approval-binding-mismatch")
        elif authorization_session is None:
            blockers.append("owner-approval-required")
        elif root_sha256 is None or context is None:
            blockers.append("authorization-session-binding-mismatch")
        elif authorization_session.lifecycle_run_id is not None:
            blockers.append("authorization-session-binding-mismatch")
        elif not authorization_session.covers(
            transaction_id=transaction_id,
            policy_sha256=context.policy_sha256,
            source_sha256=preview_digest,
            preimage_sha256=root_sha256,
            scope=manifest,
            reason_codes=assessment.reason_codes,
        ):
            blockers.append("authorization-session-binding-mismatch")
    blockers = sorted(set(blockers))
    if not blockers:
        state = ApplyState.READY
    elif blockers == ["recommendation-only"]:
        state = ApplyState.RECOMMEND
    elif blockers == ["owner-approval-required"]:
        state = ApplyState.PENDING_USER_INPUT
    else:
        state = ApplyState.BLOCK
    return MaterializationApplyPlan(
        transaction_id=transaction_id,
        preview_id=preview.preview_id,
        preview_sha256=preview_digest,
        preview=preview,
        physical_root=root,
        physical_root_sha256=root_sha256,
        files=values,
        assessment=assessment,
        state=state,
        blocker_codes=tuple(blockers),
        pre_state=pre_state,
    )


def _reason_code(message: str) -> str:
    lowered = message.casefold()
    if "physical_root" in lowered:
        return "physical-root-invalid"
    if "link" in lowered or "reparse" in lowered:
        return "link-or-reparse-point"
    if "escape" in lowered:
        return "root-escape"
    return "pre-state-unreadable"


def _snapshot_directory(snapshot_root: str | Path | None, transaction_id: str, physical_root: Path) -> Path:
    base = Path(snapshot_root) if snapshot_root is not None else Path(tempfile.gettempdir()) / "apg-p3e-snapshots"
    if not base.is_absolute():
        raise MaterializationApplyError("snapshot_root must be absolute")
    base = base.resolve(strict=False)
    if base == physical_root or base.is_relative_to(physical_root):
        raise MaterializationApplyError("snapshot_root must not be inside physical_root")
    target = base / transaction_id
    if os.path.lexists(target):
        raise MaterializationApplyError("snapshot transaction directory already exists")
    return target


def _persist_snapshot(
    snapshot_dir: Path,
    transaction_id: str,
    preview_id: str,
    physical_root_sha256: str,
    pre_state: Sequence[PreStateEntry],
    originals: Mapping[str, bytes | None],
) -> None:
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    files_root = snapshot_dir / "files"
    for path, content in originals.items():
        if content is None:
            continue
        target = files_root.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    manifest = {
        "schema_version": P3E_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "preview_id": preview_id,
        "physical_root_sha256": physical_root_sha256,
        "pre_state": [
            {
                "expected_sha256": item.expected_sha256,
                "existed": item.existed,
                "observed_sha256": item.observed_sha256,
                "path": item.path,
            }
            for item in pre_state
        ],
        "status": "prepared",
    }
    (snapshot_dir / "transaction.json").write_bytes(canonical_json_bytes(manifest))


def _update_snapshot_status(snapshot_dir: Path, status: str) -> None:
    path = snapshot_dir / "transaction.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["status"] = status
    path.write_bytes(canonical_json_bytes(value))


def _snapshot_is_bound(
    snapshot_dir: Path,
    result: MaterializationApplyResult,
) -> bool:
    path = snapshot_dir / "transaction.json"
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
        if canonical_json_bytes(value) != raw:
            return False
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SchemaError):
        return False
    return (
        set(value)
        == {"physical_root_sha256", "pre_state", "preview_id", "schema_version", "status", "transaction_id"}
        and value["schema_version"] == P3E_SCHEMA_VERSION
        and value["transaction_id"] == result.transaction_id
        and value["preview_id"] == result.preview_id
        and value["physical_root_sha256"] == result.physical_root_sha256
        and value["status"] == "applied"
    )


def _observed_hash(target: Path) -> str | None:
    if not os.path.lexists(target):
        return None
    if not target.is_file() or _is_link_or_reparse(target):
        raise MaterializationApplyError("transaction path is not a regular file")
    return _sha256_bytes(target.read_bytes())


def _write_files(
    root: Path,
    files: Sequence[MaterializationFile],
    transaction_id: str,
    pre_state: Sequence[PreStateEntry],
) -> None:
    expected = {item.path: item.observed_sha256 for item in pre_state}
    temporary: list[tuple[str, Path, Path]] = []
    try:
        for item in files:
            target = _resolve_child(root, item.path)
            if _observed_hash(target) != expected[item.path]:
                raise MaterializationApplyError(f"pre-state drift before write: {item.path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            staged = target.with_name(f".{target.name}.apg-p3e-{transaction_id}.tmp")
            staged.write_bytes(item.content)
            temporary.append((item.path, staged, target))
        if any(_observed_hash(target) != expected[path] for path, _, target in temporary):
            raise MaterializationApplyError("pre-state drift before commit")
        for _, staged, target in temporary:
            os.replace(staged, target)
    finally:
        for _, staged, _ in temporary:
            staged.unlink(missing_ok=True)


def execute_materialization_apply(
    plan: MaterializationApplyPlan,
    *,
    snapshot_root: str | Path | None = None,
) -> MaterializationApplyResult:
    """Execute only a READY plan and retain external rollback evidence."""

    if type(plan) is not MaterializationApplyPlan:
        raise TypeError("plan must be an exact MaterializationApplyPlan")
    if plan.state is not ApplyState.READY or plan.physical_root is None:
        return MaterializationApplyResult(
            plan.transaction_id,
            plan.preview_id,
            plan.preview_sha256,
            plan.physical_root_sha256,
            plan.state,
            plan.assessment,
            plan.blocker_codes,
            (),
            plan.pre_state,
            (),
            None,
            f"rollback.{plan.transaction_id}",
            plan.physical_root,
        )
    baseline = tuple(
        {"path": item.path, "expected_sha256": item.expected_sha256}
        for item in plan.preview.proposal.baseline_entries
    )
    pre_state, originals, mismatches = _read_pre_state(plan.physical_root, baseline)
    if mismatches or pre_state != plan.pre_state:
        return MaterializationApplyResult(
            plan.transaction_id,
            plan.preview_id,
            plan.preview_sha256,
            plan.physical_root_sha256,
            ApplyState.BLOCK,
            plan.assessment,
            ("pre-state-drift",),
            (),
            pre_state,
            (),
            None,
            f"rollback.{plan.transaction_id}",
            plan.physical_root,
        )
    snapshot_dir = _snapshot_directory(snapshot_root, plan.transaction_id, plan.physical_root)
    assert plan.physical_root_sha256 is not None
    _persist_snapshot(
        snapshot_dir,
        plan.transaction_id,
        plan.preview_id,
        plan.physical_root_sha256,
        pre_state,
        originals,
    )
    try:
        _write_files(plan.physical_root, plan.files, plan.transaction_id, pre_state)
        post_state = tuple(
            PreStateEntry(item.path, item.expected_sha256, _sha256_bytes(_resolve_child(plan.physical_root, item.path).read_bytes()), True)
            for item in pre_state
        )
        expected_post = {item.path: item.content_sha256 for item in plan.files}
        if any(item.observed_sha256 != expected_post[item.path] for item in post_state):
            raise MaterializationApplyError("post-state does not match frozen manifest")
        changed = tuple(item.path for item in plan.files)
        _update_snapshot_status(snapshot_dir, "applied")
        return MaterializationApplyResult(
            plan.transaction_id,
            plan.preview_id,
            plan.preview_sha256,
            plan.physical_root_sha256,
            ApplyState.APPLIED,
            plan.assessment,
            (),
            changed,
            pre_state,
            post_state,
            str(snapshot_dir),
            f"rollback.{plan.transaction_id}",
            plan.physical_root,
        )
    except (OSError, MaterializationApplyError) as error:
        blocker_codes = ["apply-failed", _reason_code(str(error))]
        try:
            _restore_originals(plan.physical_root, originals, plan.files, plan.transaction_id)
        except (MaterializationApplyError, OSError):
            blocker_codes.append("recovery-failed")
        _update_snapshot_status(
            snapshot_dir,
            "recovery-failed" if "recovery-failed" in blocker_codes else "apply-failed",
        )
        return MaterializationApplyResult(
            plan.transaction_id,
            plan.preview_id,
            plan.preview_sha256,
            plan.physical_root_sha256,
            ApplyState.BLOCK,
            plan.assessment,
            tuple(sorted(set(blocker_codes))),
            (),
            pre_state,
            (),
            str(snapshot_dir),
            f"rollback.{plan.transaction_id}",
            plan.physical_root,
        )


def _restore_originals(
    root: Path,
    originals: Mapping[str, bytes | None],
    files: Sequence[MaterializationFile],
    transaction_id: str,
) -> None:
    for item in files:
        target = _resolve_child(root, item.path)
        original = originals.get(item.path)
        if original is None:
            target.unlink(missing_ok=True)
            _remove_empty_parents(target.parent, root)
        else:
            rollback = target.with_name(f".{target.name}.apg-p3e-{transaction_id}.rollback.tmp")
            rollback.write_bytes(original)
            os.replace(rollback, target)


def _remove_empty_parents(path: Path, root: Path) -> None:
    current = path
    while current != root and current.is_relative_to(root):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _cas_restore_originals(
    root: Path,
    originals: Mapping[str, bytes | None],
    expected_current: Mapping[str, str],
    transaction_id: str,
) -> None:
    temporary: list[tuple[str, Path, Path]] = []
    try:
        for path, original in sorted(originals.items()):
            target = _resolve_child(root, path)
            if _observed_hash(target) != expected_current[path]:
                raise MaterializationApplyError(f"post-state drift before rollback: {path}")
            if original is not None:
                staged = target.with_name(
                    f".{target.name}.apg-p3e-{transaction_id}.rollback.tmp"
                )
                staged.write_bytes(original)
                temporary.append((path, staged, target))
        if any(
            _observed_hash(_resolve_child(root, path)) != expected_current[path]
            for path in originals
        ):
            raise MaterializationApplyError("post-state drift before rollback commit")
        staged_by_path = {path: (staged, target) for path, staged, target in temporary}
        for path, original in sorted(originals.items()):
            target = _resolve_child(root, path)
            if original is None:
                target.unlink()
                _remove_empty_parents(target.parent, root)
            else:
                staged, target = staged_by_path[path]
                os.replace(staged, target)
    finally:
        for _, staged, _ in temporary:
            staged.unlink(missing_ok=True)


def rollback_materialization_apply(
    result: MaterializationApplyResult,
    *,
    snapshot_root: str | Path | None = None,
) -> MaterializationRollbackResult:
    """Restore an applied transaction only when post-state has not drifted."""

    if type(result) is not MaterializationApplyResult:
        raise TypeError("result must be an exact MaterializationApplyResult")
    if result.state is not ApplyState.APPLIED or result.physical_root is None or result.snapshot_ref is None:
        return MaterializationRollbackResult(
            result.transaction_id,
            result.rollback_id,
            ApplyState.BLOCK,
            ("rollback-not-available",),
            (),
        )
    if (
        result.physical_root_sha256 is None
        or _root_fingerprint(result.physical_root) != result.physical_root_sha256
    ):
        return MaterializationRollbackResult(
            result.transaction_id,
            result.rollback_id,
            ApplyState.BLOCK,
            ("physical-root-drift",),
            (),
        )
    snapshot_dir = Path(result.snapshot_ref)
    if not snapshot_dir.is_absolute() or _is_link_or_reparse(snapshot_dir):
        return MaterializationRollbackResult(
            result.transaction_id,
            result.rollback_id,
            ApplyState.BLOCK,
            ("snapshot-reference-mismatch",),
            (),
        )
    if snapshot_root is not None:
        expected = Path(snapshot_root)
        if not expected.is_absolute():
            return MaterializationRollbackResult(
                result.transaction_id,
                result.rollback_id,
                ApplyState.BLOCK,
                ("snapshot-reference-mismatch",),
                (),
            )
        expected = expected.resolve(strict=False) / result.transaction_id
        if expected != snapshot_dir:
            return MaterializationRollbackResult(
                result.transaction_id,
                result.rollback_id,
                ApplyState.BLOCK,
                ("snapshot-reference-mismatch",),
                (),
            )
    if not _snapshot_is_bound(snapshot_dir, result):
        return MaterializationRollbackResult(
            result.transaction_id,
            result.rollback_id,
            ApplyState.BLOCK,
            ("snapshot-integrity-failure",),
            (),
        )
    drift: list[str] = []
    for item in result.post_state:
        target = _resolve_child(result.physical_root, item.path)
        if not target.is_file() or _sha256_bytes(target.read_bytes()) != item.observed_sha256:
            drift.append(item.path)
    if drift:
        return MaterializationRollbackResult(
            result.transaction_id,
            result.rollback_id,
            ApplyState.BLOCK,
            ("post-state-drift",),
            (),
        )
    originals: dict[str, bytes | None] = {}
    files_root = snapshot_dir / "files"
    for item in result.pre_state:
        source = files_root.joinpath(*item.path.split("/"))
        if item.existed:
            if not source.is_file() or _is_link_or_reparse(source):
                return MaterializationRollbackResult(
                    result.transaction_id,
                    result.rollback_id,
                    ApplyState.BLOCK,
                    ("snapshot-integrity-failure",),
                    (),
                )
            content = source.read_bytes()
            if _sha256_bytes(content) != item.observed_sha256:
                return MaterializationRollbackResult(
                    result.transaction_id,
                    result.rollback_id,
                    ApplyState.BLOCK,
                    ("snapshot-integrity-failure",),
                    (),
                )
            originals[item.path] = content
        else:
            originals[item.path] = None
    expected_current = {
        item.path: item.observed_sha256
        for item in result.post_state
        if item.observed_sha256 is not None
    }
    try:
        _cas_restore_originals(
            result.physical_root,
            originals,
            expected_current,
            result.transaction_id,
        )
    except (MaterializationApplyError, OSError):
        return MaterializationRollbackResult(
            result.transaction_id,
            result.rollback_id,
            ApplyState.BLOCK,
            ("post-state-drift",),
            (),
        )
    _update_snapshot_status(snapshot_dir, "rolled-back")
    return MaterializationRollbackResult(
        result.transaction_id,
        result.rollback_id,
        ApplyState.ROLLED_BACK,
        (),
        tuple(item.path for item in result.post_state),
    )


def _assessment_mapping(value: AuthorizationAssessment) -> dict[str, object]:
    return {
        "classification": value.classification.value,
        "reason_codes": list(value.reason_codes),
        "requires_owner_approval": value.requires_owner_approval,
    }


def _pre_state_mapping(value: PreStateEntry) -> dict[str, object]:
    return {
        "expected_sha256": value.expected_sha256,
        "existed": value.existed,
        "observed_sha256": value.observed_sha256,
        "path": value.path,
    }


def render_apply_result(value: MaterializationApplyResult) -> bytes:
    """Render a redacted canonical result suitable for a receipt."""

    if type(value) is not MaterializationApplyResult:
        raise TypeError("value must be an exact MaterializationApplyResult")
    mapping = {
        "authorization": _assessment_mapping(value.authorization),
        "blocker_codes": list(value.blocker_codes),
        "changed_paths": list(value.changed_paths),
        "post_state": [_pre_state_mapping(item) for item in value.post_state],
        "pre_state": [_pre_state_mapping(item) for item in value.pre_state],
        "physical_root_sha256": value.physical_root_sha256,
        "preview_id": value.preview_id,
        "preview_sha256": value.preview_sha256,
        "rollback_id": value.rollback_id,
        "schema_version": P3E_SCHEMA_VERSION,
        "snapshot_ref": value.snapshot_ref,
        "state": value.state.value,
        "transaction_id": value.transaction_id,
    }
    return canonical_json_bytes(mapping)


def render_rollback_result(value: MaterializationRollbackResult) -> bytes:
    if type(value) is not MaterializationRollbackResult:
        raise TypeError("value must be an exact MaterializationRollbackResult")
    return canonical_json_bytes(
        {
            "blocker_codes": list(value.blocker_codes),
            "restored_paths": list(value.restored_paths),
            "rollback_id": value.rollback_id,
            "schema_version": P3E_SCHEMA_VERSION,
            "state": value.state.value,
            "transaction_id": value.transaction_id,
        }
    )


__all__ = [
    "P3E_SCHEMA_VERSION",
    "MAX_TRANSACTION_FILES",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "MaterializationApplyError",
    "AuthorizationClass",
    "ApplyState",
    "ActionContext",
    "AuthorizationAssessment",
    "TransactionApproval",
    "MaterializationFile",
    "PreStateEntry",
    "MaterializationApplyPlan",
    "MaterializationApplyResult",
    "MaterializationRollbackResult",
    "assess_action",
    "plan_materialization_apply",
    "execute_materialization_apply",
    "rollback_materialization_apply",
    "render_apply_result",
    "render_rollback_result",
]
