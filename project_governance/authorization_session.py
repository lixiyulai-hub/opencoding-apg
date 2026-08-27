"""Exact, expiring delegation for one bounded consequential session."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable

from .storage import SchemaError, canonical_json_bytes


AUTHORIZATION_SESSION_SCHEMA_VERSION = "1.1"
MAX_TRANSACTION_IDS = 64
MAX_SCOPE_ITEMS = 256
MAX_REASON_CODES = 128
MAX_EVIDENCE_REFS = 128
MAX_LIFECYCLE_TASK_IDS = 128
MAX_WAVE_INDEX = 65535
MAX_SESSION_LIFETIME = timedelta(hours=24)

_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?Z\Z"
)
_SENSITIVE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}|\bgh[pousr]_[A-Za-z0-9_-]{8,}|"
    r"\bbearer\s+[A-Za-z0-9._~+/-]{8,}|"
    r"\b(?:api[_-]?key|token|password|secret)\s*[:=]|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)


class AuthorizationSessionError(ValueError):
    """Raised when a delegated authorization is malformed or out of scope."""


def _text(value: object, label: str, maximum: int = 240) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise AuthorizationSessionError(f"{label} must be bounded non-empty text")
    if value != unicodedata.normalize("NFC", value):
        raise AuthorizationSessionError(f"{label} must use NFC Unicode")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AuthorizationSessionError(f"{label} contains control characters")
    if _SENSITIVE.search(value):
        raise AuthorizationSessionError(f"{label} contains a sensitive-value pattern")
    return value


def _code(value: object, label: str) -> str:
    text = _text(value, label, 128)
    if not _CODE.fullmatch(text):
        raise AuthorizationSessionError(f"{label} must be a bounded stable code")
    return text


def _digest(value: object, label: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise AuthorizationSessionError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _wave_index(value: object, label: str) -> int:
    if type(value) is not int or value < 0 or value > MAX_WAVE_INDEX:
        raise AuthorizationSessionError(f"{label} must be a bounded non-negative integer")
    return value


def _scope_item(value: object, label: str) -> str:
    text = _text(value, label, 240)
    if "\\" in text:
        raise AuthorizationSessionError(f"{label} must use canonical forward slashes")
    if text.startswith("/") or ":" in text:
        raise AuthorizationSessionError(f"{label} must be relative")
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise AuthorizationSessionError(f"{label} contains an unsafe path segment")
    return text


def _canonical_strings(
    values: Iterable[object],
    label: str,
    maximum: int,
    validator,
    *,
    require_tuple: bool = False,
) -> tuple[str, ...]:
    if require_tuple:
        if type(values) is not tuple:
            raise AuthorizationSessionError(f"{label} must be an immutable tuple")
        items = values
    else:
        try:
            items = tuple(values)
        except TypeError as error:
            raise AuthorizationSessionError(f"{label} must be a sequence") from error
    if not items or len(items) > maximum:
        raise AuthorizationSessionError(f"{label} must be a bounded non-empty sequence")
    normalized = tuple(validator(item, f"{label} entry") for item in items)
    if normalized != tuple(sorted(set(normalized))):
        raise AuthorizationSessionError(f"{label} must be canonical")
    return normalized


def _timestamp(value: object, label: str, *, allow_future: bool) -> datetime:
    text = _text(value, label, 40)
    if not _TIMESTAMP.fullmatch(text):
        raise AuthorizationSessionError(f"{label} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise AuthorizationSessionError(f"{label} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise AuthorizationSessionError(f"{label} must use UTC")
    if not allow_future and parsed > datetime.now(timezone.utc):
        raise AuthorizationSessionError(f"{label} cannot be in the future")
    return parsed.astimezone(timezone.utc)


def _now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if type(value) is not datetime or value.tzinfo is None:
        raise AuthorizationSessionError("now must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class AuthorizationSession:
    """One owner-approved, bounded authority session.

    A session is a reusable proof only for explicitly enumerated child
    transactions that share one exact source, preimage, scope, and reason set.
    A lifecycle-bound session additionally names one exact run, plan, wave,
    and CONFIRM-task set. Callers must perform their own path boundary checks
    before passing a scope to the covers method.
    """

    authorization_id: str
    transaction_ids: tuple[str, ...]
    policy_sha256: str
    source_sha256: str
    preimage_sha256: str | None
    scope: tuple[str, ...]
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    actor: str
    role: str
    issued_at_utc: str
    expires_at_utc: str
    lifecycle_run_id: str | None = None
    plan_id: str | None = None
    wave_index: int | None = None
    task_ids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if type(self) is not AuthorizationSession:
            raise AuthorizationSessionError(
                "AuthorizationSession subclasses are not accepted"
            )
        _code(self.authorization_id, "authorization_id")
        _canonical_strings(
            self.transaction_ids,
            "transaction_ids",
            MAX_TRANSACTION_IDS,
            _code,
            require_tuple=True,
        )
        _digest(self.policy_sha256, "policy_sha256")
        _digest(self.source_sha256, "source_sha256")
        if self.preimage_sha256 is not None:
            _digest(self.preimage_sha256, "preimage_sha256")
        _canonical_strings(
            self.scope,
            "scope",
            MAX_SCOPE_ITEMS,
            _scope_item,
            require_tuple=True,
        )
        _canonical_strings(
            self.reason_codes,
            "reason_codes",
            MAX_REASON_CODES,
            _code,
            require_tuple=True,
        )
        _canonical_strings(
            self.evidence_refs,
            "evidence_refs",
            MAX_EVIDENCE_REFS,
            _code,
            require_tuple=True,
        )
        _text(self.actor, "actor", 128)
        if self.role != "owner":
            raise AuthorizationSessionError("role must be owner")
        issued = _timestamp(self.issued_at_utc, "issued_at_utc", allow_future=False)
        expires = _timestamp(self.expires_at_utc, "expires_at_utc", allow_future=True)
        if expires <= issued:
            raise AuthorizationSessionError("expires_at_utc must be after issued_at_utc")
        if expires - issued > MAX_SESSION_LIFETIME:
            raise AuthorizationSessionError("authorization session lifetime is too long")
        lifecycle_binding = (
            self.lifecycle_run_id,
            self.plan_id,
            self.wave_index,
            self.task_ids,
        )
        if any(value is not None for value in lifecycle_binding) and not all(
            value is not None for value in lifecycle_binding
        ):
            raise AuthorizationSessionError("lifecycle binding fields must be all present or all null")
        if all(value is not None for value in lifecycle_binding):
            _code(self.lifecycle_run_id, "lifecycle_run_id")
            _code(self.plan_id, "plan_id")
            _wave_index(self.wave_index, "wave_index")
            _canonical_strings(
                self.task_ids,
                "task_ids",
                MAX_LIFECYCLE_TASK_IDS,
                _code,
                require_tuple=True,
            )

    def covers(
        self,
        *,
        transaction_id: str,
        policy_sha256: str,
        source_sha256: str,
        scope: Iterable[str],
        reason_codes: Iterable[str],
        preimage_sha256: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Return true only when every supplied fact is inside this session."""

        current = _now(now)
        issued = _timestamp(self.issued_at_utc, "issued_at_utc", allow_future=False)
        expires = _timestamp(self.expires_at_utc, "expires_at_utc", allow_future=True)
        if current < issued or current >= expires:
            return False
        if _code(transaction_id, "transaction_id") not in self.transaction_ids:
            return False
        if policy_sha256 != self.policy_sha256:
            return False
        if source_sha256 != self.source_sha256:
            return False
        if preimage_sha256 != self.preimage_sha256:
            return False
        requested_scope = _canonical_strings(
            scope, "requested_scope", MAX_SCOPE_ITEMS, _scope_item
        )
        if requested_scope != self.scope:
            return False
        requested_reasons = _canonical_strings(
            reason_codes,
            "requested_reason_codes",
            MAX_REASON_CODES,
            _code,
        )
        return requested_reasons == self.reason_codes


