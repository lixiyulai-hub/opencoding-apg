"""Evidence-bound, deterministic technical-stack recommendation contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from .intake import (
    NeedEvidenceLevel,
    ProjectIntake,
    ProjectMode,
    Purpose,
    ResolutionState,
    StackFitness,
    StopState,
)


class StackDecisionError(ValueError):
    """Raised when a stack candidate or decision violates the closed contract."""


class StackDimension(str, Enum):
    DELIVERY = "delivery"
    EXISTING_ASSETS = "existing-assets"
    OFFLINE = "offline"
    PERFORMANCE = "performance"
    MAINTENANCE = "maintenance"
    VERIFICATION = "verification"


class CandidateKind(str, Enum):
    NEW = "new"
    EXISTING = "existing"
    REPLACEMENT = "replacement"


class RecommendationDisposition(str, Enum):
    RECOMMEND = "recommend"
    NEEDS_EVIDENCE = "needs-evidence"
    OWNER_GATE = "owner-gate"
    CORRECTION = "correction"


_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}\Z")
_MAX_REFERENCES_PER_RECORD = 16
_MAX_CANDIDATES = 16
_MAX_DECISION_REFERENCES = _MAX_REFERENCES_PER_RECORD * (_MAX_CANDIDATES + 1)
_MAX_RENDERED_BYTES = 64 * 1024
_DIMENSIONS = tuple(StackDimension)
_BASE_WEIGHTS = {
    StackDimension.DELIVERY: 3,
    StackDimension.EXISTING_ASSETS: 2,
    StackDimension.OFFLINE: 2,
    StackDimension.PERFORMANCE: 2,
    StackDimension.MAINTENANCE: 3,
    StackDimension.VERIFICATION: 3,
}
_EVIDENCE_RANK = {
    NeedEvidenceLevel.T0: 0,
    NeedEvidenceLevel.T1: 1,
    NeedEvidenceLevel.T2: 2,
}


def _code(value: object, label: str) -> str:
    if type(value) is not str or not value or not _CODE.fullmatch(value):
        raise StackDecisionError(f"{label} must be a bounded stable code")
    return value


def _refs(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
    maximum: int = _MAX_REFERENCES_PER_RECORD,
) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise StackDecisionError(f"{label} must be an immutable tuple")
    if not allow_empty and not value:
        raise StackDecisionError(f"{label} must not be empty")
    if len(value) > maximum:
        raise StackDecisionError(f"{label} exceeds its {maximum}-item bound")
    normalized = tuple(_code(item, f"{label}[{index}]") for index, item in enumerate(value))
    if normalized != tuple(sorted(set(normalized))):
        raise StackDecisionError(f"{label} must use canonical order")
    return normalized


@dataclass(frozen=True)
class DimensionAssessment:
    """One bounded, evidence-linked score for a stack dimension."""

    dimension: StackDimension
    score: int
    rationale_code: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, StackDimension):
            raise StackDecisionError("dimension must be a StackDimension")
        if type(self.score) is not int or not 0 <= self.score <= 5:
            raise StackDecisionError("score must be an integer from 0 through 5")
        _code(self.rationale_code, "rationale_code")
        _refs(self.evidence_refs, "evidence_refs")


@dataclass(frozen=True)
class StackCandidate:
    """A candidate stack with all dimensions scored before recommendation."""

    candidate_id: str
    architecture_code: str
    candidate_kind: CandidateKind
    evidence_level: NeedEvidenceLevel
    assessments: tuple[DimensionAssessment, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _code(self.candidate_id, "candidate_id")
        _code(self.architecture_code, "architecture_code")
        if not isinstance(self.candidate_kind, CandidateKind):
            raise StackDecisionError("candidate_kind must be a CandidateKind")
        if not isinstance(self.evidence_level, NeedEvidenceLevel):
            raise StackDecisionError("evidence_level must be a NeedEvidenceLevel")
        if type(self.assessments) is not tuple or len(self.assessments) != len(_DIMENSIONS):
            raise StackDecisionError("assessments must include each stack dimension exactly once")
        if any(not isinstance(item, DimensionAssessment) for item in self.assessments):
            raise StackDecisionError("assessments must contain DimensionAssessment values")
        dimensions = tuple(item.dimension for item in self.assessments)
        if dimensions != tuple(sorted(_DIMENSIONS, key=lambda item: item.value)):
            raise StackDecisionError("assessments must use canonical dimension order")
        _refs(self.evidence_refs, "evidence_refs")
        if not set(self.evidence_refs).issuperset(
            ref for item in self.assessments for ref in item.evidence_refs
        ):
            raise StackDecisionError("candidate evidence_refs must cover assessments")


@dataclass(frozen=True)
class CandidateSummary:
    candidate_id: str
    architecture_code: str
    candidate_kind: CandidateKind
    evidence_level: NeedEvidenceLevel
    score: int
    max_score: int
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _code(self.candidate_id, "candidate_id")
        _code(self.architecture_code, "architecture_code")
        if not isinstance(self.candidate_kind, CandidateKind):
            raise StackDecisionError("candidate_kind must be a CandidateKind")
        if not isinstance(self.evidence_level, NeedEvidenceLevel):
            raise StackDecisionError("evidence_level must be a NeedEvidenceLevel")
        if type(self.score) is not int or type(self.max_score) is not int:
            raise StackDecisionError("score fields must be integers")
        if self.score < 0 or self.max_score <= 0 or self.score > self.max_score:
            raise StackDecisionError("score must be within max_score")
        _refs(self.evidence_refs, "evidence_refs")


@dataclass(frozen=True)
class StackDecision:
    """Deterministic recommendation output; it never authorizes an apply."""

    intake_id: str
    project_mode: ProjectMode
    purpose: Purpose
    required_evidence_level: NeedEvidenceLevel
    disposition: RecommendationDisposition
    selected_candidate_id: str | None
    top_score: int
    top_margin: int
    candidates: tuple[CandidateSummary, ...]
    correction_codes: tuple[str, ...]
    unresolved_decision_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _code(self.intake_id, "intake_id")
        if not isinstance(self.project_mode, ProjectMode):
            raise StackDecisionError("project_mode must be a ProjectMode")
        if not isinstance(self.purpose, Purpose):
            raise StackDecisionError("purpose must be a Purpose")
        if not isinstance(self.required_evidence_level, NeedEvidenceLevel):
            raise StackDecisionError("required_evidence_level must be a NeedEvidenceLevel")
        if not isinstance(self.disposition, RecommendationDisposition):
            raise StackDecisionError("disposition must be a RecommendationDisposition")
        if self.selected_candidate_id is not None:
            _code(self.selected_candidate_id, "selected_candidate_id")
        if type(self.top_score) is not int or type(self.top_margin) is not int:
            raise StackDecisionError("score fields must be integers")
        if self.top_score < 0 or self.top_margin < 0:
            raise StackDecisionError("score fields must be non-negative")
        if type(self.candidates) is not tuple or not self.candidates:
            raise StackDecisionError("candidates must not be empty")
        if any(not isinstance(item, CandidateSummary) for item in self.candidates):
            raise StackDecisionError("candidates must contain CandidateSummary values")
        ordered = tuple(
            sorted(self.candidates, key=lambda item: (-item.score, item.candidate_id))
        )
        if self.candidates != ordered:
            raise StackDecisionError("candidates must be sorted by score and candidate_id")
        if self.selected_candidate_id is not None and self.selected_candidate_id not in {
            item.candidate_id for item in self.candidates
        }:
            raise StackDecisionError("selected_candidate_id must reference a candidate")
        _refs(self.correction_codes, "correction_codes", allow_empty=True, maximum=4)
        _refs(
            self.unresolved_decision_ids,
            "unresolved_decision_ids",
            allow_empty=True,
            maximum=32,
        )
        _refs(
            self.evidence_refs,
            "evidence_refs",
            allow_empty=True,
            maximum=_MAX_DECISION_REFERENCES,
        )


def _weights(record: ProjectIntake) -> dict[StackDimension, int]:
    weights = dict(_BASE_WEIGHTS)
    if record.project_mode is ProjectMode.EXISTING:
        weights[StackDimension.EXISTING_ASSETS] += 2
        weights[StackDimension.MAINTENANCE] += 1
    if record.purpose is Purpose.REAL_AUDIENCE:
        weights[StackDimension.PERFORMANCE] += 1
        weights[StackDimension.VERIFICATION] += 2
    if record.need_evidence_level is NeedEvidenceLevel.T2:
        weights[StackDimension.PERFORMANCE] += 1
        weights[StackDimension.VERIFICATION] += 1
    return weights


def _summary(record: ProjectIntake, candidate: StackCandidate) -> CandidateSummary:
    weights = _weights(record)
    total = sum(
        assessment.score * weights[assessment.dimension]
        for assessment in candidate.assessments
    )
    maximum = sum(5 * weight for weight in weights.values())
    return CandidateSummary(
        candidate_id=candidate.candidate_id,
        architecture_code=candidate.architecture_code,
        candidate_kind=candidate.candidate_kind,
        evidence_level=candidate.evidence_level,
        score=total,
        max_score=maximum,
        evidence_refs=candidate.evidence_refs,
    )


def score_stack_candidates(
    record: ProjectIntake, candidates: tuple[StackCandidate, ...]
) -> StackDecision:
    """Rank candidates and expose correction states without external side effects."""

    if not isinstance(record, ProjectIntake):
        raise TypeError("record must be a ProjectIntake")
    if type(candidates) is not tuple or not candidates:
        raise StackDecisionError("candidates must be a non-empty tuple")
    if len(candidates) > _MAX_CANDIDATES:
        raise StackDecisionError(f"candidates exceeds its {_MAX_CANDIDATES}-item bound")
    if any(not isinstance(item, StackCandidate) for item in candidates):
        raise StackDecisionError("candidates must contain StackCandidate values")
    if len({item.candidate_id for item in candidates}) != len(candidates):
        raise StackDecisionError("candidate IDs must be unique")

    summaries = tuple(sorted((_summary(record, item) for item in candidates), key=lambda item: (-item.score, item.candidate_id)))
    required_rank = _EVIDENCE_RANK[record.need_evidence_level]
    evidence_ok = all(_EVIDENCE_RANK[item.evidence_level] >= required_rank for item in candidates)
    top_score = summaries[0].score
    second_score = summaries[1].score if len(summaries) > 1 else 0
    top_margin = top_score - second_score
    unresolved = tuple(
        sorted(item.decision_id for item in record.decisions if item.resolution_state is ResolutionState.OPEN)
    )
    refs = tuple(
        sorted(
            set(record.project_mode_evidence_refs).union(
                ref for item in candidates for ref in item.evidence_refs
            )
        )
    )
    correction_codes: set[str] = set()
    if record.stack_fitness in (StackFitness.S0, StackFitness.S1):
        correction_codes.add("stack-fitness-low")
    if record.project_mode is ProjectMode.EXISTING and not any(
        item.candidate_kind is CandidateKind.EXISTING for item in candidates
    ):
        correction_codes.add("existing-stack-baseline-missing")

    if record.stop_state is StopState.OWNER_GATE:
        disposition = RecommendationDisposition.OWNER_GATE
        selected = None
    elif not evidence_ok:
        disposition = RecommendationDisposition.NEEDS_EVIDENCE
        selected = None
        correction_codes.add("candidate-evidence-below-required-level")
    elif unresolved:
        disposition = RecommendationDisposition.OWNER_GATE
        selected = None
    elif len(summaries) > 1 and top_margin == 0:
        disposition = RecommendationDisposition.NEEDS_EVIDENCE
        selected = None
        correction_codes.add("top-candidate-tie")
    elif correction_codes:
        disposition = RecommendationDisposition.CORRECTION
        selected = None
    else:
        disposition = RecommendationDisposition.RECOMMEND
        selected = summaries[0].candidate_id

    return StackDecision(
        intake_id=record.intake_id,
        project_mode=record.project_mode,
        purpose=record.purpose,
        required_evidence_level=record.need_evidence_level,
        disposition=disposition,
        selected_candidate_id=selected,
        top_score=top_score,
        top_margin=top_margin,
        candidates=summaries,
        correction_codes=tuple(sorted(correction_codes)),
        unresolved_decision_ids=unresolved,
        evidence_refs=refs,
    )


def render_stack_decision(decision: StackDecision) -> bytes:
    """Render a stable ADR fragment; the result is evidence, not authorization."""

    if not isinstance(decision, StackDecision):
        raise TypeError("decision must be a StackDecision")
    lines = [
        f"# Stack Decision {decision.intake_id}",
        "",
        f"- Disposition: `{decision.disposition.value}`",
        f"- Project mode: `{decision.project_mode.value}`",
        f"- Purpose: `{decision.purpose.value}`",
        f"- Required evidence: `{decision.required_evidence_level.value}`",
        f"- Selected candidate: `{decision.selected_candidate_id or 'none'}`",
        "",
        "## Candidate Scores",
        "",
        "| Candidate | Architecture | Kind | Evidence | Score |",
        "|---|---|---|---|---:|",
    ]
    lines.extend(
        f"| `{item.candidate_id}` | `{item.architecture_code}` | `{item.candidate_kind.value}` | `{item.evidence_level.value}` | {item.score}/{item.max_score} |"
        for item in decision.candidates
    )
    lines.extend(["", "## Corrections", ""])
    if decision.correction_codes:
        lines.extend(f"- `{code}`" for code in decision.correction_codes)
    else:
        lines.append("- None")
    lines.extend(["", "## Unresolved Decisions", ""])
    if decision.unresolved_decision_ids:
        lines.extend(f"- `{identifier}`" for identifier in decision.unresolved_decision_ids)
    else:
        lines.append("- None")
    lines.extend(["", "## Evidence", ""])
    if decision.evidence_refs:
        lines.extend(f"- `{reference}`" for reference in decision.evidence_refs)
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "This projection records evidence and recommendation state only. It does not authorize APG apply, Gate selection, dependency installation, runtime, promotion, or release.",
            "",
        ]
    )
    rendered = "\n".join(lines).encode("utf-8")
    if len(rendered) > _MAX_RENDERED_BYTES:
        raise StackDecisionError(
            f"rendered stack decision exceeds its {_MAX_RENDERED_BYTES}-byte bound"
        )
    return rendered


__all__ = [
    "CandidateKind",
    "CandidateSummary",
    "DimensionAssessment",
    "RecommendationDisposition",
    "StackCandidate",
    "StackDecision",
    "StackDecisionError",
    "StackDimension",
    "render_stack_decision",
    "score_stack_candidates",
]
