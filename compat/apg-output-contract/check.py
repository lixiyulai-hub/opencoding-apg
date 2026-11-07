"""Explicit local candidate entry. Does not install or replace the global APG."""
import argparse
from pathlib import Path

from project_governance.commands.check import _load_bound_policy, run_check
from project_governance.storage import canonical_json_bytes


def main():
    parser = argparse.ArgumentParser(description="APG bounded-output phase-check candidate")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--phase", choices=("fast", "full", "release"), default="fast")
    args = parser.parse_args()
    gates, policy_digest = _load_bound_policy(args.root, required=True)
    result = run_check(args.root, gates, phase=args.phase, policy_digest=policy_digest, require_policy_binding=True)
    print(canonical_json_bytes(result.receipt).decode("utf-8"))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
