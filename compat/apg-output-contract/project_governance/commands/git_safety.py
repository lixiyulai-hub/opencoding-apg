"""Read-only Git boundary and baseline safety inspection."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

from ..model import Receipt
from ..path_guard import PathViolation, WorkspaceGuard
from ..receipts import build_receipt
from ..storage import digest


DEFAULT_LARGE_FILE_LIMIT = 10 * 1024 * 1024
_MAX_SCAN_FILES = 20_000
_MAX_REPORTED_FILES = 100
_PRUNED_DIRS = {
    ".git",
    ".governance",
    ".tmp",
    ".pytest_cache",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
}
_SENSITIVE_NAMES = (
    re.compile(r"^\.env(?:\..+)?$", re.IGNORECASE),
    re.compile(r"(?:^|[._-])(credentials?|secrets?|tokens?|passwords?)(?:[._-]|$)", re.IGNORECASE),
    re.compile(r"(?:id_rsa|id_ed25519|private[-_]?key)", re.IGNORECASE),
    re.compile(r"\.(?:pem|key|p12|pfx|jks)$", re.IGNORECASE),
)


@dataclass(frozen=True)
class GitSafetyResult:
    ok: bool
    exit_code: int
    receipt: Receipt


def _run_git(root: Path, args: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _run_version() -> str:
    result = subprocess.run(
        ["git", "--version"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or "git --version failed")
    return result.stdout.strip()


def _value(root: Path, args: tuple[str, ...]) -> tuple[str, int]:
    result = _run_git(root, args)
    return result.stdout.strip(), result.returncode


def _project_git_state(root: Path) -> dict[str, object]:
    marker = root / ".git"
    if not marker.exists() and not marker.is_symlink():
        return {
            "exists": False,
            "kind": "absent",
            "file_count": 0,
            "has_head": False,
            "has_config": False,
        }
    if marker.is_file():
        content = marker.read_text(encoding="utf-8", errors="replace")
        return {
            "exists": True,
            "kind": "gitfile",
            "file_count": 1,
            "has_head": content.lower().startswith("gitdir:"),
            "has_config": False,
        }
    files = tuple(path for path in marker.rglob("*") if path.is_file())
    return {
        "exists": True,
        "kind": "directory",
        "file_count": len(files),
        "has_head": (marker / "HEAD").is_file(),
        "has_config": (marker / "config").is_file(),
    }


def _probe_git(root: Path) -> dict[str, object]:
    version = _run_version()
    top, top_code = _value(root, ("rev-parse", "--show-toplevel"))
    git_dir, git_dir_code = _value(root, ("rev-parse", "--git-dir"))
    branch, branch_code = _value(root, ("symbolic-ref", "--short", "-q", "HEAD"))
    _, head_code = _value(root, ("rev-parse", "--verify", "HEAD"))
    status, status_code = _value(root, ("status", "--porcelain=v1", "--branch"))
    user_name, user_name_code = _value(root, ("config", "--local", "--get", "user.name"))
    user_email, user_email_code = _value(root, ("config", "--local", "--get", "user.email"))
    dirty_rows = tuple(
        line for line in status.splitlines()
        if line and not line.startswith("##")
    )
    return {
        "version": version,
        "project_git": _project_git_state(root),
        "resolved_root": top if top_code == 0 else "",
        "resolved_git_dir": git_dir if git_dir_code == 0 else "",
        "resolved_root_ok": top_code == 0,
        "branch": branch if branch_code == 0 else "",
        "branch_state": "unborn" if head_code != 0 and branch else "detached-or-unknown" if not branch else "committed",
        "head_present": head_code == 0,
        "status_code": status_code,
        "dirty": bool(dirty_rows),
        "status_lines": tuple(dirty_rows[:_MAX_REPORTED_FILES]),
        "status_truncated": len(dirty_rows) > _MAX_REPORTED_FILES,
        "local_user_name": user_name if user_name_code == 0 else "",
        "local_user_email": user_email if user_email_code == 0 else "",
        "local_identity_configured": user_name_code == 0 and user_email_code == 0 and bool(user_name and user_email),
    }


def _scan_files(root: Path, limit: int) -> dict[str, object]:
    sensitive: list[dict[str, object]] = []
    sensitive_count = 0
    large: list[dict[str, object]] = []
    scanned = 0
    truncated = False
    for current_text, directory_names, file_names in os.walk(root, topdown=True):
        directory_names[:] = sorted(
            name for name in directory_names
            if name not in _PRUNED_DIRS and not (current_text == str(root) and name == ".git")
        )
        for name in sorted(file_names):
            scanned += 1
            if scanned > _MAX_SCAN_FILES:
                truncated = True
                break
            path = Path(current_text) / name
            relative = path.relative_to(root).as_posix()
            sensitive_name = any(pattern.search(name) for pattern in _SENSITIVE_NAMES) and name.lower() not in {".env.example", ".env.sample"}
            try:
                size = path.stat(follow_symlinks=False).st_size
            except OSError:
                if sensitive_name:
                    sensitive_count += 1
                    if len(sensitive) < _MAX_REPORTED_FILES:
                        sensitive.append(
                            {
                                "path_sha256": digest(relative.encode("utf-8")),
                                "bytes": None,
                            }
                        )
                continue
            if sensitive_name:
                sensitive_count += 1
                if len(sensitive) < _MAX_REPORTED_FILES:
                    sensitive.append(
                        {
                            "path_sha256": digest(relative.encode("utf-8")),
                            "bytes": size,
                        }
                    )
            if size >= limit:
                large.append({"relative_path": relative, "bytes": size})
        if truncated:
            break
    return {
        "scanned_file_count": min(scanned, _MAX_SCAN_FILES),
        "scan_truncated": truncated,
        "sensitive_file_count": sensitive_count,
        "sensitive_files": tuple(sorted(sensitive, key=lambda item: str(item["path_sha256"]))),
        "sensitive_truncated": sensitive_count > _MAX_REPORTED_FILES,
        "large_files": tuple(sorted(large, key=lambda item: (str(item["relative_path"]), int(item["bytes"])))[:_MAX_REPORTED_FILES]),
        "large_file_limit_bytes": limit,
    }


def _boundary_summary(root: Path, git: Mapping[str, object]) -> dict[str, object]:
    resolved = Path(str(git["resolved_root"])).resolve() if git["resolved_root"] else None
    project_root = root.resolve()
    if resolved is None:
        relation = "unresolved"
    elif resolved == project_root:
        relation = "independent"
    elif project_root.is_relative_to(resolved):
        relation = "ancestor"
    else:
        relation = "unexpected"
    project_git = dict(git["project_git"])
    project_git["independent_valid"] = relation == "independent" and bool(project_git["has_head"] and project_git["has_config"])
    project_git["empty"] = bool(project_git["exists"] and not project_git["file_count"])
    return {
        "project_root": str(project_root),
        "resolved_git_root": str(resolved) if resolved else "",
        "repository_relation": relation,
        "project_git": project_git,
        "parent_repository": relation == "ancestor",
    }


def _preview_outputs(root: Path) -> dict[str, object]:
    return {
        "status": "PREVIEW_ONLY",
        "execution_performed": False,
        "prospective_paths": (
            ".gitignore",
            ".gitattributes",
            ".git/HEAD",
            ".git/config",
            "initial-baseline-commit",
        ),
        "excluded_actions": (
            "remote",
            "push",
            "publish",
            "parent-repository-change",
            "automatic-commit-without-owner-confirmation",
        ),
        "rollback": "Remove only a separately approved project-local Git baseline after matching post-state; preserve source, receipts, and parent repository.",
    }


def run_git_safety(
    target: str | Path,
    *,
    preview: bool = False,
    large_file_limit: int = DEFAULT_LARGE_FILE_LIMIT,
) -> GitSafetyResult:
    try:
        guard = WorkspaceGuard(Path(target))
        if isinstance(large_file_limit, bool) or not isinstance(large_file_limit, int) or large_file_limit <= 0:
            raise ValueError("large_file_limit must be a positive integer")
        before = guard.snapshot()
        before_git = _project_git_state(guard.root)
        git = _probe_git(guard.root)
        boundary = _boundary_summary(guard.root, git)
        scan = _scan_files(guard.root, large_file_limit)
        after_git = _project_git_state(guard.root)
        changed = guard.changed_paths(before)
        read_only_passed = not changed and before_git == after_git
        warnings: list[str] = []
        if boundary["repository_relation"] == "ancestor":
            warnings.append("commands resolve to an ancestor repository")
        if boundary["project_git"]["empty"]:
            warnings.append("project .git directory is empty")
        if git["branch_state"] == "unborn":
            warnings.append("resolved repository has no commit yet")
        if not git["local_identity_configured"]:
            warnings.append("project-local Git identity is unset")
        if scan["sensitive_file_count"]:
            warnings.append("sensitive-looking filenames require review")
        if scan["large_files"]:
            warnings.append("large files require review before baseline")
        if scan["scan_truncated"]:
            warnings.append("file scan reached its bounded limit")
        outputs: dict[str, object] = {
            "git": git,
            "boundary": boundary,
            "scan": scan,
            "read_only_proof": {
                "passed": read_only_passed,
                "changed_paths": tuple(changed),
                "project_git_unchanged": before_git == after_git,
            },
            "warnings": tuple(warnings),
        }
        if preview:
            outputs["preview"] = _preview_outputs(guard.root)
        classification = "scope-violation" if not read_only_passed else "warn" if warnings else "pass"
        receipt = build_receipt(
            command="git-safety",
            target_fingerprint=digest(guard.snapshot()),
            authorized_scope=(".",),
            outputs=outputs,
            classification=classification,
        )
        return GitSafetyResult(read_only_passed, 4 if not read_only_passed else 0, receipt)
    except (OSError, ValueError, PathViolation, subprocess.SubprocessError) as error:
        receipt = build_receipt(
            command="git-safety",
            authorized_scope=(".",),
            outputs={"status": "invalid", "error_type": type(error).__name__, "message": str(error)},
            classification="invalid",
        )
        return GitSafetyResult(False, 2, receipt)


__all__ = ["DEFAULT_LARGE_FILE_LIMIT", "GitSafetyResult", "run_git_safety"]
