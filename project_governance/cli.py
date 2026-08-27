from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any, Mapping

from .model import Receipt
from .receipts import build_receipt, load_receipt_json, load_receipt_mapping
from .storage import (
    canonical_json_bytes,
)
from .version import VERSION


COMMANDS = (
    "audit",
    "init",
    "adopt",
    "plan-change",
    "check",
    "doctor",
    "progress",
    "git-safety",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="project-governance",
        description="Adaptive project governance controller.",
    )
    parser.add_argument("--version", action="store_true", help="show component version")
    parser.add_argument("--json", dest="json_output", action="store_true", help="emit JSON output")
    subparsers = parser.add_subparsers(dest="command")

    audit = subparsers.add_parser("audit")
    audit.add_argument("target", help="project root to audit")
    audit.add_argument("--receipt-dir", help="controller-owned receipt directory outside target")

    init = subparsers.add_parser("init")
    init.add_argument("target", help="project root to initialize")
    init.add_argument("--policy-file", help="canonical policy TOML input")
    init.add_argument(
        "--approval-file",
        help="structured owner approval JSON file required for write-producing apply",
    )
    init.add_argument("--apply", action="store_true", help="apply the planned initialization")

    adopt = subparsers.add_parser("adopt")
    adopt.add_argument("target", help="project root to adopt")
    adopt.add_argument("--audit-receipt", required=True, help="canonical audit receipt JSON")
    adopt.add_argument("--audit-digest", required=True, help="expected canonical audit digest")
    adopt.add_argument("--approval", required=True, help="structured adoption approval JSON")
    adopt.add_argument("--policy-file", help="canonical policy TOML input")
    adopt.add_argument("--apply", action="store_true", help="apply the adoption")
    adopt.add_argument("--structural-migration", action="store_true", help=argparse.SUPPRESS)

    plan = subparsers.add_parser("plan-change")
    plan.add_argument("target", help="project root for the change")
    plan.add_argument("--request", required=True, help="product-intent request JSON")
    plan.add_argument("--apply", action="store_true", help="apply the change record")

    check = subparsers.add_parser("check")
    check.add_argument("target", help="project root to check")
    check_selection = check.add_mutually_exclusive_group()
    check_selection.add_argument(
        "--phase", choices=("fast", "full", "release"), default="fast"
    )
    check_selection.add_argument(
        "--plan-receipt",
        help="canonical project-relative plan-change receipt for plan-bound execution",
    )
    check.add_argument(
        "--loop-run",
        help="closed feedback-loop run JSON; omitted for legacy check behavior",
    )

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("target", help="project root to diagnose")

    progress = subparsers.add_parser(
        "progress",
        help="render a read-only source-bound project status snapshot",
    )
    progress.add_argument("target", help="project root to inspect")
    progress.add_argument(
        "--definition",
        help="project-relative ProgressDefinition JSON; defaults to .governance/progress/active.json",
    )

    git_safety = subparsers.add_parser(
        "git-safety",
        help="inspect Git boundaries and preview a local baseline without Git writes",
    )
    git_safety.add_argument("target", help="project root to inspect")
    git_safety.add_argument(
        "--preview",
        action="store_true",
        help="include prospective baseline paths and excluded actions",
    )
    git_safety.add_argument(
        "--large-file-limit",
        type=int,
        default=10 * 1024 * 1024,
        help="report files at or above this byte size",
    )

    for command_parser in (
        audit,
        init,
        adopt,
        plan,
        check,
        doctor,
        progress,
        git_safety,
    ):
        command_parser.add_argument(
            "--json",
            dest="json_output",
            action="store_true",
            default=argparse.SUPPRESS,
            help="emit one canonical JSON receipt",
        )
    return parser


