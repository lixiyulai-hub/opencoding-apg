from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from .model import Receipt
from .receipts import build_receipt, load_receipt_json, load_receipt_mapping
from .storage import (
    canonical_json_bytes,
)
from .version import VERSION


COMMANDS = ("audit", "init", "adopt", "plan-change", "check", "doctor", "git-safety")


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

    for command_parser in (audit, init, adopt, plan, check, doctor, git_safety):
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


def _emit(receipt: Receipt, *, json_output: bool, label: str) -> None:
    if json_output:
        sys.stdout.buffer.write(canonical_json_bytes(receipt))
    else:
        print(f"{label}: {receipt.classification}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        if args.json_output:
            print(json.dumps({"version": VERSION}))
        else:
            print(VERSION)
        return 0
    if args.command is None:
        parser.print_help()
        return 0

    try:
        if args.command == "audit":
            from .commands.audit import run_audit

            outcome = run_audit(args.target, receipt_dir=args.receipt_dir)
            _emit(outcome.receipt, json_output=args.json_output, label="audit")
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
            _emit(outcome.receipt, json_output=args.json_output, label="init")
            return outcome.exit_code

        if args.command == "plan-change":
            from .commands.plan_change import run_plan_change

            outcome = run_plan_change(args.target, _read_json(args.request), apply=args.apply)
            receipt = outcome.receipt or _error_receipt("plan-change", outcome.message)
            _emit(receipt, json_output=args.json_output, label="plan-change")
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
            _emit(receipt, json_output=args.json_output, label="adopt")
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
            _emit(outcome.receipt, json_output=args.json_output, label="check")
            return outcome.exit_code

        if args.command == "doctor":
            from .commands.doctor import run_doctor

            outcome = run_doctor(args.target)
            _emit(outcome.receipt, json_output=args.json_output, label="doctor")
            return outcome.exit_code

        if args.command == "git-safety":
            from .commands.git_safety import run_git_safety

            outcome = run_git_safety(
                args.target,
                preview=args.preview,
                large_file_limit=args.large_file_limit,
            )
            _emit(outcome.receipt, json_output=args.json_output, label="git-safety")
            return outcome.exit_code
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        receipt = _error_receipt(args.command, error)
        _emit(receipt, json_output=args.json_output, label=args.command)
        if not args.json_output:
            print(f"{args.command}: invalid input ({type(error).__name__})", file=sys.stderr)
        return 2
    return 2


__all__ = ["COMMANDS", "build_parser", "main", "receipt_from_mapping"]
