"""Deterministic, side-effect-free routing for validated project intake."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .intake import (
    DecisionDisposition,
    ProjectIntake,
    ResolutionState,
    StopState,
)


class IntakeRoutingError(ValueError):
    """Raised when a validated intake cannot be routed consistently."""


class RoutingDisposition(str, Enum):
    NEXT_QUESTION = "next-question"
    READY_FOR_PREVIEW = "ready-for-preview"
    OWNER_GATE = "owner-gate"


@dataclass(frozen=True)
class IntakeRoute:
    """One deterministic next-question or terminal routing decision."""

    disposition: RoutingDisposition
    decision_id: str | None = None
    topic_code: str | None = None
    recommendation_code: str | None = None
    evidence_refs: tuple[str, ...] = ()
    open_decision_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, RoutingDisposition):
            raise IntakeRoutingError("disposition must be a RoutingDisposition")
        if type(self.open_decision_count) is not int or self.open_decision_count < 0:
            raise IntakeRoutingError("open_decision_count must be a non-negative integer")
        if type(self.evidence_refs) is not tuple or any(
            type(item) is not str or not item for item in self.evidence_refs
        ):
            raise IntakeRoutingError("evidence_refs must be a tuple of non-empty strings")
        if self.evidence_refs != tuple(sorted(set(self.evidence_refs))):
            raise IntakeRoutingError("evidence_refs must use canonical order")

        fields = (self.decision_id, self.topic_code, self.recommendation_code)
        if self.disposition is RoutingDisposition.NEXT_QUESTION:
            if any(type(item) is not str or not item for item in fields):
                raise IntakeRoutingError(
                    "next-question routes require decision, topic, and recommendation codes"
                )
            if self.open_decision_count < 1:
                raise IntakeRoutingError("next-question routes require an open decision")
        elif self.disposition is RoutingDisposition.OWNER_GATE:
            if type(self.decision_id) is not str or not self.decision_id:
                raise IntakeRoutingError("owner-gate routes require a decision code")
            if self.topic_code is not None or self.recommendation_code is not None:
                raise IntakeRoutingError(
                    "owner-gate routes cannot carry next-question codes"
                )
            if self.open_decision_count < 1:
                raise IntakeRoutingError("owner-gate routes require an open decision")
        elif any(item is not None for item in fields) or self.open_decision_count != 0:
            raise IntakeRoutingError("ready-for-preview routes must be terminal")


def _evidence_refs(record: ProjectIntake, decision=None) -> tuple[str, ...]:
    refs = set(record.project_mode_evidence_refs)
    if decision is not None:
        refs.update(decision.evidence_refs)
    return tuple(sorted(refs))


def route_intake(record: ProjectIntake) -> IntakeRoute:
    """Return one deterministic route without performing any external work."""

    if not isinstance(record, ProjectIntake):
        raise TypeError("record must be a ProjectIntake")

    open_decisions = tuple(
        item
        for item in record.decisions
        if item.resolution_state is ResolutionState.OPEN
    )

    if record.stop_state is StopState.READY_FOR_PREVIEW:
        if open_decisions:
            raise IntakeRoutingError(
                "ready-for-preview cannot route with open decisions"
            )
        return IntakeRoute(
            disposition=RoutingDisposition.READY_FOR_PREVIEW,
            evidence_refs=_evidence_refs(record),
        )

    if record.stop_state is StopState.OWNER_GATE:
        owner_items = tuple(
            item
            for item in open_decisions
            if item.disposition is DecisionDisposition.HUMAN_BOUND
        )
        if not owner_items:
            raise IntakeRoutingError("owner-gate requires an open human-bound decision")
        decision = owner_items[0]
        return IntakeRoute(
            disposition=RoutingDisposition.OWNER_GATE,
            decision_id=decision.decision_id,
            evidence_refs=_evidence_refs(record, decision),
            open_decision_count=len(open_decisions),
        )

    if record.stop_state is StopState.CONTINUE:
        candidates = tuple(
            item
            for item in open_decisions
            if item.disposition in (DecisionDisposition.DEFAULT, DecisionDisposition.VERIFY)
        )
        if len(candidates) != len(open_decisions) or not candidates:
            raise IntakeRoutingError(
                "continue requires at least one open D/V decision"
            )
        decision = candidates[0]
        return IntakeRoute(
            disposition=RoutingDisposition.NEXT_QUESTION,
            decision_id=decision.decision_id,
            topic_code=decision.topic_code,
            recommendation_code=decision.recommendation_code,
            evidence_refs=_evidence_refs(record, decision),
            open_decision_count=len(open_decisions),
        )

    raise IntakeRoutingError("unsupported intake stop state")


__all__ = [
    "IntakeRoute",
    "IntakeRoutingError",
    "RoutingDisposition",
    "route_intake",
]
