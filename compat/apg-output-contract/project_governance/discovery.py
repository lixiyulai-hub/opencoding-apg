from __future__ import annotations

import configparser
import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import Evidence, Finding, ProjectProfile


@dataclass(frozen=True)
class DiscoveredCommand:
    name: str
    command: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class DiscoveryResult:
    profile: ProjectProfile
    evidence: tuple[Evidence, ...]
    findings: tuple[Finding, ...]
    commands: tuple[DiscoveredCommand, ...]
    truncated: bool = False


DISCOVERY_PRUNED_DIRECTORY_NAMES = frozenset(
    {
        ".cache",
        ".douyin-candidate-cache",
        ".git",
        ".gradle",
        ".local-models",
        ".media-cache",
        ".mypy_cache",
        ".pytest_cache",
        ".resolver-research",
        ".ruff_cache",
        ".sheen-ai-data",
        ".tmp",
        ".tox",
        ".venv",
        ".voice-cache",
        "DerivedData",
        "Pods",
        "__pycache__",
        "backups",
        "build",
        "cache",
        "checkpoints",
        "coverage",
        "dist",
        "models",
        "node_modules",
        "out",
        "target",
        "third_party",
        "tmp",
        "vendor",
        "venv",
        "weights",
    }
)
DISCOVERY_PRUNED_DIRECTORY_PREFIXES = ("target-",)
_SKIP_DIR_KEYS = frozenset(
    item.casefold() for item in DISCOVERY_PRUNED_DIRECTORY_NAMES
)
_SKIP_DIR_PREFIXES = DISCOVERY_PRUNED_DIRECTORY_PREFIXES
_CONTENT_INSPECTION_NAMES = frozenset({"package.json", "pyproject.toml"})
_CONTENT_INSPECTION_PATHS = frozenset({".git/config", ".git/head"})
_BINARY_EXTENSIONS = frozenset(
    {
        ".7z",
        ".a",
        ".bin",
        ".class",
        ".dll",
        ".dylib",
        ".exe",
        ".gif",
        ".gz",
        ".ico",
        ".jar",
        ".jpeg",
        ".jpg",
        ".mov",
        ".mp3",
        ".mp4",
        ".o",
        ".onnx",
        ".pdf",
        ".pt",
        ".pth",
        ".png",
        ".pyc",
        ".so",
        ".safetensors",
        ".tar",
        ".tgz",
        ".wav",
        ".woff",
        ".woff2",
        ".zip",
    }
)
_CREATIVE_DIRECTORIES = frozenset(
    {"assets", "content", "creative", "images", "media", "static"}
)
_LICENSE_NAMES = frozenset(
    {"copying", "license", "license.md", "license.txt", "notice"}
)
_WEB_DEPENDENCIES = frozenset(
    {"@angular/core", "next", "nuxt", "react", "svelte", "vite", "vue", "webpack"}
)
_DESKTOP_DEPENDENCIES = frozenset({"@tauri-apps/api", "electron"})
_BACKEND_DEPENDENCIES = frozenset(
    {"django", "express", "fastapi", "flask", "koa", "nestjs", "spring-boot"}
)
_DATABASE_DEPENDENCIES = frozenset(
    {"mongoose", "mysql", "pg", "postgres", "prisma", "redis", "sqlalchemy"}
)
_QUEUE_DEPENDENCIES = frozenset({"amqplib", "celery", "kafka", "rabbitmq"})
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key|private[_-]?key)\b"
    r"\s*([=:])\s*([^\s,;@]+)"
)
_URL_USERINFO = re.compile(r"(://)([^/@\s]+)@")


def _safe(value: object) -> str:
    text = str(value)
    text = _URL_USERINFO.sub(r"\1[redacted]@", text)
    return _SECRET_ASSIGNMENT.sub(r"\1\2[redacted]", text)


def _finding(
    rule_id: str,
    category: str,
    severity: str,
    path: str,
    message: str,
    evidence_refs: tuple[str, ...] = (),
) -> Finding:
    return Finding(
        rule_id=rule_id,
        category=category,
        severity=severity,
        confidence="high",
        path=_safe(path),
        message=_safe(message),
        evidence_refs=tuple(_safe(item) for item in evidence_refs),
    )


def _evidence(
    source: str,
    kind: str,
    detail: str,
    confidence: str = "high",
) -> Evidence:
    return Evidence(
        source=_safe(source),
        kind=kind,
        detail=_safe(detail),
        confidence=confidence,
    )