def _mapping(value: AuthorizationSession) -> dict[str, Any]:
    return {
        "actor": value.actor,
        "authorization_id": value.authorization_id,
        "evidence_refs": list(value.evidence_refs),
        "expires_at_utc": value.expires_at_utc,
        "issued_at_utc": value.issued_at_utc,
        "lifecycle_run_id": value.lifecycle_run_id,
        "plan_id": value.plan_id,
        "policy_sha256": value.policy_sha256,
        "preimage_sha256": value.preimage_sha256,
        "reason_codes": list(value.reason_codes),
        "role": value.role,
        "schema_version": AUTHORIZATION_SESSION_SCHEMA_VERSION,
        "scope": list(value.scope),
        "source_sha256": value.source_sha256,
        "task_ids": None if value.task_ids is None else list(value.task_ids),
        "transaction_ids": list(value.transaction_ids),
        "wave_index": value.wave_index,
    }


def render_authorization_session(value: AuthorizationSession) -> bytes:
    if type(value) is not AuthorizationSession:
        raise TypeError("value must be an exact AuthorizationSession")
    return canonical_json_bytes(_mapping(value))


def _closed(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuthorizationSessionError(f"{label} must be an object")
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise AuthorizationSessionError(
            f"{label} has unknown fields: {', '.join(sorted(unknown))}"
        )
    if missing:
        raise AuthorizationSessionError(
            f"{label} is missing fields: {', '.join(sorted(missing))}"
        )
    return value


def _parse_json(payload: bytes | bytearray | memoryview) -> dict[str, Any]:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    raw = bytes(payload)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthorizationSessionError("authorization session JSON is invalid") from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise AuthorizationSessionError("authorization session JSON is not canonical")
    return value


def parse_authorization_session(
    payload: bytes | bytearray | memoryview,
) -> AuthorizationSession:
    value = _closed(
        _parse_json(payload),
        {
            "actor",
            "authorization_id",
            "evidence_refs",
            "expires_at_utc",
            "issued_at_utc",
            "lifecycle_run_id",
            "plan_id",
            "policy_sha256",
            "preimage_sha256",
            "reason_codes",
            "role",
            "schema_version",
            "scope",
            "source_sha256",
            "task_ids",
            "transaction_ids",
            "wave_index",
        },
        "authorization_session",
    )
    if value["schema_version"] != AUTHORIZATION_SESSION_SCHEMA_VERSION:
        raise SchemaError("unsupported authorization session schema_version")
    return AuthorizationSession(
        authorization_id=_code(value["authorization_id"], "authorization_id"),
        transaction_ids=_canonical_strings(
            value["transaction_ids"], "transaction_ids", MAX_TRANSACTION_IDS, _code
        ),
        policy_sha256=_digest(value["policy_sha256"], "policy_sha256"),
        source_sha256=_digest(value["source_sha256"], "source_sha256"),
        preimage_sha256=(
            None
            if value["preimage_sha256"] is None
            else _digest(value["preimage_sha256"], "preimage_sha256")
        ),
        scope=_canonical_strings(value["scope"], "scope", MAX_SCOPE_ITEMS, _scope_item),
        reason_codes=_canonical_strings(
            value["reason_codes"], "reason_codes", MAX_REASON_CODES, _code
        ),
        evidence_refs=_canonical_strings(
            value["evidence_refs"], "evidence_refs", MAX_EVIDENCE_REFS, _code
        ),
        actor=_text(value["actor"], "actor", 128),
        role=_code(value["role"], "role"),
        issued_at_utc=_text(value["issued_at_utc"], "issued_at_utc", 40),
        expires_at_utc=_text(value["expires_at_utc"], "expires_at_utc", 40),
        lifecycle_run_id=(
            None
            if value["lifecycle_run_id"] is None
            else _code(value["lifecycle_run_id"], "lifecycle_run_id")
        ),
        plan_id=(
            None if value["plan_id"] is None else _code(value["plan_id"], "plan_id")
        ),
        wave_index=(
            None
            if value["wave_index"] is None
            else _wave_index(value["wave_index"], "wave_index")
        ),
        task_ids=(
            None
            if value["task_ids"] is None
            else _canonical_strings(
                value["task_ids"], "task_ids", MAX_LIFECYCLE_TASK_IDS, _code
            )
        ),
    )


def authorization_session_sha256(value: AuthorizationSession) -> str:
    return hashlib.sha256(render_authorization_session(value)).hexdigest()


__all__ = [
    "AUTHORIZATION_SESSION_SCHEMA_VERSION",
    "MAX_SESSION_LIFETIME",
    "AuthorizationSession",
    "AuthorizationSessionError",
    "authorization_session_sha256",
    "parse_authorization_session",
    "render_authorization_session",
]
