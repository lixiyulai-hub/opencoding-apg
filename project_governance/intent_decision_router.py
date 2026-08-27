"""Closed, deterministic P3-A routing for normalized user intent."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any, Mapping
import unicodedata

from .intake import (
    DecisionDisposition as P2DecisionDisposition,
    ProjectIntake,
    ResolutionState,
)
from .stack_decision import RecommendationDisposition, StackDecision
from .storage import SchemaError, canonical_json_bytes
from .user_intent import (
    ConstraintCode,
    GoalCode,
    ProjectType,
    TargetPlatform,
    UncertaintyCode,
    UserIntent,
    UserPersona,
    parse_user_intent,
    render_user_intent,
)


INTENT_DECISION_SCHEMA_VERSION = "1.0"
MAX_INTENT_DECISION_BYTES = 64 * 1024

_MAX_CODE_LENGTH = 80
_MAX_REFERENCE_LENGTH = 240
_MAX_REFERENCES = 64
_MAX_REFERENCES_PER_ITEM = 16
_MAX_DECISIONS = 32
_MAX_QUESTIONS = 16
_MAX_PLAN_ENTRIES = 16
_MAX_RATIONALES = 32

_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")
_OPAQUE_SECRET = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}|\bghp_[A-Za-z0-9]{8,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.)"
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "automatic_decisions",
        "confidence",
        "confirmation_required_decisions",
        "evidence_refs",
        "intent_id",
        "intent_sha256",
        "necessary_questions",
        "rationale",
        "ready_for_blueprint",
        "recommended_decisions",
        "recommended_plan",
        "schema_version",
        "structured_intent",
    }
)
_DECISION_FIELDS = frozenset(
    {
        "confidence",
        "decision_id",
        "disposition",
        "evidence_refs",
        "rationale_code",
        "recommendation_code",
        "topic_code",
        "trigger_codes",
    }
)
_QUESTION_FIELDS = frozenset(
    {
        "evidence_refs",
        "impact_code",
        "question_id",
        "rationale_code",
        "recommendation_code",
        "topic_code",
    }
)

_MANDATORY_CONFIRM = frozenset(
    {
        ConstraintCode.COST,
        ConstraintCode.PRODUCTION,
        ConstraintCode.PRIVACY,
        ConstraintCode.REAL_DATA,
        ConstraintCode.PROVIDER_NETWORK,
        ConstraintCode.PUBLICATION,
        ConstraintCode.DEPLOYMENT,
        ConstraintCode.IRREVERSIBLE_EXTERNAL_ACTION,
    }
)


class IntentDecisionError(ValueError):
    """Raised when a P3-A route violates its closed contract."""


class DecisionDisposition(str, Enum):
    AUTO = "AUTO"
    RECOMMEND = "RECOMMEND"
    CONFIRM = "CONFIRM"


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def _scalar(value: object, label: str, *, maximum: int) -> str:
    if type(value) is not str or not value:
        raise IntentDecisionError(f"{label} must be a non-empty string")
    if len(value) > maximum:
        raise IntentDecisionError(f"{label} exceeds its {maximum}-character bound")
    if unicodedata.normalize("NFC", value) != value:
        raise IntentDecisionError(f"{label} must use NFC Unicode")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise IntentDecisionError(f"{label} contains control characters")
    if _OPAQUE_SECRET.search(value):
        raise IntentDecisionError(f"{label} contains a sensitive-value pattern")
    return value


def _code(value: object, label: str) -> str:
    text = _scalar(value, label, maximum=_MAX_CODE_LENGTH)
    if not _CODE.fullmatch(text):
        raise IntentDecisionError(f"{label} must be a bounded stable code")
    return text


def _safe_relative_path(value: str) -> bool:
    if "\\" in value or value.startswith("/") or _WINDOWS_DRIVE.match(value):
        return False
    if "://" in value or ":" in value or "?" in value or "#" in value:
        return False
    parts = value.split("/")
    if len(parts) < 2 or any(part in ("", ".", "..") for part in parts):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and tuple(path.parts) == tuple(parts)


def _reference(value: object, label: str) -> str:
    text = _scalar(value, label, maximum=_MAX_REFERENCE_LENGTH)
    if _CODE.fullmatch(text) or _safe_relative_path(text):
        return text
    raise IntentDecisionError(
        f"{label} must be a stable code or contained project-relative path"
    )


def _enum(value: object, enum_type: type[Enum], label: str) -> None:
    if not isinstance(value, enum_type):
        raise IntentDecisionError(f"{label} must be a {enum_type.__name__}")


def _enum_value(enum_type: type[Enum], value: object, label: str) -> Enum:
    if type(value) is not str:
        raise IntentDecisionError(f"{label} must be a string enum value")
    try:
        return enum_type(value)
    except ValueError as error:
        raise IntentDecisionError(f"{label} has an unsupported value") from error


def _tuple(value: object, label: str, maximum: int) -> tuple[object, ...]:
    if type(value) is not tuple or len(value) > maximum:
        raise IntentDecisionError(f"{label} must be a bounded immutable tuple")
    return value


def _sequence(value: object, label: str, maximum: int) -> tuple[object, ...]:
    if type(value) is not list or len(value) > maximum:
        raise IntentDecisionError(f"{label} must be a bounded array")
    return tuple(value)


def _codes(
    value: object,
    label: str,
    maximum: int,
    *,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    items = _tuple(value, label, maximum)
    if not allow_empty and not items:
        raise IntentDecisionError(f"{label} must not be empty")
    normalized = tuple(_code(item, f"{label}[{index}]") for index, item in enumerate(items))
    if normalized != tuple(sorted(set(normalized))):
        raise IntentDecisionError(f"{label} must use canonical unique order")
    return normalized


def _references(
    value: object,
    label: str,
    maximum: int = _MAX_REFERENCES_PER_ITEM,
) -> tuple[str, ...]:
    items = _tuple(value, label, maximum)
    normalized = tuple(
        _reference(item, f"{label}[{index}]") for index, item in enumerate(items)
    )
    if normalized != tuple(sorted(set(normalized))):
        raise IntentDecisionError(f"{label} must use canonical unique order")
    return normalized


@dataclass(frozen=True)
class IntentDecision:
    decision_id: str
    topic_code: str
    disposition: DecisionDisposition
    recommendation_code: str
    rationale_code: str
    confidence: Confidence
    trigger_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self) is not IntentDecision:
            raise IntentDecisionError("IntentDecision subclasses are not accepted")
        _code(self.decision_id, "decision.decision_id")
        _code(self.topic_code, "decision.topic_code")
        _enum(self.disposition, DecisionDisposition, "decision.disposition")
        _code(self.recommendation_code, "decision.recommendation_code")
        _code(self.rationale_code, "decision.rationale_code")
        _enum(self.confidence, Confidence, "decision.confidence")
        triggers = _codes(
            self.trigger_codes,
            "decision.trigger_codes",
            _MAX_REFERENCES_PER_ITEM,
        )
        _references(self.evidence_refs, "decision.evidence_refs")
        if self.disposition is DecisionDisposition.CONFIRM and not triggers:
            raise IntentDecisionError("CONFIRM decisions require a trigger code")
        if self.disposition is not DecisionDisposition.CONFIRM and triggers:
            raise IntentDecisionError("only CONFIRM decisions may carry trigger codes")


@dataclass(frozen=True)
class NecessaryQuestion:
    question_id: str
    topic_code: str
    recommendation_code: str
    rationale_code: str
    impact_code: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self) is not NecessaryQuestion:
            raise IntentDecisionError("NecessaryQuestion subclasses are not accepted")
        _code(self.question_id, "question.question_id")
        _code(self.topic_code, "question.topic_code")
        _code(self.recommendation_code, "question.recommendation_code")
        _code(self.rationale_code, "question.rationale_code")
        _code(self.impact_code, "question.impact_code")
        _references(self.evidence_refs, "question.evidence_refs")


@dataclass(frozen=True)
class IntentDecisionResult:
    schema_version: str
    intent_id: str
    intent_sha256: str
    structured_intent: UserIntent
    necessary_questions: tuple[NecessaryQuestion, ...]
    recommended_plan: tuple[str, ...]
    automatic_decisions: tuple[IntentDecision, ...]
    recommended_decisions: tuple[IntentDecision, ...]
    confirmation_required_decisions: tuple[IntentDecision, ...]
    rationale: tuple[str, ...]
    confidence: Confidence
    evidence_refs: tuple[str, ...]
    ready_for_blueprint: bool

    def __post_init__(self) -> None:
        if type(self) is not IntentDecisionResult:
            raise IntentDecisionError(
                "IntentDecisionResult subclasses are not accepted"
            )
        if self.schema_version != INTENT_DECISION_SCHEMA_VERSION:
            raise IntentDecisionError("unsupported intent-decision schema_version")
        _code(self.intent_id, "intent_id")
        if type(self.intent_sha256) is not str or not _SHA256.fullmatch(
            self.intent_sha256
        ):
            raise IntentDecisionError(
                "intent_sha256 must be a lowercase SHA-256 digest"
            )
        if type(self.structured_intent) is not UserIntent:
            raise IntentDecisionError("structured_intent must be an exact UserIntent")
        if self.intent_id != self.structured_intent.intent_id:
            raise IntentDecisionError("intent_id must match structured_intent")
        expected_digest = hashlib.sha256(
            render_user_intent(self.structured_intent)
        ).hexdigest()
        if self.intent_sha256 != expected_digest:
            raise IntentDecisionError("intent_sha256 does not bind structured_intent")

        questions = _tuple(
            self.necessary_questions, "necessary_questions", _MAX_QUESTIONS
        )
        if any(type(item) is not NecessaryQuestion for item in questions):
            raise IntentDecisionError(
                "necessary_questions must contain NecessaryQuestion records"
            )
        question_ids = tuple(item.question_id for item in questions)
        if question_ids != tuple(sorted(set(question_ids))):
            raise IntentDecisionError(
                "necessary_questions must use canonical unique order"
            )
        _codes(self.recommended_plan, "recommended_plan", _MAX_PLAN_ENTRIES)

        groups = (
            (self.automatic_decisions, DecisionDisposition.AUTO, "automatic_decisions"),
            (
                self.recommended_decisions,
                DecisionDisposition.RECOMMEND,
                "recommended_decisions",
            ),
            (
                self.confirmation_required_decisions,
                DecisionDisposition.CONFIRM,
                "confirmation_required_decisions",
            ),
        )
        all_decisions: list[IntentDecision] = []
        for values, disposition, label in groups:
            items = _tuple(values, label, _MAX_DECISIONS)
            if any(type(item) is not IntentDecision for item in items):
                raise IntentDecisionError(f"{label} must contain IntentDecision records")
            identifiers = tuple(item.decision_id for item in items)
            if identifiers != tuple(sorted(set(identifiers))):
                raise IntentDecisionError(f"{label} must use canonical unique order")
            if any(item.disposition is not disposition for item in items):
                raise IntentDecisionError(f"{label} contains the wrong disposition")
            all_decisions.extend(items)
        if len(all_decisions) > _MAX_DECISIONS:
            raise IntentDecisionError("decision groups exceed the 32-decision bound")
        identifiers = tuple(item.decision_id for item in all_decisions)
        if len(set(identifiers)) != len(identifiers):
            raise IntentDecisionError("decision groups contain duplicate decision IDs")

        _codes(self.rationale, "rationale", _MAX_RATIONALES)
        _enum(self.confidence, Confidence, "confidence")
        refs = _references(self.evidence_refs, "evidence_refs", _MAX_REFERENCES)
        expected_refs = set(self.structured_intent.evidence_refs)
        expected_refs.update(
            reference
            for item in questions
            for reference in item.evidence_refs
        )
        expected_refs.update(
            reference
            for item in all_decisions
            for reference in item.evidence_refs
        )
        if refs != tuple(sorted(expected_refs)):
            raise IntentDecisionError(
                "evidence_refs must exactly cover structured and routed evidence"
            )
        if type(self.ready_for_blueprint) is not bool:
            raise IntentDecisionError("ready_for_blueprint must be a boolean")
        expected_ready = not self.confirmation_required_decisions
        if self.ready_for_blueprint is not expected_ready:
            raise IntentDecisionError(
                "ready_for_blueprint is inconsistent with CONFIRM decisions"
            )


def _decision(
    identifier: str,
    topic: str,
    disposition: DecisionDisposition,
    recommendation: str,
    rationale: str,
    confidence: Confidence,
    refs: tuple[str, ...],
    *,
    trigger: str | None = None,
) -> IntentDecision:
    return IntentDecision(
        decision_id=identifier,
        topic_code=topic,
        disposition=disposition,
        recommendation_code=recommendation,
        rationale_code=rationale,
        confidence=confidence,
        trigger_codes=() if trigger is None else (trigger,),
        evidence_refs=refs,
    )


def _question(
    identifier: str,
    topic: str,
    recommendation: str,
    rationale: str,
    impact: str,
    refs: tuple[str, ...],
) -> NecessaryQuestion:
    return NecessaryQuestion(
        question_id=identifier,
        topic_code=topic,
        recommendation_code=recommendation,
        rationale_code=rationale,
        impact_code=impact,
        evidence_refs=refs,
    )


def _confirmation_bundle(
    decisions: tuple[IntentDecision, ...],
) -> tuple[IntentDecision, ...]:
    """Collapse simultaneous consequential triggers into one human Gate."""

    if len(decisions) <= 1:
        return decisions
    triggers = tuple(
        sorted({trigger for item in decisions for trigger in item.trigger_codes})
    )
    evidence_refs = tuple(
        sorted({reference for item in decisions for reference in item.evidence_refs})
    )
    return (
        IntentDecision(
            decision_id="decision.confirmation.bundle",
            topic_code="confirmation.bundle",
            disposition=DecisionDisposition.CONFIRM,
            recommendation_code="recommendation.resolve-confirmation-bundle",
            rationale_code="rationale.confirmation-bundle",
            confidence=Confidence.HIGH,
            trigger_codes=triggers,
            evidence_refs=evidence_refs,
        ),
    )


def _confirmation_bundle_question(refs: tuple[str, ...]) -> NecessaryQuestion:
    return _question(
        "question.confirmation.bundle",
        "confirmation.bundle",
        "recommendation.resolve-confirmation-bundle",
        "rationale.confirmation-bundle",
        "impact.consequential-boundary",
        refs,
    )


def _intent_facts(record: UserIntent) -> tuple[IntentDecision, ...]:
    refs = record.evidence_refs
    decisions: list[IntentDecision] = []
    if record.project_type is not ProjectType.UNKNOWN:
        decisions.append(
            _decision(
                "decision.intent.project-type",
                "intent.project-type",
                DecisionDisposition.AUTO,
                f"project-type.{record.project_type.value}",
                "rationale.normalized-project-type",
                Confidence.HIGH,
                refs,
            )
        )
    if record.target_platform is not TargetPlatform.UNKNOWN:
        decisions.append(
            _decision(
                "decision.intent.target-platform",
                "intent.target-platform",
                DecisionDisposition.AUTO,
                f"target-platform.{record.target_platform.value}",
                "rationale.normalized-target-platform",
                Confidence.HIGH,
                refs,
            )
        )
    if record.user_persona is not UserPersona.UNKNOWN:
        decisions.append(
            _decision(
                "decision.intent.user-persona",
                "intent.user-persona",
                DecisionDisposition.AUTO,
                f"user-persona.{record.user_persona.value}",
                "rationale.normalized-user-persona",
                Confidence.HIGH,
                refs,
            )
        )
    for goal in record.goal_codes:
        decisions.append(
            _decision(
                f"decision.intent.goal.{goal.value}",
                f"intent.goal.{goal.value}",
                DecisionDisposition.AUTO,
                f"goal.{goal.value}",
                "rationale.normalized-goal",
                Confidence.HIGH,
                refs,
            )
        )
    return tuple(decisions)


def _uncertainty_routes(
    record: UserIntent,
) -> tuple[tuple[IntentDecision, ...], tuple[NecessaryQuestion, ...]]:
    refs = record.evidence_refs
    decisions: list[IntentDecision] = []
    questions: list[NecessaryQuestion] = []
    for uncertainty in record.uncertainty_codes:
        topic = f"intent.{uncertainty.value}"
        decision_id = f"decision.intent.{uncertainty.value}"
        question_id = f"question.intent.{uncertainty.value}"
        recommendation = f"recommendation.default-{uncertainty.value}"
        if uncertainty is UncertaintyCode.PRODUCT_DIRECTION:
            disposition = DecisionDisposition.CONFIRM
            rationale = "rationale.material-product-ambiguity"
            trigger = "trigger.material-product-ambiguity"
            impact = "impact.product-direction"
        else:
            disposition = DecisionDisposition.RECOMMEND
            rationale = "rationale.incomplete-structured-intent"
            trigger = None
            impact = "impact.scope"
        decisions.append(
            _decision(
                decision_id,
                topic,
                disposition,
                recommendation,
                rationale,
                Confidence.LOW,
                refs,
                trigger=trigger,
            )
        )
        questions.append(
            _question(
                question_id,
                topic,
                recommendation,
                rationale,
                impact,
                refs,
            )
        )
    return tuple(decisions), tuple(questions)


def _constraint_routes(
    record: UserIntent,
) -> tuple[tuple[IntentDecision, ...], tuple[NecessaryQuestion, ...]]:
    refs = record.evidence_refs
    decisions: list[IntentDecision] = []
    questions: list[NecessaryQuestion] = []
    for constraint in record.constraint_codes:
        topic = f"constraint.{constraint.value}"
        identifier = f"decision.constraint.{constraint.value}"
        recommendation = f"recommendation.preserve-{constraint.value}"
        if constraint in _MANDATORY_CONFIRM:
            rationale = f"rationale.confirm-{constraint.value}"
            decisions.append(
                _decision(
                    identifier,
                    topic,
                    DecisionDisposition.CONFIRM,
                    recommendation,
                    rationale,
                    Confidence.HIGH,
                    refs,
                    trigger=f"trigger.{constraint.value}",
                )
            )
            questions.append(
                _question(
                    f"question.constraint.{constraint.value}",
                    topic,
                    recommendation,
                    rationale,
                    "impact.risk",
                    refs,
                )
            )
        else:
            decisions.append(
                _decision(
                    identifier,
                    topic,
                    DecisionDisposition.RECOMMEND,
                    recommendation,
                    "rationale.explicit-project-constraint",
                    Confidence.HIGH,
                    refs,
                )
            )
    return tuple(decisions), tuple(questions)


def _p2_routes(
    p2_intake: ProjectIntake | None,
    stack_decision: StackDecision | None,
) -> tuple[IntentDecision, ...]:
    if p2_intake is None and stack_decision is not None:
        raise IntentDecisionError("stack_decision requires its bound p2_intake")
    if p2_intake is not None and type(p2_intake) is not ProjectIntake:
        raise TypeError("p2_intake must be an exact ProjectIntake")
    if stack_decision is not None and type(stack_decision) is not StackDecision:
        raise TypeError("stack_decision must be an exact StackDecision")
    if p2_intake is None:
        return ()
    if stack_decision is not None and stack_decision.intake_id != p2_intake.intake_id:
        raise IntentDecisionError("stack_decision is not bound to p2_intake")
    if stack_decision is not None and (
        stack_decision.project_mode is not p2_intake.project_mode
        or stack_decision.purpose is not p2_intake.purpose
        or stack_decision.required_evidence_level
        is not p2_intake.need_evidence_level
    ):
        raise IntentDecisionError("stack_decision source fields do not match p2_intake")

    refs = set(p2_intake.project_mode_evidence_refs)
    refs.update(reference for item in p2_intake.decisions for reference in item.evidence_refs)
    if stack_decision is not None:
        refs.update(stack_decision.evidence_refs)
    canonical_refs = tuple(sorted(refs))
    decisions: list[IntentDecision] = []

    open_items = tuple(
        item
        for item in p2_intake.decisions
        if item.resolution_state is ResolutionState.OPEN
    )
    open_human = tuple(
        item
        for item in open_items
        if item.disposition is P2DecisionDisposition.HUMAN_BOUND
    )
    if open_human:
        decisions.append(
            _decision(
                "decision.p2.owner-gate",
                "p2.owner-gate",
                DecisionDisposition.CONFIRM,
                "recommendation.resolve-p2-owner-gate",
                "rationale.p2-human-bound-decision",
                Confidence.HIGH,
                canonical_refs,
                trigger="trigger.p2-owner-gate",
            )
        )
    elif open_items:
        decisions.append(
            _decision(
                "decision.p2.open-decisions",
                "p2.open-decisions",
                DecisionDisposition.RECOMMEND,
                "recommendation.resolve-p2-open-decisions",
                "rationale.p2-open-decision",
                Confidence.MEDIUM,
                canonical_refs,
            )
        )

    if stack_decision is not None:
        if stack_decision.disposition is RecommendationDisposition.OWNER_GATE:
            decisions.append(
                _decision(
                    "decision.p2.stack-owner-gate",
                    "p2.stack-decision",
                    DecisionDisposition.CONFIRM,
                    "recommendation.resolve-stack-owner-gate",
                    "rationale.stack-owner-gate",
                    Confidence.HIGH,
                    canonical_refs,
                    trigger="trigger.stack-owner-gate",
                )
            )
        elif stack_decision.disposition in (
            RecommendationDisposition.NEEDS_EVIDENCE,
            RecommendationDisposition.CORRECTION,
        ):
            decisions.append(
                _decision(
                    "decision.p2.stack-evidence",
                    "p2.stack-decision",
                    DecisionDisposition.RECOMMEND,
                    "recommendation.resolve-stack-evidence",
                    "rationale.stack-evidence-incomplete",
                    Confidence.LOW,
                    canonical_refs,
                )
            )
        else:
            decisions.append(
                _decision(
                    "decision.p2.stack-recommendation",
                    "p2.stack-decision",
                    DecisionDisposition.RECOMMEND,
                    "recommendation.review-stack-candidate",
                    "rationale.stack-recommendation-evidence",
                    Confidence.MEDIUM,
                    canonical_refs,
                )
            )
    return tuple(decisions)


def route_user_intent(
    record: UserIntent,
    *,
    p2_intake: ProjectIntake | None = None,
    stack_decision: StackDecision | None = None,
) -> IntentDecisionResult:
    """Recompute one side-effect-free route from validated intent evidence."""

    if type(record) is not UserIntent:
        raise TypeError("record must be an exact UserIntent")
    facts = _intent_facts(record)
    uncertainty_decisions, uncertainty_questions = _uncertainty_routes(record)
    constraint_decisions, constraint_questions = _constraint_routes(record)
    p2_decisions = _p2_routes(p2_intake, stack_decision)
    decisions = facts + uncertainty_decisions + constraint_decisions + p2_decisions
    automatic = tuple(
        sorted(
            (item for item in decisions if item.disposition is DecisionDisposition.AUTO),
            key=lambda item: item.decision_id,
        )
    )
    recommended = tuple(
        sorted(
            (
                item
                for item in decisions
                if item.disposition is DecisionDisposition.RECOMMEND
            ),
            key=lambda item: item.decision_id,
        )
    )
    confirm_candidates = tuple(
        sorted(
            (
                item
                for item in decisions
                if item.disposition is DecisionDisposition.CONFIRM
            ),
            key=lambda item: item.decision_id,
        )
    )
    confirm = _confirmation_bundle(confirm_candidates)
    questions = tuple(
        sorted(
            uncertainty_questions + constraint_questions,
            key=lambda item: item.question_id,
        )
    )
    if len(confirm_candidates) > 1:
        confirm_topics = {item.topic_code for item in confirm_candidates}
        questions = tuple(
            item for item in questions if item.topic_code not in confirm_topics
        ) + (
            _confirmation_bundle_question(
                tuple(
                    sorted(
                        {
                            reference
                            for item in confirm_candidates
                            for reference in item.evidence_refs
                        }
                    )
                )
            ),
        )
        questions = tuple(sorted(questions, key=lambda item: item.question_id))
    all_decisions = automatic + recommended + confirm
    plan = tuple(
        sorted(
            {
                "plan." + item.decision_id.removeprefix("decision.")
                for item in all_decisions
            }
        )
    )
    if len(plan) > _MAX_PLAN_ENTRIES:
        plan = tuple(
            sorted(
                {
                    "plan.review-automatic-decisions" if automatic else "",
                    "plan.review-recommended-decisions" if recommended else "",
                    "plan.await-confirmation" if confirm else "",
                }
                - {""}
            )
        )
    rationale = tuple(
        sorted(
            {item.rationale_code for item in decisions}
            | {item.rationale_code for item in all_decisions}
        )
    )
    evidence_refs = set(record.evidence_refs)
    evidence_refs.update(
        reference for item in all_decisions for reference in item.evidence_refs
    )
    evidence_refs.update(
        reference for item in questions for reference in item.evidence_refs
    )
    confidence = (
        Confidence.LOW
        if confirm
        else Confidence.MEDIUM
        if recommended
        else Confidence.HIGH
    )
    rendered_intent = render_user_intent(record)
    return IntentDecisionResult(
        schema_version=INTENT_DECISION_SCHEMA_VERSION,
        intent_id=record.intent_id,
        intent_sha256=hashlib.sha256(rendered_intent).hexdigest(),
        structured_intent=record,
        necessary_questions=questions,
        recommended_plan=plan,
        automatic_decisions=automatic,
        recommended_decisions=recommended,
        confirmation_required_decisions=confirm,
        rationale=rationale,
        confidence=confidence,
        evidence_refs=tuple(sorted(evidence_refs)),
        ready_for_blueprint=not confirm,
    )


def _intent_mapping(record: UserIntent) -> Mapping[str, Any]:
    return json.loads(render_user_intent(record))


def _decision_mapping(record: IntentDecision) -> dict[str, object]:
    return {
        "confidence": record.confidence.value,
        "decision_id": record.decision_id,
        "disposition": record.disposition.value,
        "evidence_refs": list(record.evidence_refs),
        "rationale_code": record.rationale_code,
        "recommendation_code": record.recommendation_code,
        "topic_code": record.topic_code,
        "trigger_codes": list(record.trigger_codes),
    }


def _question_mapping(record: NecessaryQuestion) -> dict[str, object]:
    return {
        "evidence_refs": list(record.evidence_refs),
        "impact_code": record.impact_code,
        "question_id": record.question_id,
        "rationale_code": record.rationale_code,
        "recommendation_code": record.recommendation_code,
        "topic_code": record.topic_code,
    }


def _mapping(result: IntentDecisionResult) -> dict[str, object]:
    return {
        "automatic_decisions": [
            _decision_mapping(item) for item in result.automatic_decisions
        ],
        "confidence": result.confidence.value,
        "confirmation_required_decisions": [
            _decision_mapping(item)
            for item in result.confirmation_required_decisions
        ],
        "evidence_refs": list(result.evidence_refs),
        "intent_id": result.intent_id,
        "intent_sha256": result.intent_sha256,
        "necessary_questions": [
            _question_mapping(item) for item in result.necessary_questions
        ],
        "rationale": list(result.rationale),
        "ready_for_blueprint": result.ready_for_blueprint,
        "recommended_decisions": [
            _decision_mapping(item) for item in result.recommended_decisions
        ],
        "recommended_plan": list(result.recommended_plan),
        "schema_version": result.schema_version,
        "structured_intent": _intent_mapping(result.structured_intent),
    }


def render_intent_decision_result(result: IntentDecisionResult) -> bytes:
    """Render one validated route to canonical UTF-8 JSON bytes."""

    if type(result) is not IntentDecisionResult:
        raise TypeError("result must be an exact IntentDecisionResult")
    _require_recomputable_route(result)
    try:
        rendered = canonical_json_bytes(_mapping(result))
    except SchemaError as error:
        raise IntentDecisionError(
            f"intent decision cannot be encoded: {error}"
        ) from error
    if len(rendered) > MAX_INTENT_DECISION_BYTES:
        raise IntentDecisionError("rendered intent decision exceeds its byte bound")
    return rendered


def _require_recomputable_route(result: IntentDecisionResult) -> None:
    routed_decisions = (
        result.automatic_decisions
        + result.recommended_decisions
        + result.confirmation_required_decisions
    )
    if any(item.decision_id.startswith("decision.p2.") for item in routed_decisions):
        raise IntentDecisionError(
            "P2-derived route cannot be serialized without its source context"
        )
    recomputed = route_user_intent(result.structured_intent)
    if result != recomputed:
        raise IntentDecisionError(
            "intent decision does not match the recomputed derived route"
        )


def _closed_mapping(
    value: object, fields: frozenset[str], label: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IntentDecisionError(f"{label} must be an object")
    keys = set(value)
    if any(type(key) is not str for key in keys):
        raise IntentDecisionError(f"{label} field names must be strings")
    unknown = sorted(keys - fields)
    missing = sorted(fields - keys)
    if unknown:
        raise IntentDecisionError(f"{label} contains unknown fields: {', '.join(unknown)}")
    if missing:
        raise IntentDecisionError(f"{label} is missing fields: {', '.join(missing)}")
    return value


def _parse_codes(value: object, label: str, maximum: int) -> tuple[str, ...]:
    return tuple(
        _code(item, f"{label}[{index}]")
        for index, item in enumerate(_sequence(value, label, maximum))
    )


def _parse_refs(
    value: object, label: str, maximum: int = _MAX_REFERENCES_PER_ITEM
) -> tuple[str, ...]:
    return tuple(
        _reference(item, f"{label}[{index}]")
        for index, item in enumerate(_sequence(value, label, maximum))
    )


def _parse_decision(value: object, label: str) -> IntentDecision:
    item = _closed_mapping(value, _DECISION_FIELDS, label)
    return IntentDecision(
        decision_id=_code(item["decision_id"], f"{label}.decision_id"),
        topic_code=_code(item["topic_code"], f"{label}.topic_code"),
        disposition=_enum_value(
            DecisionDisposition, item["disposition"], f"{label}.disposition"
        ),
        recommendation_code=_code(
            item["recommendation_code"], f"{label}.recommendation_code"
        ),
        rationale_code=_code(
            item["rationale_code"], f"{label}.rationale_code"
        ),
        confidence=_enum_value(Confidence, item["confidence"], f"{label}.confidence"),
        trigger_codes=_parse_codes(
            item["trigger_codes"], f"{label}.trigger_codes", _MAX_REFERENCES_PER_ITEM
        ),
        evidence_refs=_parse_refs(item["evidence_refs"], f"{label}.evidence_refs"),
    )


def _parse_question(value: object, label: str) -> NecessaryQuestion:
    item = _closed_mapping(value, _QUESTION_FIELDS, label)
    return NecessaryQuestion(
        question_id=_code(item["question_id"], f"{label}.question_id"),
        topic_code=_code(item["topic_code"], f"{label}.topic_code"),
        recommendation_code=_code(
            item["recommendation_code"], f"{label}.recommendation_code"
        ),
        rationale_code=_code(item["rationale_code"], f"{label}.rationale_code"),
        impact_code=_code(item["impact_code"], f"{label}.impact_code"),
        evidence_refs=_parse_refs(item["evidence_refs"], f"{label}.evidence_refs"),
    )


def _parse_mapping(value: object) -> IntentDecisionResult:
    item = _closed_mapping(value, _TOP_LEVEL_FIELDS, "intent_decision")
    if item["schema_version"] != INTENT_DECISION_SCHEMA_VERSION:
        raise IntentDecisionError("unsupported intent-decision schema_version")
    structured = parse_user_intent(canonical_json_bytes(item["structured_intent"]))
    return IntentDecisionResult(
        schema_version=INTENT_DECISION_SCHEMA_VERSION,
        intent_id=_code(item["intent_id"], "intent_id"),
        intent_sha256=item["intent_sha256"],
        structured_intent=structured,
        necessary_questions=tuple(
            _parse_question(value, f"necessary_questions[{index}]")
            for index, value in enumerate(
                _sequence(item["necessary_questions"], "necessary_questions", _MAX_QUESTIONS)
            )
        ),
        recommended_plan=_parse_codes(
            item["recommended_plan"], "recommended_plan", _MAX_PLAN_ENTRIES
        ),
        automatic_decisions=tuple(
            _parse_decision(value, f"automatic_decisions[{index}]")
            for index, value in enumerate(
                _sequence(item["automatic_decisions"], "automatic_decisions", _MAX_DECISIONS)
            )
        ),
        recommended_decisions=tuple(
            _parse_decision(value, f"recommended_decisions[{index}]")
            for index, value in enumerate(
                _sequence(item["recommended_decisions"], "recommended_decisions", _MAX_DECISIONS)
            )
        ),
        confirmation_required_decisions=tuple(
            _parse_decision(value, f"confirmation_required_decisions[{index}]")
            for index, value in enumerate(
                _sequence(
                    item["confirmation_required_decisions"],
                    "confirmation_required_decisions",
                    _MAX_DECISIONS,
                )
            )
        ),
        rationale=_parse_codes(item["rationale"], "rationale", _MAX_RATIONALES),
        confidence=_enum_value(Confidence, item["confidence"], "confidence"),
        evidence_refs=_parse_refs(item["evidence_refs"], "evidence_refs", _MAX_REFERENCES),
        ready_for_blueprint=item["ready_for_blueprint"],
    )


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IntentDecisionError("intent decision contains duplicate object fields")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise IntentDecisionError(
        f"intent decision contains unsupported JSON constant: {value}"
    )


def parse_intent_decision_result(
    payload: bytes | bytearray | memoryview,
) -> IntentDecisionResult:
    """Parse only bounded canonical UTF-8 JSON bytes into an immutable route."""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise IntentDecisionError("intent-decision payload must be bytes")
    raw = bytes(payload)
    if not raw or len(raw) > MAX_INTENT_DECISION_BYTES:
        raise IntentDecisionError(
            "intent-decision payload must use bounded non-empty bytes"
        )
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except IntentDecisionError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        RecursionError,
    ) as error:
        raise IntentDecisionError(
            "intent decision is not valid UTF-8 JSON"
        ) from error
    result = _parse_mapping(value)
    if render_intent_decision_result(result) != raw:
        raise IntentDecisionError("intent-decision JSON is not canonical")
    return result


__all__ = [
    "Confidence",
    "DecisionDisposition",
    "INTENT_DECISION_SCHEMA_VERSION",
    "IntentDecision",
    "IntentDecisionError",
    "IntentDecisionResult",
    "MAX_INTENT_DECISION_BYTES",
    "NecessaryQuestion",
    "parse_intent_decision_result",
    "render_intent_decision_result",
    "route_user_intent",
]
