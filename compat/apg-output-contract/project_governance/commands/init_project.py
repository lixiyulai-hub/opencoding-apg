from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from pathlib import Path
from typing import Any, Mapping

from ..audit_contract import snapshot_for_audit
from ..adapters import format_command, render_adapter_plans
from ..discovery import DiscoveryResult, discover_project
from ..gates import GateDefinition
from ..model import Policy, Receipt
from ..receipts import build_receipt, receipt_digest
from ..path_guard import PathViolation, WorkspaceGuard, WorkspaceTransaction
from ..policy import resolve_policy_input, select_documents, select_level
from ..storage import canonical_json_bytes, digest, dump_project_toml
from ..templates import load_template, render_template


RECEIPT_LEDGER_SCOPE = ".governance/receipts"
_APPROVAL_KEYS = frozenset({"id", "actor", "role", "timestamp_utc", "scope"})


@dataclass(frozen=True)
class InitOutcome:
    exit_code: int
    payload: Mapping[str, object]
    receipt: Receipt


def _commands(result: DiscoveryResult, phase: str | None = None) -> str:
    commands = sorted(result.commands, key=lambda item: (item.name.lower(), item.name, item.command, item.source))
    if phase is not None:
        commands = [item for item in commands if item.name.lower() == phase]
    if not commands:
        return "- Confirm the project command with evidence."
    return "\n".join(
        f"- {item.name}: `{command_text}` ({item.source})"
        for item in commands
        for command_text in (" ".join(item.command),)
    )


def _policy(result: DiscoveryResult):
    decision = select_level(result.profile)
    documents = select_documents(result.profile, level=decision.level)
    return decision, documents


def _gate_inventory(gates: tuple[GateDefinition, ...], phase: str | None = None) -> str:
    rows: list[str] = []
    for gate in sorted(gates, key=lambda item: (item.phase, item.gate_id)):
        if phase is not None and gate.phase != phase:
            continue
        if gate.kind == "command":
            rows.append(
                f"- {gate.gate_id}: `{format_command(gate.command)}` "
                "(.governance/policy.toml)"
            )
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


def _rendered_files(
    result: DiscoveryResult,
    policy: Policy,
    gates: tuple[GateDefinition, ...],
    *,
    use_discovered_commands: bool,
) -> dict[str, bytes]:
    profile = result.profile
    evidence = "\n".join(f"- `{item.source}`: {item.kind} ? {item.detail}" for item in result.evidence) or "- No discovered evidence; confirm project facts explicitly."
    command_values = (
        {
            "fast_commands": _commands(result, "fast"),
            "full_commands": _commands(result, "full"),
            "release_commands": _commands(result, "release"),
            "native_commands": _commands(result),
        }
        if use_discovered_commands
        else {
            "fast_commands": _gate_inventory(gates, "fast"),
            "full_commands": _gate_inventory(gates, "full"),
            "release_commands": _gate_inventory(gates, "release"),
            "native_commands": _gate_inventory(gates),
        }
    )
    values = {
        "project_types": ", ".join(profile.project_types) or "unknown",
        "public_surfaces": ", ".join(profile.public_surfaces) or "unknown",
        "operational_dependencies": ", ".join(profile.operational_dependencies) or "none discovered",
        "evidence": evidence,
        "level": policy.level.value,
        "level_reasons": "; ".join(policy.reasons),
        **command_values,
    }
    files: dict[str, bytes] = {}
    for document in policy.required_documents:
        if document == "docs/decisions/":
            continue
        template = load_template(document + ".tmpl")
        names = set(re.findall(r"{{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*}}", template))
        files[document] = render_template(template, {name: values[name] for name in names}).encode("utf-8")
    template = load_template("decision.md.tmpl")
    names = set(re.findall(r"{{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*}}", template))
    files["docs/decisions/0000-governance-adoption.md"] = render_template(template, {name: values[name] for name in names}).encode("utf-8")
    return files


def _policy_model(result: DiscoveryResult, decision, documents: tuple[str, ...]) -> Policy:
    return Policy(
        schema_version="1.0", policy_version="0.1.0", level=decision.level,
        reasons=decision.reasons, required_documents=documents, adapters=("codex",), gates=(),
        non_baselinable_rules=("scope-violation", "missing-evidence"),
    )


def _scope_allows(path: str, scopes: tuple[str, ...]) -> bool:
    return any(scope == "." or path == scope or path.startswith(scope + "/") for scope in scopes)


