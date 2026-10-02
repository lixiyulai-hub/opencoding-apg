"""Read-only evidence markers and privacy-safe audit projections.

This module is intentionally a projection boundary.  It accepts an in-memory
report, classifies its evidence source, and returns only status metadata for an
external reviewer.  Prompts, user answers, cookies, tokens, API keys and other
free-form content are never copied into the projection.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .safety import canonical_json, inspect_sensitive, sanitize_text, sha256_bytes


EVIDENCE_BOUNDARY_SCHEMA = "opencoding-evidence-boundary-v1"
EVIDENCE_CLASSES = ("real", "synthetic", "unverified")
PLATFORM_COMPATIBILITY_STATUSES = ("observed", "unverified", "blocked")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/-]{1,96}$")
_SAFE_SUMMARY_KEYS = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


class EvidenceBoundaryError(ValueError):
    """A report cannot be safely classified or projected."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _identifier(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise EvidenceBoundaryError(f"{field}_invalid", f"{field} must be a bounded identifier")
    if inspect_sensitive(value)["sensitive"]:
        raise EvidenceBoundaryError(f"{field}_sensitive", f"{field} contains sensitive material")
    return value


def _target_label(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 96 or any(char in value for char in "\r\n\x00"):
        raise EvidenceBoundaryError("target_invalid", "target must be a bounded label")
    if inspect_sensitive(value)["sensitive"]:
        raise EvidenceBoundaryError("target_sensitive", "target contains sensitive material")
    return value.strip()


def classify_evidence(value: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    """Return exactly one evidence class without copying user content.

    ``real`` requires an explicit current verification marker and a real-user
    marker.  A claim that lacks both remains ``unverified``.  Synthetic input
    must be explicitly labelled and cannot carry live or external markers.
    """

    if not isinstance(value, Mapping):
        raise EvidenceBoundaryError("evidence_invalid", "evidence must be an object")
    source = _identifier(source, field="source")
    explicit = value.get("evidence_class")
    if explicit is None and isinstance(value.get("evidence"), Mapping):
        explicit = value["evidence"].get("class")
    if explicit is not None and explicit not in EVIDENCE_CLASSES:
        raise EvidenceBoundaryError("evidence_class_invalid", "evidence class is unknown")
    synthetic = value.get("synthetic") is True or value.get("fixture") is True or value.get("fixture_label") == "synthetic"
    real_user = value.get("real_user") is True
    live_verified = value.get("live_verified") is True or value.get("execution_observed") is True
    provider_used = value.get("provider_used") is True
    external_actions = value.get("external_actions") is True or value.get("external_executed") is True
    if synthetic and (real_user or live_verified or provider_used or external_actions):
        raise EvidenceBoundaryError("evidence_conflict", "synthetic evidence cannot claim live or external effects")
    if explicit == "real" and not (real_user and live_verified):
        raise EvidenceBoundaryError("real_evidence_unverified", "real evidence requires real_user and live verification")
    if explicit == "synthetic" and not synthetic:
        raise EvidenceBoundaryError("synthetic_marker_missing", "synthetic evidence requires an explicit fixture marker")
    if explicit == "unverified" and live_verified:
        raise EvidenceBoundaryError("unverified_conflict", "unverified evidence cannot claim observed execution")
    if explicit is None:
        evidence_class = "synthetic" if synthetic else "real" if real_user and live_verified else "unverified"
    else:
        evidence_class = explicit
    return {
        "class": evidence_class,
        "source": source,
        "real_user": real_user,
        "synthetic": synthetic,
        "live_verified": live_verified,
        "provider_used": provider_used,
        "external_actions": external_actions,
    }


def platform_compatibility_declaration(
    target: Any,
    *,
    status: str = "unverified",
    execution_observed: bool = False,
    toolchain_status: str = "unverified",
    host_family: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Describe a target boundary without turning a label into compatibility."""

    target = _target_label(target)
    if status not in PLATFORM_COMPATIBILITY_STATUSES:
        raise EvidenceBoundaryError("platform_status_invalid", "platform compatibility status is unknown")
    if type(execution_observed) is not bool:
        raise EvidenceBoundaryError("platform_execution_invalid", "execution_observed must be boolean")
    if status == "observed" and not execution_observed:
        raise EvidenceBoundaryError("platform_observation_missing", "observed compatibility requires execution evidence")
    if execution_observed and status != "observed":
        raise EvidenceBoundaryError("platform_status_conflict", "observed execution requires observed status")
    if (not isinstance(toolchain_status, str) or not toolchain_status or len(toolchain_status) > 32
            or inspect_sensitive(toolchain_status)["sensitive"]):
        raise EvidenceBoundaryError("toolchain_status_invalid", "toolchain status is invalid")
    if host_family is not None:
        host_family = _identifier(host_family, field="host_family")
    if not isinstance(reason, str):
        raise EvidenceBoundaryError("platform_reason_invalid", "platform reason must be text")
    safe_reason = sanitize_text(reason)[:240]
    return {
        "target": target,
        "status": status,
        "claim": "observed_execution" if execution_observed else "contract_only",
        "execution_observed": execution_observed,
        "toolchain_status": toolchain_status,
        "host_family": host_family,
        "reason": safe_reason,
        "evidence_class": "real" if execution_observed else "unverified",
    }


def _summary(summary: Mapping[str, Any] | None) -> dict[str, Any]:
    if summary is None:
        return {}
    if not isinstance(summary, Mapping):
        raise EvidenceBoundaryError("summary_invalid", "audit summary must be an object")
    output: dict[str, Any] = {}
    for key, value in summary.items():
        if not isinstance(key, str) or not _SAFE_SUMMARY_KEYS.fullmatch(key):
            raise EvidenceBoundaryError("summary_key_invalid", "audit summary key is invalid")
        if isinstance(value, bool) or isinstance(value, int):
            if isinstance(value, int) and (value < 0 or value > 1_000_000):
                raise EvidenceBoundaryError("summary_value_invalid", "audit summary count is out of bounds")
            output[key] = value
        else:
            raise EvidenceBoundaryError("summary_value_invalid", "audit summary values must be boolean or bounded counts")
    return output


def build_read_only_audit_snapshot(
    payload: Mapping[str, Any],
    *,
    source: str,
    platform_declarations: Iterable[Mapping[str, Any]] = (),
    summary: Mapping[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Build a metadata-only audit view; never echo the source payload."""

    if not isinstance(payload, Mapping):
        raise EvidenceBoundaryError("payload_invalid", "audit payload must be an object")
    marker = classify_evidence(payload, source=source)
    declarations: list[dict[str, Any]] = []
    for declaration in platform_declarations:
        if not isinstance(declaration, Mapping):
            raise EvidenceBoundaryError("platform_declaration_invalid", "platform declaration must be an object")
        safe_declaration = platform_compatibility_declaration(
            declaration.get("target"),
            status=declaration.get("status", "unverified"),
            execution_observed=declaration.get("execution_observed", False),
            toolchain_status=declaration.get("toolchain_status", "unverified"),
            host_family=declaration.get("host_family"),
            reason="",
        )
        safe_declaration["reason_code"] = (
            "observed_execution" if safe_declaration["status"] == "observed"
            else "human_gate_required" if safe_declaration["status"] == "blocked"
            else "contract_only"
        )
        declarations.append(safe_declaration)
    try:
        payload_digest = sha256_bytes(canonical_json(payload))
    except (TypeError, ValueError, RecursionError) as error:
        raise EvidenceBoundaryError("payload_not_json", "audit payload must be bounded JSON") from error
    raw_status = status if status is not None else payload.get("status")
    if not isinstance(raw_status, str) or not raw_status:
        safe_status = "unreported"
    else:
        safe_status = sanitize_text(raw_status)[:80]
    snapshot = {
        "schema": EVIDENCE_BOUNDARY_SCHEMA,
        "mode": "external_audit_read_only",
        "read_only": True,
        "source": marker["source"],
        "evidence": marker,
        "status": safe_status,
        "payload_sha256": payload_digest,
        "platform_compatibility": declarations,
        "summary": _summary(summary),
        "privacy": {
            "raw_values_included": False,
            "prompt_included": False,
            "cookie_included": False,
            "token_included": False,
            "api_key_included": False,
            "user_content_included": False,
            "policy": "metadata-only; raw prompts, credentials and user content are omitted",
        },
    }
    snapshot["snapshot_digest"] = sha256_bytes(canonical_json(snapshot))
    return snapshot


def verify_read_only_audit_snapshot(snapshot: Mapping[str, Any]) -> bool:
    """Verify the projection digest without reading or writing any source data."""

    if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("snapshot_digest"), str):
        raise EvidenceBoundaryError("snapshot_digest_missing", "audit snapshot digest is missing")
    supplied = snapshot["snapshot_digest"]
    if not re.fullmatch(r"[0-9a-f]{64}", supplied):
        raise EvidenceBoundaryError("snapshot_digest_invalid", "audit snapshot digest is invalid")
    unsigned = dict(snapshot)
    unsigned.pop("snapshot_digest", None)
    expected = sha256_bytes(canonical_json(unsigned))
    if supplied != expected:
        raise EvidenceBoundaryError("snapshot_drifted", "audit snapshot digest does not match its fields")
    return True


__all__ = [
    "EVIDENCE_BOUNDARY_SCHEMA",
    "EVIDENCE_CLASSES",
    "PLATFORM_COMPATIBILITY_STATUSES",
    "EvidenceBoundaryError",
    "build_read_only_audit_snapshot",
    "classify_evidence",
    "platform_compatibility_declaration",
    "verify_read_only_audit_snapshot",
]
