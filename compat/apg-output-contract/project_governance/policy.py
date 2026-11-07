from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .gates import GateDefinition, parse_gate_definitions
from .model import GovernanceLevel, Policy, ProjectProfile, RiskClass
from .storage import digest, dump_policy_toml, load_policy_toml


@dataclass(frozen=True)
class LevelDecision:
    level: GovernanceLevel
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class RiskDecision:
    risk: RiskClass
    reasons: tuple[str, ...]
    required_phase: str


@dataclass(frozen=True)
class ResolvedPolicy:
    policy: Policy
    gates: tuple[GateDefinition, ...]
    canonical_bytes: bytes
    policy_digest: str
    input_status: str


_LEVELS = (GovernanceLevel.G1, GovernanceLevel.G2, GovernanceLevel.G3, GovernanceLevel.G4)
_BASE_DOCS = ("AGENTS.md", "PROJECT_BRIEF.md", "ARCHITECTURE.md", "QUALITY_GATES.md", "docs/decisions/")
_CONDITIONAL_DOCS = ("SKILLS.md", "WORKFLOW.md", "BRAND_GUIDELINES.md", "CHANNEL_SPECS.md", "CREATIVE_LIBRARY.md", "AUTOMATION_SPECS.md", "DATA_GOVERNANCE.md", "SECURITY.md", "PERFORMANCE.md", "RUNBOOK.md")
_CRITICAL_METRICS = ("regulated", "regulated_data", "safety_critical", "mission_critical", "irreversible_integrity", "high_blast_radius")
_LEVEL_METRICS = ("regulated_data", "safety_critical", "mission_critical", "irreversible_integrity")
_HIGH_CONFIG_NAMES = frozenset({
    ".bazelrc",
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
    "bitbucket-pipelines.yml",
    "build",
    "build.bazel",
    "build.gradle",
    "build.gradle.kts",
    "cargo.lock",
    "cargo.toml",
    "cmakelists.txt",
    "compose.yaml",
    "compose.yml",
    "composer.lock",
    "docker-compose.yaml",
    "docker-compose.yml",
    "dockerfile",
    "gemfile.lock",
    "go.mod",
    "go.sum",
    "gradle.lockfile",
    "gradle.properties",
    "jenkinsfile",
    "makefile",
    "package-lock.json",
    "package.json",
    "pipfile.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pom.xml",
    "project.pbxproj",
    "pyproject.toml",
    "settings.gradle",
    "settings.gradle.kts",
    "uv.lock",
    "workspace",
    "workspace.bazel",
    "yarn.lock",
})
_HIGH_PATH_TOKENS = frozenset({
    "auth",
    "authentication",
    "authorization",
    "database",
    "db",
    "deploy",
    "infra",
    "infrastructure",
    "migration",
    "migrations",
    "payment",
    "payments",
    "performance",
    "persistence",
    "schema",
    "terraform",
})
_HIGH_SURFACES = frozenset({
    "authentication",
    "authorization",
    "global-state",
    "payment",
    "payments",
    "performance",
    "persistence",
    "public-api",
    "sensitive-data",
})
_EXACT_CI_PATHS = frozenset({".circleci/config.yml"})


def _level(value, name):
    if value is None:
        return None
    if not isinstance(value, GovernanceLevel):
        raise TypeError(f"{name} must be a GovernanceLevel")
    return value


def _metrics(metrics):
    if metrics is None:
        return {}
    if not isinstance(metrics, Mapping):
        raise TypeError("metrics must be a mapping")
    facts = dict(metrics)
    for key in _CRITICAL_METRICS:
        if key in facts and type(facts[key]) is not bool:
            raise TypeError(f"metrics[{key!r}] must be a bool")
    return facts


def _tokens(values, name, *, allow_paths=False):
    if isinstance(values, (str, bytes, Path)):
        raise TypeError(f"{name} must be an iterable")
    try:
        iterator = iter(values)
    except TypeError:
        raise TypeError(f"{name} must be an iterable") from None
    result = []
    for value in iterator:
        if not isinstance(value, str) and not (allow_paths and isinstance(value, Path)):
            expected = "strings or paths" if allow_paths else "strings"
            raise TypeError(f"{name} elements must be {expected}")
        result.append(str(value).replace("\\", "/").strip().lower())
    return tuple(result)


def _has_any(values, allowed):
    return bool(set(values) & set(allowed))


def _governance_level(value: GovernanceLevel | str, name: str) -> GovernanceLevel:
    if isinstance(value, GovernanceLevel):
        return value
    if type(value) is not str:
        raise TypeError(f"{name} must be a GovernanceLevel or string")
    try:
        return GovernanceLevel(value)
    except ValueError as error:
        raise ValueError(f"{name} is invalid") from error


