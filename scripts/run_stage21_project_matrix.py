#!/usr/bin/env python3
"""Run Stage20 projects plus a third independent project through the same path."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from scripts.run_stage20_multi_project import SCENARIOS, _run_scenario


THIRD_SCENARIO = {
    "name": "meeting-reminder-cli",
    "target": "cli",
    "profile": "python-cli-runtime",
    "files": {
        "AGENTS.md": "# Meeting notes\nKeep reminder selection deterministic and offline.\n",
        "plan.md": "# Reminder plan\nSelect only due reminders and preserve a small test.\n",
        "src/reminders.py": (
            "def due_titles(items, today):\n"
            "    return [item['title'] for item in items if item['due'] < today]\n"
        ),
        "tests/test_reminders.py": (
            "import unittest\n"
            "from src.reminders import due_titles\n\n"
            "class ReminderTests(unittest.TestCase):\n"
            "    def test_due_titles(self):\n"
            "        items = [{'title': 'Call', 'due': 3}, {'title': 'Plan', 'due': 5}]\n"
            "        self.assertEqual(due_titles(items, 5), ['Call', 'Plan'])\n"
        ),
    },
    "repair": ("src/reminders.py", "item['due'] < today", "item['due'] <= today"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.is_absolute():
        parser.error("--output must be absolute")
    scenarios = list(SCENARIOS) + [THIRD_SCENARIO]
    projects = [_run_scenario(spec, index) for index, spec in enumerate(scenarios, 1)]
    report = {
        "schema": "opencoding-stage21-project-matrix-v1",
        "projects": projects,
        "summary": {
            "projects": len(projects),
            "all_failures_observed": all(p["failure_repair"]["failure_observed"] for p in projects),
            "all_repairs_passed": all(p["failure_repair"]["repair_passed"] for p in projects),
            "all_rollbacks_clean": all(p["rollback"]["rollback_status"] == "rolled_back" and not p["rollback"]["residual_paths"] and not p["rollback"]["probe_exists_after"] for p in projects),
            "all_rejections_prewrite": all(p["authorization_rejection"]["status"] == "rejected_before_write" and not p["authorization_rejection"]["path_exists"] for p in projects),
        },
        "boundary": "Three synthetic offline projects only; no universal Agent, browser, Windows or deployment claim.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "summary": report["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
