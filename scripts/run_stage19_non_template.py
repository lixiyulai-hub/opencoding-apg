#!/usr/bin/env python3
"""Run one offline, non-template project through the local Agent adapter.

The fixture is a small budget ledger chosen for this stage.  It is generated
from explicit synthetic requirements, then tested with a real Python
subprocess.  The script records the initial failure, a separately authorized
repair, and a transaction-layer rollback probe.  It never invokes a model,
provider, network, shell, browser, or target-specific toolchain.
"""

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

from opencoding.agent_adapter import LocalAgentAdapter
from opencoding.host_capabilities import normalize_target_platform
from opencoding.transactions import apply_changes, preview_changes, rollback_changes


TARGET = "cli"


def _write(adapter: LocalAgentAdapter, root: Path, relative: str, content: str, index: int) -> dict:
    action = {"type": "write_text", "path": relative, "content": content}
    authorization = adapter.authorization_for(
        action,
        scope="stage19-synthetic-project-generation",
        confirmation_id=f"stage19-write-{index}-{uuid.uuid4().hex}",
    )
    result = adapter.execute(
        action,
        authorization=authorization,
        target_platform=TARGET,
        run_id=f"stage19-write-{index}",
        data_scope="synthetic-local-documents",
    )
    return result


def _run_tests(adapter: LocalAgentAdapter, run_id: str) -> dict:
    action = {
        "type": "python_module",
        "module": "unittest",
        "args": ["discover", "-s", "tests", "-p", "test_*.py", "-v"],
    }
    authorization = adapter.authorization_for(
        action,
        scope="stage19-synthetic-project-test",
        confirmation_id=f"stage19-test-{run_id}-{uuid.uuid4().hex}",
    )
    return adapter.execute(
        action,
        authorization=authorization,
        target_platform=TARGET,
        run_id=run_id,
        data_scope="synthetic-local-documents",
        timeout_seconds=15,
    )


def _public_result(result: dict) -> dict:
    observation = result.get("capability_observation", {})
    return {
        "status": result.get("status"),
        "exit_code": result.get("exit_code"),
        "run_id": result.get("run_id"),
        "action_digest": result.get("action_digest"),
        "artifacts": result.get("artifacts", []),
        "stderr_summary": result.get("stderr_summary", ""),
        "stdout_summary": result.get("stdout_summary", ""),
        "target": observation.get("target"),
        "target_toolchain_verified": observation.get("target_toolchain_verified"),
        "toolchain": observation.get("toolchain"),
        "external": observation.get("external"),
        "model": observation.get("model"),
        "sandbox": observation.get("sandbox"),
        "managed_loader": observation.get("managed_loader"),
        "boundary": observation.get("boundary"),
    }


def run() -> dict:
    requirements = {
        "idea": "把一周家庭支出按类别汇总，输出可测试的命令行核心",
        "target_label": TARGET,
        "inputs": "合成的三笔支出记录；不含密钥、个人数据或外部服务",
        "acceptance": ["分类合计正确", "测试失败可修复", "事务回滚无残留"],
    }
    with tempfile.TemporaryDirectory(prefix="opencoding-stage19-ledger-") as directory:
        root = Path(directory).resolve()
        adapter = LocalAgentAdapter(root)
        matrix = adapter.capabilities(target_platform=TARGET)["capability_matrix"]

        files = {
            "AGENTS.md": "# Ledger project agent notes\nKeep the core offline and testable.\n",
            "plan.md": "# Ledger plan\n1. Categorize rows. 2. Run tests. 3. Review the receipt.\n",
            "README.md": "# Family Ledger CLI\nA tiny offline category summary for synthetic rows.\n",
            "src/ledger.py": (
                "def total_by_category(rows):\n"
                "    totals = {}\n"
                "    for row in rows:\n"
                "        category = row['categoy']\n"
                "        totals[category] = totals.get(category, 0) + row['amount']\n"
                "    return totals\n"
            ),
            "tests/test_ledger.py": (
                "import unittest\n"
                "from src.ledger import total_by_category\n\n"
                "\nclass LedgerTests(unittest.TestCase):\n"
                "    def test_category_totals(self):\n"
                "        rows = [\n"
                "            {'category': 'food', 'amount': 12},\n"
                "            {'category': 'travel', 'amount': 8},\n"
                "            {'category': 'food', 'amount': 5},\n"
                "        ]\n"
                "        self.assertEqual(total_by_category(rows), {'food': 17, 'travel': 8})\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            ),
        }
        generated = []
        for index, (relative, content) in enumerate(files.items(), 1):
            generated.append({"path": relative, **_public_result(_write(adapter, root, relative, content, index))})

        failed = _run_tests(adapter, "stage19-test-before-fix")
        repair = _write(
            adapter,
            root,
            "src/ledger.py",
            files["src/ledger.py"].replace("row['categoy']", "row['category']"),
            len(files) + 1,
        )
        passed = _run_tests(adapter, "stage19-test-after-fix")

        rollback_plan = preview_changes(root, {"notes/rollback-probe.txt": "temporary evidence\n"})
        applied = apply_changes(root, rollback_plan, approved_digest=rollback_plan["plan_digest"])
        rollback = rollback_changes(root, applied["transaction_id"]) if applied.get("transaction_id") else {"status": "not_run"}
        probe_path = root / "notes/rollback-probe.txt"

        return {
            "schema": "opencoding-stage19-non-template-e2e-v1",
            "project": "family-ledger-cli",
            "requirements": requirements,
            "input_and_confirmation": {
                "synthetic": True,
                "user_confirmation": "synthetic fixture labels only",
                "model_used": False,
                "provider_used": False,
                "external_actions": False,
            },
            "host_target": {
                "host": matrix["host"],
                "target": normalize_target_platform(TARGET),
                "toolchain": matrix["toolchain"],
                "target_toolchain_verified": matrix["compatibility"]["target_toolchain_verified"],
                "managed_loader": matrix["managed_loader"],
            },
            "generated_files": generated,
            "failure_and_fix": {
                "initial_test": _public_result(failed),
                "repair": _public_result(repair),
                "after_fix_test": _public_result(passed),
                "failure_was_observed": failed.get("status") == "failed" and failed.get("exit_code") != 0,
                "repair_passed": passed.get("status") == "succeeded" and passed.get("exit_code") == 0,
            },
            "rollback": {
                "preview_status": rollback_plan.get("status"),
                "apply_status": applied.get("status"),
                "rollback_status": rollback.get("status"),
                "rollback_residual_paths": rollback.get("rollback_residual_paths", []),
                "probe_exists_after": probe_path.exists(),
                "transaction_receipt_recorded": bool(applied.get("receipt_path")),
            },
            "boundary": {
                "statement": "This is one synthetic offline CLI project; it does not prove universal Agent capability or target execution.",
                "python_module": "same-user subprocess; arbitrary side effects and network are not controlled; rollback is not automatic",
                "sandbox": False,
                "process_boundary": "same-user-subprocess",
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.is_absolute():
        parser.error("--output must be an absolute path")
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "status": "ok", "project": report["project"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
