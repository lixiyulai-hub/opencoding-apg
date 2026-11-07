from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .discovery import (
    DISCOVERY_PRUNED_DIRECTORY_NAMES,
    DISCOVERY_PRUNED_DIRECTORY_PREFIXES,
)


class PathViolation(RuntimeError):
    pass


class RecoveryRequired(RuntimeError):
    pass


TRANSACTION_CONTENT_HASH_LIMIT = 8 * 1024 * 1024
TRANSACTION_STABLE_PRUNED_DIRS = frozenset({".git"})
TRANSACTION_PRUNED_DIRS = frozenset(
    DISCOVERY_PRUNED_DIRECTORY_NAMES
    | {
        f"{prefix}*"
        for prefix in DISCOVERY_PRUNED_DIRECTORY_PREFIXES
    }
)


def _replace(source: Path, target: Path) -> None:
    source.replace(target)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(b"file\0")
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_metadata_digest(path: Path) -> str:
    info = path.stat(follow_symlinks=False)
    payload = "\0".join(
        str(value)
        for value in (
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
    ).encode("ascii")
    return _metadata_digest("file-metadata", payload)


def _directory_metadata_digest(path: Path) -> str:
    info = path.stat(follow_symlinks=False)
    payload = "\0".join(
        str(value)
        for value in (
            info.st_mode,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
    ).encode("ascii")
    return _metadata_digest("directory-metadata", payload)


def _matches_directory_pattern(name: str, patterns: frozenset[str]) -> bool:
    key = name.casefold()
    return key in patterns or any(
        pattern.endswith("*") and key.startswith(pattern[:-1])
        for pattern in patterns
    )


def _snapshot_candidates(root: Path, pruned_dirs: frozenset[str]) -> list[Path]:
    candidates: list[Path] = []

    def on_error(error: OSError) -> None:
        raise error

    for current_text, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=on_error,
    ):
        current = Path(current_text)
        retained: list[str] = []
        for name in sorted(directory_names):
            candidate = current / name
            candidates.append(candidate)
            if (
                not _matches_directory_pattern(name, pruned_dirs)
                and not candidate.is_symlink()
            ):
                retained.append(name)
        directory_names[:] = retained
        candidates.extend(current / name for name in sorted(file_names))
    return candidates


def _metadata_digest(kind: str, value: bytes = b"") -> str:
    return hashlib.sha256(kind.encode("ascii") + b"\0" + value).hexdigest()


def _missing_directories(path: Path, stop: Path) -> tuple[Path, ...]:
    if not path.is_relative_to(stop):
        raise PathViolation(f"directory escapes cleanup boundary: {path}")
    missing: list[Path] = []
    current = path
    while current != stop and not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    return tuple(missing)


def _ensure_directory(
    path: Path,
    stop: Path,
    created: set[Path],
    *,
    require_new: bool = False,
) -> None:
    missing = _missing_directories(path, stop)
    if require_new and not missing:
        raise FileExistsError(f"transaction directory already exists: {path}")
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if directory.is_symlink() or not directory.is_dir():
                raise
            if require_new and directory == path:
                raise
        else:
            created.add(directory)
    if path.is_symlink() or not path.is_dir():
        raise PathViolation(f"transaction directory is not a real directory: {path}")


def _remove_created_directories(paths: Iterable[Path]) -> list[Exception]:
    errors: list[Exception] = []
    for path in sorted(set(paths), key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except FileNotFoundError:
            continue
        except OSError as error:
            try:
                if not path.is_symlink() and path.is_dir() and any(path.iterdir()):
                    continue
            except OSError:
                pass
            errors.append(error)
    return errors


@dataclass(frozen=True)
class TransactionResult:
    preview: bool
    planned_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]


class WorkspaceSnapshot(dict[str, str]):
    def __init__(
        self,
        values: Mapping[str, str],
        *,
        file_content_limit: int | None,
        metadata_only_dirs: frozenset[str],
        pruned_dirs: frozenset[str],
        stable_pruned_dirs: frozenset[str],
    ) -> None:
        super().__init__(values)
        self.file_content_limit = file_content_limit
        self.metadata_only_dirs = metadata_only_dirs
        self.pruned_dirs = pruned_dirs
        self.stable_pruned_dirs = stable_pruned_dirs


class WorkspaceGuard:
    def __init__(self, root: Path):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise PathViolation(f"workspace root is not a directory: {self.root}")

    def resolve_write(self, relative: str | Path) -> Path:
        requested = Path(relative)
        if requested.is_absolute():
            raise PathViolation(f"absolute write path is not allowed: {relative}")
        candidate = (self.root / requested).resolve(strict=False)
        if not candidate.is_relative_to(self.root):
            raise PathViolation(f"write escapes workspace: {relative}")
        return candidate

    def snapshot(
        self,
        paths: Iterable[str | Path] | None = None,
        *,
        file_content_limit: int | None = None,
        metadata_only_dirs: Iterable[str] = (),
        pruned_dirs: Iterable[str] = (),
        stable_pruned_dirs: Iterable[str] = (),
    ) -> dict[str, str]:
        if (
            file_content_limit is not None
            and (
                isinstance(file_content_limit, bool)
                or not isinstance(file_content_limit, int)
                or file_content_limit <= 0
            )
        ):
            raise ValueError("file_content_limit must be a positive integer")
        metadata_keys = frozenset(
            str(item).casefold() for item in metadata_only_dirs
        )
        pruned_keys = frozenset(str(item).casefold() for item in pruned_dirs)
        stable_pruned_keys = frozenset(
            str(item).casefold() for item in stable_pruned_dirs
        )
        if not stable_pruned_keys.issubset(pruned_keys):
            raise ValueError("stable_pruned_dirs must be included in pruned_dirs")
        candidates: list[Path] = []
        if paths is None:
            candidates.extend(_snapshot_candidates(self.root, pruned_keys))
        else:
            for item in paths:
                candidate = self.resolve_write(item)
                if candidate.is_dir():
                    candidates.append(candidate)
                    candidates.extend(_snapshot_candidates(candidate, pruned_keys))
                else:
                    candidates.append(candidate)

        snapshot: dict[str, str] = {}
        for candidate in sorted(set(candidates)):
            if not candidate.is_relative_to(self.root):
                raise PathViolation(f"snapshot path escapes workspace: {candidate}")
            relative = candidate.relative_to(self.root).as_posix()
            if candidate.is_symlink():
                snapshot[relative] = _metadata_digest(
                    "symlink",
                    os.fsencode(os.readlink(candidate)),
                )
                continue
            if candidate.is_dir():
                if _matches_directory_pattern(candidate.name, stable_pruned_keys):
                    snapshot[relative] = _metadata_digest("stable-pruned-directory")
                elif _matches_directory_pattern(candidate.name, pruned_keys):
                    snapshot[relative] = _directory_metadata_digest(candidate)
                else:
                    snapshot[relative] = _metadata_digest("directory")
                continue
            if not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(self.root):
                raise PathViolation(f"snapshot path escapes workspace: {candidate}")
            info = candidate.stat(follow_symlinks=False)
            metadata_only = any(
                part.casefold() in metadata_keys
                for part in candidate.relative_to(self.root).parts[:-1]
            )
            if (
                metadata_only
                or (
                    file_content_limit is not None
                    and info.st_size > file_content_limit
                )
            ):
                snapshot[relative] = _file_metadata_digest(candidate)
            else:
                snapshot[relative] = _file_digest(candidate)
        return WorkspaceSnapshot(
            dict(sorted(snapshot.items())),
            file_content_limit=file_content_limit,
            metadata_only_dirs=metadata_keys,
            pruned_dirs=pruned_keys,
            stable_pruned_dirs=stable_pruned_keys,
        )

    def changed_paths(self, before: Mapping[str, str]) -> tuple[str, ...]:
        if isinstance(before, WorkspaceSnapshot):
            after = self.snapshot(
                file_content_limit=before.file_content_limit,
                metadata_only_dirs=before.metadata_only_dirs,
                pruned_dirs=before.pruned_dirs,
                stable_pruned_dirs=before.stable_pruned_dirs,
            )
        else:
            after = self.snapshot()
        return tuple(
            sorted(
                path
                for path in set(before) | set(after)
                if before.get(path) != after.get(path)
            )
        )

    def assert_unchanged(self, before: Mapping[str, str]) -> None:
        changed = self.changed_paths(before)
        if changed:
            raise PathViolation(f"workspace changed: {', '.join(changed)}")


class WorkspaceTransaction:
    def __init__(
        self,
        guard: WorkspaceGuard,
        allowed_paths: Iterable[str | Path],
        *,
        apply: bool = False,
        checkpoint_root: Path | None = None,
    ):
        self.guard = guard
        self.apply = apply
        self._baseline = (
            guard.snapshot(
                file_content_limit=TRANSACTION_CONTENT_HASH_LIMIT,
                pruned_dirs=TRANSACTION_PRUNED_DIRS,
                stable_pruned_dirs=TRANSACTION_STABLE_PRUNED_DIRS,
            )
            if apply
            else None
        )
        self._allowed: dict[str, Path] = {}
        for item in allowed_paths:
            resolved = guard.resolve_write(item)
            relative = resolved.relative_to(guard.root).as_posix()
            self._allowed[relative] = resolved
        self._staged: dict[str, bytes] = {}
        self._controller_root = Path.cwd().resolve(strict=True)
        self._checkpoint_root = self._resolve_checkpoint_root(checkpoint_root)

    def _resolve_checkpoint_root(self, checkpoint_root: Path | None) -> Path:
        if checkpoint_root is None:
            return self.guard.resolve_write(".governance/.recovery")
        resolved = Path(checkpoint_root).resolve(strict=False)
        if not resolved.is_relative_to(self._controller_root):
            raise PathViolation(f"checkpoint root is outside controller: {resolved}")
        return resolved

    def _relative(self, relative: str | Path) -> str:
        resolved = self.guard.resolve_write(relative)
        normalized = resolved.relative_to(self.guard.root).as_posix()
        if normalized not in self._allowed:
            raise PathViolation(f"undeclared transaction path: {normalized}")
        return normalized

    def stage_bytes(self, relative: str | Path, content: bytes) -> None:
        self._staged[self._relative(relative)] = bytes(content)

    def stage_text(
        self,
        relative: str | Path,
        content: str,
        *,
        encoding: str = "utf-8",
    ) -> None:
        self.stage_bytes(relative, content.encode(encoding))

    def commit(self) -> TransactionResult:
        planned = tuple(sorted(self._staged))
        if not self.apply:
            return TransactionResult(True, planned, ())

        assert self._baseline is not None
        preexisting_drift = self.guard.changed_paths(self._baseline)
        if preexisting_drift:
            raise PathViolation(
                f"workspace changed before transaction: {', '.join(preexisting_drift)}"
            )

        transaction_id = uuid.uuid4().hex
        recovery_root = self._checkpoint_root / transaction_id
        originals: dict[str, bytes | None] = {}
        temporary: dict[str, Path] = {}
        created_target_directories: set[Path] = set()
        recovery_stop = (
            self.guard.root
            if recovery_root.is_relative_to(self.guard.root)
            else self._controller_root
        )
        created_recovery_directories: set[Path] = set()

        try:
            _ensure_directory(
                recovery_root,
                recovery_stop,
                created_recovery_directories,
                require_new=True,
            )
            for relative in planned:
                target = self._allowed[relative]
                originals[relative] = target.read_bytes() if target.exists() else None
                if target.exists():
                    backup = recovery_root / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_bytes(originals[relative] or b"")
                _ensure_directory(
                    target.parent,
                    self.guard.root,
                    created_target_directories,
                )
                staged = target.with_name(
                    f".{target.name}.project-governance-{transaction_id}.tmp"
                )
                staged.write_bytes(self._staged[relative])
                temporary[relative] = staged

            for relative in planned:
                _replace(temporary[relative], self._allowed[relative])

            changed = self.guard.changed_paths(self._baseline)
            recovery_relative = None
            if recovery_root.is_relative_to(self.guard.root):
                recovery_relative = recovery_root.relative_to(self.guard.root).as_posix()
            expected_directories = {
                path.relative_to(self.guard.root).as_posix()
                for path in created_target_directories | created_recovery_directories
                if path.is_relative_to(self.guard.root)
            }
            unexpected = tuple(
                path
                for path in changed
                if path not in self._allowed
                and path not in expected_directories
                and not (
                    recovery_relative
                    and (
                        path == recovery_relative
                        or path.startswith(recovery_relative + "/")
                    )
                )
            )
            if unexpected:
                raise PathViolation(
                    f"unexpected transaction drift: {', '.join(unexpected)}"
                )
        except Exception as error:
            recovery_errors = self._restore(
                originals,
                temporary,
                transaction_id,
                created_target_directories,
            )
            if recovery_errors:
                raise RecoveryRequired(
                    f"transaction recovery failed; evidence: {recovery_root}"
                ) from error
            cleanup_errors = self._remove_recovery(
                recovery_root,
                created_recovery_directories,
            )
            if cleanup_errors:
                raise RecoveryRequired(
                    f"transaction recovery cleanup failed; evidence: {recovery_root}"
                ) from error
            raise

        cleanup_errors = self._remove_recovery(
            recovery_root,
            created_recovery_directories,
        )
        if cleanup_errors:
            raise RecoveryRequired(
                f"transaction committed but recovery cleanup failed; evidence: {recovery_root}"
            )
        changed = tuple(
            path for path in self.guard.changed_paths(self._baseline) if path in self._allowed
        )
        return TransactionResult(False, planned, changed)

    def _restore(
        self,
        originals: Mapping[str, bytes | None],
        temporary: Mapping[str, Path],
        transaction_id: str,
        created_directories: Iterable[Path],
    ) -> list[Exception]:
        errors: list[Exception] = []
        for relative, original in originals.items():
            target = self._allowed[relative]
            try:
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    rollback = target.with_name(
                        f".{target.name}.project-governance-{transaction_id}.rollback.tmp"
                    )
                    rollback.write_bytes(original)
                    _replace(rollback, target)
            except Exception as error:
                errors.append(error)
        for staged in temporary.values():
            try:
                staged.unlink(missing_ok=True)
            except Exception as error:
                errors.append(error)
        errors.extend(_remove_created_directories(created_directories))
        return errors

    def _remove_recovery(
        self,
        recovery_root: Path,
        created_directories: Iterable[Path],
    ) -> list[Exception]:
        errors: list[Exception] = []
        try:
            shutil.rmtree(recovery_root)
        except FileNotFoundError:
            pass
        except Exception as error:
            errors.append(error)
            return errors
        errors.extend(
            _remove_created_directories(
                path for path in created_directories if path != recovery_root
            )
        )
        return errors
