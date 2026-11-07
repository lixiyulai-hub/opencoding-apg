from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from typing import Any
import unicodedata

from .gates import GateDefinition
from .model import CheckResult, CheckStatus
from .receipts import redact
from .storage import digest


CANDIDATE_SCHEMA_VERSION = "2.0"
REGRESSION_GATE_CONTRACTS_OUTPUT_KEY = "regression_gate_contracts"
GATE_CONTRACT_SCHEMA = "regression-gate-contract-v1"
GATE_CONTRACT_SNAPSHOT_SCHEMA_VERSION = "1.0"
EVIDENCE_PROJECTION_SCHEMA = "regression-evidence-v1"
CANDIDATE_ID_SCHEMA = "regression-candidate-v2"
_CANDIDATE_FIELDS = frozenset(
    {
        "candidate_schema_version",
        "candidate_id",
        "symptom_code",
        "gate_contract",
        "gate_contract_digest",
        "evidence_digest",
    }
)
_GATE_CONTRACT_FIELDS = frozenset(
    {
        "schema",
        "gate_id",
        "phase",
        "kind",
        "required",
        "timeout_seconds",
        "warning_exit_codes",
        "command_arg_count",
        "option_keys",
    }
)
_LEGACY_PROPOSAL_FIELDS = frozenset({"status", "symptom_codes"})
_V2_PROPOSAL_FIELDS = frozenset(
    {"candidate_schema_version", "status", "symptom_codes", "candidates"}
)
_GATE_CONTRACT_SNAPSHOT_FIELDS = frozenset({"schema_version", "contracts"})
_GATE_CONTRACT_SNAPSHOT_ITEM_FIELDS = frozenset(
    {"gate_contract", "gate_contract_digest"}
)
_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_SYMPTOM = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_DRIVE = re.compile(r"^[A-Za-z]:")
_MAX_EVIDENCE_REFS = 64
_MAX_CANDIDATES = 64
_MAX_GATE_CONTRACTS = 256


class RegressionIdentityError(ValueError):
    pass


class CandidateIdentityMismatch(RegressionIdentityError):
    pass


class GateContractMismatch(RegressionIdentityError):
    pass


class GateContractDigestMismatch(GateContractMismatch):
    pass


class EvidenceDigestMismatch(RegressionIdentityError):
    pass


class CandidateSourceMismatch(RegressionIdentityError):
    pass


@dataclass(frozen=True)
class GateContractProjection:
    schema: str
    gate_id: str
    phase: str
    kind: str
    required: bool
    timeout_seconds: int
    warning_exit_codes: tuple[int, ...]
    command_arg_count: int
    option_keys: tuple[str, ...]


@dataclass(frozen=True)
class RegressionCandidate:
    candidate_schema_version: str
    candidate_id: str
    symptom_code: str
    gate_contract: GateContractProjection
    gate_contract_digest: str
    evidence_digest: str


@dataclass(frozen=True)
class ParsedRegressionProposal:
    legacy: bool
    status: str
    symptom_codes: tuple[str, ...]
    candidates: tuple[RegressionCandidate, ...]


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise TypeError(f"{label} must be a string-keyed object")
    return value


