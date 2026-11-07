"""Closed, immutable project-intake contract for APG extension schema 1.0."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import PurePosixPath
import re
from typing import Any, Mapping
import unicodedata


EXTENSION_SCHEMA_VERSION = "1.0"

_MAX_CANONICAL_BYTES = 64 * 1024
_MAX_CODE_LENGTH = 80
_MAX_LOCATOR_LENGTH = 240
_MAX_DECISIONS = 32
_MAX_EVIDENCE = 64
_MAX_REFERENCES_PER_ITEM = 16

_STABLE_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}\Z")
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")
_SENSITIVE_TOKEN = re.compile(
    r"(?:^|[\s._/=-])(?:password|passwd|secret|api[-_.]?key|access[-_.]?token|"
    r"refresh[-_.]?token|authorization|bearer|session[-_.]?id|private[-_.]?key)"
    r"(?:$|[\s._/=-])",
    re.IGNORECASE,
)
_OPAQUE_SECRET = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}|\bghp_[A-Za-z0-9]{8,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.)"
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "decisions",
        "evidence",
        "extension_schema_version",
        "intake_id",
        "need_evidence_level",
        "project_mode",
        "project_mode_evidence_refs",
        "purpose",
        "remediation_level",
        "slice_complexity",
        "stack_fitness",
        "stop_state",
        "user_context",
    }
)
_USER_CONTEXT_FIELDS = frozenset(
    {
        "audience_mode",
        "domain_experience",
        "project_relationship",
        "technical_experience",
    }
)
_DECISION_FIELDS = frozenset(
    {
        "confidence",
        "decision_id",
        "disposition",
        "evidence_refs",
        "recommendation_code",
        "resolution_state",
        "topic_code",
        "user_impact_code",
    }
)
_EVIDENCE_FIELDS = frozenset({"kind", "locator", "reference_id"})


class IntakeContractError(ValueError):
    """Raised when intake bytes or records violate the closed contract."""


class ProjectMode(str, Enum):
    NEW = "new"
    EXISTING = "existing"
    AMBIGUOUS = "ambiguous"


class Purpose(str, Enum):
    PERSONAL_LEARNING = "personal-learning"
    REAL_AUDIENCE = "real-audience"


class DecisionDisposition(str, Enum):
    DEFAULT = "D"
    VERIFY = "V"
    HUMAN_BOUND = "B"


class ResolutionState(str, Enum):
    OPEN = "open"
    AI_RESOLVED = "ai-resolved"
    USER_CONFIRMED = "user-confirmed"


class RemediationLevel(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class StackFitness(str, Enum):
    S0 = "S0"
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"


class NeedEvidenceLevel(str, Enum):
    T0 = "T0"
    T1 = "T1"
    T2 = "T2"


class StopState(str, Enum):
    CONTINUE = "continue"
    READY_FOR_PREVIEW = "ready-for-preview"
    OWNER_GATE = "owner-gate"


class SliceComplexity(str, Enum):
    SIMPLE = "simple"
    COMPLEX = "complex"


class ProjectRelationship(str, Enum):
    OWNER = "owner"
    MAINTAINER = "maintainer"
    CONTRIBUTOR = "contributor"
    EVALUATOR = "evaluator"
    UNKNOWN = "unknown"


class ExperienceLevel(str, Enum):
    NONE = "none"
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"
    UNKNOWN = "unknown"


class AudienceMode(str, Enum):
    SELF = "self"
    KNOWN_GROUP = "known-group"
    PUBLIC = "public"
    UNKNOWN = "unknown"


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceKind(str, Enum):
    HYPOTHESIS = "hypothesis"
    PUBLIC_RESEARCH = "public-research"
    REAL_USER_EVIDENCE = "real-user-evidence"
    TECHNICAL_VIABILITY = "technical-viability"
    USER_CONFIRMATION = "user-confirmation"
    PROJECT_EVIDENCE = "project-evidence"


def _require_enum(value: object, enum_type: type[Enum], label: str) -> None:
    if not isinstance(value, enum_type):
        raise IntakeContractError(f"{label} must be a {enum_type.__name__}")


def _scalar(value: object, label: str, *, maximum: int) -> str:
    if type(value) is not str or not value:
        raise IntakeContractError(f"{label} must be a non-empty string")
    if len(value) > maximum:
        raise IntakeContractError(f"{label} exceeds its {maximum}-character bound")
    if unicodedata.normalize("NFC", value) != value:
        raise IntakeContractError(f"{label} must use NFC Unicode")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise IntakeContractError(f"{label} contains control characters")
    if _SENSITIVE_TOKEN.search(value) or _OPAQUE_SECRET.search(value):
        raise IntakeContractError(f"{label} contains a sensitive-value pattern")
    return value


def _code(value: object, label: str) -> str:
    text = _scalar(value, label, maximum=_MAX_CODE_LENGTH)
    if not _STABLE_CODE.fullmatch(text):
        raise IntakeContractError(f"{label} must be a bounded stable ID or code")
    return text


def _safe_relative_path(value: str, label: str) -> bool:
    if "\\" in value:
        return False
    if value.startswith("/") or _WINDOWS_DRIVE.match(value):
        return False
    if "://" in value or ":" in value:
        return False
    if "?" in value or "#" in value:
        return False
    parts = value.split("/")
    if len(parts) < 2 or any(part in ("", ".", "..") for part in parts):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and tuple(path.parts) == tuple(parts)


def _locator(value: object, label: str) -> str:
    text = _scalar(value, label, maximum=_MAX_LOCATOR_LENGTH)
    if "?" in text and "://" in text:
        raise IntakeContractError(f"{label} must not be a query-bearing URL")
    if _STABLE_CODE.fullmatch(text) or _safe_relative_path(text, label):
        return text
    raise IntakeContractError(
        f"{label} must be a bounded stable ID or contained project-relative path"
    )


def _tuple(value: object, label: str, maximum: int) -> tuple[Any, ...]:
    if type(value) is not tuple:
        raise IntakeContractError(f"{label} must be an immutable tuple")
    if len(value) > maximum:
        raise IntakeContractError(f"{label} exceeds its {maximum}-reference bound")
    return value


def _reference_tuple(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    items = _tuple(value, label, _MAX_REFERENCES_PER_ITEM)
    if not items and not allow_empty:
        raise IntakeContractError(f"{label} must contain at least one reference")
    normalized = tuple(
        _code(item, f"{label}[{index}]") for index, item in enumerate(items)
    )
    if len(set(normalized)) != len(normalized):
        raise IntakeContractError(f"{label} contains duplicate evidence references")
    if normalized != tuple(sorted(normalized)):
        raise IntakeContractError(f"{label} must use canonical reference order")
    return normalized


@dataclass(frozen=True)
class UserContext:
    project_relationship: ProjectRelationship
    domain_experience: ExperienceLevel
    technical_experience: ExperienceLevel
    audience_mode: AudienceMode

    def __post_init__(self) -> None:
        _require_enum(
            self.project_relationship, ProjectRelationship, "project_relationship"
        )
        _require_enum(self.domain_experience, ExperienceLevel, "domain_experience")
        _require_enum(
            self.technical_experience, ExperienceLevel, "technical_experience"
        )
        _require_enum(self.audience_mode, AudienceMode, "audience_mode")


@dataclass(frozen=True)
class EvidenceReference:
    reference_id: str
    kind: EvidenceKind
    locator: str

    def __post_init__(self) -> None:
        _code(self.reference_id, "evidence.reference_id")
        _require_enum(self.kind, EvidenceKind, "evidence.kind")
        _locator(self.locator, "evidence.locator")


@dataclass(frozen=True)
class Decision:
    decision_id: str
    topic_code: str
    disposition: DecisionDisposition
    resolution_state: ResolutionState
    recommendation_code: str
    evidence_refs: tuple[str, ...]
    confidence: Confidence
    user_impact_code: str

    def __post_init__(self) -> None:
        _code(self.decision_id, "decision.decision_id")
        _code(self.topic_code, "decision.topic_code")
        _require_enum(self.disposition, DecisionDisposition, "decision.disposition")
        _require_enum(
            self.resolution_state, ResolutionState, "decision.resolution_state"
        )
        _code(self.recommendation_code, "decision.recommendation_code")
        _reference_tuple(self.evidence_refs, "decision.evidence_refs")
        _require_enum(self.confidence, Confidence, "decision.confidence")
        _code(self.user_impact_code, "decision.user_impact_code")
        if (
            self.disposition is DecisionDisposition.HUMAN_BOUND
            and self.resolution_state is ResolutionState.AI_RESOLVED
        ):
            raise IntakeContractError("B decisions cannot be AI-resolved")


@dataclass(frozen=True)
class ProjectIntake:
    extension_schema_version: str
    intake_id: str
    project_mode: ProjectMode
    project_mode_evidence_refs: tuple[str, ...]
    purpose: Purpose
    user_context: UserContext
    remediation_level: RemediationLevel
    stack_fitness: StackFitness
    need_evidence_level: NeedEvidenceLevel
    slice_complexity: SliceComplexity
    decisions: tuple[Decision, ...]
    evidence: tuple[EvidenceReference, ...]
    stop_state: StopState

    def __post_init__(self) -> None:
        if self.extension_schema_version != EXTENSION_SCHEMA_VERSION:
            raise IntakeContractError(
                "unsupported intake extension_schema_version"
            )
        _code(self.intake_id, "intake_id")
        _require_enum(self.project_mode, ProjectMode, "project_mode")
        _reference_tuple(
            self.project_mode_evidence_refs, "project_mode_evidence_refs"
        )
        _require_enum(self.purpose, Purpose, "purpose")
        if not isinstance(self.user_context, UserContext):
            raise IntakeContractError("user_context must be a UserContext")
        _require_enum(
            self.remediation_level, RemediationLevel, "remediation_level"
        )
        _require_enum(self.stack_fitness, StackFitness, "stack_fitness")
        _require_enum(
            self.need_evidence_level, NeedEvidenceLevel, "need_evidence_level"
        )
        _require_enum(self.slice_complexity, SliceComplexity, "slice_complexity")
        _require_enum(self.stop_state, StopState, "stop_state")

        decisions = _tuple(self.decisions, "decisions", _MAX_DECISIONS)
        evidence = _tuple(self.evidence, "evidence", _MAX_EVIDENCE)
        if any(not isinstance(item, Decision) for item in decisions):
            raise IntakeContractError("decisions must contain Decision records")
        if any(not isinstance(item, EvidenceReference) for item in evidence):
            raise IntakeContractError(
                "evidence must contain EvidenceReference records"
            )

        decision_ids = tuple(item.decision_id for item in decisions)
        if len(set(decision_ids)) != len(decision_ids):
            raise IntakeContractError("duplicate decision IDs are not allowed")
        if decision_ids != tuple(sorted(decision_ids)):
            raise IntakeContractError("decisions must use canonical decision order")

        evidence_ids = tuple(item.reference_id for item in evidence)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise IntakeContractError("duplicate evidence IDs are not allowed")
        if evidence_ids != tuple(sorted(evidence_ids)):
            raise IntakeContractError("evidence must use canonical reference order")

        available = set(evidence_ids)
        referenced = set(self.project_mode_evidence_refs)
        for item in decisions:
            referenced.update(item.evidence_refs)
        missing = sorted(referenced - available)
        if missing:
            raise IntakeContractError(
                "intake references unknown evidence IDs: " + ", ".join(missing)
            )

        human_budget = 3 if self.slice_complexity is SliceComplexity.SIMPLE else 5
        if self.human_decision_count > human_budget:
            raise IntakeContractError(
                f"{self.slice_complexity.value} intake exceeds its "
                f"{human_budget}-item human decision budget"
            )

        open_items = tuple(
            item
            for item in decisions
            if item.resolution_state is ResolutionState.OPEN
        )
        open_human = tuple(
            item
            for item in open_items
            if item.disposition is DecisionDisposition.HUMAN_BOUND
        )
        if self.stop_state is StopState.READY_FOR_PREVIEW and open_items:
            raise IntakeContractError(
                "ready-for-preview is inconsistent with open decisions"
            )
        if self.stop_state is StopState.OWNER_GATE and not open_human:
            raise IntakeContractError(
                "owner-gate requires at least one open B decision"
            )
        if self.stop_state is StopState.CONTINUE and (
            not open_items or open_human
        ):
            raise IntakeContractError(
                "continue requires open D/V decisions and no open B decision"
            )

        kinds = {item.kind for item in evidence}
        if self.purpose is Purpose.PERSONAL_LEARNING:
            if self.user_context.audience_mode is not AudienceMode.SELF:
                raise IntakeContractError(
                    "personal-learning purpose requires self audience mode"
                )
        elif self.user_context.audience_mode is AudienceMode.SELF:
            raise IntakeContractError(
                "real-audience purpose cannot use self audience mode"
            )

        if self.need_evidence_level is NeedEvidenceLevel.T0:
            if self.purpose is not Purpose.PERSONAL_LEARNING:
                raise IntakeContractError("T0 requires personal-learning purpose")
        elif self.need_evidence_level is NeedEvidenceLevel.T1:
            if (
                self.purpose is not Purpose.REAL_AUDIENCE
                or EvidenceKind.PUBLIC_RESEARCH not in kinds
            ):
                raise IntakeContractError(
                    "T1 requires real-audience purpose and public research"
                )
            if EvidenceKind.REAL_USER_EVIDENCE in kinds:
                raise IntakeContractError(
                    "T1 cannot claim real-user evidence reserved for T2"
                )
        elif EvidenceKind.REAL_USER_EVIDENCE not in kinds:
            raise IntakeContractError("T2 requires real-user evidence")

    @property
    def human_decision_count(self) -> int:
        return sum(
            item.disposition is DecisionDisposition.HUMAN_BOUND
            for item in self.decisions
        )

    @property
    def ai_resolved_count(self) -> int:
        return sum(
            item.disposition
            in (DecisionDisposition.DEFAULT, DecisionDisposition.VERIFY)
            and item.resolution_state is ResolutionState.AI_RESOLVED
            for item in self.decisions
        )

    @property
    def unresolved_count(self) -> int:
        return sum(
            item.resolution_state is ResolutionState.OPEN
            for item in self.decisions
        )


def _closed_mapping(
    value: object,
    fields: frozenset[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IntakeContractError(f"{label} must be an object")
    keys = set(value)
    if any(type(key) is not str for key in keys):
        raise IntakeContractError(f"{label} field names must be strings")
    unknown = sorted(keys - fields)
    missing = sorted(fields - keys)
    if unknown:
        raise IntakeContractError(
            f"{label} contains unknown fields: {', '.join(unknown)}"
        )
    if missing:
        raise IntakeContractError(
            f"{label} is missing fields: {', '.join(missing)}"
        )
    return value


def _sequence(value: object, label: str, maximum: int) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        raise IntakeContractError(f"{label} must be an array")
    if len(value) > maximum:
        if label == "decisions":
            noun = "decision"
        elif label == "evidence":
            noun = "evidence"
        else:
            noun = "reference"
        raise IntakeContractError(f"{label} exceeds its {maximum}-{noun} bound")
    return tuple(value)


def _enum_value(enum_type: type[Enum], value: object, label: str) -> Enum:
    if type(value) is not str:
        raise IntakeContractError(f"{label} must be a string enum value")
    try:
        return enum_type(value)
    except ValueError as error:
        raise IntakeContractError(f"{label} has an unsupported value") from error


def _reference_values(value: object, label: str) -> tuple[str, ...]:
    items = _sequence(value, label, _MAX_REFERENCES_PER_ITEM)
    return tuple(_code(item, f"{label}[{index}]") for index, item in enumerate(items))


def _parse_user_context(value: object) -> UserContext:
    item = _closed_mapping(value, _USER_CONTEXT_FIELDS, "user_context")
    return UserContext(
        project_relationship=_enum_value(
            ProjectRelationship,
            item["project_relationship"],
            "user_context.project_relationship",
        ),
        domain_experience=_enum_value(
            ExperienceLevel,
            item["domain_experience"],
            "user_context.domain_experience",
        ),
        technical_experience=_enum_value(
            ExperienceLevel,
            item["technical_experience"],
            "user_context.technical_experience",
        ),
        audience_mode=_enum_value(
            AudienceMode,
            item["audience_mode"],
            "user_context.audience_mode",
        ),
    )


def _parse_decision(value: object, index: int) -> Decision:
    label = f"decisions[{index}]"
    item = _closed_mapping(value, _DECISION_FIELDS, label)
    return Decision(
        decision_id=_code(item["decision_id"], f"{label}.decision_id"),
        topic_code=_code(item["topic_code"], f"{label}.topic_code"),
        disposition=_enum_value(
            DecisionDisposition, item["disposition"], f"{label}.disposition"
        ),
        resolution_state=_enum_value(
            ResolutionState,
            item["resolution_state"],
            f"{label}.resolution_state",
        ),
        recommendation_code=_code(
            item["recommendation_code"], f"{label}.recommendation_code"
        ),
        evidence_refs=_reference_values(
            item["evidence_refs"], f"{label}.evidence_refs"
        ),
        confidence=_enum_value(
            Confidence, item["confidence"], f"{label}.confidence"
        ),
        user_impact_code=_code(
            item["user_impact_code"], f"{label}.user_impact_code"
        ),
    )


def _parse_evidence(value: object, index: int) -> EvidenceReference:
    label = f"evidence[{index}]"
    item = _closed_mapping(value, _EVIDENCE_FIELDS, label)
    return EvidenceReference(
        reference_id=_code(item["reference_id"], f"{label}.reference_id"),
        kind=_enum_value(EvidenceKind, item["kind"], f"{label}.kind"),
        locator=_locator(item["locator"], f"{label}.locator"),
    )


def _parse_mapping(value: object) -> ProjectIntake:
    item = _closed_mapping(value, _TOP_LEVEL_FIELDS, "intake")
    decisions = _sequence(item["decisions"], "decisions", _MAX_DECISIONS)
    evidence = _sequence(item["evidence"], "evidence", _MAX_EVIDENCE)
    version = item["extension_schema_version"]
    if type(version) is not str or version != EXTENSION_SCHEMA_VERSION:
        raise IntakeContractError("unsupported intake extension_schema_version")
    return ProjectIntake(
        extension_schema_version=version,
        intake_id=_code(item["intake_id"], "intake_id"),
        project_mode=_enum_value(
            ProjectMode, item["project_mode"], "project_mode"
        ),
        project_mode_evidence_refs=_reference_values(
            item["project_mode_evidence_refs"], "project_mode_evidence_refs"
        ),
        purpose=_enum_value(Purpose, item["purpose"], "purpose"),
        user_context=_parse_user_context(item["user_context"]),
        remediation_level=_enum_value(
            RemediationLevel, item["remediation_level"], "remediation_level"
        ),
        stack_fitness=_enum_value(
            StackFitness, item["stack_fitness"], "stack_fitness"
        ),
        need_evidence_level=_enum_value(
            NeedEvidenceLevel,
            item["need_evidence_level"],
            "need_evidence_level",
        ),
        slice_complexity=_enum_value(
            SliceComplexity, item["slice_complexity"], "slice_complexity"
        ),
        decisions=tuple(
            _parse_decision(decision, index)
            for index, decision in enumerate(decisions)
        ),
        evidence=tuple(
            _parse_evidence(reference, index)
            for index, reference in enumerate(evidence)
        ),
        stop_state=_enum_value(StopState, item["stop_state"], "stop_state"),
    )


def _mapping(record: ProjectIntake) -> dict[str, object]:
    return {
        "decisions": [
            {
                "confidence": item.confidence.value,
                "decision_id": item.decision_id,
                "disposition": item.disposition.value,
                "evidence_refs": list(item.evidence_refs),
                "recommendation_code": item.recommendation_code,
                "resolution_state": item.resolution_state.value,
                "topic_code": item.topic_code,
                "user_impact_code": item.user_impact_code,
            }
            for item in record.decisions
        ],
        "evidence": [
            {
                "kind": item.kind.value,
                "locator": item.locator,
                "reference_id": item.reference_id,
            }
            for item in record.evidence
        ],
        "extension_schema_version": record.extension_schema_version,
        "intake_id": record.intake_id,
        "need_evidence_level": record.need_evidence_level.value,
        "project_mode": record.project_mode.value,
        "project_mode_evidence_refs": list(record.project_mode_evidence_refs),
        "purpose": record.purpose.value,
        "remediation_level": record.remediation_level.value,
        "slice_complexity": record.slice_complexity.value,
        "stack_fitness": record.stack_fitness.value,
        "stop_state": record.stop_state.value,
        "user_context": {
            "audience_mode": record.user_context.audience_mode.value,
            "domain_experience": record.user_context.domain_experience.value,
            "project_relationship": record.user_context.project_relationship.value,
            "technical_experience": record.user_context.technical_experience.value,
        },
    }


def render_intake(record: ProjectIntake) -> bytes:
    """Render one validated record to unique canonical UTF-8 JSON bytes."""

    if not isinstance(record, ProjectIntake):
        raise TypeError("record must be a ProjectIntake")
    try:
        encoded = json.dumps(
            _mapping(record),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise IntakeContractError(
            f"intake cannot be encoded as canonical JSON: {error}"
        ) from error
    return encoded.encode("utf-8") + b"\n"


def _reject_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IntakeContractError("intake contains duplicate object fields")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise IntakeContractError(f"intake contains unsupported JSON constant: {value}")


def parse_intake(payload: bytes | bytearray | memoryview) -> ProjectIntake:
    """Parse only bounded canonical UTF-8 JSON bytes into an immutable record."""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise IntakeContractError("intake payload must be bytes")
    raw = bytes(payload)
    if not raw or len(raw) > _MAX_CANONICAL_BYTES:
        raise IntakeContractError(
            "intake payload must use bounded non-empty canonical bytes"
        )
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_fields,
            parse_constant=_reject_constant,
        )
    except IntakeContractError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        RecursionError,
    ) as error:
        raise IntakeContractError("intake is not valid UTF-8 JSON") from error
    try:
        record = _parse_mapping(value)
        if render_intake(record) != raw:
            raise IntakeContractError("intake JSON is not canonical")
        return record
    except IntakeContractError:
        raise
    except (TypeError, ValueError, RecursionError) as error:
        raise IntakeContractError("intake JSON is not canonical") from error


__all__ = [
    "EXTENSION_SCHEMA_VERSION",
    "AudienceMode",
    "Confidence",
    "Decision",
    "DecisionDisposition",
    "EvidenceKind",
    "EvidenceReference",
    "ExperienceLevel",
    "IntakeContractError",
    "NeedEvidenceLevel",
    "ProjectIntake",
    "ProjectMode",
    "ProjectRelationship",
    "Purpose",
    "RemediationLevel",
    "ResolutionState",
    "SliceComplexity",
    "StackFitness",
    "StopState",
    "UserContext",
    "parse_intake",
    "render_intake",
]