def _parse_owner_approval(
    value: object,
    *,
    max_age_days: int = 30,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _APPROVAL_KEYS:
        raise ValueError("init approval must be a structured mapping")
    item = dict(value)
    if any(type(item[key]) is not str or not item[key].strip() for key in ("id", "actor", "role")):
        raise ValueError("init approval requires id, actor, and role")
    if item["role"] != "owner":
        raise ValueError("init approval requires owner role")
    try:
        stamp = datetime.fromisoformat(item["timestamp_utc"].replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("init approval timestamp is invalid") from error
    now = datetime.now(timezone.utc)
    if stamp.tzinfo is None or stamp.utcoffset() != timezone.utc.utcoffset(stamp):
        raise ValueError("init approval timestamp must be UTC")
    if stamp > now:
        raise ValueError("init approval timestamp must not be in the future")
    if (now - stamp.astimezone(timezone.utc)) > timedelta(days=max_age_days):
        raise ValueError("init approval timestamp is stale")
    scope = item["scope"]
    if isinstance(scope, str) or not isinstance(scope, (list, tuple)) or not scope:
        raise ValueError("init approval scope is required")
    normalized: list[str] = []
    for path in scope:
        if type(path) is not str or not path.strip():
            raise ValueError("init approval scope must contain strings")
        relative = path.replace("\\", "/")
        if relative != "." and (Path(relative).is_absolute() or ".." in Path(relative).parts):
            raise ValueError("init approval scope must be project-relative")
        normalized.append(relative.rstrip("/") or ".")
    item["scope"] = tuple(sorted(set(normalized)))
    return item


def _require_approval_coverage(
    approval: Mapping[str, Any],
    managed_paths: tuple[str, ...],
) -> None:
    required = tuple(managed_paths) + (RECEIPT_LEDGER_SCOPE,)
    unauthorized = tuple(
        sorted(path for path in required if not _scope_allows(path, approval["scope"]))
    )
    if unauthorized:
        raise ValueError("init approval scope does not cover all planned write paths")


def _receipt_filename(receipt: Receipt) -> str:
    stamp = receipt.timestamp_utc.replace(":", "").replace("-", "")
    return f"{RECEIPT_LEDGER_SCOPE}/{stamp}-init-{receipt_digest(receipt)[:12]}.json"


def _transaction_matches_expectation(
    changed_paths: tuple[str, ...],
    *,
    managed_paths: tuple[str, ...],
    receipt_path: str,
) -> bool:
    expected = tuple(sorted(managed_paths + (receipt_path,)))
    actual = tuple(sorted(changed_paths))
    if actual != expected:
        return False
    if not receipt_path.startswith(RECEIPT_LEDGER_SCOPE + "/"):
        return False
    if receipt_path.count("/") != RECEIPT_LEDGER_SCOPE.count("/") + 1:
        return False
    return True


def run_init(
    target: str | Path,
    *,
    policy_file: str | Path | None = None,
    approval: Mapping[str, Any] | None = None,
    apply: bool = False,
    max_age_days: int = 30,
) -> InitOutcome:
    try:
        guard = WorkspaceGuard(Path(target))
        result = discover_project(guard.root)
        decision, documents = _policy(result)
        resolved = resolve_policy_input(
            policy_file,
            fallback_policy=_policy_model(result, decision, documents),
            fallback_status="generated",
            audit_level=decision.level,
            required_documents=documents,
        )
        policy_model = resolved.policy
        files = _rendered_files(
            result,
            policy_model,
            resolved.gates,
            use_discovered_commands=resolved.input_status == "generated",
        )
        files[".governance/project.toml"] = dump_project_toml(result.profile).encode("utf-8")
        files[".governance/policy.toml"] = resolved.canonical_bytes
        adapter_ids = policy_model.adapters or ("codex",)
        validation_commands = (
            tuple(
                format_command(gate.command)
                for gate in resolved.gates
                if gate.kind == "command"
            )
            if resolved.input_status == "explicit"
            else tuple(" ".join(item.command) for item in result.commands)
        )
        adapter_plans = render_adapter_plans(
            policy_version=policy_model.policy_version,
            policy_digest=resolved.policy_digest,
            project_root=".",
            validation_commands=validation_commands,
            adapters=adapter_ids,
            approved_adapters=adapter_ids,
        )
        for adapter in adapter_plans.values():
            files[adapter.target_relative_path] = adapter.rendered_content.encode("utf-8")
        staged: dict[str, bytes] = {}
        conflicts: list[str] = []
        for path, content in sorted(files.items()):
            existing = guard.resolve_write(path)
            if existing.exists():
                if existing.is_file() and existing.read_bytes() == content:
                    continue
                conflicts.append(path)
                continue
            staged[path] = content
        managed_paths = tuple(sorted(staged))
        payload: dict[str, object] = {
            "command": "init",
            "level": policy_model.level.value,
            "planned_paths": list(managed_paths),
            "managed_changed_paths": list(managed_paths),
            "receipt_ledger_scope": RECEIPT_LEDGER_SCOPE,
            "changed_paths": [],
            "conflicts": sorted(conflicts),
            "digests": {path: digest(content) for path, content in sorted(staged.items())},
            "rollback_plan": "restore declared paths from transaction checkpoint",
            "mode": "apply" if apply else "preview",
            "policy_input_status": resolved.input_status,
            "policy_digest": resolved.policy_digest,
            "gate_ids": tuple(sorted(gate.gate_id for gate in resolved.gates)),
            "gate_argv_summary": _gate_argv_summary(resolved.gates),
            "planned_documents": policy_model.required_documents,
            "adapters": tuple(sorted(adapter_plans)),
        }

        def make_receipt(
            mode: str,
            *,
            approval_value: Mapping[str, Any] | None = None,
            managed: tuple[str, ...] = managed_paths,
        ) -> Receipt:
            inputs: dict[str, object] = {
                "target": ".",
                "level": policy_model.level.value,
                "mode": mode,
                "policy_input_status": resolved.input_status,
            }
            approvals: tuple[str, ...] = ()
            if approval_value is not None:
                inputs["approval"] = dict(approval_value)
                approvals = (str(approval_value["id"]),)
            return build_receipt(
                command="init",
                policy_digest=resolved.policy_digest,
                target_fingerprint=digest(snapshot_for_audit(guard)),
                authorized_scope=tuple(managed) + (RECEIPT_LEDGER_SCOPE,),
                inputs=inputs,
                outputs={
                    **payload,
                    "mode": mode,
                    "managed_changed_paths": list(managed),
                    "receipt_ledger_scope": RECEIPT_LEDGER_SCOPE,
                    "changed_paths": list(managed) if mode == "apply" else [],
                    "artifacts": tuple(
                        {"path": path, "digest": digest(content)}
                        for path, content in sorted(staged.items())
                    ),
                    "rollback": {
                        "plan": "restore declared paths from transaction checkpoint",
                        "evidence": "transaction checkpoint",
                    },
                },
                findings=result.findings,
                approvals=approvals,
                classification="preview" if mode == "preview" else "init",
                evidence_refs=tuple(sorted({item.source for item in result.evidence})),
            )

        if not apply:
            return InitOutcome(0, payload, make_receipt("preview"))
        if not staged:
            payload["mode"] = "no-op"
            payload["managed_changed_paths"] = []
            return InitOutcome(0, payload, make_receipt("no-op", managed=()))

        if approval is None:
            raise ValueError("structured owner approval is required for write-producing init")
        approval_value = _parse_owner_approval(approval, max_age_days=max_age_days)
        _require_approval_coverage(approval_value, managed_paths)

        receipt = make_receipt("apply", approval_value=approval_value, managed=managed_paths)
        receipt_bytes = canonical_json_bytes(receipt)
        receipt_relative = _receipt_filename(receipt)
        if receipt_relative in staged:
            raise ValueError("managed artifact path collides with receipt ledger path")
        write_set = dict(staged)
        write_set[receipt_relative] = receipt_bytes
        transaction = WorkspaceTransaction(guard, tuple(sorted(write_set)), apply=True)
        for path, content in sorted(write_set.items()):
            transaction.stage_bytes(path, content)
        transaction_result = transaction.commit()
        changed = tuple(transaction_result.changed_paths)
        if not _transaction_matches_expectation(
            changed,
            managed_paths=managed_paths,
            receipt_path=receipt_relative,
        ):
            raise ValueError("init transaction changed paths do not match managed paths plus one receipt")
        payload["changed_paths"] = list(changed)
        payload["managed_changed_paths"] = list(managed_paths)
        return InitOutcome(0, payload, receipt)
    except (OSError, ValueError, TypeError, PathViolation) as error:
        payload = {"command": "init", "status": "invalid", "error": type(error).__name__, "mode": "invalid"}
        return InitOutcome(2, payload, build_receipt(command="init", outputs=payload, classification="invalid"))
    except Exception as error:
        payload = {"command": "init", "status": "failed", "error": type(error).__name__, "mode": "failed"}
        return InitOutcome(1, payload, build_receipt(command="init", outputs=payload, classification="fail"))


__all__ = ["InitOutcome", "RECEIPT_LEDGER_SCOPE", "run_init"]