def _closed(value: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    missing = fields - set(value)
    unknown = set(value) - fields
    if missing:
        raise ValueError(f"{label} is missing required fields")
    if unknown:
        raise ValueError(f"{label} has unknown fields")


def _code(value: object, label: str) -> str:
    if type(value) is not str or not _CODE.fullmatch(value):
        raise ValueError(f"{label} must match the bounded code grammar")
    if redact(value) != value:
        raise ValueError(f"{label} contains sensitive material")
    return value


def _syntax_code(value: object, label: str) -> str:
    if type(value) is not str or not _CODE.fullmatch(value):
        raise ValueError(f"{label} must match the bounded code grammar")
    return value


def _safe_gate_id(value: object) -> str:
    gate_id = _syntax_code(value, "gate_id")
    return "redacted" if redact(gate_id) != gate_id else gate_id


def _safe_gate_kind(value: object) -> str:
    kind = _syntax_code(value, "gate kind")
    return "redacted" if redact(kind) != kind else kind


def _symptom(value: object) -> str:
    if type(value) is not str or not _SYMPTOM.fullmatch(value):
        raise ValueError("symptom_code must match the bounded code grammar")
    if redact(value) != value:
        raise ValueError("symptom_code contains sensitive material")
    return value


def _digest_value(value: object, label: str) -> str:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _defect_class(symptom_code: str) -> str:
    return "gate_failure" if symptom_code.startswith("gate.") else "governance_control"


def _normalize_reference(
    value: object,
    *,
    allow_bare_whitespace: bool = False,
) -> str | None:
    if type(value) is not str or not value:
        raise ValueError("evidence reference must be bounded text")
    if len(value.encode("utf-8")) > 512:
        raise ValueError("evidence reference must be bounded text")
    if value == "[REDACTED]":
        return None
    normalized = value.replace("\\", "/")
    if normalized == ".":
        return "project.root"
    if (
        "\x00" in normalized
        or "?" in normalized
        or "#" in normalized
        or "://" in normalized
        or normalized.startswith("/")
        or normalized.startswith("//")
        or _DRIVE.match(normalized)
        or normalized.count(":") > 1
        or any(unicodedata.category(character).startswith("C") for character in normalized)
    ):
        raise ValueError("evidence reference must be a constrained project locator")
    path_text, separator, locator = normalized.partition(":")
    path = PurePosixPath(path_text)
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("evidence reference must be traversal-free")
    if any(part != part.strip() or len(part.encode("utf-8")) > 256 for part in parts):
        raise ValueError("evidence reference has invalid path syntax")
    has_path_syntax = "/" in path_text or "." in parts[-1] or bool(separator)
    if (
        any(character.isspace() for character in normalized)
        and not has_path_syntax
        and not allow_bare_whitespace
    ):
        raise ValueError("evidence reference must be path-like")
    if separator:
        _code(locator, "evidence locator")
    if redact(normalized) != normalized:
        return None
    return normalized


def normalize_evidence_refs(
    values: Iterable[str],
    *,
    allow_bare_whitespace: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("evidence references must be an iterable of strings")
    raw = tuple(values)
    if len(raw) > _MAX_EVIDENCE_REFS:
        raise ValueError("evidence references exceed the bounded limit")
    normalized_items = tuple(
        _normalize_reference(
            value,
            allow_bare_whitespace=allow_bare_whitespace,
        )
        for value in raw
    )
    visible = tuple(item for item in normalized_items if item is not None)
    if len(set(visible)) != len(visible):
        raise ValueError("evidence references contain duplicates")
    redacted_count = len(normalized_items) - len(visible)
    redacted = tuple(
        f"redacted/evidence-{index:03d}.ref"
        for index in range(1, redacted_count + 1)
    )
    return tuple(sorted(visible + redacted))


def gate_contract_mapping(value: GateContractProjection) -> dict[str, Any]:
    if not isinstance(value, GateContractProjection):
        raise TypeError("gate contract must be a GateContractProjection")
    return {
        "schema": value.schema,
        "gate_id": value.gate_id,
        "phase": value.phase,
        "kind": value.kind,
        "required": value.required,
        "timeout_seconds": value.timeout_seconds,
        "warning_exit_codes": value.warning_exit_codes,
        "command_arg_count": value.command_arg_count,
        "option_keys": value.option_keys,
    }


def _parse_gate_contract(value: object) -> GateContractProjection:
    mapping = _mapping(value, "gate_contract")
    _closed(mapping, _GATE_CONTRACT_FIELDS, "gate_contract")
    if mapping["schema"] != GATE_CONTRACT_SCHEMA:
        raise ValueError("unsupported gate contract schema")
    required = mapping["required"]
    timeout = mapping["timeout_seconds"]
    argument_count = mapping["command_arg_count"]
    if type(required) is not bool:
        raise TypeError("gate contract required must be bool")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("gate contract timeout must be positive")
    if isinstance(argument_count, bool) or not isinstance(argument_count, int) or argument_count < 0:
        raise ValueError("gate contract argument count must be non-negative")
    warning_codes = mapping["warning_exit_codes"]
    option_keys = mapping["option_keys"]
    if isinstance(warning_codes, (str, bytes)) or not isinstance(warning_codes, Sequence):
        raise TypeError("gate contract warning codes must be an array")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in warning_codes):
        raise TypeError("gate contract warning codes must be integers")
    if isinstance(option_keys, (str, bytes)) or not isinstance(option_keys, Sequence):
        raise TypeError("gate contract option keys must be an array")
    normalized_keys = tuple(_code(item, "gate option key") for item in option_keys)
    if normalized_keys != tuple(sorted(set(normalized_keys))):
        raise ValueError("gate contract option keys must be unique and sorted")
    normalized_warnings = tuple(sorted(set(warning_codes)))
    if tuple(warning_codes) != normalized_warnings:
        raise ValueError("gate contract warning codes must be unique and sorted")
    return GateContractProjection(
        GATE_CONTRACT_SCHEMA,
        _safe_gate_id(mapping["gate_id"]),
        _syntax_code(mapping["phase"], "gate phase"),
        _safe_gate_kind(mapping["kind"]),
        required,
        timeout,
        normalized_warnings,
        argument_count,
        normalized_keys,
    )


def gate_contract_for_definition(gate: GateDefinition) -> GateContractProjection:
    if not isinstance(gate, GateDefinition):
        raise TypeError("gate must be a GateDefinition")
    option_keys = tuple(sorted(_code(key, "gate option key") for key in gate.options))
    return GateContractProjection(
        GATE_CONTRACT_SCHEMA,
        _safe_gate_id(gate.gate_id),
        _syntax_code(gate.phase, "gate phase"),
        _safe_gate_kind(gate.kind),
        gate.required,
        gate.timeout_seconds,
        tuple(sorted(set(gate.warning_exit_codes))),
        len(gate.command),
        option_keys,
    )


def _governance_contract(symptom_code: str) -> GateContractProjection:
    return GateContractProjection(
        GATE_CONTRACT_SCHEMA,
        _code(symptom_code, "governance gate_id"),
        "control",
        "governance",
        True,
        1,
        (),
        0,
        (),
    )


def gate_contract_digest(value: GateContractProjection) -> str:
    return digest(gate_contract_mapping(value))


def gate_contract_snapshot(gates: Iterable[GateDefinition]) -> Mapping[str, Any]:
    gate_items = tuple(gates)
    if len(gate_items) > _MAX_GATE_CONTRACTS:
        raise ValueError("Gate contract snapshots exceed the bounded limit")
    contracts = tuple(gate_contract_for_definition(gate) for gate in gate_items)
    items = tuple(
        sorted(
            (
                {
                    "gate_contract": gate_contract_mapping(contract),
                    "gate_contract_digest": gate_contract_digest(contract),
                }
                for contract in contracts
            ),
            key=lambda item: item["gate_contract_digest"],
        )
    )
    if len({item["gate_contract_digest"] for item in items}) != len(items):
        raise ValueError("safe Gate contract snapshots collide")
    return MappingProxyType(
        {
            "schema_version": GATE_CONTRACT_SNAPSHOT_SCHEMA_VERSION,
            "contracts": items,
        }
    )


def parse_gate_contract_snapshot(value: object) -> Mapping[str, GateContractProjection]:
    mapping = _mapping(value, REGRESSION_GATE_CONTRACTS_OUTPUT_KEY)
    _closed(mapping, _GATE_CONTRACT_SNAPSHOT_FIELDS, REGRESSION_GATE_CONTRACTS_OUTPUT_KEY)
    if mapping["schema_version"] != GATE_CONTRACT_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("unsupported Gate contract snapshot schema")
    contracts_value = mapping["contracts"]
    if isinstance(contracts_value, (str, bytes)) or not isinstance(
        contracts_value, Sequence
    ):
        raise TypeError("Gate contract snapshots must be an array")
    if len(contracts_value) > _MAX_GATE_CONTRACTS:
        raise ValueError("Gate contract snapshots exceed the bounded limit")
    result: dict[str, GateContractProjection] = {}
    previous = ""
    for raw in contracts_value:
        item = _mapping(raw, "Gate contract snapshot")
        _closed(item, _GATE_CONTRACT_SNAPSHOT_ITEM_FIELDS, "Gate contract snapshot")
        contract = _parse_gate_contract(item["gate_contract"])
        stored_digest = _digest_value(
            item["gate_contract_digest"], "gate_contract_digest"
        )
        if stored_digest != gate_contract_digest(contract):
            raise GateContractDigestMismatch(
                "Gate contract snapshot digest does not match its projection"
            )
        if stored_digest in result or stored_digest < previous:
            raise ValueError("Gate contract snapshots must be unique and sorted")
        previous = stored_digest
        result[stored_digest] = contract
    return MappingProxyType(result)


def _evidence_digest(
    *,
    gate_id: str,
    phase: str,
    status: str,
    evidence_refs: Iterable[str],
    allow_bare_whitespace: bool = False,
) -> str:
    return digest(
        {
            "schema": EVIDENCE_PROJECTION_SCHEMA,
            "source_kind": "gate",
            "gate_id": _safe_gate_id(gate_id),
            "phase": _syntax_code(phase, "gate phase"),
            "status": _code(status.replace("-", "_"), "gate status"),
            "evidence_refs": normalize_evidence_refs(
                evidence_refs,
                allow_bare_whitespace=allow_bare_whitespace,
            ),
        }
    )


def candidate_id_for(
    symptom_code: str,
    gate_digest: str,
    evidence_digest: str,
) -> str:
    symptom = _symptom(symptom_code)
    return digest(
        {
            "schema": CANDIDATE_ID_SCHEMA,
            "defect_class": _defect_class(symptom),
            "symptom_code": symptom,
            "gate_contract_digest": _digest_value(gate_digest, "gate_contract_digest"),
            "evidence_digest": _digest_value(evidence_digest, "evidence_digest"),
        }
    )


def _candidate(
    symptom_code: str,
    contract: GateContractProjection,
    evidence_digest: str,
) -> RegressionCandidate:
    contract_digest = gate_contract_digest(contract)
    return RegressionCandidate(
        CANDIDATE_SCHEMA_VERSION,
        candidate_id_for(symptom_code, contract_digest, evidence_digest),
        _symptom(symptom_code),
        contract,
        contract_digest,
        _digest_value(evidence_digest, "evidence_digest"),
    )


def symptom_code_for_check(check: CheckResult) -> str:
    if not isinstance(check, CheckResult):
        raise TypeError("check must be a CheckResult")
    normalized = re.sub(r"[^a-z0-9._-]+", "-", check.gate_id.casefold()).strip("-._")
    if redact(normalized) != normalized:
        normalized = "redacted"
    candidate = f"gate.{normalized or 'unknown'}.{check.status.value.replace('-', '_')}"
    candidate = candidate[:64].rstrip("-._")
    return _symptom(candidate)


def candidate_for_gate(gate: GateDefinition, check: CheckResult) -> RegressionCandidate:
    if not isinstance(check, CheckResult):
        raise TypeError("check must be a CheckResult")
    if check.status not in {CheckStatus.FAIL, CheckStatus.INCONCLUSIVE}:
        raise ValueError("only failed or inconclusive checks form regression candidates")
    if gate.gate_id != check.gate_id or gate.phase != check.phase:
        raise GateContractMismatch("gate contract does not match the check result")
    contract = gate_contract_for_definition(gate)
    evidence = _evidence_digest(
        gate_id=check.gate_id,
        phase=check.phase,
        status=check.status.value,
        evidence_refs=check.evidence_refs,
    )
    return _candidate(symptom_code_for_check(check), contract, evidence)


def candidate_for_governance(
    symptom_code: str,
    *,
    evidence_refs: Iterable[str] = (),
) -> RegressionCandidate:
    symptom = _symptom(symptom_code)
    contract = _governance_contract(symptom)
    status = "fail" if symptom == "governance.scope_violation" else "inconclusive"
    evidence = _evidence_digest(
        gate_id=contract.gate_id,
        phase=contract.phase,
        status=status,
        evidence_refs=evidence_refs,
        allow_bare_whitespace=symptom == "governance.scope_violation",
    )
    return _candidate(symptom, contract, evidence)


def candidate_mapping(value: RegressionCandidate) -> dict[str, Any]:
    if not isinstance(value, RegressionCandidate):
        raise TypeError("candidate must be a RegressionCandidate")
    return {
        "candidate_schema_version": value.candidate_schema_version,
        "candidate_id": value.candidate_id,
        "symptom_code": value.symptom_code,
        "gate_contract": gate_contract_mapping(value.gate_contract),
        "gate_contract_digest": value.gate_contract_digest,
        "evidence_digest": value.evidence_digest,
    }


def parse_candidate(value: object) -> RegressionCandidate:
    mapping = _mapping(value, "regression candidate")
    _closed(mapping, _CANDIDATE_FIELDS, "regression candidate")
    if mapping["candidate_schema_version"] != CANDIDATE_SCHEMA_VERSION:
        raise ValueError("unsupported regression candidate schema")
    contract = _parse_gate_contract(mapping["gate_contract"])
    stored_contract_digest = _digest_value(
        mapping["gate_contract_digest"], "gate_contract_digest"
    )
    if stored_contract_digest != gate_contract_digest(contract):
        raise GateContractDigestMismatch("gate contract digest does not match its projection")
    symptom = _symptom(mapping["symptom_code"])
    evidence = _digest_value(mapping["evidence_digest"], "evidence_digest")
    candidate_id = _digest_value(mapping["candidate_id"], "candidate_id")
    if candidate_id != candidate_id_for(symptom, stored_contract_digest, evidence):
        raise CandidateIdentityMismatch("candidate_id does not match its identity fields")
    return RegressionCandidate(
        CANDIDATE_SCHEMA_VERSION,
        candidate_id,
        symptom,
        contract,
        stored_contract_digest,
        evidence,
    )


def parse_regression_proposal(value: object) -> ParsedRegressionProposal:
    mapping = _mapping(value, "proposed_regression_delta")
    fields = frozenset(mapping)
    if fields == _LEGACY_PROPOSAL_FIELDS:
        symptoms = mapping["symptom_codes"]
        if isinstance(symptoms, (str, bytes)) or not isinstance(symptoms, Sequence):
            raise TypeError("symptom_codes must be an array")
        normalized = tuple(_symptom(item) for item in symptoms)
        status = mapping["status"]
        if status not in {"none", "candidate"} or (status == "none") != (not normalized):
            raise ValueError("legacy regression proposal is inconsistent")
        return ParsedRegressionProposal(True, status, normalized, ())
    _closed(mapping, _V2_PROPOSAL_FIELDS, "proposed_regression_delta")
    if mapping["candidate_schema_version"] != CANDIDATE_SCHEMA_VERSION:
        raise ValueError("unsupported regression proposal schema")
    candidates_value = mapping["candidates"]
    symptoms_value = mapping["symptom_codes"]
    if isinstance(candidates_value, (str, bytes)) or not isinstance(candidates_value, Sequence):
        raise TypeError("candidates must be an array")
    if isinstance(symptoms_value, (str, bytes)) or not isinstance(symptoms_value, Sequence):
        raise TypeError("symptom_codes must be an array")
    if len(candidates_value) > _MAX_CANDIDATES:
        raise ValueError("candidate count exceeds the bounded limit")
    candidates = tuple(parse_candidate(item) for item in candidates_value)
    if len({item.candidate_id for item in candidates}) != len(candidates):
        raise ValueError("regression proposal contains duplicate candidate IDs")
    candidates = tuple(sorted(candidates, key=lambda item: item.candidate_id))
    symptoms = tuple(_symptom(item) for item in symptoms_value)
    expected_symptoms = tuple(sorted({item.symptom_code for item in candidates}))
    if symptoms != expected_symptoms:
        raise ValueError("regression proposal symptom codes do not match candidates")
    status = mapping["status"]
    if status not in {"none", "candidate"} or (status == "none") != (not candidates):
        raise ValueError("regression proposal status is inconsistent")
    return ParsedRegressionProposal(False, status, symptoms, candidates)


def candidate_proposal(
    gates: Iterable[GateDefinition],
    checks: Iterable[CheckResult],
    *,
    exit_code: int,
    governance_evidence_refs: Iterable[str] = (),
) -> Mapping[str, Any]:
    gate_items = tuple(gates)
    gate_map = {gate.gate_id: gate for gate in gate_items}
    if len(gate_map) != len(gate_items):
        raise ValueError("gate definitions contain duplicate IDs")
    candidates = []
    for check in checks:
        if check.status not in {CheckStatus.FAIL, CheckStatus.INCONCLUSIVE}:
            continue
        gate = gate_map.get(check.gate_id)
        if gate is None:
            raise GateContractMismatch("failed check is missing its Gate definition")
        candidates.append(candidate_for_gate(gate, check))
    if exit_code == 4:
        candidates.append(
            candidate_for_governance(
                "governance.scope_violation",
                evidence_refs=governance_evidence_refs,
            )
        )
    elif exit_code == 3 and not candidates:
        candidates.append(candidate_for_governance("governance.inconclusive"))
    ordered = tuple(sorted(candidates, key=lambda item: item.candidate_id))
    if len(ordered) > _MAX_CANDIDATES:
        raise ValueError("regression candidate count exceeds the bounded limit")
    if len({item.candidate_id for item in ordered}) != len(ordered):
        raise ValueError("safe regression candidate identities collide")
    return MappingProxyType(
        {
            "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
            "status": "candidate" if ordered else "none",
            "symptom_codes": tuple(sorted({item.symptom_code for item in ordered})),
            "candidates": tuple(candidate_mapping(item) for item in ordered),
        }
    )


def validate_candidate_against_receipt(
    candidate: RegressionCandidate,
    checks: Iterable[CheckResult],
    *,
    governance_evidence_refs: Iterable[str] = (),
) -> None:
    if candidate.symptom_code.startswith("gate."):
        matches = tuple(
            check
            for check in checks
            if _safe_gate_id(check.gate_id) == candidate.gate_contract.gate_id
            and check.phase == candidate.gate_contract.phase
            and symptom_code_for_check(check) == candidate.symptom_code
        )
        if len(matches) != 1:
            raise CandidateSourceMismatch("candidate does not match one receipt check")
        expected = _evidence_digest(
            gate_id=matches[0].gate_id,
            phase=matches[0].phase,
            status=matches[0].status.value,
            evidence_refs=matches[0].evidence_refs,
        )
    elif candidate.symptom_code == "governance.scope_violation":
        if candidate.gate_contract != _governance_contract(candidate.symptom_code):
            raise GateContractMismatch("governance candidate contract is inconsistent")
        expected = _evidence_digest(
            gate_id=candidate.gate_contract.gate_id,
            phase=candidate.gate_contract.phase,
            status="fail",
            evidence_refs=governance_evidence_refs,
            allow_bare_whitespace=True,
        )
    elif candidate.symptom_code == "governance.inconclusive":
        if candidate.gate_contract != _governance_contract(candidate.symptom_code):
            raise GateContractMismatch("governance candidate contract is inconsistent")
        expected = _evidence_digest(
            gate_id=candidate.gate_contract.gate_id,
            phase=candidate.gate_contract.phase,
            status="inconclusive",
            evidence_refs=(),
        )
    else:
        raise CandidateSourceMismatch("candidate symptom is unsupported")
    if candidate.evidence_digest != expected:
        raise EvidenceDigestMismatch("candidate evidence digest does not match the receipt")


def validate_proposal_against_receipt(
    value: object,
    checks: Iterable[CheckResult],
    *,
    exit_code: int,
    governance_evidence_refs: Iterable[str] = (),
    gate_contracts: object | None = None,
) -> ParsedRegressionProposal:
    parsed = parse_regression_proposal(value)
    if parsed.legacy:
        return parsed
    if gate_contracts is None:
        raise GateContractMismatch("v2 receipt is missing its Gate contract snapshot")
    contract_snapshot = parse_gate_contract_snapshot(gate_contracts)
    check_items = tuple(checks)
    for candidate in parsed.candidates:
        if candidate.symptom_code.startswith("gate."):
            source_contract = contract_snapshot.get(candidate.gate_contract_digest)
            if source_contract != candidate.gate_contract:
                raise GateContractMismatch(
                    "candidate Gate contract does not match the receipt snapshot"
                )
        validate_candidate_against_receipt(
            candidate,
            check_items,
            governance_evidence_refs=governance_evidence_refs,
        )
    expected_gate_keys = Counter(
        (_safe_gate_id(check.gate_id), check.phase)
        for check in check_items
        if check.status in {CheckStatus.FAIL, CheckStatus.INCONCLUSIVE}
    )
    actual_gate_keys = Counter(
        (candidate.gate_contract.gate_id, candidate.gate_contract.phase)
        for candidate in parsed.candidates
        if candidate.symptom_code.startswith("gate.")
    )
    if actual_gate_keys != expected_gate_keys:
        raise CandidateSourceMismatch("proposal candidates do not cover receipt checks")
    expected_governance = set()
    if exit_code == 4:
        expected_governance.add("governance.scope_violation")
    elif exit_code == 3 and not expected_gate_keys:
        expected_governance.add("governance.inconclusive")
    actual_governance = {
        candidate.symptom_code
        for candidate in parsed.candidates
        if not candidate.symptom_code.startswith("gate.")
    }
    if actual_governance != expected_governance:
        raise CandidateSourceMismatch("proposal governance candidates are inconsistent")
    return parsed


__all__ = [
    "CANDIDATE_SCHEMA_VERSION",
    "REGRESSION_GATE_CONTRACTS_OUTPUT_KEY",
    "CandidateIdentityMismatch",
    "CandidateSourceMismatch",
    "EvidenceDigestMismatch",
    "GateContractDigestMismatch",
    "GateContractMismatch",
    "GateContractProjection",
    "ParsedRegressionProposal",
    "RegressionCandidate",
    "RegressionIdentityError",
    "candidate_for_gate",
    "candidate_for_governance",
    "candidate_id_for",
    "candidate_mapping",
    "candidate_proposal",
    "gate_contract_digest",
    "gate_contract_for_definition",
    "gate_contract_mapping",
    "gate_contract_snapshot",
    "normalize_evidence_refs",
    "parse_candidate",
    "parse_gate_contract_snapshot",
    "parse_regression_proposal",
    "symptom_code_for_check",
    "validate_candidate_against_receipt",
    "validate_proposal_against_receipt",
]
