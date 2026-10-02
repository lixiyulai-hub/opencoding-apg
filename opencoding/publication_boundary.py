"""Offline publication previews with an explicit human-gate boundary.

The functions in this module only validate a candidate's minimum evidence and
return a metadata-only preview.  There is deliberately no publish, deploy,
remote Git, provider or credential operation here.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .evidence_boundary import (
    EVIDENCE_BOUNDARY_SCHEMA,
    EVIDENCE_CLASSES,
    PLATFORM_COMPATIBILITY_STATUSES,
    verify_read_only_audit_snapshot,
)
from .safety import canonical_json, inspect_sensitive, sha256_bytes


PUBLICATION_BOUNDARY_SCHEMA = "opencoding-publication-boundary-v1"
FACT_STATUSES = ("observed", "unverified", "blocked")
MINIMUM_EVIDENCE_IDS = (
    "source_revision",
    "test_baseline",
    "independent_review",
    "privacy_audit",
    "rollback_plan",
)
PUBLICATION_FACT_IDS = (
    "platform_compatibility",
    "real_user_acceptance",
    "license_and_contribution_review",
    "public_scope_review",
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,96}$")


class PublicationBoundaryError(ValueError):
    """A publication preview cannot be safely constructed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _safe_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value) or inspect_sensitive(value)["sensitive"]:
        raise PublicationBoundaryError(f"{field}_invalid", f"{field} must be a bounded non-sensitive id")
    return value


def _status(value: Any, *, field: str) -> str:
    if value not in FACT_STATUSES:
        raise PublicationBoundaryError(f"{field}_status_invalid", f"{field} status is unknown")
    return value


