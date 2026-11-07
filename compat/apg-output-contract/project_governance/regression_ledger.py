from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from .receipts import (
    FEEDBACK_LOOP_DECISION_OUTPUT_KEY,
    load_receipt_json,
    redact,
)
from .regression_identity import (
    REGRESSION_GATE_CONTRACTS_OUTPUT_KEY,
    CandidateIdentityMismatch,
    CandidateSourceMismatch,
    EvidenceDigestMismatch,
    GateContractDigestMismatch,
    GateContractMismatch,
    RegressionCandidate,
    candidate_id_for,
    parse_regression_proposal,
    validate_proposal_against_receipt,
)
from .storage import SchemaError, canonical_json_bytes, digest


REGRESSION_UPDATE_KEY = "regression_update"
REGRESSION_SCHEMA_VERSION = "1.0"
REGRESSION_V2_SCHEMA_VERSION = "1.1"
REGRESSION_FINGERPRINT_SCHEMA = "regression-fingerprint-v1"
_UPDATE_FIELDS_V1 = frozenset(
    {
        "extension_schema_version",
        "source_receipt_ref",
        "symptom_code",
        "owner",
        "status",
        "next_gate",
        "permanent_assets",
    }
)
_UPDATE_FIELDS_V2 = frozenset(
    {
        "extension_schema_version",
        "source_receipt_ref",
        "candidate_id",
        "owner",
        "status",
        "next_gate",
        "permanent_assets",
    }
)
_RECORD_FIELDS_V1 = frozenset(
    {
        "extension_schema_version",
        "fingerprint",
        "defect_class",
        "symptom_code",
        "first_seen_receipt_ref",
        "last_seen_receipt_ref",
        "recurrence_count",
        "permanent_assets",
        "owner",
        "status",
        "next_gate",
    }
)
_RECORD_FIELDS_V2 = frozenset(
    {
        "extension_schema_version",
        "fingerprint",
        "defect_class",
        "symptom_code",
        "gate_contract_digest",
        "evidence_digest",
        "first_seen_receipt_ref",
        "last_seen_receipt_ref",
        "recurrence_count",
        "permanent_assets",
        "owner",
        "status",
        "next_gate",
    }
)
_ASSET_FIELDS = frozenset({"type", "path"})
_ASSET_TYPES = frozenset(
    {"test", "eval", "static_rule", "runtime_probe", "incident_fingerprint"}
)
_STATUSES = frozenset({"open", "closed"})
_CODE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")


class UnsupportedRegressionSchema(ValueError):
    pass


@dataclass(frozen=True)
class PermanentAsset:
    type: str
    path: str


@dataclass(frozen=True)
class RegressionRecord:
    extension_schema_version: str
    fingerprint: str
    defect_class: str
    symptom_code: str
    first_seen_receipt_ref: str
    last_seen_receipt_ref: str
    recurrence_count: int
    permanent_assets: tuple[PermanentAsset, ...]
    owner: str
    status: str
    next_gate: str


@dataclass(frozen=True)
class RegressionRecordV2:
    extension_schema_version: str
    fingerprint: str
    defect_class: str
    symptom_code: str
    gate_contract_digest: str
    evidence_digest: str
    first_seen_receipt_ref: str
    last_seen_receipt_ref: str
    recurrence_count: int
    permanent_assets: tuple[PermanentAsset, ...]
    owner: str
    status: str
    next_gate: str


@dataclass(frozen=True)
class PreparedRegressionUpdate:
    record: RegressionRecord | RegressionRecordV2
    relative_path: str
    payload: bytes
    preexisting: bool


@dataclass(frozen=True)
class RegressionDiagnostics:
    records: int
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise TypeError(f"{label} must be a string-keyed object")
    return value