def _read_json(path: str | Path) -> Mapping[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON input must be an object")
    return value


def receipt_from_mapping(value: Mapping[str, Any]) -> Receipt:
    return load_receipt_mapping(value)


def _gates(target: Path):
    from .commands.check import _load_bound_policy

    adopted = (target / ".governance" / "adoption.json").is_file()
    return _load_bound_policy(target, required=adopted)


def _error_receipt(command: str, error: Exception | str) -> Receipt:
    error_type = error if isinstance(error, str) else type(error).__name__
    return build_receipt(
        command=command,
        outputs={"status": "invalid", "error_type": error_type},
        classification="invalid",
    )


def _fallback_terminal_status(
    *,
    label: str,
    receipt: Receipt,
    reason: str,
) -> str:
    """Render the fail-closed terminal status when progress cannot be read."""

    terminal_result = f"terminal-result-{label}-{receipt.classification}"
    error_type = receipt.outputs.get("error_type")
    reasons = [reason, terminal_result]
    if (
        type(error_type) is str
        and error_type
        and error_type.replace("-", "").replace("_", "").isalnum()
    ):
        reasons.append(f"terminal-error-{error_type.casefold()}")
    elif error_type is not None:
        reasons.append("terminal-error-detail-retained-in-receipt")
    invalid_input = (
        receipt.classification == "invalid"
        or reason == "progress-target-unavailable"
    )
    if invalid_input:
        delivery_state = "blocked-by-invalid-input"
        terminal_state = "invalid-input"
        next_action = "inspect-invalid-input"
        terminal_reason = "terminal-invalid-input"
        review_state = "blocked-by-invalid-input"
        resume_condition = "resume.after-invalid-input-is-corrected"
    else:
        delivery_state = "blocked-by-progress-source"
        terminal_state = "progress-source-unavailable"
        next_action = "inspect-progress-scope"
        terminal_reason = "terminal-progress-source-unavailable"
        review_state = "blocked-by-progress-source"
        resume_condition = "resume.after-progress-source-is-readable"
    return "\n".join(
        (
            "Status Snapshot",
            "Scope: not-computable",
            "Progress basis: definition_id=absent; denominator_tasks=not-computable; "
            "denominator_weight=not-computable; lifecycle_ref=not-computable; "
            "plan_id=not-computable",
            f"Completed work: unavailable; terminal_result={label}:{receipt.classification}",
            "Total progress: execution=not-computable verified=not-computable",
            "Program progress: scope=not-computable; definition_id=absent; "
            "execution=not-computable verified=not-computable; "
            "reason=program-roadmap-unavailable",
            "Current phase: unavailable/not-computable",
            "Lifecycle stage: unavailable; execution=not-computable verified=not-computable",
            "Program stage (current): unavailable/not-computable; packages=not-computable; "
            "execution=not-computable verified=not-computable",
            "Next lifecycle boundary: unavailable/not-computable",
            "Immediate program transaction: unavailable/not-computable",
            "Following program stage: unavailable/not-computable",
            "Roadmap: unavailable/not-computable",
            "Delivery and Gates: target=none; "
            f"state={delivery_state}; gate_health=unavailable; terminal_state={terminal_state}",
            f"Next automatic work: {next_action}",
            "Human gate: none",
            "Blockers and review: "
            f"reasons={','.join(reasons)}; terminal_reason={terminal_reason}; independent_review={review_state}",
            "Later boundaries: unavailable/not-computable",
            f"Continuation: state=freeze; action={next_action}; "
            "owner=harness-controller; requires_existing_authority=false; "
            "dispatch_permitted=false; "
            f"resume_condition={resume_condition}",
        )
    )


def _with_terminal_result(
    status_snapshot: str,
    *,
    label: str,
    receipt: Receipt,
) -> str:
    """Bind the command result to an otherwise source-bound status snapshot."""

    lines = status_snapshot.rstrip("\n").splitlines()
    required_prefixes = (
        "Status Snapshot",
        "Scope: ",
        "Progress basis: ",
        "Completed work: ",
        "Total progress: ",
        "Program progress: ",
        "Current phase: ",
        "Lifecycle stage: ",
        "Program stage (current): ",
        "Next lifecycle boundary: ",
        "Immediate program transaction: ",
        "Following program stage: ",
        "Roadmap: ",
        "Delivery and Gates: ",
        "Next automatic work: ",
        "Human gate: ",
        "Blockers and review: ",
        "Later boundaries: ",
        "Continuation: ",
    )
    if (
        lines.count("Status Snapshot") != 1
        or not lines
        or lines[0] != "Status Snapshot"
        or not lines[-1].startswith("Continuation: ")
        or any(sum(line.startswith(prefix) for line in lines) != 1 for prefix in required_prefixes[1:])
    ):
        return _fallback_terminal_status(
            label=label,
            receipt=receipt,
            reason="progress-terminal-status-invalid",
        )
    for index, line in enumerate(lines):
        if line.startswith("Completed work: "):
            lines[index] = (
                f"{line}; terminal_result={label}:{receipt.classification}"
            )
            break
    if label == "check":
        _overlay_check_terminal_outcome(lines, receipt)
    else:
        _overlay_terminal_stop(lines, label=label, receipt=receipt)
    return "\n".join(lines)


def _replace_status_field(line: str, field: str, value: str) -> str:
    """Replace one semicolon-delimited status field without changing its order."""

    parts = line.split("; ")
    marker = f"{field}="
    for index, part in enumerate(parts):
        if part.startswith(marker):
            parts[index] = f"{marker}{value}"
            return "; ".join(parts)
    parts.append(f"{marker}{value}")
    return "; ".join(parts)


def _freeze_terminal_status(
    lines: list[str],
    *,
    delivery_state: str,
    gate_health: str | None,
    next_action: str,
    terminal_field: str,
    terminal_value: str,
    terminal_reason: str,
    review_state: str,
    resume_condition: str,
    owner: str,
    requires_existing_authority: bool,
    human_gate: str | None = None,
) -> None:
    """Overlay a terminal stop without changing source-bound progress values."""

    authority = "true" if requires_existing_authority else "false"
    for index, line in enumerate(lines):
        if line.startswith("Delivery and Gates: "):
            line = _replace_status_field(line, "state", delivery_state)
            if gate_health is not None:
                line = _replace_status_field(line, "gate_health", gate_health)
            lines[index] = _replace_status_field(
                line,
                terminal_field,
                terminal_value,
            )
        elif line.startswith("Next automatic work: "):
            lines[index] = f"Next automatic work: {next_action}"
        elif line.startswith("Blockers and review: "):
            line = _replace_status_field(line, "terminal_reason", terminal_reason)
            lines[index] = _replace_status_field(
                line,
                "independent_review",
                review_state,
            )
        elif line.startswith("Human gate: ") and human_gate is not None:
            lines[index] = f"Human gate: {human_gate}"
        elif line.startswith("Continuation: "):
            lines[index] = (
                f"Continuation: state=freeze; action={next_action}; owner={owner}; "
                f"requires_existing_authority={authority}; dispatch_permitted=false; "
                f"resume_condition={resume_condition}"
            )


def _overlay_terminal_stop(
    lines: list[str],
    *,
    label: str,
    receipt: Receipt,
) -> None:
    """Keep generic non-check terminal failures out of a work-in-progress state."""

    classification = receipt.classification
    conflicts = receipt.outputs.get("conflicts")
    if label == "adopt" and isinstance(conflicts, (list, tuple)) and conflicts:
        _freeze_terminal_status(
            lines,
            delivery_state="blocked-by-adoption-conflict",
            gate_health="evidence-blocked",
            next_action="resolve-adoption-conflicts",
            terminal_field="terminal_state",
            terminal_value="adoption-conflict",
            terminal_reason="terminal-adoption-conflict",
            review_state="blocked-by-adoption-conflict",
            resume_condition="resume.after-adoption-conflicts-are-resolved",
            owner="authorized-executor",
            requires_existing_authority=True,
            human_gate="resolve-adoption-conflict",
        )
    elif classification == "invalid":
        _freeze_terminal_status(
            lines,
            delivery_state="blocked-by-invalid-input",
            gate_health="evidence-blocked",
            next_action="inspect-invalid-input",
            terminal_field="terminal_state",
            terminal_value="invalid-input",
            terminal_reason="terminal-invalid-input",
            review_state="blocked-by-invalid-input",
            resume_condition="resume.after-invalid-input-is-corrected",
            owner="harness-controller",
            requires_existing_authority=False,
        )
    elif classification == "scope-violation":
        _freeze_terminal_status(
            lines,
            delivery_state="scope-violation",
            gate_health="evidence-blocked",
            next_action="inspect-scope-and-replan",
            terminal_field="terminal_state",
            terminal_value="scope-violation",
            terminal_reason="terminal-scope-violation",
            review_state="blocked-by-scope",
            resume_condition="resume.after-scope-repair-and-replan",
            owner="validator",
            requires_existing_authority=True,
        )
    elif classification == "inconclusive":
        _freeze_terminal_status(
            lines,
            delivery_state="blocked-by-inconclusive-evidence",
            gate_health="evidence-blocked",
            next_action="resolve-inconclusive-evidence",
            terminal_field="terminal_state",
            terminal_value="inconclusive",
            terminal_reason="terminal-inconclusive",
            review_state="blocked-by-evidence",
            resume_condition="resume.after-inconclusive-evidence-is-resolved",
            owner="validator",
            requires_existing_authority=True,
        )
    elif classification in {"diagnostic", "fail"}:
        _freeze_terminal_status(
            lines,
            delivery_state="blocked-by-terminal-diagnostic",
            gate_health="evidence-blocked",
            next_action="inspect-terminal-diagnostic",
            terminal_field="terminal_state",
            terminal_value="diagnostic",
            terminal_reason="terminal-diagnostic",
            review_state="blocked-by-diagnostic",
            resume_condition="resume.after-terminal-diagnostic-is-resolved",
            owner="validator",
            requires_existing_authority=True,
        )


def _overlay_check_terminal_outcome(lines: list[str], receipt: Receipt) -> None:
    """Make nonzero check exits visible instead of leaving a pending Gate label."""

    exit_code = receipt.outputs.get("exit_code")
    if type(exit_code) is not int:
        if receipt.classification == "invalid":
            _freeze_terminal_status(
                lines,
                delivery_state="blocked-by-invalid-check-input",
                gate_health="evidence-blocked",
                next_action="inspect-invalid-check-input",
                terminal_field="terminal_gate",
                terminal_value="invalid-check-input",
                terminal_reason="terminal-invalid-check-input",
                review_state="blocked-by-invalid-check-input",
                resume_condition="resume.after-invalid-check-input-is-corrected",
                owner="harness-controller",
                requires_existing_authority=False,
            )
        return
    if exit_code == 0 and receipt.classification != "invalid":
        return
    if exit_code == 1:
        terminal_gate = "required-gate-failed"
        delivery_state = "blocked-by-required-gate"
        next_action = "recover-required-gates"
        reason = "terminal-required-gate-failed"
        resume_condition = "resume.after-required-gates-pass"
        review_state = "blocked-by-gate"
    elif exit_code == 3:
        terminal_gate = "required-gate-inconclusive"
        delivery_state = "blocked-by-required-gate"
        next_action = "recover-required-gates"
        reason = "terminal-required-gate-inconclusive"
        resume_condition = "resume.after-required-gates-pass"
        review_state = "blocked-by-gate"
    elif exit_code == 4:
        terminal_gate = "scope-violation"
        delivery_state = "scope-violation"
        next_action = "inspect-scope-and-replan"
        reason = "terminal-scope-violation"
        resume_condition = "resume.after-scope-repair-and-replan"
        review_state = "blocked-by-scope"
    else:
        terminal_gate = "check-failed"
        delivery_state = "blocked-by-check"
        next_action = "inspect-check-result"
        reason = "terminal-check-nonzero"
        resume_condition = "resume.after-check-review"
        review_state = "blocked-by-check"
    _freeze_terminal_status(
        lines,
        delivery_state=delivery_state,
        gate_health="evidence-blocked",
        next_action=next_action,
        terminal_field="terminal_gate",
        terminal_value=terminal_gate,
        terminal_reason=reason,
        review_state=review_state,
        resume_condition=resume_condition,
        owner="validator",
        requires_existing_authority=True,
    )
    for index, line in enumerate(lines):
        if line.startswith("Delivery and Gates: "):
            lines[index] = _replace_status_field(
                line, "check_exit_code", str(exit_code)
            )
            break


def _terminal_status_snapshot(
    *,
    target: str | Path | None,
    label: str,
    receipt: Receipt,
    status_snapshot: str | None = None,
) -> str:
    if status_snapshot is None:
        if target is None:
            return _fallback_terminal_status(
                label=label,
                receipt=receipt,
                reason="progress-target-unavailable",
            )
        try:
            from .commands.progress import run_progress

            status_snapshot = run_progress(target).status_snapshot
        except Exception:
            return _fallback_terminal_status(
                label=label,
                receipt=receipt,
                reason="progress-source-unavailable",
            )
    return _with_terminal_result(
        status_snapshot,
        label=label,
        receipt=receipt,
    )


def _emit(
    receipt: Receipt,
    *,
    json_output: bool,
    label: str,
    target: str | Path | None = None,
    status_snapshot: str | None = None,
) -> None:
    if json_output:
        sys.stdout.buffer.write(canonical_json_bytes(receipt))
    else:
        print(
            _terminal_status_snapshot(
                target=target,
                label=label,
                receipt=receipt,
                status_snapshot=status_snapshot,
            )
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if "--json" in raw_argv:
            with redirect_stderr(io.StringIO()):
                args = parser.parse_args(raw_argv)
        else:
            args = parser.parse_args(raw_argv)
    except SystemExit as error:
        if error.code != 2:
            raise
        label = next(
            (item for item in raw_argv if item in COMMANDS),
            "controller",
        )
        if "--json" in raw_argv and label in COMMANDS:
            _emit(
                _error_receipt(label, "ArgumentError"),
                json_output=True,
                label=label,
            )
            return 2
        _emit(
            _error_receipt(label, "ArgumentError"),
            json_output=False,
            label=label,
        )
        return 2
    if args.version:
        if args.json_output:
            print(json.dumps({"version": VERSION}))
        else:
            print(VERSION)
        return 0
    if args.command is None:
        _emit(
            _error_receipt("controller", "ArgumentError"),
            json_output=args.json_output,
            label="controller",
        )
        return 2

    try:
        if args.command == "audit":
            from .commands.audit import run_audit

            outcome = run_audit(args.target, receipt_dir=args.receipt_dir)
            _emit(
                outcome.receipt,
                json_output=args.json_output,
                label="audit",
                target=args.target,
            )
            return outcome.exit_code

        if args.command == "init":
            from .commands.init_project import run_init

            approval = (
                _read_json(args.approval_file)
                if getattr(args, "approval_file", None)
                else None
            )
            outcome = run_init(
                args.target,
                policy_file=args.policy_file,
                approval=approval,
                apply=args.apply,
            )
            _emit(
                outcome.receipt,
                json_output=args.json_output,
                label="init",
                target=args.target,
            )
            return outcome.exit_code

        if args.command == "plan-change":
            from .commands.plan_change import run_plan_change

            outcome = run_plan_change(args.target, _read_json(args.request), apply=args.apply)
            receipt = outcome.receipt or _error_receipt("plan-change", outcome.message)
            _emit(
                receipt,
                json_output=args.json_output,
                label="plan-change",
                target=args.target,
            )
            return 0 if outcome.ok else 2

        if args.command == "adopt":
            from .commands.adopt import run_adopt

            audit_receipt = load_receipt_json(
                Path(args.audit_receipt).read_bytes(),
            )
            outcome = run_adopt(
                args.target,
                audit_receipt,
                approval=_read_json(args.approval),
                audit_digest=args.audit_digest,
                policy_file=args.policy_file,
                apply=args.apply,
                structural_migration=args.structural_migration,
            )
            receipt = outcome.receipt or _error_receipt("adopt", outcome.message)
            _emit(
                receipt,
                json_output=args.json_output,
                label="adopt",
                target=args.target,
            )
            return 0 if outcome.ok else 2

        if args.command == "check":
            from .commands.check import run_check

            target = Path(args.target).resolve(strict=True)
            loop_run = _read_json(args.loop_run) if args.loop_run is not None else None
            gates, policy_digest = _gates(target)
            outcome = run_check(
                target,
                gates,
                phase=args.phase,
                loop_run=loop_run,
                plan_receipt=args.plan_receipt,
                policy_digest=policy_digest,
                require_policy_binding=True,
            )
            _emit(
                outcome.receipt,
                json_output=args.json_output,
                label="check",
                target=args.target,
            )
            return outcome.exit_code

        if args.command == "doctor":
            from .commands.doctor import run_doctor

            outcome = run_doctor(args.target)
            _emit(
                outcome.receipt,
                json_output=args.json_output,
                label="doctor",
                target=args.target,
            )
            return outcome.exit_code

        if args.command == "progress":
            from .commands.progress import run_progress

            outcome = run_progress(args.target, definition=args.definition)
            _emit(
                outcome.receipt,
                json_output=args.json_output,
                label="progress",
                target=args.target,
                status_snapshot=outcome.status_snapshot,
            )
            return outcome.exit_code

        if args.command == "git-safety":
            from .commands.git_safety import run_git_safety

            outcome = run_git_safety(
                args.target,
                preview=args.preview,
                large_file_limit=args.large_file_limit,
            )
            _emit(
                outcome.receipt,
                json_output=args.json_output,
                label="git-safety",
                target=args.target,
            )
            return outcome.exit_code
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        receipt = _error_receipt(args.command, error)
        if not args.json_output:
            print(f"{args.command}: invalid input ({type(error).__name__})", file=sys.stderr)
        _emit(
            receipt,
            json_output=args.json_output,
            label=args.command,
            target=getattr(args, "target", None),
        )
        return 2
    return 2


__all__ = ["COMMANDS", "build_parser", "main", "receipt_from_mapping"]
