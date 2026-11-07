from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
import time
from typing import Iterable, Mapping, Any

from ..audit_contract import snapshot_for_audit
from ..consistency_manifest import (
    ConsistencyManifestError,
    _is_link_or_reparse,
    _open_read_handle,
    _validate_opened_path,
)
from ..feedback_loops import (
    FEEDBACK_LOOP_DECISION_OUTPUT_KEY,
    FEEDBACK_LOOP_INPUT_KEY,
    evaluate_loop,
    feedback_loop_receipt_input,
    feedback_loop_receipt_output,
    prepare_loop_run,
    validate_progress_gate_ids,
)
from ..gate_execution_evidence import (
    GATE_EXECUTION_EVIDENCE_OUTPUT_KEY,
    gate_contract_sha256,
    gate_execution_evidence_document,
    parse_gate_execution_evidence,
)
from ..gates import (
    GateDefinition,
    GateRun,
    orchestrate_gates,
    parse_gate_definitions,
)
from ..model import Receipt
from ..path_guard import PathViolation, WorkspaceGuard, WorkspaceTransaction
from ..plan_bound_execution import (
    PLAN_BOUND_EXECUTION_OUTPUT_KEY,
    PlanBoundExecutionError,
    PlanBoundSelection,
    parse_plan_bound_execution,
    plan_bound_execution_evidence,
    prepare_plan_bound_selection,
    selected_gate_definitions,
)
from ..receipts import (
    ReceiptLedgerError,
    ReceiptLedgerInventory,
    build_receipt,
    require_canonical_receipt_ledger,
)
from ..regression_identity import (
    REGRESSION_GATE_CONTRACTS_OUTPUT_KEY,
    gate_contract_snapshot,
)
from ..storage import (
    canonical_json_bytes,
    digest,
    dump_policy_toml,
    load_policy_toml,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_POLICY_RELATIVE_PATH = ".governance/policy.toml"
_MAX_POLICY_BYTES = 1_048_576
_PHASE_MEMBERSHIP = {
    "fast": frozenset({"fast"}),
    "full": frozenset({"fast", "full"}),
    "release": frozenset({"fast", "full", "release"}),
}


@dataclass(frozen=True)
class CheckOutcome:
    gate_run: GateRun
    receipt: Receipt
    adopted: bool
    changed_paths: tuple[str, ...] = ()

    @property
    def checks(self):
        return self.gate_run.checks

    @property
    def exit_code(self):
        return self.gate_run.exit_code


def _adopted(root: Path) -> bool:
    return (root / ".governance" / "adoption.json").is_file()


def _load_bound_policy(
    root: Path,
    *,
    required: bool,
) -> tuple[tuple[GateDefinition, ...], str | None]:
    if type(required) is not bool:
        raise TypeError("required must be bool")
    project_root = Path(root).resolve(strict=True)
    policy_path = project_root / _POLICY_RELATIVE_PATH
    if not os.path.lexists(policy_path):
        if required:
            raise ValueError("bound check policy is missing")
        return (), None
    try:
        current = project_root
        for part in Path(_POLICY_RELATIVE_PATH).parts:
            current /= part
            if not os.path.lexists(current) or _is_link_or_reparse(current):
                raise ValueError("check policy must be a regular project file")
        with _open_read_handle(policy_path, "check policy") as handle:
            _validate_opened_path(
                handle,
                project_root=project_root,
                expected=policy_path,
                expected_relative=_POLICY_RELATIVE_PATH,
                label="check policy",
                allow_manifest=True,
            )
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("check policy must be a regular project file")
            payload = handle.read(_MAX_POLICY_BYTES + 1)
            after = os.fstat(handle.fileno())
            if len(payload) > _MAX_POLICY_BYTES:
                raise ValueError("check policy exceeds the 1 MiB limit")
            identity_fields = (
                "st_dev",
                "st_ino",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if any(getattr(before, field) != getattr(after, field) for field in identity_fields):
                raise ValueError("check policy changed while being read")
            _validate_opened_path(
                handle,
                project_root=project_root,
                expected=policy_path,
                expected_relative=_POLICY_RELATIVE_PATH,
                label="check policy",
                allow_manifest=True,
            )
    except ConsistencyManifestError as error:
        raise ValueError("check policy must be a regular project file") from error
    policy = load_policy_toml(payload.decode("utf-8"))
    canonical_policy = dump_policy_toml(policy).encode("utf-8")
    return parse_gate_definitions(policy.gates), digest(canonical_policy)


def _validate_policy_binding(
    root: Path,
    gates: tuple[GateDefinition, ...],
    policy_digest: str | None,
) -> None:
    if policy_digest is None:
        return
    if type(policy_digest) is not str or not _SHA256.fullmatch(policy_digest):
        raise ValueError("policy_digest must be a lowercase SHA-256 value")
    current_gates, current_policy_digest = _load_bound_policy(root, required=True)
    if current_policy_digest != policy_digest:
        raise ValueError("bound check policy changed before execution")
    supplied_contracts = tuple(gate_contract_sha256(gate) for gate in gates)
    current_contracts = tuple(gate_contract_sha256(gate) for gate in current_gates)
    if supplied_contracts != current_contracts:
        raise ValueError("bound check Gate definitions do not match policy")


def _build_check_receipt(
    *,
    outputs: Mapping[str, Any],
    **kwargs: Any,
) -> Receipt:
    execution_evidence = outputs.get(GATE_EXECUTION_EVIDENCE_OUTPUT_KEY)
    parse_gate_execution_evidence(execution_evidence)
    plan_evidence = outputs.get(PLAN_BOUND_EXECUTION_OUTPUT_KEY)
    if plan_evidence is not None:
        parse_plan_bound_execution(plan_evidence)
    redacted_outputs = dict(outputs)
    redacted_outputs.pop(GATE_EXECUTION_EVIDENCE_OUTPUT_KEY)
    redacted_outputs.pop(PLAN_BOUND_EXECUTION_OUTPUT_KEY, None)
    receipt = build_receipt(outputs=redacted_outputs, **kwargs)
    closed_outputs = dict(receipt.outputs)
    closed_outputs[GATE_EXECUTION_EVIDENCE_OUTPUT_KEY] = execution_evidence
    if plan_evidence is not None:
        closed_outputs[PLAN_BOUND_EXECUTION_OUTPUT_KEY] = plan_evidence
    return replace(receipt, outputs=closed_outputs)


def _validate_gate_run_evidence(
    gate_run: GateRun,
    gates: tuple[GateDefinition, ...],
    *,
    phase: str,
) -> None:
    if phase not in _PHASE_MEMBERSHIP:
        raise ValueError("invalid phase")
    selected = tuple(
        sorted(
            (gate for gate in gates if gate.phase in _PHASE_MEMBERSHIP[phase]),
            key=lambda gate: (("fast", "full", "release").index(gate.phase), gate.gate_id),
        )
    )
    if not gate_run.evidence:
        if selected or gate_run.checks:
            raise ValueError("Gate execution evidence must align with checks")
        return
    if len(gate_run.evidence) != len(gate_run.checks):
        raise ValueError("Gate execution evidence must align with checks")
    if len(selected) != len(gate_run.evidence):
        raise ValueError("Gate execution evidence does not match the selected Gates")
    for index, (gate, check, evidence) in enumerate(
        zip(selected, gate_run.checks, gate_run.evidence)
    ):
        if (
            evidence.check_index != index
            or evidence.gate_id != gate.gate_id
            or evidence.phase != gate.phase
            or evidence.kind != gate.kind
            or evidence.required is not gate.required
            or evidence.gate_contract_sha256 != gate_contract_sha256(gate)
            or check.gate_id != evidence.gate_id
            or check.phase != evidence.phase
            or check.status.value != evidence.status
            or check.duration_ms != evidence.duration_ms
        ):
            raise ValueError("Gate execution evidence does not match its CheckResult")


def _plan_scope_violation_outcome(
    *,
    gate_run: GateRun,
    selection: PlanBoundSelection,
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
    changed_paths: Iterable[str],
    authority_stable: bool,
    policy_digest: str | None,
    target_fingerprint: str,
    adopted: bool,
) -> CheckOutcome:
    changed = tuple(sorted(set(changed_paths)))
    violation_outputs = dict(outputs)
    violation_outputs[PLAN_BOUND_EXECUTION_OUTPUT_KEY] = plan_bound_execution_evidence(
        selection,
        (entry.gate_id for entry in gate_run.evidence),
        authority_status="stable" if authority_stable else "changed",
    )
    violation_outputs.update({"exit_code": 4, "changed_paths": changed})
    receipt = _build_check_receipt(
        command="check",
        policy_digest=policy_digest or "",
        target_fingerprint=target_fingerprint,
        authorized_scope=(),
        inputs=inputs,
        outputs=violation_outputs,
        checks=gate_run.checks,
        classification="scope-violation",
        evidence_refs=changed or (selection.plan_receipt_ref,),
    )
    return CheckOutcome(
        GateRun(gate_run.checks, 4, gate_run.evidence),
        receipt,
        adopted,
        changed,
    )


def run_check(
    target: str | Path,
    gates: Iterable[GateDefinition],
    *,
    phase: str = "fast",
    loop_run: Mapping[str, Any] | None = None,
    plan_receipt: str | None = None,
    policy_digest: str | None = None,
    require_policy_binding: bool = False,
) -> CheckOutcome:
    gates = tuple(gates)
    if any("output_dir" in gate.options for gate in gates):
        from ..bounded_outputs import run_output_check
        if loop_run is not None or plan_receipt is not None:
            raise ValueError("bounded outputs candidate supports phase checks only; plan/loop binding is not yet supported")
        return run_output_check(target, gates, phase=phase, policy_digest=policy_digest)
    root = Path(target).resolve(strict=True)
    guard = WorkspaceGuard(root)
    adopted = _adopted(root)
    if type(require_policy_binding) is not bool:
        raise TypeError("require_policy_binding must be bool")
    if phase not in _PHASE_MEMBERSHIP:
        raise ValueError("invalid phase")
    if require_policy_binding and adopted and policy_digest is None:
        raise ValueError("adopted canonical check requires a policy binding")
    if loop_run is not None and not adopted:
        raise ValueError("feedback-loop checks require an adopted project")
    if plan_receipt is not None:
        if type(plan_receipt) is not str or not plan_receipt:
            raise TypeError("plan_receipt must be a non-empty project-relative path")
        if not adopted:
            raise ValueError("plan-bound checks require an adopted project")
        if loop_run is not None:
            raise ValueError("plan-bound checks do not accept feedback-loop input")
    prepared = prepare_loop_run(root, loop_run) if loop_run is not None else None
    gate_definitions = tuple(gates)
    if prepared is not None:
        validate_progress_gate_ids(gate.gate_id for gate in gate_definitions)
    baseline = snapshot_for_audit(guard)
    _validate_policy_binding(root, gate_definitions, policy_digest)
    selection: PlanBoundSelection | None = None
    ledger_inventory: ReceiptLedgerInventory | None = None
    selected_gates = gate_definitions
    effective_phase = phase
    if plan_receipt is not None:
        selection = prepare_plan_bound_selection(
            root,
            plan_receipt,
            gate_definitions,
            policy_sha256=policy_digest,
        )
        selected_gates = selected_gate_definitions(selection, gate_definitions)
        effective_phase = selection.effective_phase
        if (
            prepare_plan_bound_selection(
                root,
                selection.plan_receipt_ref,
                gate_definitions,
                policy_sha256=policy_digest,
            )
            != selection
        ):
            raise PlanBoundExecutionError("plan authority changed before execution")
        try:
            ledger_inventory = require_canonical_receipt_ledger(root)
        except ReceiptLedgerError as error:
            raise PlanBoundExecutionError(
                "receipt ledger is invalid before execution"
            ) from error
    started = time.monotonic()
    gate_run = (
        GateRun((), 3, ())
        if selection is not None and selection.mode == "inconclusive"
        else orchestrate_gates(selected_gates, root, phase=effective_phase)
    )
    _validate_gate_run_evidence(gate_run, selected_gates, phase=effective_phase)
    elapsed = time.monotonic() - started
    decision = (
        evaluate_loop(
            prepared,
            gate_run.checks,
            exit_code=gate_run.exit_code,
            elapsed_seconds=elapsed,
            gate_definitions=gate_definitions,
        )
        if prepared is not None
        else None
    )
    inputs: dict[str, Any] = (
        {"phase": phase}
        if selection is None
        else {"plan_receipt": selection.plan_receipt_ref}
    )
    outputs: dict[str, Any] = {
        "exit_code": gate_run.exit_code,
        GATE_EXECUTION_EVIDENCE_OUTPUT_KEY: gate_execution_evidence_document(
            gate_run.evidence,
            phase=effective_phase,
            policy_sha256=policy_digest,
            selection_mode="plan" if selection is not None else "phase",
            performed=bool(gate_run.evidence),
        ),
    }
    if prepared is not None and decision is not None:
        inputs[FEEDBACK_LOOP_INPUT_KEY] = feedback_loop_receipt_input(prepared)
        outputs[FEEDBACK_LOOP_DECISION_OUTPUT_KEY] = feedback_loop_receipt_output(decision)
        outputs[REGRESSION_GATE_CONTRACTS_OUTPUT_KEY] = gate_contract_snapshot(
            gate_definitions
        )
    authority_stable = True
    ledger_stable = True
    if selection is not None:
        try:
            current_gates, current_policy_digest = _load_bound_policy(root, required=True)
            refreshed = prepare_plan_bound_selection(
                root,
                selection.plan_receipt_ref,
                current_gates,
                policy_sha256=current_policy_digest,
            )
            authority_stable = refreshed == selection
        except (OSError, TypeError, ValueError, PlanBoundExecutionError):
            authority_stable = False
        assert ledger_inventory is not None
        try:
            refreshed_ledger = require_canonical_receipt_ledger(root)
            ledger_stable = (
                refreshed_ledger.fingerprint == ledger_inventory.fingerprint
            )
        except ReceiptLedgerError:
            ledger_stable = False
        outputs[PLAN_BOUND_EXECUTION_OUTPUT_KEY] = plan_bound_execution_evidence(
            selection,
            (entry.gate_id for entry in gate_run.evidence),
            authority_status="stable" if authority_stable else "changed",
        )
    plan_transaction: WorkspaceTransaction | None = None
    plan_receipt_relative: str | None = None
    if selection is not None and adopted:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        plan_receipt_relative = (
            f".governance/receipts/{timestamp}-check-{digest(baseline)[:12]}.json"
        )
        plan_transaction = WorkspaceTransaction(
            guard, (plan_receipt_relative,), apply=True
        )
    changed = guard.changed_paths(baseline)
    if changed or not authority_stable or not ledger_stable:
        if selection is not None:
            return _plan_scope_violation_outcome(
                gate_run=gate_run,
                selection=selection,
                inputs=inputs,
                outputs=outputs,
                changed_paths=changed,
                authority_stable=authority_stable,
                policy_digest=policy_digest,
                target_fingerprint=digest(baseline),
                adopted=adopted,
            )
        violation_run = GateRun(gate_run.checks, 4, gate_run.evidence)
        if prepared is not None:
            decision = evaluate_loop(
                prepared,
                gate_run.checks,
                exit_code=4,
                elapsed_seconds=elapsed,
                gate_definitions=gate_definitions,
                governance_evidence_refs=changed,
            )
            outputs[FEEDBACK_LOOP_DECISION_OUTPUT_KEY] = feedback_loop_receipt_output(
                decision
            )
        violation_outputs = dict(outputs)
        violation_outputs.update({"exit_code": 4, "changed_paths": changed})
        evidence_refs = changed or (
            (selection.plan_receipt_ref,) if selection is not None else ()
        )
        violation_receipt = _build_check_receipt(
            command="check",
            policy_digest=policy_digest or "",
            target_fingerprint=digest(baseline),
            authorized_scope=(),
            inputs=inputs,
            outputs=violation_outputs,
            checks=gate_run.checks,
            classification="scope-violation",
            evidence_refs=evidence_refs,
        )
        return CheckOutcome(
            violation_run,
            violation_receipt,
            adopted,
            tuple(changed),
        )
    receipt = _build_check_receipt(
        command="check",
        policy_digest=policy_digest or "",
        target_fingerprint=digest(baseline),
        authorized_scope=(".governance/receipts",),
        inputs=inputs,
        outputs=outputs,
        checks=gate_run.checks,
        classification="check",
    )
    if not adopted:
        guard.assert_unchanged(baseline)
        return CheckOutcome(gate_run, receipt, False)
    if selection is None:
        guard.assert_unchanged(baseline)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        relative = (
            f".governance/receipts/{timestamp}-check-"
            f"{receipt.target_fingerprint[:12]}.json"
        )
        transaction = WorkspaceTransaction(guard, (relative,), apply=True)
    else:
        assert plan_transaction is not None and plan_receipt_relative is not None
        transaction = plan_transaction
        relative = plan_receipt_relative
    transaction.stage_bytes(relative, canonical_json_bytes(receipt))
    try:
        committed = transaction.commit()
    except PathViolation:
        if selection is None:
            raise
        changed = guard.changed_paths(baseline)
        try:
            current_gates, current_policy_digest = _load_bound_policy(
                root, required=True
            )
            refreshed = prepare_plan_bound_selection(
                root,
                selection.plan_receipt_ref,
                current_gates,
                policy_sha256=current_policy_digest,
            )
            authority_stable = refreshed == selection
        except (OSError, TypeError, ValueError, PlanBoundExecutionError):
            authority_stable = False
        return _plan_scope_violation_outcome(
            gate_run=gate_run,
            selection=selection,
            inputs=inputs,
            outputs=outputs,
            changed_paths=changed,
            authority_stable=authority_stable,
            policy_digest=policy_digest,
            target_fingerprint=digest(baseline),
            adopted=adopted,
        )
    return CheckOutcome(gate_run, receipt, True, committed.changed_paths)


__all__ = ["CheckOutcome", "run_check"]
