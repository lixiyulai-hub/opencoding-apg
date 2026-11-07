from __future__ import annotations

import os
import platform
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..audit_contract import audit_proof_contract, snapshot_for_audit
from ..discovery import DiscoveryResult, discover_project
from ..model import CheckResult, CheckStatus, Finding, Receipt
from ..path_guard import PathViolation, WorkspaceGuard
from ..policy import select_documents, select_level
from ..receipts import build_receipt, receipt_digest
from ..storage import canonical_json_bytes, digest


def _read_only_proof(
    passed: bool,
    changed_paths: tuple[str, ...],
) -> dict[str, object]:
    return {
        "passed": passed,
        "changed_paths": changed_paths,
        **audit_proof_contract(),
    }


@dataclass(frozen=True)
class AuditOutcome:
    exit_code: int
    receipt: Receipt
    receipt_path: Path | None = None


def _paths_overlap(left: Path, right: Path) -> bool:
    return (
        left == right
        or left.is_relative_to(right)
        or right.is_relative_to(left)
    )


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        info = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse)


def _reject_link_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    for component in (absolute, *absolute.parents):
        if os.path.lexists(component) and _is_link_or_reparse(component):
            raise PathViolation(
                "receipt directory contains a symlink or reparse component"
            )


def _resolve_receipt_dir(target: Path, value: str | Path) -> Path:
    requested = Path(value)
    if ".." in requested.parts:
        raise PathViolation("receipt directory contains parent traversal")
    absolute = Path(os.path.abspath(requested))
    _reject_link_components(absolute)
    try:
        resolved = absolute.resolve(strict=False)
    except OSError as error:
        raise PathViolation("receipt directory cannot be resolved") from error
    if _paths_overlap(target, resolved):
        raise PathViolation("receipt directory overlaps audited target")
    return absolute


def _read_only_check(passed: bool, changed_paths: tuple[str, ...]) -> CheckResult:
    return CheckResult(
        gate_id="audit.read-only",
        phase="audit",
        status=CheckStatus.PASS if passed else CheckStatus.FAIL,
        message=(
            "target remained unchanged"
            if passed
            else f"target changed: {', '.join(changed_paths)}"
        ),
        evidence_refs=changed_paths,
    )


def _error_outcome(
    *,
    exit_code: int,
    classification: str,
    message: str,
    fingerprint: str = "",
    changed_paths: tuple[str, ...] = (),
    findings: Iterable[Finding] = (),
) -> AuditOutcome:
    passed = not changed_paths
    receipt = build_receipt(
        command="audit",
        target_fingerprint=fingerprint,
        authorized_scope=(".",),
        inputs={"target": "."},
        outputs={
            "error": message,
            "changed_paths": changed_paths,
            "read_only_proof": _read_only_proof(passed, changed_paths),
        },
        findings=tuple(findings),
        checks=(_read_only_check(passed, changed_paths),),
        classification=classification,
        evidence_refs=changed_paths,
    )
    return AuditOutcome(exit_code, receipt, None)


def _scope_violation(
    fingerprint: str,
    changed_paths: tuple[str, ...],
    findings: Iterable[Finding] = (),
) -> AuditOutcome:
    return _error_outcome(
        exit_code=4,
        classification="scope-violation",
        message="audit changed the target workspace",
        fingerprint=fingerprint,
        changed_paths=changed_paths,
        findings=findings,
    )


def _is_inconclusive(result: DiscoveryResult) -> bool:
    return result.truncated or any(
        finding.rule_id in {"discovery.inaccessible", "discovery.truncated"}
        for finding in result.findings
    )


def _audit_receipt(
    result: DiscoveryResult,
    fingerprint: str,
    *,
    classification: str,
) -> Receipt:
    decision = select_level(result.profile)
    documents = select_documents(result.profile, level=decision.level)
    inconclusive = classification == "inconclusive"
    discovery_status = (
        CheckStatus.INCONCLUSIVE
        if inconclusive
        else CheckStatus.WARN
        if result.findings or decision.warnings
        else CheckStatus.PASS
    )
    commands = tuple(
        {
            "name": item.name,
            "source": item.source,
            "argument_count": len(item.command),
        }
        for item in result.commands
    )
    evidence_refs = tuple(
        sorted(
            {item.source for item in result.evidence}
            | set(result.profile.evidence_refs)
        )
    )
    return build_receipt(
        command="audit",
        target_fingerprint=fingerprint,
        actor="controller",
        authorized_scope=(".",),
        inputs={
            "target": ".",
            "environment_summary": {
                "platform": sys.platform,
                "python": platform.python_version(),
            },
        },
        outputs={
            "profile": result.profile,
            "level": decision.level.value,
            "level_reasons": decision.reasons,
            "level_warnings": decision.warnings,
            "required_documents": documents,
            "commands": commands,
            "truncated": result.truncated,
            "changed_paths": (),
            "read_only_proof": _read_only_proof(True, ()),
        },
        findings=result.findings,
        checks=(
            CheckResult(
                gate_id="audit.discovery",
                phase="audit",
                status=discovery_status,
                message=(
                    "discovery evidence is incomplete"
                    if inconclusive
                    else "discovery completed with findings"
                    if result.findings
                    else "discovery completed"
                ),
                evidence_refs=evidence_refs,
            ),
            _read_only_check(True, ()),
        ),
        classification=classification,
        evidence_refs=evidence_refs,
    )


