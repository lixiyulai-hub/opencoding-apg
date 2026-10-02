"""Codex-compatible local skill host adapter.

Codex's documented local skill convention is ``$CODEX_HOME/skills/<name>/SKILL.md``.
This module implements the deterministic project-side part of that contract:
discover, validate, and import the declared OpenCoding entrypoint.  It does not
pretend to observe a managed Codex process; that observation remains a separate
field in every report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid
from typing import Any, Iterable

from .agent_adapter import LocalAgentAdapter


class CodexHostError(ValueError):
    """A skill cannot be discovered, installed, or loaded by this adapter."""


@dataclass(frozen=True)
class SkillResource:
    directory: Path
    source: str


def _safe_host_root(path: Path) -> Path:
    """Reject symlinked host roots and linked existing ancestors."""
    if not path.is_absolute():
        raise CodexHostError("host root must be absolute")
    current = path.absolute()
    while True:
        if current.is_symlink():
            raise CodexHostError(f"host root contains a symlink: {current}")
        parent = current.parent
        if parent == current:
            break
        current = parent
    if path.exists() and not path.is_dir():
        raise CodexHostError("host root must be a directory")
    return path


def _frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        raise CodexHostError("SKILL.md frontmatter is missing")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise CodexHostError("SKILL.md frontmatter has no closing delimiter") from exc
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise CodexHostError("SKILL.md frontmatter contains an invalid line")
        key, value = (part.strip() for part in line.split(":", 1))
        if key not in {"name", "description"} or not value:
            raise CodexHostError("SKILL.md name/description is invalid")
        fields[key] = value.strip("'\"")
    if fields.get("name") != "opencoding" or not fields.get("description"):
        raise CodexHostError("SKILL.md does not identify the opencoding skill")
    return fields


def _manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexHostError("skill.json is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("name") != "opencoding":
        raise CodexHostError("skill.json does not identify the opencoding skill")
    if value.get("entrypoint") != "opencoding.agent_adapter:LocalAgentAdapter":
        raise CodexHostError("skill.json entrypoint is not supported")
    if value.get("host_independent") is not True or value.get("sandbox") is not False:
        raise CodexHostError("skill.json host/sandbox contract is invalid")
    if value.get("external_actions") is not False or value.get("model_available") is not False:
        raise CodexHostError("skill.json external/model contract is invalid")
    return value


def _is_resource(directory: Path) -> bool:
    return (directory / "SKILL.md").is_file() and (directory / "skill.json").is_file()


def _resource_candidates(project_root: Path, codex_home: Path | None, extra_roots: Iterable[Path]) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    if codex_home is not None:
        candidates.append((codex_home / "skills" / "opencoding", "codex_home"))
    candidates.extend([
        (project_root / ".agents" / "skills" / "opencoding", "project_standard"),
        (project_root / "skills" / "opencoding", "project_source_fallback"),
    ])
    for root in extra_roots:
        root = Path(root)
        candidates.append((root / "opencoding", "extra_root"))
        candidates.append((root, "extra_root_direct"))
    # A wheel/sdist has no project checkout, so expose its explicit package
    # resource after all caller-provided project and host locations.
    candidates.append((Path(__file__).resolve().parent / "resources" / "skill", "installed_package"))
    return candidates


class CodexSkillHost:
    """Discover and load OpenCoding through Codex's local skill path contract."""

    def __init__(self, project_root: str | os.PathLike[str] | Path, *, codex_home: str | os.PathLike[str] | Path | None = None, extra_roots: Iterable[str | os.PathLike[str] | Path] = ()):
        root = Path(project_root)
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise CodexHostError("project_root must be an existing absolute directory")
        self.project_root = root.resolve()
        self.codex_home = _safe_host_root(Path(codex_home)) if codex_home is not None else None
        self.extra_roots = tuple(Path(item).resolve() for item in extra_roots)

    def discover(self) -> SkillResource:
        for directory, source in _resource_candidates(self.project_root, self.codex_home, self.extra_roots):
            if directory.exists() and not _is_resource(directory):
                # A partially installed higher-priority host resource must not
                # silently fall back to a different project copy.
                if source in {"codex_home", "extra_root", "extra_root_direct"}:
                    raise CodexHostError(f"incomplete skill resource: {directory}")
                continue
            if not _is_resource(directory):
                continue
            _frontmatter(directory / "SKILL.md")
            _manifest(directory / "skill.json")
            return SkillResource(directory=directory.resolve(), source=source)
        raise CodexHostError("no complete opencoding skill resource in project or Codex host roots")

    def load(self, *, target_platform: str | None = None) -> dict[str, Any]:
        resource = self.discover()
        manifest = _manifest(resource.directory / "skill.json")
        module_name, _separator, attr_name = str(manifest["entrypoint"]).partition(":")
        old_path = list(sys.path)
        try:
            sys.path.insert(0, str(self.project_root))
            module = importlib.import_module(module_name)
            entrypoint = getattr(module, attr_name)
            if entrypoint is not LocalAgentAdapter:
                # Subclasses/wrappers may be valid callables; do not execute an
                # unrecognized object as a host adapter.
                if not callable(entrypoint):
                    raise CodexHostError("skill entrypoint is not callable")
            adapter = entrypoint(self.project_root)
            capabilities = adapter.capabilities(target_platform=target_platform)
        except (ImportError, AttributeError, TypeError, ValueError, OSError) as exc:
            raise CodexHostError(f"skill entrypoint failed to load: {exc}") from exc
        finally:
            sys.path[:] = old_path
        return {
            "format_valid": True,
            "resource_discovered": True,
            "resource_source": resource.source,
            "resource_directory": str(resource.directory),
            "host_root_contract": "$CODEX_HOME/skills/<name>/SKILL.md",
            "host_adapter_loaded": True,
            "capabilities": capabilities,
            "codex_managed_loader_observed": None,
            "codex_managed_loader_status": "not_observable_without_host_response",
            "status": "format_valid_resource_discovered_adapter_loaded_codex_loader_unobserved",
        }


