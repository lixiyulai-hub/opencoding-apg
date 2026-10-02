#!/usr/bin/env python3
"""Validate two distinct offline projects with explicit toolchain observations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import uuid

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from opencoding.agent_adapter import AgentAdapterError, LocalAgentAdapter
from opencoding.host_capabilities import normalize_target_platform
from opencoding.toolchain_probe import probe_target_toolchain
from opencoding.transactions import apply_changes, preview_changes, rollback_changes


SCENARIOS = [
    {
        "name": "recipe-index-cli",
        "target": "cli",
        "profile": "python-cli-runtime",
        "files": {
            "AGENTS.md": "# Recipe index notes\nKeep indexing deterministic and offline.\n",
            "plan.md": "# Recipe index plan\nGroup recipe titles by label, then test.\n",
            "src/index.py": (
                "def titles_by_label(rows):\n"
                "    result = {}\n"
                "    for row in rows:\n"
                "        label = row['lable']\n"
                "        result.setdefault(label, []).append(row['title'])\n"
                "    return result\n"
            ),
            "tests/test_index.py": (
                "import unittest\n"
                "from src.index import titles_by_label\n\n"
                "class IndexTests(unittest.TestCase):\n"
                "    def test_grouping(self):\n"
                "        rows = [{'label': 'quick', 'title': 'Soup'}, {'label': 'quick', 'title': 'Rice'}]\n"
                "        self.assertEqual(titles_by_label(rows), {'quick': ['Soup', 'Rice']})\n"
            ),
        },
        "repair": ("src/index.py", "row['lable']", "row['label']"),
    },
    {
        "name": "stock-delta-web",
        "target": "web",
        "profile": "node-web-runtime",
        "files": {
            "AGENTS.md": "# Stock notes\nThe computation is local; browser delivery is a later adapter.\n",
            "plan.md": "# Stock plan\nCalculate remaining quantity and preserve a reviewable test.\n",
            "src/stock.py": (
                "def remaining(received, shipped, damaged):\n"
                "    return received - shipped + damaged\n"
            ),
            "tests/test_stock.py": (
                "import unittest\n"
                "from src.stock import remaining\n\n"
                "class StockTests(unittest.TestCase):\n"
                "    def test_remaining(self):\n"
                "        self.assertEqual(remaining(20, 7, 2), 11)\n"
            ),
        },
        "repair": ("src/stock.py", "received - shipped + damaged", "received - shipped - damaged"),
    },
]


def _write(adapter: LocalAgentAdapter, relative: str, content: str, label: str, observation: dict, target: str) -> dict:
    action = {"type": "write_text", "path": relative, "content": content}
    authorization = adapter.authorization_for(
        action, scope="stage20-synthetic-project", confirmation_id=f"{label}-{uuid.uuid4().hex}"
    )
    result = adapter.execute(
        action, authorization=authorization, target_platform=target,
        toolchain_observation=observation, run_id=label, data_scope="synthetic-local-documents",
    )
    return {
        "status": result.get("status"), "exit_code": result.get("exit_code"),
        "path": relative, "action_digest": result.get("action_digest"),
        "target": result.get("capability_observation", {}).get("target"),
        "toolchain": result.get("capability_observation", {}).get("toolchain"),
        "target_toolchain_verified": result.get("capability_observation", {}).get("target_toolchain_verified"),
        "artifacts": result.get("artifacts", []),
    }


def _test(adapter: LocalAgentAdapter, label: str, observation: dict, target: str) -> dict:
    action = {"type": "python_module", "module": "unittest", "args": ["discover", "-s", "tests", "-p", "test_*.py", "-v"]}
    authorization = adapter.authorization_for(
        action, scope="stage20-synthetic-tests", confirmation_id=f"{label}-{uuid.uuid4().hex}"
    )
    result = adapter.execute(
        action, authorization=authorization, target_platform=target,
        toolchain_observation=observation, run_id=label, data_scope="synthetic-local-documents", timeout_seconds=15,
    )
    return {
        "status": result.get("status"), "exit_code": result.get("exit_code"),
        "stderr_summary": result.get("stderr_summary", ""),
        "target": result.get("capability_observation", {}).get("target"),
        "toolchain": result.get("capability_observation", {}).get("toolchain"),
        "target_toolchain_verified": result.get("capability_observation", {}).get("target_toolchain_verified"),
        "test_runtime": "python_module:unittest",
    }


def _run_scenario(spec: dict, ordinal: int) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"opencoding-stage20-{ordinal}-") as directory:
        root = Path(directory).resolve()
        adapter = LocalAgentAdapter(root)
        probe = probe_target_toolchain(spec["profile"], target_platform=spec["target"])
        matrix = adapter.capabilities(target_platform=spec["target"], toolchain_observation=probe)["capability_matrix"]
        generated = [_write(adapter, path, content, f"stage20-{ordinal}-write-{index}", probe, spec["target"])
                     for index, (path, content) in enumerate(spec["files"].items(), 1)]
        failed = _test(adapter, f"stage20-{ordinal}-test-before-fix", probe, spec["target"])

        repair_path, old, new = spec["repair"]
        repair_content = spec["files"][repair_path].replace(old, new)
        repaired = _write(adapter, repair_path, repair_content, f"stage20-{ordinal}-repair", probe, spec["target"])
        passed = _test(adapter, f"stage20-{ordinal}-test-after-fix", probe, spec["target"])

        # A tampered root authorization must fail before any destination write.
        rejected = {"status": "not_run", "code": None, "path_exists": False}
        reject_action = {"type": "write_text", "path": "review/rejected.txt", "content": "must not write\n"}
        reject_auth = adapter.authorization_for(reject_action, scope="stage20-rejection", confirmation_id=f"stage20-reject-{uuid.uuid4().hex}")
        reject_auth["root"] = str(root.parent)
        try:
            adapter.execute(reject_action, authorization=reject_auth, target_platform=spec["target"], toolchain_observation=probe, run_id=f"stage20-{ordinal}-rejected")
        except AgentAdapterError as error:
            rejected = {"status": "rejected_before_write", "code": error.code, "path_exists": (root / "review/rejected.txt").exists()}

        plan = preview_changes(root, {"review/rollback-probe.txt": "temporary\n"})
        applied = apply_changes(root, plan, approved_digest=plan["plan_digest"])
        rolled = rollback_changes(root, applied["transaction_id"]) if applied.get("transaction_id") else {"status": "not_run"}
        probe_path = root / "review/rollback-probe.txt"
        return {
            "name": spec["name"],
            "requirements": "synthetic domain fixture; no personal data or external service",
            "host_target": {
                "host": matrix["host"], "target": normalize_target_platform(spec["target"]),
                "toolchain_probe": probe,
                "toolchain": matrix["toolchain"],
                "target_toolchain_verified": matrix["compatibility"]["target_toolchain_verified"],
                "managed_loader": matrix["managed_loader"],
            },
            "generated": generated,
            "failure_repair": {
                "initial": failed, "repair": repaired, "after_fix": passed,
                "failure_observed": failed["status"] == "failed" and failed["exit_code"] != 0,
                "repair_passed": passed["status"] == "succeeded" and passed["exit_code"] == 0,
            },
            "authorization_rejection": rejected,
            "rollback": {
                "apply_status": applied.get("status"), "rollback_status": rolled.get("status"),
                "residual_paths": rolled.get("rollback_residual_paths", []),
                "probe_exists_after": probe_path.exists(),
            },
            "boundaries": {
                "synthetic_input": True, "synthetic_confirmation": True,
                "model_used": False, "provider_used": False, "external_actions": False,
                "python_test_runtime": "does not prove web/browser execution" if spec["target"] == "web" else "local CLI runtime only",
                "sandbox": False, "process_boundary": "same-user-subprocess",
            },
        }


def run() -> dict:
    results = [_run_scenario(spec, index) for index, spec in enumerate(SCENARIOS, 1)]
    return {
        "schema": "opencoding-stage20-multi-project-e2e-v1",
        "projects": results,
        "summary": {
            "projects": len(results),
            "all_failures_observed": all(item["failure_repair"]["failure_observed"] for item in results),
            "all_repairs_passed": all(item["failure_repair"]["repair_passed"] for item in results),
            "all_rollbacks_clean": all(item["rollback"]["rollback_status"] == "rolled_back" and not item["rollback"]["residual_paths"] and not item["rollback"]["probe_exists_after"] for item in results),
            "all_rejections_prewrite": all(item["authorization_rejection"]["status"] == "rejected_before_write" and not item["authorization_rejection"]["path_exists"] for item in results),
        },
        "boundary": "Two synthetic offline projects are evidence of this adapter path only; they do not prove universal Agent capability, browser execution, or deployment.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.is_absolute():
        parser.error("--output must be absolute")
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "summary": report["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
