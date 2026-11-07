"""Canonical repository-local agent and Git adapter rendering."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path
import re
import shlex
from typing import Mapping

BEGIN_MARKER = "<!-- project-governance:begin -->"
END_MARKER = "<!-- project-governance:end -->"
HASH_BEGIN_MARKER = "# project-governance:begin"
HASH_END_MARKER = "# project-governance:end"
GENERATOR_VERSION = "1"
CANONICAL_ADAPTER_IDS = ("codex", "claude-code", "cursor", "git", "github")
_ADAPTER_ALIASES = {"claude": "claude-code"}
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_SOURCE_TEMPLATE_DIR = (
    _PACKAGE_ROOT / "templates" / "adaptive-project-governance" / "adapters"
)
_PACKAGED_TEMPLATE_DIR = _PACKAGE_ROOT / "templates" / "adapters"
_TEMPLATE_DIR = (
    _SOURCE_TEMPLATE_DIR
    if _SOURCE_TEMPLATE_DIR.is_dir()
    else _PACKAGED_TEMPLATE_DIR
)


class AdapterState(str, Enum):
    ABSENT = "absent"
    CURRENT = "current"
    STALE = "stale"
    DRIFTED = "drifted"
    INVALID = "invalid"


class AdapterValidationError(ValueError):
    pass


@dataclass(frozen=True)
class AdapterPlan:
    adapter_id: str
    target_relative_path: str
    rendered_content: str
    state: AdapterState
    apply_allowed: bool


@dataclass(frozen=True)
class _MarkerDialect:
    begin: str
    end: str
    metadata_prefix: str
    metadata_re: re.Pattern[str]


_METADATA_FIELDS = (
    r"project-governance:metadata policy-version=(?P<policy_version>\S+) "
    r"policy-digest=(?P<policy_digest>\S+) generator-version=(?P<generator_version>\S+) "
    r"scope=(?P<scope>\S+) body-digest=(?P<body_digest>\S+)"
)
_DIALECTS = {
    "html": _MarkerDialect(
        BEGIN_MARKER,
        END_MARKER,
        "<!-- project-governance:metadata ",
        re.compile(r"^<!-- " + _METADATA_FIELDS + r" -->$"),
    ),
    "hash": _MarkerDialect(
        HASH_BEGIN_MARKER,
        HASH_END_MARKER,
        "# project-governance:metadata ",
        re.compile(r"^# " + _METADATA_FIELDS + r"$"),
    ),
}


def _markers(dialect: str) -> _MarkerDialect:
    try:
        return _DIALECTS[dialect]
    except KeyError as exc:
        raise AdapterValidationError(f"unknown marker dialect: {dialect!r}") from exc


def _adapter_dialect(adapter_id: str) -> str:
    return "hash" if adapter_id in {"git", "github"} else "html"


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _validate_shape(text: str, *, dialect: str = "html") -> tuple[int, int] | None:
    markers = _markers(dialect)
    begin_count = text.count(markers.begin)
    end_count = text.count(markers.end)
    if begin_count == 0 and end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise AdapterValidationError("duplicate or nested managed blocks")
    begin = text.index(markers.begin)
    end = text.index(markers.end)
    if end < begin:
        raise AdapterValidationError("managed block ends before it begins")
    return begin, end


def _normalize(text: str, newline: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)


def _metadata_line(
    *,
    policy_digest: str,
    policy_version: str,
    generator_version: str,
    scope: str,
    body_digest: str,
    dialect: str,
) -> str:
    _markers(dialect)
    fields = (
        f"project-governance:metadata policy-version={policy_version} "
        f"policy-digest={policy_digest} generator-version={generator_version} "
        f"scope={scope} body-digest={body_digest}"
    )
    if dialect == "html":
        return f"<!-- {fields} -->"
    return f"# {fields}"


def _render_block(
    body: str,
    *,
    policy_digest: str,
    policy_version: str,
    generator_version: str,
    scope: str,
    newline: str,
    dialect: str,
) -> str:
    markers = _markers(dialect)
    metadata = _metadata_line(
        policy_digest=policy_digest,
        policy_version=policy_version,
        generator_version=generator_version,
        scope=scope,
        body_digest=sha256(body.encode()).hexdigest(),
        dialect=dialect,
    )
    return newline.join((markers.begin, metadata, body, markers.end))


def merge_managed_block(original: str, body: str, *, policy_digest: str, policy_version: str = "1", generator_version: str = GENERATOR_VERSION, scope: str = ".", dialect: str = "html") -> str:
    values = (policy_digest, policy_version, generator_version, scope)
    if not all(isinstance(value, str) and value for value in values):
        raise AdapterValidationError("metadata values must be non-empty strings")
    markers = _markers(dialect)
    shape = _validate_shape(original, dialect=dialect)
    newline = _newline(original)
    normalized_body = _normalize(body, newline)
    if shape is not None:
        parsed = _parse_block(original, dialect=dialect)
        if parsed is None or parsed[0] is AdapterState.INVALID:
            raise AdapterValidationError("malformed managed block")
        _, metadata, existing_body = parsed
        expected = {"policy_digest": policy_digest, "policy_version": policy_version, "generator_version": generator_version, "scope": scope}
        if metadata["body_digest"] != sha256(existing_body.encode()).hexdigest():
            raise AdapterValidationError("managed block is drifted or manually altered")
        if metadata["scope"] != scope or metadata["generator_version"] != generator_version:
            raise AdapterValidationError("managed block scope or generator is incompatible")
        if existing_body == normalized_body and all(metadata[key] == value for key, value in expected.items()):
            return original
        replacement = _render_block(
            normalized_body,
            policy_digest=policy_digest,
            policy_version=policy_version,
            generator_version=generator_version,
            scope=scope,
            newline=newline,
            dialect=dialect,
        )
        begin, end = shape
        return original[:begin] + replacement + original[end + len(markers.end):]
    block = _render_block(
        normalized_body,
        policy_digest=policy_digest,
        policy_version=policy_version,
        generator_version=generator_version,
        scope=scope,
        newline=newline,
        dialect=dialect,
    ) + newline
    if not original:
        return block
    separator = "" if original.endswith(("\n", "\r")) else newline
    return original + separator + block


def _parse_block(text: str, *, dialect: str = "html") -> tuple[AdapterState, Mapping[str, str], str] | None:
    markers = _markers(dialect)
    shape = _validate_shape(text, dialect=dialect)
    if shape is None:
        return None
    begin, end = shape
    newline = _newline(text)
    lines = text[begin : end + len(markers.end)].replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) < 4 or lines[0] != markers.begin or lines[-1] != markers.end:
        return AdapterState.INVALID, {}, ""
    match = markers.metadata_re.match(lines[1])
    if not match:
        return AdapterState.INVALID, {}, ""
    return AdapterState.CURRENT, match.groupdict(), newline.join(lines[2:-1])


def verify_managed_block(text: str, policy_digest: str, *, policy_version: str | None = None, generator_version: str | None = None, scope: str | None = None, expected_body: str | None = None, dialect: str = "html") -> AdapterState:
    try:
        parsed = _parse_block(text, dialect=dialect)
    except AdapterValidationError:
        return AdapterState.INVALID
    if parsed is None:
        return AdapterState.ABSENT
    state, metadata, body = parsed
    if state is AdapterState.INVALID:
        return state
    if metadata["body_digest"] != sha256(body.encode()).hexdigest():
        return AdapterState.DRIFTED
    if metadata["policy_digest"] != policy_digest:
        return AdapterState.STALE
    if scope is None and metadata["scope"] != ".":
        return AdapterState.DRIFTED
    expected = {"policy_version": policy_version, "generator_version": generator_version, "scope": scope}
    if any(value is not None and metadata[key] != value for key, value in expected.items()):
        return AdapterState.DRIFTED
    if expected_body is not None and body != _normalize(expected_body, _newline(text)).rstrip("\r\n"):
        return AdapterState.DRIFTED
    return AdapterState.CURRENT


def _contains_dialect(text: str, dialect: str) -> bool:
    markers = _markers(dialect)
    return any(
        token in text
        for token in (markers.begin, markers.end, markers.metadata_prefix)
    )


def _plan_block(plan: AdapterPlan) -> tuple[str, Mapping[str, str], str]:
    dialect = _adapter_dialect(plan.adapter_id)
    parsed = _parse_block(plan.rendered_content, dialect=dialect)
    if parsed is None or parsed[0] is not AdapterState.CURRENT:
        raise AdapterValidationError("adapter plan does not contain a current managed block")
    return dialect, parsed[1], parsed[2]


def _legacy_expected_bodies(plan: AdapterPlan, body: str) -> tuple[str, ...]:
    bodies = [body]
    if plan.adapter_id == "git":
        strict_sequence = (
            "# Preview-only local hook. No global installation is required.\n"
            "set -e\n"
        )
        if strict_sequence in body:
            bodies.append(
                body.replace(
                    strict_sequence,
                    "# Preview-only local hook. No global installation is required.\n",
                    1,
                )
            )
    return tuple(bodies)


def _verify_legacy_candidate(
    text: str,
    plan: AdapterPlan,
    metadata: Mapping[str, str],
    body: str,
) -> AdapterState:
    integrity_state = verify_managed_block(
        text,
        metadata["policy_digest"],
        policy_version=metadata["policy_version"],
        generator_version=metadata["generator_version"],
        scope=metadata["scope"],
        dialect="html",
    )
    if integrity_state is not AdapterState.CURRENT:
        return integrity_state
    for expected_body in _legacy_expected_bodies(plan, body):
        state = verify_managed_block(
            text,
            metadata["policy_digest"],
            policy_version=metadata["policy_version"],
            generator_version=metadata["generator_version"],
            scope=metadata["scope"],
            expected_body=expected_body,
            dialect="html",
        )
        if state is AdapterState.CURRENT:
            return state
    return AdapterState.DRIFTED


def verify_adapter_plan(text: str, plan: AdapterPlan) -> AdapterState:
    dialect, metadata, body = _plan_block(plan)
    target_present = _contains_dialect(text, dialect)
    legacy_present = dialect == "hash" and _contains_dialect(text, "html")
    if target_present and legacy_present:
        return AdapterState.INVALID
    state = verify_managed_block(
        text,
        metadata["policy_digest"],
        policy_version=metadata["policy_version"],
        generator_version=metadata["generator_version"],
        scope=metadata["scope"],
        expected_body=body,
        dialect=dialect,
    )
    if state is not AdapterState.ABSENT or not legacy_present:
        return state
    legacy_state = _verify_legacy_candidate(text, plan, metadata, body)
    return AdapterState.STALE if legacy_state is AdapterState.CURRENT else legacy_state


def merge_adapter_plan(original: str, plan: AdapterPlan) -> str:
    dialect, metadata, body = _plan_block(plan)
    target_present = _contains_dialect(original, dialect)
    legacy_present = dialect == "hash" and _contains_dialect(original, "html")
    if target_present and legacy_present:
        raise AdapterValidationError("multiple managed marker dialects are present")
    if legacy_present:
        legacy_state = _verify_legacy_candidate(original, plan, metadata, body)
        if legacy_state is not AdapterState.CURRENT:
            raise AdapterValidationError(
                "legacy managed block is not an exact current-policy migration candidate"
            )
        legacy_shape = _validate_shape(original, dialect="html")
        if legacy_shape is None:
            raise AdapterValidationError("legacy managed block is malformed")
        newline = _newline(original)
        replacement = _render_block(
            _normalize(body, newline),
            policy_digest=metadata["policy_digest"],
            policy_version=metadata["policy_version"],
            generator_version=metadata["generator_version"],
            scope=metadata["scope"],
            newline=newline,
            dialect=dialect,
        )
        begin, end = legacy_shape
        original = original[:begin] + replacement + original[end + len(END_MARKER):]
    return merge_managed_block(
        original,
        body,
        policy_digest=metadata["policy_digest"],
        policy_version=metadata["policy_version"],
        generator_version=metadata["generator_version"],
        scope=metadata["scope"],
        dialect=dialect,
    )


def validate_cursor_frontmatter(text: str) -> dict[str, str]:
    newline = _newline(text)
    if not text.startswith("---" + newline):
        raise AdapterValidationError("Cursor rule is missing frontmatter")
    lines = text.splitlines()
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise AdapterValidationError("unterminated Cursor frontmatter") from exc
    values: dict[str, str] = {}
    for line in lines[1:closing]:
        if ":" not in line:
            raise AdapterValidationError("invalid Cursor frontmatter line")
        key, value = (part.strip() for part in line.split(":", 1))
        if not key or not value or key in values:
            raise AdapterValidationError("invalid or duplicate Cursor frontmatter key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    if closing == len(lines) - 1 or not {"description", "globs"}.issubset(values):
        raise AdapterValidationError("Cursor frontmatter requires description and globs")
    return values


def _normalize_adapter(adapter: str) -> str:
    normalized = _ADAPTER_ALIASES.get(adapter, adapter)
    if normalized not in CANONICAL_ADAPTER_IDS:
        raise AdapterValidationError(f"unknown adapters: {adapter!r}")
    return normalized


def _validate_root(project_root: str) -> str:
    if not isinstance(project_root, str) or not project_root or Path(project_root).is_absolute() or project_root == ".." or project_root.startswith("..\\") or project_root.startswith("../"):
        raise AdapterValidationError("project_root must be a relative non-parent path")
    return project_root


def _template_name(adapter_id: str) -> str:
    return {"codex": "AGENTS.managed.md.tmpl", "claude-code": "CLAUDE.managed.md.tmpl", "cursor": "cursor-rule.mdc.tmpl", "git": "pre-commit.tmpl", "github": "github-governance.yml.tmpl"}[adapter_id]


def format_command(command: object) -> str:
    if isinstance(command, (str, bytes)) or not isinstance(command, Sequence):
        raise AdapterValidationError("command must be a sequence of strings")
    values = tuple(command)
    if not values or any(type(value) is not str or not value for value in values):
        raise AdapterValidationError("command must contain non-empty strings")
    return shlex.join(values)


def validation_commands_from_gates(gates: object) -> tuple[str, ...]:
    if isinstance(gates, (str, bytes)) or not isinstance(gates, Sequence):
        raise AdapterValidationError("gates must be a sequence")
    commands: list[str] = []
    for gate in gates:
        if not isinstance(gate, Mapping) or gate.get("kind") != "command":
            continue
        commands.append(format_command(gate.get("command", ())))
    return tuple(dict.fromkeys(commands))


def render_adapter(adapter: str, *, policy_version: str, policy_digest: str, project_root: str, validation_commands: tuple[str, ...] = (), approved_adapters: tuple[str, ...] = ()) -> AdapterPlan:
    adapter_id = _normalize_adapter(adapter)
    dialect = _adapter_dialect(adapter_id)
    markers = _markers(dialect)
    root = _validate_root(project_root)
    commands = validation_commands or ("python -X utf8 -m unittest",)
    if not all(isinstance(command, str) and command and not re.search(r"(?i)(pip\s+install|npm\s+install|global|checkout\s+-b|branch\s+protection)", command) for command in commands):
        raise AdapterValidationError("commands contain unsupported global, destructive, or protection claims")
    markdown_command_text = "\n".join(f"- `{command}`" for command in commands)
    executable_command_text = "\n".join(commands)
    github_command_text = "\n".join(f"          {command}" for command in commands)
    template = (_TEMPLATE_DIR / _template_name(adapter_id)).read_text(encoding="utf-8").replace("\ufeff", "")
    values = {
        "policy_version": policy_version,
        "policy_digest": policy_digest,
        "generator_version": GENERATOR_VERSION,
        "scope": root,
        "validation_command": markdown_command_text,
        "validation_commands": executable_command_text,
        "github_validation_commands": github_command_text,
        "project_root": root,
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    if adapter_id == "cursor":
        validate_cursor_frontmatter(template)
    if adapter_id == "github" and "branch protection is configured" in template.lower():
        raise AdapterValidationError("GitHub adapter must not claim branch protection")
    if markers.begin not in template or markers.end not in template:
        raise AdapterValidationError(f"{adapter_id} template has invalid managed markers")
    managed_body = template.split(markers.begin, 1)[1].split(markers.end, 1)[0].strip("\r\n")
    managed_lines = managed_body.replace("\r\n", "\n").split("\n")
    if managed_lines and managed_lines[0].startswith(markers.metadata_prefix):
        managed_lines = managed_lines[1:]
    rendered = merge_managed_block("", "\n".join(managed_lines), policy_digest=policy_digest, policy_version=policy_version, scope=root, dialect=dialect)
    if adapter_id == "cursor":
        frontmatter = template.split(markers.begin, 1)[0]
        rendered = frontmatter + rendered
    elif template.startswith("#!"):
        rendered = template.split(markers.begin, 1)[0] + rendered
    else:
        prefix = template.split(markers.begin, 1)[0]
        rendered = prefix + rendered
    state = verify_managed_block(rendered, policy_digest, policy_version=policy_version, scope=root, dialect=dialect)
    return AdapterPlan(adapter_id, {"codex": "AGENTS.md", "claude-code": "CLAUDE.md", "cursor": ".cursor/rules/project-governance.mdc", "git": ".git/hooks/pre-commit", "github": ".github/workflows/project-governance.yml"}[adapter_id], rendered, state, adapter_id not in {"git", "github"} or adapter_id in {_normalize_adapter(item) for item in approved_adapters})


def render_adapter_plans(*, policy_version: str, policy_digest: str, project_root: str, validation_commands: tuple[str, ...] = (), adapters: tuple[str, ...] = ("codex",), approved_adapters: tuple[str, ...] = ()) -> dict[str, AdapterPlan]:
    result: dict[str, AdapterPlan] = {}
    for adapter in dict.fromkeys(_normalize_adapter(item) for item in adapters):
        result[adapter] = render_adapter(adapter, policy_version=policy_version, policy_digest=policy_digest, project_root=project_root, validation_commands=validation_commands, approved_adapters=approved_adapters)
    return result
