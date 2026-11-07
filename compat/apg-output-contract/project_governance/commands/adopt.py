"""Canonical additive project adoption."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Mapping
from ..adapters import AdapterValidationError, format_command, merge_adapter_plan, render_adapter_plans
from ..audit_contract import snapshot_for_audit_receipt
from ..baseline import dump_baseline, load_baseline
from ..gates import GateDefinition
from ..model import GovernanceLevel, Policy, ProjectProfile, Receipt
from ..path_guard import PathViolation, WorkspaceGuard, WorkspaceTransaction
from ..policy import resolve_policy_input
from ..receipts import build_receipt, receipt_digest
from ..storage import canonical_json_bytes, digest, dump_project_toml
from ..templates import load_template, render_template

@dataclass(frozen=True)
class AdoptResult:
    ok: bool
    message: str
    planned_paths: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    receipt: Any = None

def _fail(message: str) -> AdoptResult:
    return AdoptResult(False, message)

def _approval(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"id", "actor", "role", "timestamp_utc", "scope"}:
        raise ValueError("adoption approval must be a structured mapping")
    item = dict(value)
    if any(type(item[key]) is not str or not item[key].strip() for key in ("id", "actor", "role")):
        raise ValueError("adoption approval requires id, actor, and role")
    try:
        stamp = datetime.fromisoformat(item["timestamp_utc"].replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("adoption approval timestamp is invalid") from error
    if stamp.tzinfo is None or stamp.utcoffset() != timezone.utc.utcoffset(stamp) or stamp > datetime.now(timezone.utc):
        raise ValueError("adoption approval timestamp must be current or past UTC")
    scope = item["scope"]
    if isinstance(scope, str) or not isinstance(scope, (list, tuple)) or not scope:
        raise ValueError("adoption approval scope is required")
    normalized = []
    for path in scope:
        if type(path) is not str or not path.strip():
            raise ValueError("adoption approval scope must contain strings")
        value = path.replace("\\", "/")
        if value != "." and (Path(value).is_absolute() or ".." in Path(value).parts):
            raise ValueError("adoption approval scope must be project-relative")
        normalized.append(value.rstrip("/") or ".")
    item["scope"] = tuple(sorted(set(normalized)))
    return item

def _scope_allows(path: str, scopes: tuple[str, ...]) -> bool:
    return any(scope == "." or path == scope or path.startswith(scope + "/") for scope in scopes)

def _profile(root: Path, value: object) -> ProjectProfile:
    if isinstance(value, ProjectProfile):
        if Path(value.root).resolve() != root.resolve():
            raise ValueError("audit profile root does not match target")
        return value
    if not isinstance(value, Mapping):
        raise ValueError("audit receipt profile is missing")
    fields = {"project_id", "root", "project_types", "lifecycle", "public_surfaces", "data_risk", "user_exposure", "release_model", "test_burden", "operational_dependencies", "owners", "evidence_refs"}
    if set(value) != fields:
        raise ValueError("audit profile is not canonical")
    data = dict(value)
    data["root"] = str(root)
    return ProjectProfile(**{key: tuple(value) if key in {"project_types", "public_surfaces", "operational_dependencies", "owners", "evidence_refs"} else value for key, value in data.items()})

def _policy(value: object, level: object, documents: object) -> Policy:
    if isinstance(value, Policy):
        return value
    data = dict(value) if isinstance(value, Mapping) else {}
    try:
        resolved_level = GovernanceLevel(str(data.get("level", level)))
    except ValueError as error:
        raise ValueError("audit policy level is invalid") from error
    return Policy("1.0", "0.1.0", resolved_level, tuple(data.get("reasons", ())), tuple(data.get("required_documents", documents or ())), tuple(data.get("adapters", ("codex",))), tuple(data.get("gates", ())), tuple(data.get("non_baselinable_rules", ())))


def _gate_inventory(gates: tuple[GateDefinition, ...], phase: str | None = None) -> str:
    rows: list[str] = []
    for gate in sorted(gates, key=lambda item: (item.phase, item.gate_id)):
        if phase is not None and gate.phase != phase:
            continue
        if gate.kind == "command":
            rows.append(f"- {gate.gate_id}: `{format_command(gate.command)}` (.governance/policy.toml)")
        else:
            rows.append(f"- {gate.gate_id}: {gate.kind} (.governance/policy.toml)")
    return "\n".join(rows) or "- Confirm the project command with evidence."


def _gate_argv_summary(gates: tuple[GateDefinition, ...]) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "gate_id": gate.gate_id,
            "phase": gate.phase,
            "kind": gate.kind,
            "argument_count": len(gate.command),
        }
        for gate in sorted(gates, key=lambda item: (item.phase, item.gate_id))
    )


def _render_documents(profile: ProjectProfile, policy: Policy, gates: tuple[GateDefinition, ...]) -> dict[str, bytes]:
    evidence = "\n".join(f"- `{item}`" for item in profile.evidence_refs) or "- No discovered evidence; confirm project facts explicitly."
    values = {
        "project_types": ", ".join(profile.project_types) or "unknown",
        "public_surfaces": ", ".join(profile.public_surfaces) or "unknown",
        "operational_dependencies": ", ".join(profile.operational_dependencies) or "none discovered",
        "evidence": evidence,
        "level": policy.level.value,
        "level_reasons": "; ".join(policy.reasons),
        "fast_commands": _gate_inventory(gates, "fast"),
        "full_commands": _gate_inventory(gates, "full"),
        "release_commands": _gate_inventory(gates, "release"),
        "native_commands": _gate_inventory(gates),
    }
    files: dict[str, bytes] = {}
    for document in policy.required_documents:
        if document == "docs/decisions/":
            path = "docs/decisions/0000-governance-adoption.md"
            template = load_template("decision.md.tmpl")
        else:
            path = document.rstrip("/")
            template = load_template(Path(path).name + ".tmpl")
        names = set(re.findall(r"{{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*}}", template))
        files[path] = render_template(template, {name: values[name] for name in names}).encode("utf-8")
    return files

def run_adopt(
    target: str | Path,
    audit_receipt: Receipt,
    *,
    approval: Mapping[str, Any] | None = None,
    audit_digest: str | None = None,
    policy_file: str | Path | None = None,
    apply: bool = False,
    structural_migration: bool = False,
    max_age_days: int = 30,
    future_skew_seconds: int = 60,
) -> AdoptResult:
    if structural_migration:
        return _fail("structural migration is not permitted by adopt")
    try:
        guard = WorkspaceGuard(Path(target))
        if not isinstance(audit_receipt, Receipt) or audit_receipt.command != "audit":
            return _fail("an audit receipt is required")
        canonical_digest = receipt_digest(audit_receipt)
        if audit_digest != canonical_digest:
            return _fail("caller-supplied audit digest does not match receipt_digest")
        if approval is None:
            return _fail("structured project adoption approval is required")
        approval_value = _approval(approval)
        if approval_value["role"] != "owner":
            return _fail("project adoption approval requires owner role")
        timestamp = datetime.fromisoformat(audit_receipt.timestamp_utc.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if timestamp.tzinfo is None or timestamp > now + __import__('datetime').timedelta(seconds=future_skew_seconds):
            return _fail("audit receipt timestamp is in the future")
        if (now - timestamp.astimezone(timezone.utc)).total_seconds() > max_age_days * 86400:
            return _fail("audit receipt is expired")
        proof = audit_receipt.outputs.get("read_only_proof", {})
        if not isinstance(proof, Mapping):
            return _fail("audit receipt proof is invalid")
        if proof.get("passed") is not True or proof.get("changed_paths"):
            return _fail("audit receipt lacks a passing read-only proof")
        proof_snapshot = snapshot_for_audit_receipt(guard, proof)
        if audit_receipt.target_fingerprint != digest(proof_snapshot):
            return _fail("audit receipt target fingerprint does not match target")
        profile = _profile(guard.root, audit_receipt.outputs.get("profile"))
        audit_level = audit_receipt.outputs.get("level", "G1")
        audit_documents = audit_receipt.outputs.get("required_documents", ())
        resolved = resolve_policy_input(
            policy_file,
            fallback_policy=_policy(
                audit_receipt.outputs.get("policy", {}),
                audit_level,
                audit_documents,
            ),
            fallback_status="embedded",
            audit_level=audit_level,
            required_documents=audit_documents,
        )
        policy = resolved.policy
        gate_definitions = resolved.gates
        baseline_value = audit_receipt.outputs.get("baseline", {"schema_version": "1.0", "entries": []})
        baseline_text = baseline_value if isinstance(baseline_value, str) else canonical_json_bytes(baseline_value).decode()
        baseline_text = dump_baseline(load_baseline(baseline_text))
        files: dict[str, bytes] = {
            ".governance/project.toml": dump_project_toml(profile).encode(),
            ".governance/policy.toml": resolved.canonical_bytes,
            ".governance/baseline.json": baseline_text.encode(),
            ".governance/audit-receipt.json": canonical_json_bytes(audit_receipt),
            ".governance/adoption.json": canonical_json_bytes({"audit_digest": canonical_digest, "approval": approval_value, "structural_migration": False}),
        }
        files.update(_render_documents(profile, policy, gate_definitions))
        validation_commands = tuple(dict.fromkeys(
            format_command(gate.command)
            for gate in gate_definitions
            if gate.kind == "command"
        ))
        adapter_ids = policy.adapters or ("codex",)
        adapter_plans = render_adapter_plans(
            policy_version=policy.policy_version,
            policy_digest=resolved.policy_digest,
            project_root=".",
            validation_commands=validation_commands,
            adapters=adapter_ids,
            approved_adapters=adapter_ids,
        )
        adapter_conflicts: set[str] = set()
        for plan in adapter_plans.values():
            if plan.adapter_id in {"git", "github"} and plan.adapter_id not in adapter_ids:
                continue
            rendered = plan.rendered_content
            target = guard.resolve_write(plan.target_relative_path)
            if target.exists():
                try:
                    rendered = merge_adapter_plan(
                        target.read_text(encoding="utf-8"),
                        plan,
                    )
                except (AdapterValidationError, UnicodeDecodeError):
                    adapter_conflicts.add(plan.target_relative_path)
            files[plan.target_relative_path] = rendered.encode()
        unauthorized = tuple(sorted(path for path in files if not _scope_allows(path, approval_value["scope"])))
        if unauthorized:
            return _fail("adoption approval scope does not cover all planned paths")
        adapter_paths = {
            plan.target_relative_path for plan in adapter_plans.values()
        }
        conflicts = tuple(sorted(adapter_conflicts | {
            path
            for path, content in files.items()
            if path not in adapter_paths
            and guard.resolve_write(path).exists()
            and guard.resolve_write(path).read_bytes() != content
        }))
        receipt_scope = tuple(sorted(files)) + (".governance/receipts",)
        receipt = build_receipt(
            command="adopt",
            policy_digest=resolved.policy_digest,
            target_fingerprint=audit_receipt.target_fingerprint,
            authorized_scope=receipt_scope,
            inputs={
                "audit_digest": canonical_digest,
                "approval": approval_value,
                "policy_input_status": resolved.input_status,
            },
            outputs={
                "conflicts": conflicts,
                "policy_input_status": resolved.input_status,
                "policy_digest": resolved.policy_digest,
                "gate_ids": tuple(sorted(gate.gate_id for gate in gate_definitions)),
                "gate_argv_summary": _gate_argv_summary(gate_definitions),
                "planned_documents": policy.required_documents,
                "adapters": tuple(sorted(adapter_plans)),
                "rollback": "restore transaction checkpoint",
            },
            approvals=(approval_value["id"],),
            classification="adopt",
        )
        receipt_path = f".governance/receipts/{receipt.timestamp_utc.replace(':', '').replace('-', '')}-adopt-{receipt_digest(receipt)[:12]}.json"
        files[receipt_path] = canonical_json_bytes(receipt)
        if conflicts:
            return AdoptResult(False, "unresolved adoption conflicts", tuple(sorted(files)), (), conflicts, receipt)
        transaction = WorkspaceTransaction(guard, tuple(sorted(files)), apply=apply)
        for path, content in sorted(files.items()):
            transaction.stage_bytes(path, content)
        committed = transaction.commit()
        return AdoptResult(True, "adoption planned" if not apply else "adopted", committed.planned_paths, committed.changed_paths, (), receipt)
    except (OSError, ValueError, TypeError, PathViolation) as error:
        return _fail(str(error))

def json_dumps(value: object) -> str:
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"

__all__ = ["AdoptResult", "run_adopt"]
