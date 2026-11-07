"""Read-only canonical governance diagnostics."""
from __future__ import annotations
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
from ..architecture_graph import (
    ARCHITECTURE_GRAPH_RELATIVE_PATH,
    ArchitectureGraphError,
    load_architecture_graph,
)
from ..consistency_manifest import (
    CONSISTENCY_MANIFEST_RELATIVE_PATH,
    ConsistencyManifestError,
    evaluate_consistency_manifest,
    load_consistency_manifest,
)
from ..audit_contract import snapshot_for_audit
from ..adapters import AdapterState, format_command, render_adapter_plans, verify_adapter_plan
from ..baseline import load_baseline, validate_baseline
from ..gates import GateDefinition, parse_gate_definitions
from ..model import Receipt
from ..path_guard import PathViolation, WorkspaceGuard
from ..receipts import build_receipt, inspect_receipt_ledger
from ..regression_ledger import diagnose_regressions
from ..storage import (
    digest,
    dump_policy_toml,
    load_current_state_toml,
    load_policy_toml,
    load_project_toml,
)

_CURRENT_STATE_MAX_FILE_BYTES = 8 * 1024 * 1024

@dataclass(frozen=True)
class DoctorResult:
    ok: bool
    checks: tuple[dict[str, object], ...]
    message: str = ""
    exit_code: int = 0
    receipt: object = None

def _check(identifier: str, status: str, message: str, suggestion: str = "") -> dict[str, object]:
    result = {"id": identifier, "status": status, "message": message}
    if suggestion:
        result["suggestion"] = suggestion
    return result

def _document_exists(root: Path, document: str) -> bool:
    if document == "docs/decisions/":
        directory = root / document.rstrip("/")
        return directory.is_dir() and any(directory.glob("*.md"))
    return (root / document.rstrip("/")).exists()