def _validate_limit(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _relative_or_none(path: Path, root: Path) -> str | None:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def _real_git_root(root: Path) -> Path | None:
    git_root = root / ".git"
    try:
        if git_root.is_symlink() or not git_root.is_dir():
            return None
        resolved = git_root.resolve(strict=True)
    except OSError:
        return None
    if resolved != git_root or not resolved.is_relative_to(root):
        return None
    return git_root


def _open_verified_binary(path: Path, root: Path):
    handle = path.open("rb")
    try:
        descriptor_stat = os.fstat(handle.fileno())
        resolved = path.resolve(strict=True)
        path_stat = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or resolved != path
            or not resolved.is_relative_to(root)
            or not stat.S_ISREG(descriptor_stat.st_mode)
            or not os.path.samestat(descriptor_stat, path_stat)
        ):
            raise OSError("path changed or crossed the discovery root")
    except Exception:
        handle.close()
        raise
    return handle, descriptor_stat


def _iter_workspace_files(
    root: Path,
    directory_markers: set[str],
    findings: list[Finding],
):
    git_root = _real_git_root(root)
    if git_root is not None:
        for name in ("HEAD", "config"):
            candidate = git_root / name
            if candidate.is_file() and not candidate.is_symlink():
                yield candidate, f".git/{name}"

    def on_error(error: OSError) -> None:
        source = _relative_or_none(Path(error.filename), root) if error.filename else "."
        findings.append(
            _finding(
                "discovery.inaccessible",
                "discovery",
                "warning",
                source or ".",
                type(error).__name__,
            )
        )

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
            relative = candidate.relative_to(root).as_posix()
            if (
                name.casefold() in _SKIP_DIR_KEYS
                or name.casefold().startswith(_SKIP_DIR_PREFIXES)
            ):
                continue
            try:
                if candidate.is_symlink():
                    continue
                resolved = candidate.resolve(strict=False)
            except OSError:
                findings.append(
                    _finding(
                        "discovery.inaccessible",
                        "discovery",
                        "warning",
                        relative,
                        "directory resolution failed",
                    )
                )
                continue
            if not resolved.is_relative_to(root):
                continue
            lowered = name.lower()
            if lowered in _CREATIVE_DIRECTORIES or lowered.endswith(".xcodeproj"):
                directory_markers.add(relative)
            retained.append(name)
        directory_names[:] = retained

        for name in sorted(file_names):
            candidate = current / name
            relative = candidate.relative_to(root).as_posix()
            if relative.startswith(".git/"):
                continue
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                resolved = candidate.resolve(strict=True)
            except OSError:
                findings.append(
                    _finding(
                        "discovery.inaccessible",
                        "discovery",
                        "warning",
                        relative,
                        "file resolution failed",
                    )
                )
                continue
            if not resolved.is_relative_to(root):
                continue
            if candidate.suffix.lower() in _BINARY_EXTENSIONS:
                continue
            yield candidate, relative


def _bounded_files(
    root: Path,
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
    directory_markers: set[str],
    evidence: set[Evidence],
    findings: list[Finding],
) -> tuple[list[str], dict[str, bytes], bool]:
    selected_paths: list[str] = []
    selected_content: dict[str, bytes] = {}
    total_bytes = 0
    truncated = False
    aggregate_limit_reported = False
    for index, (path, relative) in enumerate(
        _iter_workspace_files(root, directory_markers, findings)
    ):
        if index >= max_files:
            truncated = True
            findings.append(
                _finding(
                    "discovery.truncated",
                    "discovery",
                    "warning",
                    ".",
                    f"file-count limit reached: {max_files}",
                )
            )
            break
        selected_paths.append(relative)
        lowered = relative.casefold()
        if (
            Path(relative).name.casefold() not in _CONTENT_INSPECTION_NAMES
            and lowered not in _CONTENT_INSPECTION_PATHS
        ):
            continue
        remaining_bytes = max_total_bytes - total_bytes
        try:
            handle, descriptor_stat = _open_verified_binary(path, root)
            with handle:
                opened_size = descriptor_stat.st_size
                if opened_size > max_file_bytes:
                    truncated = True
                    findings.append(
                        _finding(
                            "discovery.truncated",
                            "discovery",
                            "warning",
                            relative,
                            f"file exceeds byte limit: {max_file_bytes}",
                        )
                    )
                    continue
                if opened_size > remaining_bytes:
                    truncated = True
                    if not aggregate_limit_reported:
                        findings.append(
                            _finding(
                                "discovery.truncated",
                                "discovery",
                                "warning",
                                ".",
                                f"aggregate byte limit reached: {max_total_bytes}",
                            )
                        )
                        aggregate_limit_reported = True
                    continue
                payload = handle.read(opened_size)
                final_size = os.fstat(handle.fileno()).st_size
        except OSError as error:
            findings.append(
                _finding(
                    "discovery.inaccessible",
                    "discovery",
                    "warning",
                    relative,
                    type(error).__name__,
                )
            )
            continue
        total_bytes += len(payload)
        if len(payload) != opened_size or final_size != opened_size:
            truncated = True
            findings.append(
                _finding(
                    "discovery.truncated",
                    "discovery",
                    "warning",
                    relative,
                    "file changed during bounded read",
                )
            )
            continue
        selected_content[relative] = payload

    if truncated:
        evidence.add(_evidence(".", "limit", "discovery was bounded by configured limits"))
    return selected_paths, selected_content, truncated