def _closed(value: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    missing = sorted(fields - set(value))
    unknown = sorted(set(value) - fields)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise ValueError(f"{label} fields are invalid: {'; '.join(details)}")


def _code(value: object, label: str) -> str:
    if type(value) is not str or not _CODE.fullmatch(value):
        raise ValueError(f"{label} must match the bounded code grammar")
    if redact(value) != value:
        raise ValueError(f"{label} contains sensitive material")
    return value


def _relative(root: Path, value: object, label: str) -> str:
    if type(value) is not str or not value or len(value) > 256 or not value.isascii():
        raise ValueError(f"{label} must be bounded ASCII")
    if "?" in value or "#" in value or "://" in value:
        raise ValueError(f"{label} cannot contain URL or query data")
    normalized = value.replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ValueError(f"{label} must be project-relative and traversal-free")
    resolved = (root / path).resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError(f"{label} escapes the project root")
    if redact(normalized) != normalized:
        raise ValueError(f"{label} contains sensitive material")
    return normalized


def _receipt_ref(root: Path, value: object, label: str) -> str:
    relative = _relative(root, value, label)
    path = Path(relative)
    if path.parent.as_posix() != ".governance/receipts" or path.suffix != ".json":
        raise ValueError(f"{label} must reference a governance receipt")
    return relative


def _defect_class(symptom_code: str) -> str:
    return "gate_failure" if symptom_code.startswith("gate.") else "governance_control"


def regression_fingerprint(symptom_code: str) -> str:
    symptom = _code(symptom_code, "symptom_code")
    return digest(
        {
            "schema": REGRESSION_FINGERPRINT_SCHEMA,
            "defect_class": _defect_class(symptom),
            "symptom_code": symptom,
        }
    )


def regression_record_path(fingerprint: str) -> str:
    if type(fingerprint) is not str or not _FINGERPRINT.fullmatch(fingerprint):
        raise ValueError("fingerprint must be lowercase SHA-256")
    return f".governance/regressions/{fingerprint}.json"


def _assets(root: Path, value: object, *, require_exists: bool) -> tuple[PermanentAsset, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) > 32:
        raise ValueError("permanent_assets must be a bounded sequence")
    assets = []
    for item in value:
        mapping = _mapping(item, "permanent_asset")
        _closed(mapping, _ASSET_FIELDS, "permanent_asset")
        asset_type = mapping["type"]
        if asset_type not in _ASSET_TYPES:
            raise ValueError("permanent asset type is unsupported")
        path = _relative(root, mapping["path"], "permanent asset path")
        if require_exists and not (root / path).is_file():
            raise ValueError("permanent asset does not exist")
        assets.append(PermanentAsset(asset_type, path))
    return tuple(sorted(set(assets), key=lambda item: (item.type, item.path)))


def _digest_value(value: object, label: str) -> str:
    if type(value) is not str or not _FINGERPRINT.fullmatch(value):
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def parse_regression_record(
    value: object,
    root: str | Path,
) -> RegressionRecord | RegressionRecordV2:
    project_root = Path(root).resolve(strict=True)
    mapping = _mapping(value, "regression_record")
    version = mapping.get("extension_schema_version")
    if version == REGRESSION_SCHEMA_VERSION:
        _closed(mapping, _RECORD_FIELDS_V1, "regression_record")
    elif version == REGRESSION_V2_SCHEMA_VERSION:
        _closed(mapping, _RECORD_FIELDS_V2, "regression_record")
    else:
        raise UnsupportedRegressionSchema("unsupported regression schema version")
    symptom = _code(mapping["symptom_code"], "symptom_code")
    fingerprint = _digest_value(mapping["fingerprint"], "regression fingerprint")
    gate_digest = ""
    evidence_digest = ""
    if version == REGRESSION_SCHEMA_VERSION:
        if fingerprint != regression_fingerprint(symptom):
            raise ValueError("regression fingerprint does not match its normalized defect")
    else:
        gate_digest = _digest_value(
            mapping["gate_contract_digest"], "gate_contract_digest"
        )
        evidence_digest = _digest_value(mapping["evidence_digest"], "evidence_digest")
        if fingerprint != candidate_id_for(symptom, gate_digest, evidence_digest):
            raise CandidateIdentityMismatch(
                "regression fingerprint does not match its v2 candidate identity"
            )
    defect_class = mapping["defect_class"]
    if defect_class != _defect_class(symptom):
        raise ValueError("regression defect class is inconsistent")
    count = mapping["recurrence_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("recurrence_count must be a positive integer")
    status = mapping["status"]
    if status not in _STATUSES:
        raise ValueError("regression status is unsupported")
    common = (
        fingerprint,
        defect_class,
        symptom,
        _receipt_ref(
            project_root,
            mapping["first_seen_receipt_ref"],
            "first_seen_receipt_ref",
        ),
        _receipt_ref(
            project_root,
            mapping["last_seen_receipt_ref"],
            "last_seen_receipt_ref",
        ),
        count,
        _assets(project_root, mapping["permanent_assets"], require_exists=False),
        _code(mapping["owner"], "owner"),
        status,
        _code(mapping["next_gate"], "next_gate"),
    )
    if version == REGRESSION_SCHEMA_VERSION:
        return RegressionRecord(REGRESSION_SCHEMA_VERSION, *common)
    return RegressionRecordV2(
        REGRESSION_V2_SCHEMA_VERSION,
        common[0],
        common[1],
        common[2],
        gate_digest,
        evidence_digest,
        *common[3:],
    )


def regression_record_bytes(record: RegressionRecord | RegressionRecordV2) -> bytes:
    if not isinstance(record, (RegressionRecord, RegressionRecordV2)):
        raise TypeError("record must be a regression record")
    return canonical_json_bytes(record)


def _source_proposal(receipt: object) -> tuple[Mapping[str, Any], object]:
    if getattr(receipt, "schema_version", None) != "1.0" or getattr(
        receipt, "command", None
    ) != "check":
        raise ValueError("source receipt is not a canonical check receipt")
    outputs = _mapping(getattr(receipt, "outputs", None), "source receipt outputs")
    decision = _mapping(outputs.get(FEEDBACK_LOOP_DECISION_OUTPUT_KEY), "feedback loop decision")
    proposal = decision.get("proposed_regression_delta")
    return outputs, proposal


def _proposal_symptoms(receipt: object) -> tuple[str, ...]:
    _, proposal = _source_proposal(receipt)
    parsed = parse_regression_proposal(proposal)
    if not parsed.legacy or parsed.status != "candidate":
        raise ValueError("source receipt has no regression candidate")
    return tuple(_code(item, "proposed symptom_code") for item in parsed.symptom_codes)


def _source_candidate(receipt: object, candidate_id: str) -> RegressionCandidate:
    outputs, proposal = _source_proposal(receipt)
    parsed = parse_regression_proposal(proposal)
    if parsed.legacy or parsed.status != "candidate":
        raise ValueError("v2 regression update requires a v2 source candidate")
    selected = tuple(item for item in parsed.candidates if item.candidate_id == candidate_id)
    if len(selected) != 1:
        raise CandidateSourceMismatch("candidate_id is not uniquely proposed by the source receipt")
    exit_code = outputs.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code < 0:
        raise ValueError("source receipt has an invalid exit code")
    governance_refs = outputs.get("changed_paths", ())
    if isinstance(governance_refs, (str, bytes)) or not isinstance(
        governance_refs, Sequence
    ):
        raise TypeError("source receipt changed_paths must be an array")
    validate_proposal_against_receipt(
        proposal,
        getattr(receipt, "checks", ()),
        exit_code=exit_code,
        governance_evidence_refs=governance_refs,
        gate_contracts=outputs.get(REGRESSION_GATE_CONTRACTS_OUTPUT_KEY),
    )
    return selected[0]


def _consistent(record: RegressionRecord | RegressionRecordV2) -> bool:
    if record.status == "closed":
        return bool(record.permanent_assets) and record.next_gate == "closed"
    return record.next_gate != "closed"


def prepare_regression_update(root: str | Path, value: object) -> PreparedRegressionUpdate:
    project_root = Path(root).resolve(strict=True)
    mapping = _mapping(value, REGRESSION_UPDATE_KEY)
    version = mapping.get("extension_schema_version")
    if version == REGRESSION_SCHEMA_VERSION:
        _closed(mapping, _UPDATE_FIELDS_V1, REGRESSION_UPDATE_KEY)
    elif version == REGRESSION_V2_SCHEMA_VERSION:
        _closed(mapping, _UPDATE_FIELDS_V2, REGRESSION_UPDATE_KEY)
    else:
        raise UnsupportedRegressionSchema("unsupported regression update schema version")
    source_ref = _receipt_ref(project_root, mapping["source_receipt_ref"], "source_receipt_ref")
    source_path = project_root / source_ref
    if not source_path.is_file():
        raise ValueError("source check receipt does not exist")
    try:
        source_receipt = load_receipt_json(source_path.read_bytes(), require_canonical=True)
    except (OSError, SchemaError) as error:
        raise ValueError("source check receipt is malformed") from error
    selected_candidate: RegressionCandidate | None = None
    if version == REGRESSION_SCHEMA_VERSION:
        symptom = _code(mapping["symptom_code"], "symptom_code")
        if symptom not in _proposal_symptoms(source_receipt):
            raise ValueError("selected symptom_code is not proposed by the source receipt")
        fingerprint = regression_fingerprint(symptom)
        gate_digest = ""
        evidence_digest = ""
    else:
        candidate_id = _digest_value(mapping["candidate_id"], "candidate_id")
        selected_candidate = _source_candidate(source_receipt, candidate_id)
        symptom = selected_candidate.symptom_code
        fingerprint = selected_candidate.candidate_id
        gate_digest = selected_candidate.gate_contract_digest
        evidence_digest = selected_candidate.evidence_digest
    owner = _code(mapping["owner"], "owner")
    status = mapping["status"]
    if status not in _STATUSES:
        raise ValueError("regression status is unsupported")
    next_gate = _code(mapping["next_gate"], "next_gate")
    requested_assets = _assets(project_root, mapping["permanent_assets"], require_exists=True)
    relative = regression_record_path(fingerprint)
    existing_path = project_root / relative
    preexisting = existing_path.is_file()
    if preexisting:
        try:
            existing_payload = existing_path.read_bytes()
            existing = parse_regression_record(
                json.loads(existing_payload.decode("utf-8")), project_root
            )
        except (OSError, ValueError, TypeError) as error:
            raise ValueError("existing regression record is malformed") from error
        if regression_record_bytes(existing) != existing_payload:
            raise ValueError("existing regression record is not canonical")
        if version == REGRESSION_SCHEMA_VERSION and not isinstance(existing, RegressionRecord):
            raise ValueError("v1 update cannot modify a v2 regression record")
        if version == REGRESSION_V2_SCHEMA_VERSION and not isinstance(
            existing, RegressionRecordV2
        ):
            raise ValueError("v2 update cannot modify a v1 regression record")
        if not _consistent(existing):
            raise ValueError("existing regression record has inconsistent status")
        merged_assets = tuple(
            sorted(
                set(existing.permanent_assets) | set(requested_assets),
                key=lambda item: (item.type, item.path),
            )
        )
        if source_ref == existing.last_seen_receipt_ref:
            if (
                owner != existing.owner
                or status != existing.status
                or next_gate != existing.next_gate
                or merged_assets != existing.permanent_assets
            ):
                raise ValueError("duplicate source receipt cannot change a regression record")
            record = existing
        else:
            if isinstance(existing, RegressionRecordV2):
                record = RegressionRecordV2(
                    REGRESSION_V2_SCHEMA_VERSION,
                    fingerprint,
                    existing.defect_class,
                    symptom,
                    gate_digest,
                    evidence_digest,
                    existing.first_seen_receipt_ref,
                    source_ref,
                    existing.recurrence_count + 1,
                    merged_assets,
                    owner,
                    status,
                    next_gate,
                )
            else:
                record = RegressionRecord(
                    REGRESSION_SCHEMA_VERSION,
                    fingerprint,
                    existing.defect_class,
                    symptom,
                    existing.first_seen_receipt_ref,
                    source_ref,
                    existing.recurrence_count + 1,
                    merged_assets,
                    owner,
                    status,
                    next_gate,
                )
    else:
        if selected_candidate is not None:
            record = RegressionRecordV2(
                REGRESSION_V2_SCHEMA_VERSION,
                fingerprint,
                _defect_class(symptom),
                symptom,
                gate_digest,
                evidence_digest,
                source_ref,
                source_ref,
                1,
                requested_assets,
                owner,
                status,
                next_gate,
            )
        else:
            record = RegressionRecord(
                REGRESSION_SCHEMA_VERSION,
                fingerprint,
                _defect_class(symptom),
                symptom,
                source_ref,
                source_ref,
                1,
                requested_assets,
                owner,
                status,
                next_gate,
            )
    if not _consistent(record):
        raise ValueError("closed regressions require a permanent asset and next_gate=closed")
    return PreparedRegressionUpdate(
        record, relative, regression_record_bytes(record), preexisting
    )


def _receipt_timestamp(receipt: object) -> datetime:
    stamp = getattr(receipt, "timestamp_utc", None)
    if type(stamp) is not str:
        raise ValueError("receipt timestamp is missing")
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("receipt timestamp is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _source_coherence_errors(
    record: RegressionRecord | RegressionRecordV2,
    receipt: object,
    relative: str,
) -> tuple[str, ...]:
    if isinstance(record, RegressionRecord):
        try:
            if record.symptom_code not in _proposal_symptoms(receipt):
                return (f"source-symptom-mismatch:{relative}",)
        except (TypeError, ValueError):
            return (f"source-symptom-mismatch:{relative}",)
        return ()
    try:
        candidate = _source_candidate(receipt, record.fingerprint)
    except (GateContractDigestMismatch, GateContractMismatch):
        return (f"gate-contract-digest-mismatch:{relative}",)
    except EvidenceDigestMismatch:
        return (f"evidence-digest-mismatch:{relative}",)
    except (CandidateIdentityMismatch, CandidateSourceMismatch, TypeError, ValueError):
        return (f"candidate-source-mismatch:{relative}",)
    errors = []
    if candidate.gate_contract_digest != record.gate_contract_digest:
        errors.append(f"gate-contract-digest-mismatch:{relative}")
    if candidate.evidence_digest != record.evidence_digest:
        errors.append(f"evidence-digest-mismatch:{relative}")
    if candidate.symptom_code != record.symptom_code:
        errors.append(f"candidate-source-mismatch:{relative}")
    return tuple(errors)


def diagnose_regressions(
    root: str | Path,
    *,
    now: datetime | None = None,
    stale_after_days: int = 90,
) -> RegressionDiagnostics:
    project_root = Path(root).resolve(strict=True)
    directory = project_root / ".governance" / "regressions"
    if not directory.exists():
        return RegressionDiagnostics(0, (), ())
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    errors: list[str] = []
    warnings: list[str] = []
    records = 0
    for path in sorted(directory.iterdir()):
        relative = path.relative_to(project_root).as_posix()
        if not path.is_file() or path.suffix != ".json":
            errors.append(f"malformed:{relative}")
            continue
        records += 1
        try:
            payload = path.read_bytes()
            value = json.loads(payload.decode("utf-8"))
            record = parse_regression_record(value, project_root)
        except UnsupportedRegressionSchema:
            errors.append(f"unsupported-schema:{relative}")
            continue
        except CandidateIdentityMismatch:
            errors.append(f"candidate-fingerprint-mismatch:{relative}")
            continue
        except (OSError, ValueError, TypeError):
            errors.append(f"malformed:{relative}")
            continue
        if canonical_json_bytes(record) != payload:
            errors.append(f"noncanonical-record:{relative}")
        if path.stem != record.fingerprint:
            errors.append(f"filename-mismatch:{relative}")
        if not _consistent(record):
            errors.append(f"inconsistent-status:{relative}")
        source_receipts: dict[str, object] = {}
        for receipt_ref in {
            record.first_seen_receipt_ref,
            record.last_seen_receipt_ref,
        }:
            receipt_path = project_root / receipt_ref
            if not receipt_path.is_file():
                errors.append(f"missing-receipt:{relative}:{receipt_ref}")
                continue
            try:
                source_receipts[receipt_ref] = load_receipt_json(
                    receipt_path.read_bytes(), require_canonical=True
                )
            except SchemaError as error:
                code = (
                    "noncanonical-receipt"
                    if "not canonical" in str(error)
                    else "malformed-receipt"
                )
                errors.append(f"{code}:{relative}:{receipt_ref}")
            except OSError:
                errors.append(f"malformed-receipt:{relative}:{receipt_ref}")
        for source_receipt in source_receipts.values():
            errors.extend(_source_coherence_errors(record, source_receipt, relative))
        last_receipt = source_receipts.get(record.last_seen_receipt_ref)
        if last_receipt is not None:
            try:
                age_days = (current - _receipt_timestamp(last_receipt)).total_seconds() / 86400
                if age_days > stale_after_days:
                    warnings.append(f"stale-evidence:{relative}")
            except (ValueError, TypeError):
                errors.append(f"malformed-receipt:{relative}:{record.last_seen_receipt_ref}")
        for asset in record.permanent_assets:
            if not (project_root / asset.path).is_file():
                errors.append(f"missing-asset:{relative}:{asset.path}")
    return RegressionDiagnostics(
        records, tuple(sorted(set(errors))), tuple(sorted(set(warnings)))
    )


__all__ = [
    "PermanentAsset",
    "PreparedRegressionUpdate",
    "REGRESSION_SCHEMA_VERSION",
    "REGRESSION_V2_SCHEMA_VERSION",
    "REGRESSION_UPDATE_KEY",
    "RegressionDiagnostics",
    "RegressionRecord",
    "RegressionRecordV2",
    "UnsupportedRegressionSchema",
    "diagnose_regressions",
    "parse_regression_record",
    "prepare_regression_update",
    "regression_fingerprint",
    "regression_record_bytes",
    "regression_record_path",
]
