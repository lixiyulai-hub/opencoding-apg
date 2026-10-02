#!/usr/bin/env python3
"""Write JSON and user-readable Markdown acceptance status."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from opencoding.acceptance_report import build_acceptance_report, render_acceptance_markdown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, help="current product run root for evidence-bound observed checks")
    parser.add_argument("--run-id", help="current product run id")
    parser.add_argument("--expected-run-digest", help="record_digest from the current product run")
    args = parser.parse_args()
    if not args.json_output.is_absolute() or not args.markdown_output.is_absolute():
        parser.error("outputs must be absolute")
    if any(value is not None for value in (args.run_root, args.run_id, args.expected_run_digest)) and not all(value is not None for value in (args.run_root, args.run_id, args.expected_run_digest)):
        parser.error("--run-root, --run-id and --expected-run-digest must be supplied together")
    if args.run_root is not None and not args.run_root.is_absolute():
        parser.error("--run-root must be absolute")
    report = build_acceptance_report(run_root=args.run_root, run_id=args.run_id, expected_run_digest=args.expected_run_digest)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.markdown_output.write_text(render_acceptance_markdown(report), encoding="utf-8")
    print(json.dumps({"json": str(args.json_output), "markdown": str(args.markdown_output), "schema_version": report["schema_version"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
