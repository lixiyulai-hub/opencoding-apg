#!/usr/bin/env python3
"""Inspect or recover one OpenCoding transaction in an explicit local root.

This helper is deliberately limited to the application transaction receipt. It
never contacts a provider, host service, network, or remote Git. Recovery only
handles file changes represented by a root-bound receipt; legacy receipts that
lack the canonical root are inspect-only. Arbitrary Python side effects remain
outside automatic rollback.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opencoding.transactions import read_receipt_state, rollback_changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-root", required=True, type=Path)
    parser.add_argument("--transaction-id", required=True)
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args(argv)
    if not args.execution_root.is_absolute() or not args.execution_root.is_dir():
        print(json.dumps({"status": "blocked", "error": "--execution-root must be an existing absolute directory"}, ensure_ascii=False, indent=2))
        return 2
    try:
        before = read_receipt_state(args.execution_root, args.transaction_id)
        result = {"schema": "opencoding-transaction-recovery-v1", "execution_root": str(args.execution_root), "transaction_id": args.transaction_id, "before": before}
        if args.rollback:
            if before.get("root_binding") != "verified":
                raise ValueError("legacy_unbound: inspect only; recovery requires a root-bound transaction")
            result["rollback"] = rollback_changes(args.execution_root, args.transaction_id)
            result["after"] = read_receipt_state(args.execution_root, args.transaction_id)
            result["residual_paths"] = list(result["after"].get("residual_paths") or [])
            result["status"] = "recovered" if result["rollback"].get("status") in {"rolled_back", "already_rolled_back"} and not result["residual_paths"] else "recovery_incomplete"
        else:
            result["status"] = "inspected"
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["status"] in {"inspected", "recovered"} else 1
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
