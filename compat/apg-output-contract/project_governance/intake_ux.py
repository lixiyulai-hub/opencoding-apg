"""Pure, bounded beginner-facing projection for adaptive project intake."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import re

from .domain_pack import DomainPack, DomainPackRegistry, MAX_PACKS
from .intake import ProjectIntake
from .intake_routing import IntakeRoute, RoutingDisposition, route_intake
from .stack_decision import RecommendationDisposition, StackDecision


INTAKE_UX_SCHEMA_VERSION = "1.0"
MAX_TEXT_LENGTH = 512
MAX_EVIDENCE_REFS = 384
MAX_PROFESSIONAL_GATE_IDS = 256
MAX_REFERENCE_BYTES = 48 * 1024
MAX_RENDERED_BYTES = 64 * 1024

_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}\Z")
_AUTHORITY_NOTICE = (
    "This view explains intake evidence only. It does not create an approval, "
    "select or execute an APG Gate, persist state, or authorize an apply."
)


class IntakeUXError(ValueError):
    """Raised when a guided intake view would contradict its source evidence."""


def _code(value: object, label: str) -> str:
    if type(value) is not str or not _CODE.fullmatch(value):
        raise IntakeUXError(f"{label} must be a bounded stable code")
    return value


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or len(value) > MAX_TEXT_LENGTH:
        raise IntakeUXError(f"{label} must be bounded non-empty text")
    if any(ord(character) < 32 for character in value):
        raise IntakeUXError(f"{label} must be single-line printable text")
    return value


def _canonical_codes(
    value: object, label: str, *, maximum: int
) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > maximum:
        raise IntakeUXError(f"{label} must be a bounded immutable tuple")
    normalized = tuple(_code(item, f"{label}[{index}]") for index, item in enumerate(value))
    if normalized != tuple(sorted(set(normalized))):
        raise IntakeUXError(f"{label} must use canonical unique order")
    return normalized


def _canonical_refs(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > MAX_EVIDENCE_REFS:
        raise IntakeUXError("evidence_refs must be a bounded immutable tuple")
    for index, item in enumerate(value):
        if type(item) is not str or not item or len(item) > 240:
            raise IntakeUXError(f"evidence_refs[{index}] must be bounded text")
        if any(ord(character) < 32 for character in item):
            raise IntakeUXError(
                f"evidence_refs[{index}] must not contain control characters"
            )
    if value != tuple(sorted(set(value))):
        raise IntakeUXError("evidence_refs must use canonical unique order")
    if sum(len(item.encode("utf-8")) for item in value) > MAX_REFERENCE_BYTES:
        raise IntakeUXError("evidence_refs exceed the aggregate byte bound")
    return value


@dataclass(frozen=True)
class DomainApplicabilityEvidence:
    """Intake-bound risk and data evidence used for Domain Pack matching."""

    domains: tuple[str, ...]
    risk_level: str
    data_class: str | None
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _canonical_codes(self.domains, "domains", maximum=MAX_PACKS)
        if not self.domains:
            raise IntakeUXError("applicability domains must not be empty")
        _code(self.risk_level, "risk_level")
        if self.data_class is not None:
            _code(self.data_class, "data_class")
        _canonical_refs(self.evidence_refs)
        expected_refs = {
            *(f"evidence.domain.{domain}" for domain in self.domains),
            f"evidence.risk.{self.risk_level}",
        }
        if self.data_class is not None:
            expected_refs.add(f"evidence.data-class.{self.data_class}")
        if self.evidence_refs != tuple(sorted(expected_refs)):
            raise IntakeUXError(
                "applicability evidence_refs must exactly bind domain, risk, and data claims"
            )


@dataclass(frozen=True)
class GuidedQuestion:
    """Stable codes from which one nonterminal question is derived."""

    decision_id: str
    topic_code: str
    recommendation_code: str
    user_impact_code: str
    disposition: RoutingDisposition

    def __post_init__(self) -> None:
        if type(self) is not GuidedQuestion:
            raise IntakeUXError("GuidedQuestion subclasses are not accepted")
        _code(self.decision_id, "decision_id")
        _code(self.topic_code, "topic_code")
        _code(self.recommendation_code, "recommendation_code")
        _code(self.user_impact_code, "user_impact_code")
        if self.disposition not in (
            RoutingDisposition.NEXT_QUESTION,
            RoutingDisposition.OWNER_GATE,
        ):
            raise IntakeUXError("question disposition must be nonterminal")

    @property
    def prompt(self) -> str:
        if self.disposition is RoutingDisposition.OWNER_GATE:
            return f"Which project-owner choice should govern {self.topic_code}?"
        return f"Which option best matches {self.topic_code} for this project?"

    @property
    def recommendation(self) -> str:
        if self.disposition is RoutingDisposition.OWNER_GATE:
            return (
                "Review the evidence first, then explicitly choose whether to follow "
                f"{self.recommendation_code}."
            )
        return (
            f"Start with {self.recommendation_code} unless your known constraints "
            "point to a different option."
        )

    @property
    def reason(self) -> str:
        if self.disposition is RoutingDisposition.OWNER_GATE:
            return (
                "This decision is human-bound, so the intake must wait for an explicit "
                "owner choice instead of treating an AI suggestion as approval."
            )
        return (
            "This is the first unresolved choice in the validated intake, so resolving "
            "it keeps the conversation focused on one useful decision."
        )

    @property
    def impact(self) -> str:
        return (
            f"Your answer resolves {self.decision_id}; its recorded impact is "
            f"{self.user_impact_code}."
        )

    @property
    def unknown_evidence_task(self) -> str:
        return (
            "If you are unsure, collect one bounded evidence item for "
            f"{self.decision_id} and keep the intake open; do not mark it complete."
        )


@dataclass(frozen=True)
class GuidedIntakeView:
    """Immutable UX projection; source contracts remain the authority."""

    intake_id: str
    disposition: RoutingDisposition
    open_decision_count: int
    question: GuidedQuestion | None
    source_record: ProjectIntake = field(repr=False)
    source_applicable_pack_ids: tuple[str, ...] = field(repr=False)
    source_stack_decision: StackDecision | None = field(default=None, repr=False)
    source_domain_pack_registry: DomainPackRegistry | None = field(
        default=None, repr=False
    )
    source_applicability_evidence: DomainApplicabilityEvidence | None = field(
        default=None, repr=False
    )
    stack_disposition: RecommendationDisposition | None = None
    selected_stack_candidate_id: str | None = None
    applicable_pack_ids: tuple[str, ...] = ()
    professional_gate_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    schema_version: str = INTAKE_UX_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self) is not GuidedIntakeView:
            raise IntakeUXError("GuidedIntakeView subclasses are not accepted")
        _code(self.intake_id, "intake_id")
        if not isinstance(self.disposition, RoutingDisposition):
            raise IntakeUXError("disposition must be a RoutingDisposition")
        if type(self.open_decision_count) is not int or self.open_decision_count < 0:
            raise IntakeUXError("open_decision_count must be a non-negative integer")
        if self.schema_version != INTAKE_UX_SCHEMA_VERSION:
            raise IntakeUXError(
                f"schema_version must be {INTAKE_UX_SCHEMA_VERSION}"
            )
        _canonical_codes(
            self.applicable_pack_ids,
            "applicable_pack_ids",
            maximum=MAX_PACKS,
        )
        _canonical_codes(
            self.professional_gate_ids,
            "professional_gate_ids",
            maximum=MAX_PROFESSIONAL_GATE_IDS,
        )
        _canonical_refs(self.evidence_refs)
        _canonical_codes(
            self.source_applicable_pack_ids,
            "source_applicable_pack_ids",
            maximum=MAX_PACKS,
        )

        terminal = self.disposition is RoutingDisposition.READY_FOR_PREVIEW
        if terminal:
            if self.question is not None or self.open_decision_count != 0:
                raise IntakeUXError("ready-for-preview views must contain no question")
        elif type(self.question) is not GuidedQuestion:
            raise IntakeUXError("nonterminal views must contain exactly one question")
        elif self.open_decision_count < 1:
            raise IntakeUXError("nonterminal views require an open decision")
        elif self.question.disposition is not self.disposition:
            raise IntakeUXError("question disposition must match its view")

        if self.stack_disposition is not None and type(
            self.stack_disposition
        ) is not RecommendationDisposition:
            raise IntakeUXError(
                "stack_disposition must be a RecommendationDisposition"
            )
        if self.selected_stack_candidate_id is not None:
            _code(self.selected_stack_candidate_id, "selected_stack_candidate_id")
            if self.stack_disposition is not RecommendationDisposition.RECOMMEND:
                raise IntakeUXError(
                    "a selected stack candidate requires recommend disposition"
                )
        elif self.stack_disposition is RecommendationDisposition.RECOMMEND:
            raise IntakeUXError("recommend disposition requires a selected candidate")

        _validate_view_source_binding(self)

    @property
    def status_message(self) -> str:
        return _status_message(self.disposition)

    @property
    def authority_notice(self) -> str:
        return _AUTHORITY_NOTICE


def _status_message(disposition: RoutingDisposition) -> str:
    if disposition is RoutingDisposition.READY_FOR_PREVIEW:
        return (
            "The current intake has no open question and is ready to prepare a "
            "governance preview; this is not apply approval."
        )
    if disposition is RoutingDisposition.OWNER_GATE:
        return (
            "One project-owner decision is required before this intake can continue; "
            "this view does not create that approval."
        )
    return "One unresolved project choice is ready for your answer."


def _decision_for_route(record: ProjectIntake, route: IntakeRoute):
    if route.decision_id is None:
        return None
    matches = tuple(
        decision
        for decision in record.decisions
        if decision.decision_id == route.decision_id
    )
    if len(matches) != 1:
        raise IntakeUXError("route decision is not bound to the intake record")
    return matches[0]


def _question(record: ProjectIntake, route: IntakeRoute) -> GuidedQuestion | None:
    if route.disposition is RoutingDisposition.READY_FOR_PREVIEW:
        return None
    decision = _decision_for_route(record, route)
    if decision is None:
        raise IntakeUXError("nonterminal route requires a bound decision")

    return GuidedQuestion(
        decision_id=decision.decision_id,
        topic_code=decision.topic_code,
        recommendation_code=decision.recommendation_code,
        user_impact_code=decision.user_impact_code,
        disposition=route.disposition,
    )


def _validate_stack(record: ProjectIntake, decision: StackDecision | None) -> None:
    if decision is None:
        return
    if type(decision) is not StackDecision:
        raise TypeError("stack_decision must be a StackDecision")
    if (
        decision.intake_id != record.intake_id
        or decision.project_mode is not record.project_mode
        or decision.purpose is not record.purpose
        or decision.required_evidence_level is not record.need_evidence_level
    ):
        raise IntakeUXError("stack decision is not bound to the intake record")


def _selected_packs(
    record: ProjectIntake,
    registry: DomainPackRegistry | None,
    pack_ids: tuple[str, ...],
    applicability_evidence: DomainApplicabilityEvidence | None,
) -> tuple[DomainPack, ...]:
    _canonical_codes(pack_ids, "applicable_pack_ids", maximum=MAX_PACKS)
    if registry is None:
        if pack_ids:
            raise IntakeUXError(
                "applicable pack IDs require a DomainPackRegistry"
            )
        if applicability_evidence is not None:
            raise IntakeUXError("applicability evidence requires selected packs")
        return ()
    if type(registry) is not DomainPackRegistry:
        raise TypeError("domain_pack_registry must be a DomainPackRegistry")
    if not pack_ids:
        if applicability_evidence is not None:
            raise IntakeUXError("applicability evidence requires selected packs")
        return ()
    if applicability_evidence is not None and type(
        applicability_evidence
    ) is not DomainApplicabilityEvidence:
        raise TypeError(
            "applicability_evidence must be DomainApplicabilityEvidence"
        )

    by_id = {pack.pack_id: pack for pack in registry.packs}
    if not set(pack_ids).issubset(by_id):
        raise IntakeUXError("applicable pack IDs must exist in the registry")
    selected = tuple(by_id[pack_id] for pack_id in pack_ids)
    if applicability_evidence is None:
        raise IntakeUXError(
            "selected packs require applicability evidence"
        )
    available_refs = {item.reference_id for item in record.evidence}
    if not set(applicability_evidence.evidence_refs).issubset(available_refs):
        raise IntakeUXError(
            "applicability evidence_refs must exist in the intake record"
        )
    for pack in selected:
        applicability = pack.applicability
        if pack.domain.value not in applicability_evidence.domains:
            raise IntakeUXError(
                f"selected pack {pack.pack_id} is not bound to a project domain"
            )
        context = {
            "domain": pack.domain.value,
            "project_mode": record.project_mode.value,
            "purpose": record.purpose.value,
            "risk_level": (
                applicability_evidence.risk_level
                if applicability_evidence is not None
                else None
            ),
            "data_class": (
                applicability_evidence.data_class
                if applicability_evidence is not None
                else None
            ),
        }
        if not applicability.matches(context):
            raise IntakeUXError(
                f"selected pack {pack.pack_id} contradicts applicability evidence"
            )
    return selected


def _pack_evidence(packs: tuple[DomainPack, ...]) -> tuple[set[str], set[str]]:
    refs: set[str] = set()
    gate_ids: set[str] = set()
    for pack in packs:
        refs.update(pack.source_refs)
        for profile in pack.test_profiles:
            refs.update(profile.evidence_refs)
        for profile in pack.performance_profiles:
            refs.add(profile.baseline_ref)
            refs.update(profile.evidence_refs)
        for requirement in pack.professional_gates:
            gate_ids.add(requirement.gate_id)
            refs.update(requirement.evidence_refs)
    return refs, gate_ids


def _projection(
    record: ProjectIntake,
    *,
    stack_decision: StackDecision | None,
    domain_pack_registry: DomainPackRegistry | None,
    applicable_pack_ids: tuple[str, ...],
    applicability_evidence: DomainApplicabilityEvidence | None,
) -> dict[str, object]:
    if type(record) is not ProjectIntake:
        raise TypeError("record must be a ProjectIntake")
    canonical_route = route_intake(record)
    _validate_stack(record, stack_decision)
    packs = _selected_packs(
        record,
        domain_pack_registry,
        applicable_pack_ids,
        applicability_evidence,
    )
    pack_refs, gate_ids = _pack_evidence(packs)
    refs = set(canonical_route.evidence_refs)
    if stack_decision is not None:
        refs.update(stack_decision.evidence_refs)
    refs.update(pack_refs)
    if applicability_evidence is not None:
        refs.update(applicability_evidence.evidence_refs)

    return {
        "intake_id": record.intake_id,
        "disposition": canonical_route.disposition,
        "open_decision_count": canonical_route.open_decision_count,
        "question": _question(record, canonical_route),
        "stack_disposition": (
            stack_decision.disposition if stack_decision is not None else None
        ),
        "selected_stack_candidate_id": (
            stack_decision.selected_candidate_id
            if stack_decision is not None
            else None
        ),
        "applicable_pack_ids": applicable_pack_ids,
        "professional_gate_ids": tuple(sorted(gate_ids)),
        "evidence_refs": tuple(sorted(refs)),
    }


def _validate_view_source_binding(
    view: GuidedIntakeView,
) -> dict[str, object]:
    if type(view) is not GuidedIntakeView:
        raise TypeError("view must be an exact GuidedIntakeView")
    if type(view.source_record) is not ProjectIntake:
        raise IntakeUXError("source_record must be an exact ProjectIntake")
    if type(view.intake_id) is not str:
        raise IntakeUXError("intake_id must be an exact string")
    if type(view.disposition) is not RoutingDisposition:
        raise IntakeUXError("disposition must be an exact RoutingDisposition")
    if type(view.open_decision_count) is not int:
        raise IntakeUXError("open_decision_count must be an exact integer")
    if view.question is not None and type(view.question) is not GuidedQuestion:
        raise IntakeUXError("question must be an exact GuidedQuestion")
    if view.stack_disposition is not None and type(
        view.stack_disposition
    ) is not RecommendationDisposition:
        raise IntakeUXError(
            "stack_disposition must be an exact RecommendationDisposition"
        )
    if view.selected_stack_candidate_id is not None and type(
        view.selected_stack_candidate_id
    ) is not str:
        raise IntakeUXError("selected_stack_candidate_id must be an exact string")
    if any(
        type(value) is not tuple
        for value in (
            view.source_applicable_pack_ids,
            view.applicable_pack_ids,
            view.professional_gate_ids,
            view.evidence_refs,
        )
    ):
        raise IntakeUXError("view code and evidence collections must be exact tuples")
    if view.source_stack_decision is not None and type(
        view.source_stack_decision
    ) is not StackDecision:
        raise IntakeUXError("source_stack_decision must be an exact StackDecision")
    if view.source_domain_pack_registry is not None and type(
        view.source_domain_pack_registry
    ) is not DomainPackRegistry:
        raise IntakeUXError(
            "source_domain_pack_registry must be an exact DomainPackRegistry"
        )
    if view.source_applicability_evidence is not None and type(
        view.source_applicability_evidence
    ) is not DomainApplicabilityEvidence:
        raise IntakeUXError(
            "source_applicability_evidence must be exact DomainApplicabilityEvidence"
        )
    expected = _projection(
        view.source_record,
        stack_decision=view.source_stack_decision,
        domain_pack_registry=view.source_domain_pack_registry,
        applicable_pack_ids=view.source_applicable_pack_ids,
        applicability_evidence=view.source_applicability_evidence,
    )
    actual = {
        "intake_id": view.intake_id,
        "disposition": view.disposition,
        "open_decision_count": view.open_decision_count,
        "question": view.question,
        "stack_disposition": view.stack_disposition,
        "selected_stack_candidate_id": view.selected_stack_candidate_id,
        "applicable_pack_ids": view.applicable_pack_ids,
        "professional_gate_ids": view.professional_gate_ids,
        "evidence_refs": view.evidence_refs,
    }
    if actual != expected:
        raise IntakeUXError(
            "guided intake view fields must match the recomputed source projection"
        )
    return expected


def build_guided_intake_view(
    record: ProjectIntake,
    *,
    route: IntakeRoute | None = None,
    stack_decision: StackDecision | None = None,
    domain_pack_registry: DomainPackRegistry | None = None,
    applicable_pack_ids: tuple[str, ...] = (),
    applicability_evidence: DomainApplicabilityEvidence | None = None,
) -> GuidedIntakeView:
    """Build one deterministic view after recomputing every routing decision."""

    if type(record) is not ProjectIntake:
        raise TypeError("record must be a ProjectIntake")
    canonical_route = route_intake(record)
    if route is not None:
        if type(route) is not IntakeRoute:
            raise TypeError("route must be an IntakeRoute")
        if route != canonical_route:
            raise IntakeUXError("supplied route does not match the recomputed route")

    projection = _projection(
        record,
        stack_decision=stack_decision,
        domain_pack_registry=domain_pack_registry,
        applicable_pack_ids=applicable_pack_ids,
        applicability_evidence=applicability_evidence,
    )
    return GuidedIntakeView(
        **projection,
        source_record=record,
        source_applicable_pack_ids=applicable_pack_ids,
        source_stack_decision=stack_decision,
        source_domain_pack_registry=domain_pack_registry,
        source_applicability_evidence=applicability_evidence,
    )


def _question_mapping(question: GuidedQuestion | None):
    if question is None:
        return None
    if type(question) is not GuidedQuestion:
        raise IntakeUXError("question must be an exact GuidedQuestion")
    return {
        "decision_id": question.decision_id,
        "impact": question.impact,
        "prompt": question.prompt,
        "reason": question.reason,
        "recommendation": question.recommendation,
        "topic_code": question.topic_code,
        "unknown_evidence_task": question.unknown_evidence_task,
    }


def render_guided_intake_view(view: GuidedIntakeView) -> bytes:
    """Render one view to canonical, bounded UTF-8 JSON bytes."""

    projection = _validate_view_source_binding(view)
    disposition = projection["disposition"]
    stack_disposition = projection["stack_disposition"]
    payload = {
        "applicable_pack_ids": list(projection["applicable_pack_ids"]),
        "authority_notice": _AUTHORITY_NOTICE,
        "disposition": disposition.value,
        "evidence_refs": list(projection["evidence_refs"]),
        "intake_id": projection["intake_id"],
        "open_decision_count": projection["open_decision_count"],
        "professional_gate_ids": list(projection["professional_gate_ids"]),
        "question": _question_mapping(projection["question"]),
        "schema_version": INTAKE_UX_SCHEMA_VERSION,
        "selected_stack_candidate_id": projection["selected_stack_candidate_id"],
        "stack_disposition": (
            stack_disposition.value
            if stack_disposition is not None
            else None
        ),
        "status_message": _status_message(disposition),
    }
    rendered = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    if len(rendered) > MAX_RENDERED_BYTES:
        raise IntakeUXError(
            f"rendered intake view exceeds its {MAX_RENDERED_BYTES}-byte bound"
        )
    return rendered


build_intake_view = build_guided_intake_view
render_intake_view = render_guided_intake_view
render_intake_ux = render_guided_intake_view


__all__ = [
    "DomainApplicabilityEvidence",
    "GuidedIntakeView",
    "GuidedQuestion",
    "INTAKE_UX_SCHEMA_VERSION",
    "IntakeUXError",
    "MAX_RENDERED_BYTES",
    "build_guided_intake_view",
    "build_intake_view",
    "render_guided_intake_view",
    "render_intake_ux",
    "render_intake_view",
]
