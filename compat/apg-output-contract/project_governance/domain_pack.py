"""Bounded, evidence-only Domain Pack composition and routing contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


DOMAIN_PACK_SCHEMA_VERSION = "1.0"
MAX_PACKS = 32
MAX_DEPENDENCIES = 16
MAX_PROFILES = 32
MAX_GATE_REQUIREMENTS = 32
MAX_REFERENCES = 16
MAX_RENDERED_BYTES = 64 * 1024

_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}\Z")
_VERSION = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?\Z")
_SENSITIVE = re.compile(
    r"(?i)(?:^|[._/-])(?:api[_-]?key|secret|password|passwd|token|"
    r"access[_-]?token|refresh[_-]?token|private[_-]?key)(?:$|[._/-])"
)


class DomainPackError(ValueError):
    """Raised when a Domain Pack violates the closed bounded contract."""


class DomainPackSchemaError(DomainPackError):
    """Compatibility alias for callers that distinguish schema failures."""


class DomainCode(str, Enum):
    GAME = "game"
    THREE_D = "three-d"
    ECOMMERCE = "ecommerce"
    PAYMENTS = "payments"
    PRIVACY = "privacy"
    SECURITY = "security"
    AI_CONTENT = "ai-content"
    COPYRIGHT = "copyright"
    MEDICAL = "medical"
    FINANCE = "finance"
    INDUSTRIAL_CONTROL = "industrial-control"
    ACCESSIBILITY = "accessibility"


class GatePhase(str, Enum):
    FAST = "fast"
    FULL = "full"
    RELEASE = "release"


class Comparator(str, Enum):
    LTE = "lte"
    LT = "lt"
    GTE = "gte"
    GT = "gt"
    EQ = "eq"


def _code(value: object, label: str) -> str:
    if not isinstance(value, str) or isinstance(value, bool) or not _CODE.fullmatch(value):
        raise DomainPackError(f"{label} must be a bounded stable code")
    if _SENSITIVE.search(value):
        raise DomainPackError(f"{label} contains sensitive material")
    return value


def _version(value: object, label: str) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise DomainPackError(f"{label} must be a semantic version")
    return value


def _locator(value: object, label: str) -> str:
    if not isinstance(value, str) or isinstance(value, bool) or not value or len(value) > 240:
        raise DomainPackError(f"{label} must be a bounded project-relative locator")
    if "\x00" in value or "?" in value or "#" in value or "\\" in value:
        raise DomainPackError(f"{label} must be a safe project-relative locator")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DomainPackError(f"{label} must remain project-relative")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        raise DomainPackError(f"{label} must not be a URL or URI")
    if _SENSITIVE.search(value):
        raise DomainPackError(f"{label} contains sensitive material")
    return value


def _refs(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise DomainPackError(f"{label} must be an immutable tuple")
    if not allow_empty and not value:
        raise DomainPackError(f"{label} must not be empty")
    if len(value) > MAX_REFERENCES:
        raise DomainPackError(f"{label} exceeds its {MAX_REFERENCES}-item bound")
    result = tuple(_locator(item, f"{label}[{i}]") for i, item in enumerate(value))
    if result != tuple(sorted(set(result))):
        raise DomainPackError(f"{label} must use canonical order")
    return result


def _codes(value: object, label: str, *, maximum: int = MAX_REFERENCES) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise DomainPackError(f"{label} must be an immutable tuple")
    if len(value) > maximum:
        raise DomainPackError(f"{label} exceeds its {maximum}-item bound")
    result = tuple(_code(item, f"{label}[{i}]") for i, item in enumerate(value))
    if result != tuple(sorted(set(result))):
        raise DomainPackError(f"{label} must use canonical order")
    return result


def _enum(value: object, enum_type: type[Enum], label: str):
    if not isinstance(value, enum_type):
        try:
            value = enum_type(value)
        except (TypeError, ValueError) as error:
            raise DomainPackError(f"{label} has an unsupported value") from error
    return value


def _finite(value: object, label: str, *, nonnegative: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise DomainPackError(f"{label} must be a finite number")
    if nonnegative and value < 0:
        raise DomainPackError(f"{label} must be non-negative")
    return float(value)


def _freeze(value: object) -> object:
    if type(value) in (type(None), bool, int, float, str):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise DomainPackError("mapping keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    raise DomainPackError("nested values must be immutable scalars, mappings, or sequences")


@dataclass(frozen=True)
class DomainApplicability:
    """Bounded conditions; an empty dimension means any value."""

    domains: tuple[str, ...] = ()
    project_modes: tuple[str, ...] = ()
    purposes: tuple[str, ...] = ()
    risk_levels: tuple[str, ...] = ()
    data_classes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("domains", "project_modes", "purposes", "risk_levels", "data_classes"):
            _codes(getattr(self, name), name)

    def matches(self, context: Mapping[str, object] | object) -> bool:
        def get(name: str) -> object:
            return context.get(name) if isinstance(context, Mapping) else getattr(context, name, None)

        singular = {
            "domains": "domain",
            "project_modes": "project_mode",
            "purposes": "purpose",
            "risk_levels": "risk_level",
            "data_classes": "data_class",
        }
        for name in ("domains", "project_modes", "purposes", "risk_levels", "data_classes"):
            allowed = getattr(self, name)
            actual = get(singular[name])
            if isinstance(actual, Enum):
                actual = actual.value
            if allowed and actual not in allowed:
                return False
        return True


@dataclass(frozen=True)
class TestProfile:
    profile_id: str
    test_kind: str
    evidence_refs: tuple[str, ...] = ()
    required: bool = True

    def __post_init__(self) -> None:
        _code(self.profile_id, "profile_id")
        _code(self.test_kind, "test_kind")
        _refs(self.evidence_refs, "evidence_refs", allow_empty=True)
        if type(self.required) is not bool:
            raise DomainPackError("required must be bool")


@dataclass(frozen=True)
class PerformanceProfile:
    profile_id: str
    metric: str
    workload: str
    environment: str
    comparator: Comparator | str
    threshold: float
    tolerance: float
    variance_policy: str
    baseline_ref: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _code(self.profile_id, "profile_id")
        for name, value in (("metric", self.metric), ("workload", self.workload), ("environment", self.environment), ("variance_policy", self.variance_policy)):
            _code(value, name)
        object.__setattr__(self, "comparator", _enum(self.comparator, Comparator, "comparator"))
        object.__setattr__(self, "threshold", _finite(self.threshold, "threshold"))
        object.__setattr__(self, "tolerance", _finite(self.tolerance, "tolerance"))
        _locator(self.baseline_ref, "baseline_ref")
        _refs(self.evidence_refs, "evidence_refs", allow_empty=True)


@dataclass(frozen=True)
class ProfessionalGateRequirement:
    gate_id: str
    reason_code: str
    phase: GatePhase | str = GatePhase.FULL
    required: bool = True
    owner_gate: bool = False
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _code(self.gate_id, "gate_id")
        _code(self.reason_code, "reason_code")
        object.__setattr__(self, "phase", _enum(self.phase, GatePhase, "phase"))
        if type(self.required) is not bool or type(self.owner_gate) is not bool:
            raise DomainPackError("required and owner_gate must be bool")
        _refs(self.evidence_refs, "evidence_refs", allow_empty=True)


@dataclass(frozen=True)
class GateRouteEvidence:
    pack_id: str
    domain: DomainCode
    gate_id: str
    reason_code: str
    phase: GatePhase
    required: bool
    owner_gate: bool
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _code(self.pack_id, "pack_id")
        object.__setattr__(self, "domain", _enum(self.domain, DomainCode, "domain"))
        _code(self.gate_id, "gate_id")
        _code(self.reason_code, "reason_code")
        object.__setattr__(self, "phase", _enum(self.phase, GatePhase, "phase"))
        if type(self.required) is not bool or type(self.owner_gate) is not bool:
            raise DomainPackError("required and owner_gate must be bool")
        _refs(self.evidence_refs, "evidence_refs", allow_empty=True)


@dataclass(frozen=True)
class DomainPack:
    pack_id: str
    version: str
    domain: DomainCode | str
    source_refs: tuple[str, ...]
    applicability: DomainApplicability
    dependencies: tuple[str, ...] = ()
    test_profiles: tuple[TestProfile, ...] = ()
    performance_profiles: tuple[PerformanceProfile, ...] = ()
    professional_gates: tuple[ProfessionalGateRequirement, ...] = ()
    schema_version: str = DOMAIN_PACK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _code(self.pack_id, "pack_id")
        _version(self.version, "version")
        if self.schema_version != DOMAIN_PACK_SCHEMA_VERSION:
            raise DomainPackError(f"schema_version must be {DOMAIN_PACK_SCHEMA_VERSION}")
        object.__setattr__(self, "domain", _enum(self.domain, DomainCode, "domain"))
        if not isinstance(self.applicability, DomainApplicability):
            raise DomainPackError("applicability must be a DomainApplicability")
        _refs(self.source_refs, "source_refs")
        _codes(self.dependencies, "dependencies", maximum=MAX_DEPENDENCIES)
        for name, maximum, cls in (("test_profiles", MAX_PROFILES, TestProfile), ("performance_profiles", MAX_PROFILES, PerformanceProfile), ("professional_gates", MAX_GATE_REQUIREMENTS, ProfessionalGateRequirement)):
            value = getattr(self, name)
            if type(value) is not tuple or len(value) > maximum or any(not isinstance(item, cls) for item in value):
                raise DomainPackError(f"{name} must be a bounded tuple of {cls.__name__}")
            ids = tuple(item.profile_id if hasattr(item, "profile_id") else item.gate_id for item in value)
            if ids != tuple(sorted(set(ids))):
                raise DomainPackError(f"{name} must use canonical unique order")


@dataclass(frozen=True)
class DomainPackRegistry:
    packs: tuple[DomainPack, ...]
    schema_version: str = DOMAIN_PACK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DOMAIN_PACK_SCHEMA_VERSION:
            raise DomainPackError(f"schema_version must be {DOMAIN_PACK_SCHEMA_VERSION}")
        if type(self.packs) is not tuple or len(self.packs) > MAX_PACKS:
            raise DomainPackError(f"packs must contain at most {MAX_PACKS} items")
        if any(not isinstance(item, DomainPack) for item in self.packs):
            raise DomainPackError("packs must contain DomainPack values")
        if self.packs != tuple(sorted(self.packs, key=lambda item: item.pack_id)):
            raise DomainPackError("packs must use canonical pack_id order")
        ids = {item.pack_id for item in self.packs}
        if len(ids) != len(self.packs):
            raise DomainPackError("pack IDs must be unique")
        for pack in self.packs:
            if not set(pack.dependencies).issubset(ids):
                raise DomainPackError(f"dependencies for {pack.pack_id} are not closed")
        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        dependencies = {pack.pack_id: pack.dependencies for pack in self.packs}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(pack_id: str) -> None:
            if pack_id in visiting:
                raise DomainPackError("pack dependencies must be acyclic")
            if pack_id in visited:
                return
            visiting.add(pack_id)
            for dependency in dependencies[pack_id]:
                visit(dependency)
            visiting.remove(pack_id)
            visited.add(pack_id)

        for pack_id in dependencies:
            visit(pack_id)

    @classmethod
    def compose(cls, *registries: "DomainPackRegistry | Iterable[DomainPack]") -> "DomainPackRegistry":
        packs: list[DomainPack] = []
        for registry in registries:
            packs.extend(registry.packs if isinstance(registry, DomainPackRegistry) else tuple(registry))
        return cls(tuple(sorted(packs, key=lambda item: item.pack_id)))

    @classmethod
    def from_packs(cls, packs: Iterable[DomainPack]) -> "DomainPackRegistry":
        return cls(tuple(sorted(tuple(packs), key=lambda item: item.pack_id)))

    def applicable(self, context: Mapping[str, object] | object) -> tuple[DomainPack, ...]:
        def get(name: str) -> object:
            return context.get(name) if isinstance(context, Mapping) else getattr(context, name, None)

        requested_domain = get("domain")
        if isinstance(requested_domain, Enum):
            requested_domain = requested_domain.value
        return tuple(
            pack
            for pack in self.packs
            if (requested_domain is None or requested_domain == pack.domain.value)
            and pack.applicability.matches(context)
        )


def compose_domain_packs(*packs_or_registries: DomainPack | DomainPackRegistry | Iterable[DomainPack]) -> DomainPackRegistry:
    packs: list[DomainPack] = []
    for value in packs_or_registries:
        if isinstance(value, DomainPack):
            packs.append(value)
        elif isinstance(value, DomainPackRegistry):
            packs.extend(value.packs)
        else:
            packs.extend(tuple(value))
    return DomainPackRegistry.from_packs(packs)


def route_professional_gates(
    registry: DomainPackRegistry,
    context: Mapping[str, object] | object,
) -> tuple[GateRouteEvidence, ...]:
    """Return evidence-only route records; this function never executes a Gate."""

    if not isinstance(registry, DomainPackRegistry):
        raise TypeError("registry must be a DomainPackRegistry")
    routes = [
        GateRouteEvidence(
            pack_id=pack.pack_id,
            domain=pack.domain,
            gate_id=requirement.gate_id,
            reason_code=requirement.reason_code,
            phase=requirement.phase,
            required=requirement.required,
            owner_gate=requirement.owner_gate,
            evidence_refs=requirement.evidence_refs,
        )
        for pack in registry.applicable(context)
        for requirement in pack.professional_gates
    ]
    unique = {(item.pack_id, item.gate_id, item.reason_code): item for item in routes}
    return tuple(unique[key] for key in sorted(unique))


def render_domain_pack(pack: DomainPack) -> bytes:
    if not isinstance(pack, DomainPack):
        raise TypeError("pack must be a DomainPack")
    lines = [
        f"# Domain Pack {pack.pack_id}",
        "",
        f"- Schema: `{pack.schema_version}`",
        f"- Version: `{pack.version}`",
        f"- Domain: `{pack.domain.value}`",
        f"- Sources: {', '.join(f'`{ref}`' for ref in pack.source_refs)}",
        "",
        "## Applicability",
        "",
        f"- Domains: {', '.join(f'`{item}`' for item in pack.applicability.domains) or 'any'}",
        f"- Project modes: {', '.join(f'`{item}`' for item in pack.applicability.project_modes) or 'any'}",
        f"- Purposes: {', '.join(f'`{item}`' for item in pack.applicability.purposes) or 'any'}",
        f"- Risk levels: {', '.join(f'`{item}`' for item in pack.applicability.risk_levels) or 'any'}",
        f"- Data classes: {', '.join(f'`{item}`' for item in pack.applicability.data_classes) or 'any'}",
        "",
        "## Dependencies",
        "",
    ]
    lines.extend(f"- `{item}`" for item in pack.dependencies) if pack.dependencies else lines.append("- None")
    lines.extend([
        "",
        "## Test Profiles",
        "",
    ])
    lines.extend(f"- `{item.profile_id}` ({item.test_kind})" for item in pack.test_profiles) if pack.test_profiles else lines.append("- None")
    lines.extend([
        "",
        "## Performance Profiles",
        "",
    ])
    lines.extend(f"- `{item.profile_id}`: {item.metric} {item.comparator.value} {item.threshold:g} (baseline `{item.baseline_ref}`)" for item in pack.performance_profiles) if pack.performance_profiles else lines.append("- None")
    lines.extend([
        "",
        "## Professional Gate Evidence",
        "",
    ])
    lines.extend(f"- `{item.gate_id}` ({item.phase.value}, reason `{item.reason_code}`)" for item in pack.professional_gates) if pack.professional_gates else lines.append("- None")
    lines.extend([
        "",
        "This projection is evidence only. It does not select or execute APG Gates, create approval, authorize operations, or establish runtime acceptance.",
        "",
    ])
    result = "\n".join(lines).encode("utf-8")
    if len(result) > MAX_RENDERED_BYTES:
        raise DomainPackError(f"rendered Domain Pack exceeds its {MAX_RENDERED_BYTES}-byte bound")
    return result


SUPPORTED_DOMAINS = tuple(item.value for item in DomainCode)
DOMAIN_CATALOG = tuple(DomainCode)


def render_registry(registry: DomainPackRegistry) -> bytes:
    if not isinstance(registry, DomainPackRegistry):
        raise TypeError("registry must be a DomainPackRegistry")
    chunks = [render_domain_pack(pack).decode("utf-8") for pack in registry.packs]
    result = ("\n".join(chunks)).encode("utf-8")
    if len(result) > MAX_RENDERED_BYTES:
        raise DomainPackError(f"rendered Domain Pack registry exceeds its {MAX_RENDERED_BYTES}-byte bound")
    return result


render_domain_pack_registry = render_registry
route_professional_gate_evidence = route_professional_gates


__all__ = [
    "Comparator",
    "DOMAIN_CATALOG",
    "DOMAIN_PACK_SCHEMA_VERSION",
    "DomainApplicability",
    "DomainCode",
    "DomainPack",
    "DomainPackError",
    "DomainPackRegistry",
    "DomainPackSchemaError",
    "GatePhase",
    "GateRouteEvidence",
    "MAX_PACKS",
    "PerformanceProfile",
    "ProfessionalGateRequirement",
    "SUPPORTED_DOMAINS",
    "TestProfile",
    "compose_domain_packs",
    "render_domain_pack",
    "render_domain_pack_registry",
    "render_registry",
    "route_professional_gate_evidence",
    "route_professional_gates",
]