def _read_text(
    payload: bytes,
    relative: str,
    findings: list[Finding],
) -> str | None:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        findings.append(
            _finding(
                "discovery.inaccessible",
                "discovery",
                "warning",
                relative,
                type(error).__name__,
            )
        )
        return None


def _parse_package(
    payload: bytes,
    relative: str,
    evidence: set[Evidence],
    findings: list[Finding],
) -> dict[str, Any] | None:
    text = _read_text(payload, relative, findings)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        findings.append(
            _finding(
                "discovery.parse",
                "discovery",
                "warning",
                relative,
                type(error).__name__,
            )
        )
        return None
    evidence.add(_evidence(relative, "manifest", "parsed package manifest"))
    if not isinstance(value, dict):
        findings.append(
            _finding(
                "discovery.parse",
                "discovery",
                "warning",
                relative,
                "package manifest root must be an object",
            )
        )
        return None
    return value


def _parse_pyproject(
    payload: bytes,
    relative: str,
    evidence: set[Evidence],
    findings: list[Finding],
) -> dict[str, Any] | None:
    text = _read_text(payload, relative, findings)
    if text is None:
        return None
    try:
        value = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        findings.append(
            _finding(
                "discovery.parse",
                "discovery",
                "warning",
                relative,
                type(error).__name__,
            )
        )
        return None
    evidence.add(_evidence(relative, "manifest", "parsed Python manifest"))
    return value


def _dependency_name(value: object) -> str:
    text = str(value).strip().lower()
    return re.split(r"[<>=!~\[\s;]", text, maxsplit=1)[0]


def _package_facts(
    document: dict[str, Any],
    relative: str,
    dependencies: set[str],
    commands: set[DiscoveredCommand],
    findings: list[Finding],
) -> None:
    for field in ("dependencies", "devDependencies"):
        value = document.get(field, {})
        if not isinstance(value, dict):
            findings.append(
                _finding(
                    "discovery.parse",
                    "discovery",
                    "warning",
                    relative,
                    f"{field} must be an object",
                )
            )
            continue
        dependencies.update(str(name).lower() for name in value)

    scripts = document.get("scripts", {})
    if not isinstance(scripts, dict):
        findings.append(
            _finding(
                "discovery.parse",
                "discovery",
                "warning",
                relative,
                "scripts must be an object",
            )
        )
        return
    for name, value in scripts.items():
        if isinstance(value, str):
            argv = (value,)
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            argv = tuple(value)
        else:
            findings.append(
                _finding(
                    "discovery.parse",
                    "discovery",
                    "warning",
                    relative,
                    f"script {name} must be a string or string array",
                )
            )
            continue
        commands.add(DiscoveredCommand(str(name), argv, _safe(relative)))


def _script_table(
    value: object,
    relative: str,
    commands: set[DiscoveredCommand],
    findings: list[Finding],
) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        findings.append(
            _finding(
                "discovery.parse",
                "discovery",
                "warning",
                relative,
                "script table must be an object",
            )
        )
        return
    for name, command in value.items():
        if not isinstance(command, str):
            findings.append(
                _finding(
                    "discovery.parse",
                    "discovery",
                    "warning",
                    relative,
                    f"script {name} must be a string",
                )
            )
            continue
        commands.add(DiscoveredCommand(str(name), (command,), _safe(relative)))


def _pyproject_facts(
    document: dict[str, Any],
    relative: str,
    dependencies: set[str],
    commands: set[DiscoveredCommand],
    findings: list[Finding],
) -> None:
    project = document.get("project", {})
    if project is not None and not isinstance(project, dict):
        findings.append(
            _finding(
                "discovery.parse",
                "discovery",
                "warning",
                relative,
                "project table must be an object",
            )
        )
        project = {}
    if isinstance(project, dict):
        project_dependencies = project.get("dependencies", [])
        if not isinstance(project_dependencies, list) or any(
            not isinstance(item, str) for item in project_dependencies
        ):
            findings.append(
                _finding(
                    "discovery.parse",
                    "discovery",
                    "warning",
                    relative,
                    "project.dependencies must be a string array",
                )
            )
        else:
            dependencies.update(_dependency_name(item) for item in project_dependencies)
        _script_table(project.get("scripts"), relative, commands, findings)

    tool = document.get("tool", {})
    if isinstance(tool, dict):
        poetry = tool.get("poetry", {})
        if isinstance(poetry, dict):
            _script_table(poetry.get("scripts"), relative, commands, findings)