def _required_document_floor(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, Path)):
        raise TypeError("required document floor must be an iterable of strings")
    documents = tuple(values)
    if any(type(item) is not str or not item.strip() for item in documents):
        raise TypeError("required document floor must contain non-empty strings")
    return tuple(dict.fromkeys(documents))


def resolve_policy_input(
    policy_file: str | Path | None,
    *,
    fallback_policy: Policy,
    fallback_status: str,
    audit_level: GovernanceLevel | str,
    required_documents: Iterable[str],
) -> ResolvedPolicy:
    """Load and validate the policy authority before any project write is planned."""
    if not isinstance(fallback_policy, Policy):
        raise TypeError("fallback_policy must be a Policy")
    if type(fallback_status) is not str or not fallback_status.strip():
        raise ValueError("fallback_status is required")
    if policy_file is None:
        policy = fallback_policy
        input_status = fallback_status
    else:
        if not isinstance(policy_file, (str, Path)):
            raise TypeError("policy_file must be a path")
        policy = load_policy_toml(Path(policy_file).read_text(encoding="utf-8"))
        input_status = "explicit"

    audit_floor = _governance_level(audit_level, "audit_level")
    floor_level = max(
        (audit_floor, fallback_policy.level),
        key=_LEVELS.index,
    )
    if _LEVELS.index(policy.level) < _LEVELS.index(floor_level):
        raise ValueError(
            f"policy level {policy.level.value} is below governance floor {floor_level.value}"
        )

    document_floor = _required_document_floor(
        (*required_documents, *fallback_policy.required_documents)
    )
    configured_documents = set(policy.required_documents)
    missing_documents = tuple(
        document for document in document_floor if document not in configured_documents
    )
    if missing_documents:
        raise ValueError(
            "policy required_documents omit audit-selected documents: "
            + ", ".join(missing_documents)
        )

    gates = parse_gate_definitions(policy.gates)
    if policy.level is not GovernanceLevel.G1 and not gates:
        raise ValueError(f"{policy.level.value} policy requires at least one quality gate")
    command_gates = tuple(gate for gate in gates if gate.kind == "command")
    if (
        input_status == "explicit"
        or (input_status == "embedded" and policy.level is not GovernanceLevel.G1)
    ) and not command_gates:
        raise ValueError(
            f"{input_status} {policy.level.value} policy requires at least one command gate "
            "for adapter projection"
        )

    canonical_bytes = dump_policy_toml(policy).encode("utf-8")
    return ResolvedPolicy(
        policy=policy,
        gates=gates,
        canonical_bytes=canonical_bytes,
        policy_digest=digest(canonical_bytes),
        input_status=input_status,
    )


def select_level(profile: ProjectProfile, *, metrics: Mapping[str, object] | None = None, existing_level: GovernanceLevel | None = None, subsystem_level: GovernanceLevel | None = None) -> LevelDecision:
    if not isinstance(profile, ProjectProfile):
        raise TypeError("profile must be a ProjectProfile")
    existing = _level(existing_level, "existing_level")
    subsystem = _level(subsystem_level, "subsystem_level")
    facts = _metrics(metrics)
    types = _tokens(profile.project_types, "project_types")
    surfaces = _tokens(profile.public_surfaces, "public_surfaces")
    ops = _tokens(profile.operational_dependencies, "operational_dependencies")
    data = str(profile.data_risk).lower()
    exposure = str(profile.user_exposure).lower()
    release = str(profile.release_model).lower()
    burden = str(profile.test_burden).lower()
    warnings = sorted({f"unknown:{name}" for name, value in (("data_risk", data), ("user_exposure", exposure), ("release_model", release), ("test_burden", burden)) if value in {"unknown", "unspecified"}})
    reasons = []
    explicit_critical = data == "regulated" or exposure == "safety/mission-critical" or "irreversible-integrity" in surfaces or any(facts.get(key) is True for key in _LEVEL_METRICS)
    if explicit_critical:
        level = GovernanceLevel.G4
        reasons.append("explicit:critical-evidence")
    elif ((exposure == "public" and data in {"sensitive", "personal", "confidential"}) or (exposure == "public" and ops) or (data in {"sensitive", "personal", "confidential"} and ops) or len(surfaces) > 1 or burden in {"high", "heavy"} or release in {"high", "strict", "regulated"}):
        level = GovernanceLevel.G3
        reasons.append("evidence:high-governance-combination")
    elif surfaces or len(types) > 1 or ops or any(facts.get(key) is True for key in ("automation", "integrations", "operational_dependencies", "multiple_modules", "multiple_types")):
        level = GovernanceLevel.G2
        reasons.append("evidence:meaningful-contract-or-dependency")
    else:
        level = GovernanceLevel.G1
        reasons.append("default:isolated-project")
    floors = [value for value in (existing, subsystem) if value is not None]
    if floors:
        floor = max(floors, key=_LEVELS.index)
        if _LEVELS.index(floor) > _LEVELS.index(level):
            level = floor
            reasons.append(f"floor:{floor.value}")
    return LevelDecision(level, tuple(sorted(reasons)), tuple(warnings))