def _count(value: Any, *, field: str, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise PublicationBoundaryError(f"{field}_invalid", f"{field} must be a bounded non-negative count")
    return value


def _test_summary(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise PublicationBoundaryError("test_baseline_invalid", "test baseline must be an object")
    output = {key: _count(value.get(key), field=key) for key in ("total", "passed", "skipped", "failures", "errors")}
    output["returncode"] = _count(value.get("returncode"), field="returncode", maximum=255)
    if output["total"] <= 0 or output["total"] != output["passed"] + output["skipped"] + output["failures"] + output["errors"]:
        raise PublicationBoundaryError("test_baseline_invalid", "test counts do not reconcile")
    if output["returncode"] != 0 or output["failures"] or output["errors"]:
        raise PublicationBoundaryError("test_baseline_failed", "publication evidence requires a successful test baseline")
    return output


def _independent_review(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PublicationBoundaryError("independent_review_invalid", "independent review must be an object")
    status = value.get("status", "observed")
    if status != "observed":
        raise PublicationBoundaryError("independent_review_unverified", "independent review is not observed")
    focused_total = _count(value.get("focused_total"), field="focused_total")
    focused_passed = _count(value.get("focused_passed"), field="focused_passed")
    returncode = _count(value.get("returncode"), field="returncode", maximum=255)
    if focused_total <= 0 or focused_passed != focused_total or returncode != 0:
        raise PublicationBoundaryError("independent_review_invalid", "independent review counts do not show a clean pass")
    return {"status": "observed", "focused_total": focused_total, "focused_passed": focused_passed, "returncode": 0}


def _privacy_audit(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema") != EVIDENCE_BOUNDARY_SCHEMA:
        raise PublicationBoundaryError("privacy_audit_invalid", "privacy audit schema is missing")
    _safe_id(value.get("source"), field="privacy_audit_source")
    try:
        verify_read_only_audit_snapshot(value)
    except (TypeError, ValueError, KeyError) as error:
        raise PublicationBoundaryError("privacy_audit_drifted", "privacy audit snapshot digest does not match") from error
    digest = value.get("payload_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PublicationBoundaryError("privacy_audit_digest_invalid", "privacy audit digest is invalid")
    if value.get("read_only") is not True:
        raise PublicationBoundaryError("privacy_audit_not_read_only", "privacy audit must be read-only")
    privacy = value.get("privacy")
    required = ("raw_values_included", "prompt_included", "cookie_included", "token_included", "api_key_included", "user_content_included")
    if not isinstance(privacy, Mapping) or any(privacy.get(key) is not False for key in required):
        raise PublicationBoundaryError("privacy_audit_leaky", "privacy audit contains a raw-value or user-content claim")
    platforms = value.get("platform_compatibility")
    if not isinstance(platforms, list) or any(
        not isinstance(item, Mapping) or item.get("status") not in PLATFORM_COMPATIBILITY_STATUSES for item in platforms
    ):
        raise PublicationBoundaryError("privacy_audit_platforms_invalid", "privacy audit platform statuses are invalid")
    evidence_class = value.get("evidence", {}).get("class", "unverified") if isinstance(value.get("evidence"), Mapping) else "unverified"
    if evidence_class not in EVIDENCE_CLASSES:
        raise PublicationBoundaryError("privacy_audit_evidence_invalid", "privacy audit evidence class is unknown")
    return {
        "status": "observed",
        "read_only": True,
        "platform_count": len(platforms),
        "platform_statuses": [item["status"] for item in platforms],
        "evidence_class": evidence_class,
        "raw_values_included": False,
        "user_content_included": False,
    }


def _rollback(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PublicationBoundaryError("rollback_invalid", "rollback plan must be an object")
    if value.get("automatic") is not False:
        raise PublicationBoundaryError("rollback_automatic_invalid", "publication rollback cannot be automatic")
    scope = _safe_id(value.get("scope"), field="rollback_scope")
    status = value.get("status")
    if status not in {"observed", "simulated_receipt_scope", "available"}:
        raise PublicationBoundaryError("rollback_status_invalid", "rollback status is not reviewable")
    return {"status": "observed" if status == "observed" else "simulated", "scope": scope, "automatic": False}


def _fact_markers(facts: Mapping[str, Any]) -> list[dict[str, str]]:
    if not isinstance(facts, Mapping):
        raise PublicationBoundaryError("facts_invalid", "publication facts must be an object")
    output: list[dict[str, str]] = []
    for identifier in PUBLICATION_FACT_IDS:
        value = facts.get(identifier, {})
        if not isinstance(value, Mapping):
            raise PublicationBoundaryError("fact_invalid", f"fact {identifier} must be an object")
        status = _status(value.get("status", "unverified"), field=identifier)
        source = _safe_id(value.get("source", "not-provided"), field=f"{identifier}_source")
        output.append({"id": identifier, "status": status, "source": source})
    return output


def build_publication_preview(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return a blocked, metadata-only publication preview.

    A successful return means that the minimum evidence is internally
    consistent.  It never means that publication is approved or executed.
    """

    if not isinstance(candidate, Mapping):
        raise PublicationBoundaryError("candidate_invalid", "publication candidate must be an object")
    revision = _safe_id(candidate.get("source_revision"), field="source_revision")
    tests = _test_summary(candidate.get("test_summary"))
    independent = _independent_review(candidate.get("independent_review"))
    privacy = _privacy_audit(candidate.get("audit_snapshot"))
    rollback = _rollback(candidate.get("rollback"))
    facts = _fact_markers(candidate.get("facts", {}))
    platform_fact = next(item for item in facts if item["id"] == "platform_compatibility")
    if platform_fact["status"] == "observed" and any(status != "observed" for status in privacy["platform_statuses"]):
        raise PublicationBoundaryError("fact_drift", "platform fact cannot exceed the audit snapshot")
    user_fact = next(item for item in facts if item["id"] == "real_user_acceptance")
    if user_fact["status"] == "observed" and privacy["evidence_class"] != "real":
        raise PublicationBoundaryError("real_user_evidence_missing", "real-user fact lacks real evidence")
    missing = [identifier for identifier, value in {
        "source_revision": bool(revision),
        "test_baseline": bool(tests),
        "independent_review": bool(independent),
        "privacy_audit": bool(privacy),
        "rollback_plan": bool(rollback),
    }.items() if not value]
    minimum_status = "observed" if not missing else "blocked"
    blocked_codes = ["publication_human_gate_required"]
    if missing:
        blocked_codes.append("minimum_evidence_missing")
    if any(item["status"] == "blocked" for item in facts):
        blocked_codes.append("publication_fact_blocked")
    public_projection = {
        "schema": PUBLICATION_BOUNDARY_SCHEMA,
        "source_revision": revision,
        "minimum_evidence": {
            "status": minimum_status,
            "required": list(MINIMUM_EVIDENCE_IDS),
            "missing": missing,
            "test_baseline": tests,
            "independent_review": independent,
            "privacy_audit": privacy,
            "rollback_plan": rollback,
        },
        "facts": facts,
        "status": "blocked_human_gate" if not missing else "blocked_incomplete_evidence",
        "blocked_codes": blocked_codes,
        "gate": {
            "required": True,
            "recorded": False,
            "external_action_allowed": False,
            "reason_code": "publication_requires_explicit_human_gate",
        },
        "actions": {"publish_executed": False, "release_executed": False, "deployment_executed": False},
        "privacy": {
            "raw_values_included": False,
            "prompt_included": False,
            "cookie_included": False,
            "token_included": False,
            "api_key_included": False,
            "user_content_included": False,
        },
    }
    public_projection["preview_digest"] = sha256_bytes(canonical_json(public_projection))
    return public_projection


def verify_publication_preview(preview: Mapping[str, Any]) -> bool:
    """Verify a preview's digest and permanent blocked-action boundary."""

    if not isinstance(preview, Mapping) or not isinstance(preview.get("preview_digest"), str):
        raise PublicationBoundaryError("preview_digest_missing", "publication preview digest is missing")
    supplied = preview["preview_digest"]
    if not re.fullmatch(r"[0-9a-f]{64}", supplied):
        raise PublicationBoundaryError("preview_digest_invalid", "publication preview digest is invalid")
    unsigned = dict(preview)
    unsigned.pop("preview_digest", None)
    if supplied != sha256_bytes(canonical_json(unsigned)):
        raise PublicationBoundaryError("preview_drifted", "publication preview digest does not match its fields")
    if preview.get("status") not in {"blocked_human_gate", "blocked_incomplete_evidence"}:
        raise PublicationBoundaryError("preview_status_invalid", "publication preview must remain blocked")
    gate = preview.get("gate")
    actions = preview.get("actions")
    if not isinstance(gate, Mapping) or gate.get("recorded") is not False or gate.get("external_action_allowed") is not False:
        raise PublicationBoundaryError("publication_gate_invalid", "publication gate is not closed")
    if actions != {"publish_executed": False, "release_executed": False, "deployment_executed": False}:
        raise PublicationBoundaryError("publication_actions_invalid", "publication actions are not all false")
    return True


__all__ = [
    "FACT_STATUSES",
    "MINIMUM_EVIDENCE_IDS",
    "PUBLICATION_BOUNDARY_SCHEMA",
    "PUBLICATION_FACT_IDS",
    "PublicationBoundaryError",
    "build_publication_preview",
    "verify_publication_preview",
]