def _git_facts(
    selected: dict[str, bytes],
    root: Path,
    evidence: set[Evidence],
    findings: list[Finding],
) -> tuple[bool, bool]:
    remote_present = False
    revision_present = False
    config_payload = selected.get(".git/config")
    if config_payload is not None:
        text = _read_text(config_payload, ".git/config", findings)
        if text is not None:
            try:
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_string(text)
                for section in sorted(parser.sections()):
                    if section.startswith("remote ") and parser.has_option(section, "url"):
                        remote_present = True
                        evidence.add(
                            _evidence(
                                ".git/config",
                                "remote",
                                f"remote configured: {parser.get(section, 'url')}",
                            )
                        )
            except configparser.Error as error:
                findings.append(
                    _finding(
                        "discovery.parse",
                        "git",
                        "warning",
                        ".git/config",
                        type(error).__name__,
                    )
                )

    head_payload = selected.get(".git/HEAD")
    if head_payload is not None:
        text = _read_text(head_payload, ".git/HEAD", findings)
        if text is not None and text.strip():
            revision_present = True
            evidence.add(
                _evidence(
                    ".git/HEAD",
                    "revision",
                    f"HEAD: {text.strip()}",
                )
            )

    git_root = _real_git_root(root)
    git_index = git_root / "index" if git_root is not None else None
    if git_index is not None and os.path.lexists(git_index):
        try:
            handle, _ = _open_verified_binary(git_index, root)
        except OSError as error:
            findings.append(
                _finding(
                    "discovery.inaccessible",
                    "git",
                    "warning",
                    ".git/index",
                    type(error).__name__,
                )
            )
        else:
            handle.close()
            evidence.add(_evidence(".git/index", "git-index", "Git index marker present"))
            findings.append(
                _finding(
                    "evidence.git-dirty-state",
                    "git",
                    "warning",
                    ".",
                    "dirty state is unknown because discovery does not execute Git",
                    (".git/index",),
                )
            )
    return remote_present, revision_present


def _sorted_evidence(values: set[Evidence]) -> tuple[Evidence, ...]:
    return tuple(sorted(values, key=lambda item: (item.source, item.kind, item.detail)))


def _sorted_findings(values: list[Finding]) -> tuple[Finding, ...]:
    return tuple(
        sorted(
            set(values),
            key=lambda item: (item.path, item.rule_id, item.message),
        )
    )


