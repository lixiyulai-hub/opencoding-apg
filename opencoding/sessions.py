"""Explicit-root, fail-closed session persistence with a cooperative process lock."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any
import uuid

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .intake import SCHEMA_VERSION, _validate_session_shape, sanitize_text


class SessionConflictError(RuntimeError):
    """Raised when a save would overwrite a newer or divergent session."""

    code = "session_revision_conflict"


class SessionLockError(RuntimeError):
    """Raised when another process owns the session persistence lock."""

    code = "session_lock_busy"


_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BINARY_FLAG = getattr(os, "O_BINARY", 0)
_IMMUTABLE_SESSION_FIELDS = ("id", "schema_version", "goal", "questions")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("session object keys must be strings")
            result[key] = _sanitize_value(item)
        return result
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise TypeError("session contains a non-JSON value")


def _reparse_point(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _hard_link(path: Path) -> bool:
    try:
        return os.lstat(path).st_nlink > 1
    except FileNotFoundError:
        return False


def _check_existing_chain(path: Path, *, root: Path) -> None:
    """Reject links, reparse points and hard-linked files on every relevant component."""

    absolute = Path(os.path.abspath(path))
    root_absolute = Path(os.path.abspath(root))
    try:
        if os.path.commonpath([str(absolute), str(root_absolute)]) != str(root_absolute):
            raise ValueError("path escapes explicit root")
    except ValueError as exc:
        raise ValueError("path escapes explicit root") from exc
    chain: list[Path] = []
    current = absolute
    while True:
        chain.append(current)
        if current == root_absolute:
            break
        parent = current.parent
        if parent == current:
            raise ValueError("path is outside explicit root")
        current = parent
    for item in reversed(chain):
        if item.exists() or item.is_symlink():
            if _reparse_point(item):
                raise ValueError(f"link or reparse point is not allowed: {item}")
            if item.is_file() and _hard_link(item):
                raise ValueError(f"hard-linked file is not allowed: {item}")


def _check_root_ancestors(root: Path) -> None:
    """Reject a linked/reparse root or any ancestor used to reach it."""

    current = Path(os.path.abspath(root))
    while True:
        if _reparse_point(current):
            raise ValueError(f"link or reparse point is not allowed: {current}")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _root(root: Path) -> Path:
    if not isinstance(root, Path):
        raise TypeError("root must be an explicit pathlib.Path")
    if not root.is_absolute():
        raise ValueError("root must be an absolute pathlib.Path")
    if not root.exists() or not root.is_dir():
        raise ValueError("root must be an existing directory")
    absolute = Path(os.path.abspath(root))
    _check_root_ancestors(absolute)
    _check_existing_chain(absolute, root=absolute)
    return absolute


def _path(root: Path, session_id: str) -> Path:
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise ValueError("invalid session id")
    destination = root / ".opencoding" / "sessions" / f"{session_id}.json"
    _check_existing_chain(destination.parent, root=root)
    if destination.exists() or destination.is_symlink():
        _check_existing_chain(destination, root=root)
    return destination


def _validate(session: dict[str, Any]) -> dict[str, Any]:
    _validate_session_shape(session)
    if session.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported session schema_version")
    return session


def _lock_path(root: Path) -> Path:
    lock = root / ".opencoding" / ".session-write.lock"
    _check_existing_chain(lock.parent, root=root)
    if lock.exists() or lock.is_symlink():
        _check_existing_chain(lock, root=root)
    return lock


def _lock_owner_payload() -> bytes:
    owner = {
        "pid": os.getpid(),
        "token": uuid.uuid4().hex,
    }
    return (_canonical(owner) + "\n").encode("ascii")


def _write_all(handle: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        offset += os.write(handle, payload[offset:])


def _write_lock_owner(handle: int, payload: bytes) -> None:
    os.lseek(handle, 0, os.SEEK_SET)
    os.ftruncate(handle, 0)
    _write_all(handle, payload)
    os.fsync(handle)
    os.lseek(handle, 0, os.SEEK_SET)


def _ensure_guard_byte(handle: int) -> None:
    os.lseek(handle, 0, os.SEEK_END)
    if os.lseek(handle, 0, os.SEEK_CUR) == 0:
        os.write(handle, b"\0")
        os.fsync(handle)
    os.lseek(handle, 0, os.SEEK_SET)


def _lock_os(handle: int) -> None:
    try:
        if os.name == "nt":
            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise SessionLockError("session persistence lock is already held") from exc


def _unlock_os(handle: int) -> None:
    try:
        if os.name == "nt":
            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass


def _acquire_lock(root: Path) -> tuple[Path, int, bytes]:
    lock = _lock_path(root)
    handle = os.open(lock, os.O_CREAT | os.O_RDWR | _BINARY_FLAG, 0o600)
    try:
        _check_existing_chain(lock, root=root)
        opened = os.fstat(handle)
        current = lock.stat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("lock path changed while opening")
        _ensure_guard_byte(handle)
        _lock_os(handle)
        payload = _lock_owner_payload()
        _write_lock_owner(handle, payload)
        return lock, handle, payload
    except BaseException:
        _unlock_os(handle)
        os.close(handle)
        raise


def _release_lock(lock: Path, handle: int, payload: bytes) -> None:
    _unlock_os(handle)
    try:
        os.close(handle)
    except OSError:
        pass


def _ensure_layout(root: Path) -> None:
    metadata = root / ".opencoding"
    sessions = metadata / "sessions"
    _check_existing_chain(metadata, root=root)
    if metadata.exists() and not metadata.is_dir():
        raise ValueError(".opencoding must be a directory")
    metadata.mkdir(exist_ok=True)
    _check_existing_chain(metadata, root=root)
    _check_existing_chain(sessions, root=root)
    if sessions.exists() and not sessions.is_dir():
        raise ValueError("session store must be a directory")
    sessions.mkdir(exist_ok=True)
    _check_existing_chain(sessions, root=root)


def _ensure_metadata_root(root: Path) -> None:
    """Create only the lock parent before the first save, then re-check it."""

    metadata = root / ".opencoding"
    _check_existing_chain(metadata, root=root)
    if metadata.exists() and not metadata.is_dir():
        raise ValueError(".opencoding must be a directory")
    metadata.mkdir(exist_ok=True)
    _check_existing_chain(metadata, root=root)


def _read_existing(destination: Path) -> dict[str, Any] | None:
    if not destination.exists():
        return None
    try:
        raw = json.loads(destination.read_text(encoding="utf-8"))
        return _validate(_sanitize_value(raw))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("stored session is invalid") from exc


def save_session(root: Path, session: dict[str, Any]) -> dict[str, Any]:
    """Persist a sanitized session while holding one lock across read/compare/write."""

    root = _root(root)
    candidate = _validate(_sanitize_value(session))
    _ensure_metadata_root(root)
    lock, handle, lock_payload = _acquire_lock(root)
    try:
        _ensure_layout(root)
        destination = _path(root, candidate["id"])
        _check_existing_chain(destination.parent, root=root)
        existing = _read_existing(destination)
        if existing is None:
            if candidate["revision"] != 0:
                raise SessionConflictError("first session save must use revision zero")
        elif _canonical(existing) == _canonical(candidate):
            return {"status": "unchanged", "session_id": candidate["id"], "revision": candidate["revision"], "path": str(destination)}
        elif any(candidate[field] != existing[field] for field in _IMMUTABLE_SESSION_FIELDS):
            raise SessionConflictError("immutable session predecessor changed")
        elif candidate["revision"] != existing["revision"] + 1:
            raise SessionConflictError("session revision is stale or divergent")
        elif candidate["answer_history"][:-1] != existing["answer_history"]:
            raise SessionConflictError("session history does not extend the stored predecessor")
        payload = _canonical(candidate) + "\n"
        temporary = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4().hex}")
        _check_existing_chain(temporary, root=root)
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _BINARY_FLAG, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _check_existing_chain(destination.parent, root=root)
            _check_existing_chain(temporary, root=root)
            os.replace(temporary, destination)
        finally:
            if temporary.exists() or temporary.is_symlink():
                try:
                    temporary.unlink()
                except OSError:
                    pass
        return {"status": "saved", "session_id": candidate["id"], "revision": candidate["revision"], "path": str(destination)}
    finally:
        _release_lock(lock, handle, lock_payload)


def load_session(root: Path, session_id: str) -> dict[str, Any]:
    """Load one strictly validated session; missing reads never create directories."""

    root = _root(root)
    destination = _path(root, session_id)
    if not destination.exists():
        raise FileNotFoundError(f"session not found: {session_id}")
    lock, handle, lock_payload = _acquire_lock(root)
    try:
        _check_existing_chain(destination, root=root)
        session = _read_existing(destination)
        if session is None or session["id"] != session_id:
            raise ValueError("session id does not match path")
        return session
    finally:
        _release_lock(lock, handle, lock_payload)


__all__ = ["SessionConflictError", "SessionLockError", "load_session", "save_session"]