def _receipt_filename(receipt: Receipt) -> str:
    stamp = "".join(character for character in receipt.timestamp_utc if character.isalnum())
    return f"{stamp}-audit-{receipt_digest(receipt)[:12]}.json"


def _remove_owned_receipt(target: Path, payload: bytes) -> bool:
    try:
        if target.is_symlink() or not target.is_file():
            return False
        if target.read_bytes() != payload:
            return False
        target.unlink()
        return True
    except OSError:
        return False


def _write_receipt(directory: Path, receipt: Receipt) -> Path:
    _reject_link_components(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _reject_link_components(directory)
    resolved = directory.resolve(strict=True)
    _reject_link_components(directory)
    if not resolved.is_dir():
        raise PathViolation("receipt directory is not a directory")
    filename = _receipt_filename(receipt)
    guard = WorkspaceGuard(resolved)
    before = guard.snapshot()
    target = guard.resolve_write(filename)
    payload = canonical_json_bytes(receipt)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        _remove_owned_receipt(target, payload)
        raise
    changed = guard.changed_paths(before)
    if changed != (filename,):
        if not _remove_owned_receipt(target, payload):
            raise PathViolation(
                f"receipt verification failed; residue preserved: {target}"
            )
        raise PathViolation("receipt verification failed; owned file removed")
    return target


def run_audit(
    target: str | Path,
    receipt_dir: str | Path | None = None,
) -> AuditOutcome:
    try:
        guard = WorkspaceGuard(Path(target))
        before = snapshot_for_audit(guard)
    except (OSError, PathViolation, ValueError) as error:
        return _error_outcome(
            exit_code=2,
            classification="invalid",
            message=f"invalid audit target: {type(error).__name__}",
        )

    fingerprint = digest(before)
    external_dir: Path | None = None
    if receipt_dir is not None:
        try:
            external_dir = _resolve_receipt_dir(guard.root, receipt_dir)
        except PathViolation as error:
            return _error_outcome(
                exit_code=4,
                classification="scope-violation",
                message=str(error),
                fingerprint=fingerprint,
            )

    try:
        result = discover_project(guard.root)
    except Exception as error:
        changed = guard.changed_paths(before)
        if changed:
            return _scope_violation(fingerprint, changed)
        finding = Finding(
            rule_id="audit.discovery-error",
            category="discovery",
            severity="warning",
            confidence="high",
            path=".",
            message=type(error).__name__,
            evidence_refs=(),
            baselinable=False,
        )
        return _error_outcome(
            exit_code=3,
            classification="inconclusive",
            message="discovery could not complete",
            fingerprint=fingerprint,
            findings=(finding,),
        )

    changed = guard.changed_paths(before)
    if changed:
        return _scope_violation(fingerprint, changed, result.findings)

    inconclusive = _is_inconclusive(result)
    classification = (
        "inconclusive"
        if inconclusive
        else "warn"
        if result.findings
        else "pass"
    )
    exit_code = 3 if inconclusive else 0
    receipt = _audit_receipt(result, fingerprint, classification=classification)

    receipt_path: Path | None = None
    if external_dir is not None:
        try:
            _reject_link_components(external_dir)
            external_dir.mkdir(parents=True, exist_ok=True)
            _reject_link_components(external_dir)
            resolved_external = external_dir.resolve(strict=True)
            _reject_link_components(external_dir)
            if _paths_overlap(guard.root, resolved_external):
                raise PathViolation("receipt directory overlaps audited target")
            receipt_path = _write_receipt(external_dir, receipt)
        except (OSError, PathViolation) as error:
            return _error_outcome(
                exit_code=4,
                classification="scope-violation",
                message=f"receipt write rejected: {type(error).__name__}",
                fingerprint=fingerprint,
                findings=result.findings,
            )

    for _ in range(2):
        final_changed = guard.changed_paths(before)
        if final_changed:
            violation = _scope_violation(
                fingerprint,
                final_changed,
                result.findings,
            )
            corrective_path = receipt_path
            if external_dir is not None:
                try:
                    corrective_path = _write_receipt(
                        external_dir,
                        violation.receipt,
                    )
                except (OSError, PathViolation):
                    pass
            return AuditOutcome(4, violation.receipt, corrective_path)

    return AuditOutcome(exit_code, receipt, receipt_path)


__all__ = ["AuditOutcome", "run_audit"]