def classify_change(changed_paths: Iterable[str | Path], *, surfaces: Iterable[str] = (), metrics: Mapping[str, object] | None = None) -> RiskDecision:
    paths = _tokens(changed_paths, "changed_paths", allow_paths=True)
    surface_values = _tokens(surfaces, "surfaces")
    facts = _metrics(metrics)
    if _has_any(surface_values, {"safety-critical", "mission-critical", "regulated", "irreversible-integrity"}) or any(facts.get(key) is True for key in _CRITICAL_METRICS):
        return RiskDecision(RiskClass.CRITICAL, ("critical:explicit-or-blast-radius-evidence",), "release")
    path_names = {path.rsplit("/", 1)[-1] for path in paths}
    path_parts = {part for path in paths for part in path.split("/")}
    path_stems = {
        name.rsplit(".", 1)[0] if "." in name else name
        for name in path_names
    }
    governance_contract = any(
        path == ".governance"
        or path.startswith(".governance/")
        or path.rsplit("/", 1)[-1] == "agents.md"
        for path in paths
    )
    github_workflow = any(
        path.startswith(".github/workflows/") and "/" not in path[len(".github/workflows/"):]
        for path in paths
    )
    exact_ci_path = any(
        path in _EXACT_CI_PATHS
        or any(path.endswith(f"/{ci_path}") for ci_path in _EXACT_CI_PATHS)
        for path in paths
    )
    dockerfile_variant = any(name.startswith("dockerfile.") for name in path_names)
    core_build_config = bool(path_names & _HIGH_CONFIG_NAMES) or github_workflow or exact_ci_path or dockerfile_variant
    path_risk = bool(path_parts & _HIGH_PATH_TOKENS or path_stems & _HIGH_PATH_TOKENS)
    if governance_contract or path_risk or core_build_config or _has_any(surface_values, _HIGH_SURFACES) or facts.get("performance") is True:
        return RiskDecision(RiskClass.HIGH, ("high:security-data-state-or-operational-surface",), "full")
    moderate_parts = {"integration", "integrations", "ui", "frontend", "user-visible", "module", "modules"}
    moderate_surfaces = {"ui", "user-visible", "integration", "integrations"}
    crosses_source_and_tests = any(path.startswith("tests/") for path in paths) and any(path.startswith(("src/", "app/", "lib/")) for path in paths)
    dependency_manifest = any(
        name == "requirements.txt"
        or (name.startswith("requirements-") and name.endswith(".txt"))
        for name in path_names
    )
    if crosses_source_and_tests or path_parts & moderate_parts or _has_any(surface_values, moderate_surfaces) or dependency_manifest:
        return RiskDecision(RiskClass.MODERATE, ("moderate:crossing-or-user-visible-surface",), "full")
    reasons = ["routine:isolated-reversible-change"]
    if len(paths) >= 20:
        reasons.append("review:large-change-size")
    return RiskDecision(RiskClass.ROUTINE, tuple(sorted(reasons)), "fast")


def select_documents(profile: ProjectProfile, *, level: GovernanceLevel | None = None) -> tuple[str, ...]:
    if not isinstance(profile, ProjectProfile):
        raise TypeError("profile must be a ProjectProfile")
    _level(level, "level")
    types = _tokens(profile.project_types, "project_types")
    surfaces = _tokens(profile.public_surfaces, "public_surfaces")
    ops = _tokens(profile.operational_dependencies, "operational_dependencies")
    refs = _tokens(profile.evidence_refs, "evidence_refs")
    data = str(profile.data_risk).lower()
    burden = str(profile.test_burden).lower()
    selected = set()
    if any(ref.rsplit("/", 1)[-1] == "skill.md" for ref in refs):
        selected.add("SKILLS.md")
    if "automation" in types:
        selected.update(("WORKFLOW.md", "AUTOMATION_SPECS.md"))
    if "creative/content" in types:
        selected.update(("BRAND_GUIDELINES.md", "CREATIVE_LIBRARY.md"))
    if len(surfaces) > 1:
        selected.add("CHANNEL_SPECS.md")
    if data in {"sensitive", "personal", "confidential", "regulated"}:
        selected.add("DATA_GOVERNANCE.md")
    if "authentication" in surfaces or "security" in types:
        selected.add("SECURITY.md")
    if burden in {"high", "heavy"}:
        selected.add("PERFORMANCE.md")
    if ops:
        selected.add("RUNBOOK.md")
    return _BASE_DOCS + tuple(document for document in _CONDITIONAL_DOCS if document in selected)