def _current_state_diagnostic(
    guard: WorkspaceGuard,
    governance: Path,
    canonical_receipts: tuple[tuple[str, Path, Receipt], ...],
):
    current_state = governance / "current-state.md"
    latest_ref = canonical_receipts[-1][0] if canonical_receipts else ""
    summary: dict[str, object] = {
        "status": "absent",
        "source_receipt": "",
        "source_digest_match": False,
        "file_count": 0,
        "matched_files": 0,
        "latest_receipt_ref": latest_ref,
    }
    if not current_state.exists():
        return (
            _check(
                "current-state",
                "pass",
                "optional current-state projection is absent",
            ),
            summary,
        )
    try:
        resolved_current_state = current_state.resolve(strict=True)
        if current_state.is_symlink() or not resolved_current_state.is_relative_to(guard.root):
            raise OSError("current-state projection is not contained")
    except OSError:
        summary["status"] = "invalid"
        return (
            _check(
                "current-state",
                "warn",
                "current-state projection is not a contained regular file",
                "remove the unsafe projection and preserve canonical receipts",
            ),
            summary,
        )
    issues: list[str] = []
    try:
        projection = load_current_state_toml(current_state.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        summary["status"] = "invalid"
        return (
            _check(
                "current-state",
                "warn",
                f"current-state projection is invalid: {type(error).__name__}",
                "regenerate the non-authoritative projection from canonical receipts",
            ),
            summary,
        )
    summary["source_receipt"] = projection.source_receipt
    summary["file_count"] = len(projection.files)
    receipt_map = {reference: path for reference, path, _ in canonical_receipts}
    source_path = receipt_map.get(projection.source_receipt)
    if source_path is None:
        issues.append("source receipt is missing or non-canonical")
    else:
        source_match = digest(source_path.read_bytes()) == projection.source_receipt_sha256
        summary["source_digest_match"] = source_match
        if not source_match:
            issues.append("source receipt digest drift")
    if latest_ref and projection.source_receipt != latest_ref:
        issues.append("source receipt is not current")

    declared = {item.path: item.sha256 for item in projection.files}
    required = tuple(
        path
        for path in (
            ".governance/project.toml",
            ".governance/policy.toml",
            ".governance/baseline.json",
        )
        if (guard.root / path).is_file()
    )
    for path in required:
        if path not in declared:
            issues.append(f"required file digest is missing: {path}")
    matched = 0
    for path, expected in sorted(declared.items()):
        candidate = guard.root / path
        try:
            resolved = candidate.resolve(strict=True)
            if candidate.is_symlink() or not resolved.is_relative_to(guard.root):
                issues.append(f"file path is not contained: {path}")
                continue
            if not resolved.is_file():
                issues.append(f"file is missing: {path}")
                continue
            if resolved.stat().st_size > _CURRENT_STATE_MAX_FILE_BYTES:
                issues.append(f"file exceeds digest limit: {path}")
                continue
            if digest(resolved.read_bytes()) != expected:
                issues.append(f"file digest drift: {path}")
                continue
            matched += 1
        except OSError:
            issues.append(f"file is unavailable: {path}")
    summary["matched_files"] = matched
    summary["status"] = "warn" if issues else "current"
    return (
        _check(
            "current-state",
            "warn" if issues else "pass",
            "; ".join(issues) if issues else "current-state projection matches canonical evidence",
            "regenerate the non-authoritative projection from canonical receipts" if issues else "",
        ),
        summary,
    )

def run_doctor(target: str | Path) -> DoctorResult:
    try:
        guard = WorkspaceGuard(Path(target))
        before = snapshot_for_audit(guard)
    except (OSError, ValueError, PathViolation) as error:
        receipt = build_receipt(command="doctor", target_fingerprint="", authorized_scope=(".",), outputs={"read_only_proof": {"passed": False, "changed_paths": ()}, "checks": ({"id": "root-containment", "status": "fail"},)}, classification="invalid")
        return DoctorResult(False, (_check("root-containment", "fail", str(error)),), str(error), 2, receipt)
    checks: list[dict[str, object]] = [_check("root-containment", "pass", "target root is contained")]
    governance = guard.root / ".governance"
    profile = policy = None
    gate_definitions: tuple[GateDefinition, ...] = ()
    gate_error = ""
    project_path = governance / "project.toml"
    policy_path = governance / "policy.toml"
    for path, identifier, loader in ((project_path, "schema-support", load_project_toml), (policy_path, "schema-support", load_policy_toml)):
        if not path.exists():
            checks.append(_check(identifier + ("-project" if path == project_path else "-policy"), "warn", f"{path.name} is missing", "run init or adopt"))
            continue
        try:
            loaded = loader(path.read_text(encoding="utf-8"))
            if path == project_path:
                profile = loaded
            else:
                policy = loaded
            checks.append(_check(identifier + ("-project" if path == project_path else "-policy"), "pass", "schema is supported"))
        except Exception as error:
            checks.append(_check(identifier, "fail", f"schema invalid: {type(error).__name__}", "restore canonical schema"))
    if profile and policy and profile.project_id and policy.level:
        checks.append(_check("policy-profile-coherence", "pass", "policy and profile are coherent"))
    else:
        checks.append(_check("policy-profile-coherence", "fail", "policy/profile coherence cannot be established", "repair canonical project and policy"))
    if policy is None:
        checks.append(_check("quality-gates", "fail", "quality gates cannot be evaluated", "restore canonical policy"))
    else:
        try:
            gate_definitions = parse_gate_definitions(policy.gates)
        except (TypeError, ValueError) as error:
            gate_error = f"{type(error).__name__}: {error}"
        if gate_error:
            checks.append(_check("quality-gates", "fail", f"quality gate definitions are invalid: {gate_error}", "repair canonical gate mappings"))
        elif policy.level.value in {"G2", "G3", "G4"} and not gate_definitions:
            checks.append(_check("quality-gates", "fail", f"{policy.level.value} policy requires at least one quality gate", "configure a bounded project-native gate"))
        else:
            checks.append(_check("quality-gates", "pass", "quality gates match the governance level"))
    try:
        architecture_graph = load_architecture_graph(
            guard.root,
            policy_gate_ids=tuple(gate.gate_id for gate in gate_definitions),
        )
    except ArchitectureGraphError as error:
        checks.append(
            _check(
                "architecture-graph",
                "fail",
                f"architecture graph is invalid: {error}",
                "repair the graph through an approved plan-change; doctor is read-only",
            )
        )
    else:
        if architecture_graph is not None:
            graph_summary = (
                f"architecture graph {architecture_graph.digest} is canonical "
                f"(nodes={architecture_graph.node_count}, "
                f"edges={architecture_graph.edge_count}, "
                f"cycles={architecture_graph.cycle_count})"
            )
            if architecture_graph.unknown_gate_ids:
                checks.append(
                    _check(
                        "architecture-graph",
                        "fail",
                        graph_summary
                        + "; Gate IDs absent from current policy: "
                        + ", ".join(architecture_graph.unknown_gate_ids),
                        "repair graph Gate references through an approved plan-change",
                    )
                )
            elif architecture_graph.has_cycle:
                checks.append(
                    _check(
                        "architecture-graph",
                        "warn",
                        graph_summary + "; dependency cycles require conservative full fallback",
                        "review graph cycles through an approved plan-change",
                    )
                )
            else:
                checks.append(_check("architecture-graph", "pass", graph_summary))
    try:
        consistency_manifest = load_consistency_manifest(guard.root)
        consistency_evaluation = (
            None
            if consistency_manifest is None
            else evaluate_consistency_manifest(guard.root, consistency_manifest)
        )
    except ConsistencyManifestError as error:
        checks.append(
            _check(
                "consistency-manifest",
                "fail",
                f"consistency manifest is invalid: {error}",
                "repair the manifest or declared files through an approved plan-change; doctor is read-only",
            )
        )
    else:
        if consistency_manifest is not None and consistency_evaluation is not None:
            summary = (
                f"consistency manifest {consistency_manifest.digest} is canonical "
                f"(relationships={consistency_manifest.relationship_count}, "
                f"members={consistency_manifest.member_count}, "
                f"pass={consistency_evaluation.pass_count}, "
                f"missing={consistency_evaluation.missing_count}, "
                f"drift={consistency_evaluation.drift_count})"
            )
            if consistency_evaluation.status == "pass":
                checks.append(_check("consistency-manifest", "pass", summary))
            else:
                checks.append(
                    _check(
                        "consistency-manifest",
                        "fail",
                        summary
                        + "; failing relationships: "
                        + ", ".join(consistency_evaluation.failing_relationship_ids),
                        "repair declared files through an approved plan-change; doctor is read-only",
                    )
                )
    adapter_failures = []
    if policy and not gate_error:
        try:
            policy_digest = digest(dump_policy_toml(policy).encode())
            validation_commands = tuple(dict.fromkeys(
                format_command(gate.command)
                for gate in gate_definitions
                if gate.kind == "command"
            ))
            plans = render_adapter_plans(
                policy_version=policy.policy_version,
                policy_digest=policy_digest,
                project_root=".",
                validation_commands=validation_commands,
                adapters=policy.adapters or ("codex",),
                approved_adapters=policy.adapters,
            )
            for adapter_id, plan in plans.items():
                target = guard.root / plan.target_relative_path
                if not target.is_file():
                    adapter_failures.append(f"{adapter_id}:missing")
                    continue
                actual = target.read_text(encoding="utf-8")
                state = verify_adapter_plan(actual, plan)
                if state is not AdapterState.CURRENT:
                    adapter_failures.append(f"{adapter_id}:{state.value}")
        except (OSError, TypeError, ValueError) as error:
            adapter_failures.append(type(error).__name__)
    elif gate_error:
        adapter_failures.append("gate-definition:invalid")
    adapter_suggestion = (
        "repair canonical gate mappings before adapter diagnosis"
        if gate_error
        else "regenerate adapters from canonical policy" if adapter_failures else ""
    )
    checks.append(_check("adapter-drift", "fail" if adapter_failures else "pass", "adapter drift: " + ", ".join(adapter_failures) if adapter_failures else "managed adapters are current", adapter_suggestion))
    missing: list[str] = []
    tools = governance / "tools.json"
    if tools.exists():
        try:
            configured = json.loads(tools.read_text(encoding="utf-8"))
            missing = [str(item) for item in configured.values() if isinstance(item, str) and shutil.which(item) is None] if isinstance(configured, dict) else ["tools.json"]
        except (OSError, ValueError):
            missing = ["tools.json"]
    if policy and not gate_error:
        for gate in gate_definitions:
            if gate.command:
                executable = gate.command[0]
                candidate = Path(executable)
                available = candidate.is_file() if candidate.is_absolute() else shutil.which(executable) is not None
                if not available:
                    missing.append(executable)
    missing = sorted(set(missing))
    if gate_error:
        details = "; missing tools.json entries: " + ", ".join(missing) if missing else ""
        checks.append(_check("missing-tools", "fail", "configured gate tools cannot be evaluated until quality gate definitions are repaired" + details, "repair canonical gate mappings before tool diagnosis"))
    else:
        checks.append(_check("missing-tools", "fail" if missing else "pass", "missing configured tools: " + ", ".join(missing) if missing else "configured tools are available", "install tools or update tools.json" if missing else ""))
    baseline = governance / "baseline.json"
    if not baseline.exists():
        checks.append(_check("baseline", "warn", "baseline is missing", "run audit before enforcement"))
    else:
        try:
            baseline_entries = load_baseline(baseline.read_text(encoding="utf-8"))
            validate_baseline(baseline_entries)
            checks.append(_check("baseline", "pass", "baseline is current"))
        except (OSError, ValueError, TypeError) as error:
            message = str(error) if "expired baseline entry" in str(error) else "baseline is malformed"
            checks.append(_check("baseline", "fail", message, "restore canonical baseline JSON"))
    receipt_inventory = inspect_receipt_ledger(guard.root)
    invalid_receipts = receipt_inventory.invalid_filenames
    canonical_receipts = receipt_inventory.canonical_records
    receipt_summary = receipt_inventory.summary
    checks.append(
        _check(
            "receipts",
            "fail" if invalid_receipts else "pass",
            (
                "invalid receipt evidence: " + ", ".join(invalid_receipts)
                if invalid_receipts
                else (
                    "receipts are canonical and retained "
                    f"({receipt_summary['receipt_canonical']} records)"
                )
            ),
            "restore canonical receipt bytes without deleting history"
            if invalid_receipts
            else "",
        )
    )
    current_state_check, current_state_summary = _current_state_diagnostic(
        guard, governance, canonical_receipts
    )
    checks.append(current_state_check)
    regression_diagnostics = diagnose_regressions(guard.root)
    regression_issues = regression_diagnostics.errors + regression_diagnostics.warnings
    regression_status = "fail" if regression_diagnostics.errors else "warn" if regression_diagnostics.warnings else "pass"
    checks.append(_check(
        "regression-ledger",
        regression_status,
        "; ".join(regression_issues) if regression_issues else f"regression ledger is canonical ({regression_diagnostics.records} records)",
        "repair through an approved plan-change; doctor is read-only" if regression_issues else "",
    ))
    recovery = governance / ".recovery"
    interrupted = recovery.exists() and any(recovery.iterdir())
    checks.append(_check("interrupted-recovery", "fail" if interrupted else "pass", "interrupted transaction evidence exists" if interrupted else "no interrupted transaction evidence", "resolve recovery before changes" if interrupted else ""))
    allowed_governance = {"project.toml", "policy.toml", "baseline.json", "audit-receipt.json", "adoption.json", "tools.json", "receipts", "changes", "regressions", ".recovery", "current-state.md", Path(ARCHITECTURE_GRAPH_RELATIVE_PATH).name, Path(CONSISTENCY_MANIFEST_RELATIVE_PATH).name}
    generated = [path.relative_to(guard.root).as_posix() for path in governance.iterdir() if path.name not in allowed_governance] if governance.is_dir() else []
    checks.append(_check("generated-files", "warn" if generated else "pass", "unexpected generated files: " + ", ".join(generated) if generated else "no unexpected generated files", "remove or approve generated files" if generated else ""))
    documents_match = not policy or all(_document_exists(guard.root, document) for document in policy.required_documents)
    checks.append(_check("conditional-documents", "pass" if documents_match else "warn", "conditional documents match policy" if documents_match else "conditional documents do not match policy", "review required documents" if not documents_match else ""))
    changed = guard.changed_paths(before)
    if changed:
        checks.append(_check("scope", "fail", "doctor changed the target", "investigate read-only violation"))
    failures = [item for item in checks if item["status"] == "fail"]
    warnings = [item for item in checks if item["status"] in {"warn", "inconclusive"}]
    exit_code = 1 if failures else 0
    if any(item["id"] == "scope" for item in failures):
        exit_code = 4
    elif interrupted:
        exit_code = 5
    elif any(item["status"] == "inconclusive" for item in checks) and not failures:
        exit_code = 3
    receipt = build_receipt(command="doctor", policy_digest=digest(dump_policy_toml(policy).encode()) if policy else "", target_fingerprint=digest(before), authorized_scope=(".",), outputs={"checks": tuple(checks), "receipt_state": {**receipt_summary, "current_state": current_state_summary}, "read_only_proof": {"passed": not changed, "changed_paths": changed}}, classification="pass" if exit_code == 0 else "diagnostic")
    return DoctorResult(exit_code == 0, tuple(checks), "doctor completed", exit_code, receipt)

__all__ = ["DoctorResult", "run_doctor"]