def discover_project(
    root: Path,
    *,
    max_files: int = 5000,
    max_file_bytes: int = 1_048_576,
    max_total_bytes: int = 8_388_608,
) -> DiscoveryResult:
    _validate_limit("max_files", max_files)
    _validate_limit("max_file_bytes", max_file_bytes)
    _validate_limit("max_total_bytes", max_total_bytes)
    try:
        resolved_root = Path(root).resolve(strict=True)
    except OSError as error:
        raise ValueError("root must exist and be a directory") from error
    if not resolved_root.is_dir():
        raise ValueError("root must exist and be a directory")

    evidence: set[Evidence] = set()
    findings: list[Finding] = []
    commands: set[DiscoveredCommand] = set()
    directory_markers: set[str] = set()
    selected_paths, selected_content, truncated = _bounded_files(
        resolved_root,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        directory_markers=directory_markers,
        evidence=evidence,
        findings=findings,
    )
    selected_by_path = selected_content

    project_types: set[str] = set()
    dependencies: set[str] = set()
    license_present = False
    workflow_present = False

    for marker in sorted(directory_markers):
        name = Path(marker).name.lower()
        if name in _CREATIVE_DIRECTORIES:
            project_types.add("creative/content")
            evidence.add(_evidence(marker, "creative", "creative directory marker"))
        if name.endswith(".xcodeproj"):
            project_types.add("app")
            evidence.add(_evidence(marker, "manifest", "Xcode project marker"))

    for relative in selected_paths:
        lowered = relative.lower()
        name = Path(relative).name.lower()
        parts = {part.lower() for part in Path(relative).parts}

        if name == "package.json" and relative in selected_content:
            payload = selected_content[relative]
            document = _parse_package(payload, relative, evidence, findings)
            if document is not None:
                _package_facts(document, relative, dependencies, commands, findings)
        elif name == "pyproject.toml" and relative in selected_content:
            payload = selected_content[relative]
            document = _parse_pyproject(payload, relative, evidence, findings)
            if document is not None:
                _pyproject_facts(document, relative, dependencies, commands, findings)
        elif name in _LICENSE_NAMES:
            license_present = True
            evidence.add(_evidence(relative, "license", "license marker present"))

        if lowered.startswith(".github/workflows/"):
            workflow_present = True
            project_types.add("automation")
            evidence.add(_evidence(relative, "workflow", "workflow marker present"))
        if name in {"dockerfile", "docker-compose.yml", "docker-compose.yaml"}:
            project_types.add("automation")
            evidence.add(_evidence(relative, "container", "container marker present"))
        if name in {"playbook.yml", "playbook.yaml"}:
            project_types.add("automation")
            evidence.add(_evidence(relative, "automation", "automation marker present"))
        if any(part in _CREATIVE_DIRECTORIES for part in parts):
            project_types.add("creative/content")
            evidence.add(_evidence(relative, "creative", "creative content marker"))
        if name.startswith("requirements") and name.endswith(".txt"):
            project_types.add("api/backend")
            evidence.add(_evidence(relative, "manifest", "Python dependency marker"))
        if name in {"cargo.toml", "go.mod"}:
            project_types.add("api/backend")
            evidence.add(_evidence(relative, "manifest", "compiled project marker"))
        if name in {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}:
            project_types.add("app")
            evidence.add(_evidence(relative, "manifest", "Gradle project marker"))
        if name == "tauri.conf.json" or "src-tauri" in parts:
            project_types.add("desktop")
            evidence.add(_evidence(relative, "manifest", "Tauri project marker"))
        if (
            lowered.startswith(".governance/")
            or name in {"agents.md", "claude.md", "architecture.md", "quality_gates.md"}
            or lowered.startswith(".cursor/rules/")
        ):
            evidence.add(_evidence(relative, "governance", "governance marker present"))

    if dependencies & _DESKTOP_DEPENDENCIES:
        project_types.add("desktop")
    if dependencies & _WEB_DEPENDENCIES:
        project_types.add("web")
    if dependencies & _BACKEND_DEPENDENCIES:
        project_types.add("api/backend")

    remote_present, revision_present = _git_facts(
        selected_by_path,
        resolved_root,
        evidence,
        findings,
    )
    if remote_present and not revision_present:
        findings.append(
            _finding(
                "upstream.revision.missing",
                "provenance",
                "warning",
                ".git/HEAD",
                "imported project has no revision evidence",
                (".git/config",),
            )
        )
    if remote_present and not license_present:
        findings.append(
            _finding(
                "upstream.license.missing",
                "provenance",
                "warning",
                ".",
                "imported project has no license evidence",
                (".git/config",),
            )
        )

    findings.append(
        _finding(
            "evidence.data-risk",
            "profile",
            "warning",
            ".",
            "data risk remains unknown until classified evidence is supplied",
        )
    )

    public_surfaces: set[str] = set()
    if "web" in project_types:
        public_surfaces.add("ui")
    if "api/backend" in project_types:
        public_surfaces.add("api")
    if any(command.source.endswith("pyproject.toml") for command in commands):
        public_surfaces.add("cli")

    operational_dependencies: set[str] = set()
    if dependencies & _DATABASE_DEPENDENCIES:
        operational_dependencies.add("database")
    if dependencies & _QUEUE_DEPENDENCIES:
        operational_dependencies.add("queue")

    evidence_tuple = _sorted_evidence(evidence)
    profile = ProjectProfile(
        project_id=resolved_root.name,
        root=".",
        project_types=tuple(sorted(project_types)),
        lifecycle="unknown",
        public_surfaces=tuple(sorted(public_surfaces)),
        data_risk="unknown",
        user_exposure="unknown",
        release_model="continuous" if workflow_present else "unknown",
        test_burden=(
            "moderate"
            if any(command.name.lower() in {"test", "tests", "pytest"} for command in commands)
            else "unknown"
        ),
        operational_dependencies=tuple(sorted(operational_dependencies)),
        owners=(),
        evidence_refs=tuple(sorted({item.source for item in evidence_tuple})),
    )
    return DiscoveryResult(
        profile=profile,
        evidence=evidence_tuple,
        findings=_sorted_findings(findings),
        commands=tuple(sorted(commands, key=lambda item: (item.source, item.name, item.command))),
        truncated=truncated,
    )