def install_skill(source_root: str | os.PathLike[str] | Path, codex_home: str | os.PathLike[str] | Path, *, overwrite: bool = False) -> dict[str, Any]:
    """Install only the resource files into an explicit private Codex home."""
    source_root = Path(source_root).resolve()
    codex_home = _safe_host_root(Path(codex_home))
    source = None
    for candidate, _source_name in _resource_candidates(source_root, None, ()):
        if _is_resource(candidate):
            source = candidate
            break
    if source is None:
        raise CodexHostError("source root has no complete opencoding skill resource")
    _frontmatter(source / "SKILL.md")
    _manifest(source / "skill.json")
    target = codex_home / "skills" / "opencoding"
    if target.is_symlink():
        raise CodexHostError(f"destination is a symlink: {target}")
    if target.exists() and not overwrite:
        raise CodexHostError(f"destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise CodexHostError(f"destination parent is a symlink: {target.parent}")
    stage = Path(tempfile.mkdtemp(prefix=f".opencoding-skill-{uuid.uuid4().hex}-", dir=str(target.parent)))
    copied: list[str] = []
    try:
        for name in ("SKILL.md", "skill.json", "README.md"):
            item = source / name
            if not item.is_file():
                if name == "README.md":
                    continue
                raise CodexHostError(f"skill file missing: {item}")
            destination = stage / name
            shutil.copyfile(item, destination)
            copied.append(name)
        backup: Path | None = None
        if target.exists():
            backup = target.parent / f".opencoding-old-{uuid.uuid4().hex}"
            os.replace(target, backup)
        try:
            os.replace(stage, target)
        except Exception:
            if backup is not None and not target.exists():
                os.replace(backup, target)
            raise
        if backup is not None:
            shutil.rmtree(backup)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {
        "installed": True,
        "source": str(source),
        "destination": str(target),
        "host_root_contract": "$CODEX_HOME/skills/<name>/SKILL.md",
        "copied": copied,
        "overwrite": overwrite,
        "installed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


__all__ = ["CodexHostError", "CodexSkillHost", "SkillResource", "install_skill"]
