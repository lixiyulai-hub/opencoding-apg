"""Deterministic legacy finding baselines and quality-ratchet comparison."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Iterable, Mapping

from .model import Finding

SCHEMA_VERSION = "1.0"
_SEVERITY_RANK = {"info": 0, "notice": 0, "warning": 1, "error": 2, "critical": 3, "blocker": 4}
_DEFAULT_NON_BASELINABLE_RULES = frozenset({"secret.private-key", "unauthorized-write", "data-integrity", "data.integrity-corruption", "integrity-corruption"})
_NON_BASELINABLE_TOKENS = frozenset({"secret", "private-key", "unauthorized-write", "data-integrity", "integrity-corruption", "critical-control"})
_METRIC_DIRECTIONS = frozenset({"lower-is-better", "higher-is-better"})
_DURATION_RE = re.compile(r"\b(?:in|after|within|took)\s+\d+(?:\.\d+)?\s*(?:ms|milliseconds|s|sec|seconds|m|min|minutes)\b", re.I)
_TIMESTAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+Z-]+)?\b")
_LINE_RE = re.compile(r"\b(?:at\s+)?line\s+\d+\b", re.I)
_RANDOM_ID_RE = re.compile(r"\b(?:run|request|trace|job|session|execution)[-_ ]?[A-Za-z0-9]{4,}\b", re.I)


class BaselineViolation(ValueError):
    """Raised when baseline data or comparison violates governance rules."""


@dataclass(frozen=True)
class BaselineEntry:
    fingerprint: str
    rule_id: str
    severity: str
    accepted_at: str
    actor: str
    owner: str
    review_by: str
    source_audit_receipt: str
    rule_version: str
    metric_value: float | int | None = None


@dataclass(frozen=True)
class Comparison:
    existing: tuple[Finding, ...] = ()
    new: tuple[Finding, ...] = ()
    worsened: tuple[Finding | str, ...] = ()
    resolved: tuple[str, ...] = ()


def _canonical_path(path: str, project_root: str | None = None) -> str:
    if not isinstance(path, str) or not path.strip():
        raise BaselineViolation("finding path must be a non-empty string")
    value = path.replace("\\", "/")
    absolute = bool(re.match(r"^[A-Za-z]:/", value) or value.startswith("/"))
    def normalize(parts: list[str]) -> list[str]:
        normalized: list[str] = []
        for part in parts:
            if not part or part == ".":
                continue
            if part == "..":
                if normalized:
                    normalized.pop()
                continue
            normalized.append(part)
        return normalized
    if absolute:
        if not isinstance(project_root, str) or not project_root.strip():
            raise BaselineViolation("absolute finding paths require an explicit project root")
        root = project_root.replace("\\", "/")
        if not (re.match(r"^[A-Za-z]:/", root) or root.startswith("/")):
            raise BaselineViolation("project root must be absolute for absolute finding paths")
        root_parts = normalize(root.split("/"))
        path_parts = normalize(value.split("/"))
        casefold = bool(re.match(r"^[A-Za-z]:$", root_parts[0])) if root_parts else False
        equal = (lambda left, right: left.lower() == right.lower()) if casefold else (lambda left, right: left == right)
        if len(path_parts) < len(root_parts) or any(not equal(left, right) for left, right in zip(path_parts, root_parts)):
            raise BaselineViolation("finding path is outside project root")
        value = "/".join(path_parts[len(root_parts):])
    return "/".join(normalize(value.split("/")))


def _stable_message(finding: Finding) -> str:
    message = finding.message.strip().lower()
    if finding.rule_id == "tests.failed":
        message = _DURATION_RE.sub("", message)
        message = _TIMESTAMP_RE.sub("<timestamp>", message)
        message = _LINE_RE.sub("line", message)
        message = _RANDOM_ID_RE.sub("<id>", message)
    return " ".join(message.split())


def _is_non_baselinable(rule_id: str, category: str = "", configured_rules: Iterable[str] = ()) -> bool:
    rule = rule_id.strip().lower()
    configured = {item.strip().lower() for item in configured_rules}
    if rule in _DEFAULT_NON_BASELINABLE_RULES or rule in configured:
        return True
    tokens = {token for token in re.split(r"[^a-z0-9-]+", f"{rule} {category.lower()}") if token}
    return bool(tokens & _NON_BASELINABLE_TOKENS)


def _ensure_finding_baselinable(finding: Finding, configured_rules: Iterable[str] = ()) -> None:
    if not finding.baselinable or _is_non_baselinable(finding.rule_id, finding.category, configured_rules):
        raise BaselineViolation(f"finding is not baselinable: {finding.rule_id}")


def fingerprint(finding: Finding, *, project_root: str | None = None, non_baselinable_rules: Iterable[str] = ()) -> str:
    if not isinstance(finding, Finding):
        raise TypeError("finding must be a Finding")
    _ensure_finding_baselinable(finding, non_baselinable_rules)
    payload = {"category": finding.category, "message_class": _stable_message(finding), "path": _canonical_path(finding.path, project_root), "rule_id": finding.rule_id}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise BaselineViolation(f"invalid timestamp: {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise BaselineViolation(f"invalid timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        raise BaselineViolation("timestamps must include timezone")
    return parsed.astimezone(timezone.utc)


def _validate_metric(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise BaselineViolation(f"{label} must be a finite non-bool number")
    return float(value)


def _validate_entry(entry: BaselineEntry, *, current: datetime | None = None, seen: set[str] | None = None) -> None:
    if not isinstance(entry, BaselineEntry):
        raise BaselineViolation("baseline entries must be BaselineEntry values")
    if seen is not None:
        if entry.fingerprint in seen:
            raise BaselineViolation(f"duplicate baseline fingerprint: {entry.fingerprint}")
        seen.add(entry.fingerprint)
    fields = (entry.fingerprint, entry.rule_id, entry.severity, entry.accepted_at, entry.actor, entry.owner, entry.review_by, entry.source_audit_receipt, entry.rule_version)
    if any(type(value) is not str or not value.strip() for value in fields):
        raise BaselineViolation("baseline entry is missing required metadata")
    severity = entry.severity.lower()
    if severity not in _SEVERITY_RANK:
        raise BaselineViolation(f"unknown severity: {entry.severity}")
    if _is_non_baselinable(entry.rule_id) or _SEVERITY_RANK[severity] >= _SEVERITY_RANK["critical"]:
        raise BaselineViolation(f"rule cannot be baselined: {entry.rule_id}")
    if entry.metric_value is not None:
        _validate_metric(entry.metric_value, "metric_value")
    accepted = _parse_time(entry.accepted_at)
    review = _parse_time(entry.review_by)
    if current is not None and review < current.astimezone(timezone.utc):
        raise BaselineViolation(f"expired baseline entry: {entry.fingerprint}")
    if accepted > review:
        raise BaselineViolation(f"accepted_at must not be after review_by: {entry.fingerprint}")


def validate_baseline(entries: Iterable[BaselineEntry], *, now: str | datetime | None = None) -> tuple[BaselineEntry, ...]:
    if isinstance(now, str):
        current = _parse_time(now)
    elif now is None:
        current = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        current = now
    else:
        raise BaselineViolation("now must be a timestamp")
    if current.tzinfo is None:
        raise BaselineViolation("now must include timezone")
    values = tuple(entries)
    seen: set[str] = set()
    for entry in values:
        _validate_entry(entry, current=current, seen=seen)
    return tuple(sorted(values, key=lambda item: item.fingerprint))


def compare_findings(baseline_fingerprints: Iterable[str], findings: Iterable[Finding], *, baseline: Iterable[BaselineEntry] = (), metrics: Mapping[str, float | int] | None = None, metric_tolerances: Mapping[str, float | int] | None = None, metric_directions: Mapping[str, str] | None = None, project_root: str | None = None) -> Comparison:
    baseline_values = validate_baseline(baseline) if baseline else ()
    entries = {entry.fingerprint: entry for entry in baseline_values}
    known = set(baseline_fingerprints) | set(entries)
    if any(type(value) is not str or not value.strip() for value in known):
        raise BaselineViolation("baseline fingerprints must be non-empty strings")
    collapsed: dict[str, Finding] = {}
    for item in findings:
        if not isinstance(item, Finding):
            raise BaselineViolation("current findings must be Finding values")
        item_fingerprint = fingerprint(item, project_root=project_root)
        prior = collapsed.get(item_fingerprint)
        if prior is None or _SEVERITY_RANK[item.severity.lower()] > _SEVERITY_RANK[prior.severity.lower()] or (
            _SEVERITY_RANK[item.severity.lower()] == _SEVERITY_RANK[prior.severity.lower()]
            and (item.rule_id, item.category, item.path, item.message, item.confidence, item.evidence_refs, item.baselinable)
            < (prior.rule_id, prior.category, prior.path, prior.message, prior.confidence, prior.evidence_refs, prior.baselinable)
        ):
            collapsed[item_fingerprint] = item
    current = tuple(sorted(collapsed.values(), key=lambda item: (fingerprint(item, project_root=project_root), item.rule_id, item.path, item.message)))
    existing: list[Finding] = []
    new: list[Finding] = []
    worsened: list[Finding | str] = []
    for item in current:
        item_fingerprint = fingerprint(item, project_root=project_root)
        if item_fingerprint not in known:
            new.append(item)
        elif item_fingerprint in entries and _SEVERITY_RANK[item.severity.lower()] > _SEVERITY_RANK[entries[item_fingerprint].severity.lower()]:
            worsened.append(item)
        else:
            existing.append(item)
    metrics = metrics or {}
    tolerances = metric_tolerances or {}
    directions = metric_directions or {}
    for key, value in metrics.items():
        _validate_metric(value, f"metric {key}")
    for key, value in tolerances.items():
        if _validate_metric(value, f"tolerance {key}") < 0:
            raise BaselineViolation(f"tolerance {key} must be non-negative")
    for key, direction in directions.items():
        if direction not in _METRIC_DIRECTIONS:
            raise BaselineViolation(f"unknown metric direction: {direction}")
    for entry in baseline_values:
        if entry.metric_value is not None and entry.fingerprint in metrics:
            actual = _validate_metric(metrics[entry.fingerprint], f"metric {entry.fingerprint}")
            tolerance = _validate_metric(tolerances.get(entry.fingerprint, 0), f"tolerance {entry.fingerprint}")
            direction = directions.get(entry.fingerprint, "lower-is-better")
            limit = float(entry.metric_value) * (1 + tolerance) if direction == "lower-is-better" else float(entry.metric_value) * (1 - tolerance)
            if (direction == "lower-is-better" and actual > limit) or (direction == "higher-is-better" and actual < limit):
                worsened.append(entry.fingerprint)
    return Comparison(tuple(existing), tuple(new), tuple(worsened), tuple(sorted(known - set(collapsed))))


def _entry_dict(entry: BaselineEntry) -> dict[str, object]:
    data = {"accepted_at": entry.accepted_at, "actor": entry.actor, "fingerprint": entry.fingerprint, "owner": entry.owner, "review_by": entry.review_by, "rule_id": entry.rule_id, "rule_version": entry.rule_version, "severity": entry.severity, "source_audit_receipt": entry.source_audit_receipt}
    if entry.metric_value is not None:
        data["metric_value"] = entry.metric_value
    return data


def _validate_entries_for_storage(entries: Iterable[BaselineEntry]) -> tuple[BaselineEntry, ...]:
    values = tuple(entries)
    seen: set[str] = set()
    for entry in values:
        _validate_entry(entry, seen=seen)
    return tuple(sorted(values, key=lambda item: item.fingerprint))


def dump_baseline(entries: Iterable[BaselineEntry]) -> str:
    values = _validate_entries_for_storage(entries)
    return json.dumps({"entries": [_entry_dict(entry) for entry in values], "schema_version": SCHEMA_VERSION}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load_baseline(text: str) -> tuple[BaselineEntry, ...]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise BaselineViolation("invalid baseline JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("schema_version"), str):
        raise BaselineViolation("missing baseline schema_version")
    if payload["schema_version"].split(".", 1)[0] != SCHEMA_VERSION.split(".", 1)[0]:
        raise BaselineViolation(f"unsupported baseline schema major: {payload['schema_version']}")
    if set(payload) != {"schema_version", "entries"}:
        raise BaselineViolation("unknown baseline schema fields")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise BaselineViolation("baseline entries must be an array")
    allowed = {"accepted_at", "actor", "fingerprint", "owner", "review_by", "rule_id", "rule_version", "severity", "source_audit_receipt", "metric_value"}
    entries = []
    for raw in raw_entries:
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise BaselineViolation("malformed baseline entry")
        try:
            entries.append(BaselineEntry(**raw))
        except TypeError as error:
            raise BaselineViolation("malformed baseline entry") from error
    return _validate_entries_for_storage(entries)
