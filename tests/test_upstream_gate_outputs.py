"""Isolated candidate controller tests; never mutate the installed APG."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

CANDIDATE = Path(__file__).resolve().parents[1] / "compat/apg-output-contract"
sys.path.insert(0, str(CANDIDATE))
from project_governance.commands.check import run_check, _load_bound_policy
from project_governance.model import GovernanceLevel, Policy
from project_governance.storage import dump_policy_toml
sys.path.remove(str(CANDIDATE))


def policy(root, commands):
    gates = [{"gate_id": name, "phase": "fast", "command": (sys.executable, "-B", "-c", command),
              "timeout_seconds": timeout, "options": {"output_dir": ".cache/gate-runs/" + name} if declared else {}}
             for name, command, declared, timeout in commands]
    path = root / ".governance/policy.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(dump_policy_toml(Policy("1.0", "0.1.0", GovernanceLevel.G1, (), (), (), tuple(gates), ())), encoding="utf-8")
    return _load_bound_policy(root, required=True)


WRITE = "import os,pathlib,sqlite3; p=pathlib.Path(os.environ['APG_GATE_OUTPUT_DIR']); c=sqlite3.connect(p/'test.sqlite'); c.execute('create table sample(id int)'); c.commit(); c.close(); (p/'probe.log').write_text('ok')"


class GateOutputTests(unittest.TestCase):
    def test_legal_sqlite_outputs_and_receipt_retained(self):
        for precreated in (False, True):
            with self.subTest(precreated=precreated), tempfile.TemporaryDirectory() as d:
                root = Path(d).resolve()
                if precreated:
                    (root / ".cache").mkdir()
                gates, digest = policy(root, [("tests", WRITE, True, 10)])
                result = run_check(root, gates, policy_digest=digest)
                self.assertEqual(result.exit_code, 0, result.receipt)
                row = result.receipt.outputs["bounded_outputs"][0]
                self.assertEqual(len(row["files"]), 2)
                self.assertEqual(row["scope"], "passed")
                self.assertTrue((root / row["run_directory"] / "test.sqlite").exists())
                self.assertEqual(len(list((root / ".governance/receipts").glob("*.json"))), 1)

    def test_default_readonly_still_rejects_cache_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / ".cache").mkdir()
            gates, digest = policy(root, [("tests", "from pathlib import Path; Path('.cache/probe.txt').write_text('ok')", False, 10)])
            result = run_check(root, gates, policy_digest=digest)
            self.assertEqual(result.exit_code, 4)
            self.assertEqual(result.gate_run.evidence[0].process_exit_code, 0)

    def test_source_and_undeclared_writes_fail_and_stop_next_gate(self):
        for path in ("source.py", ".cache/elsewhere.txt"):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as d:
                root = Path(d).resolve()
                command = WRITE + "; pathlib.Path(" + repr(path) + ").write_text('bad')"
                gates, digest = policy(root, [("a", command, True, 10), ("b", WRITE, True, 10)])
                result = run_check(root, gates, policy_digest=digest)
                self.assertEqual(result.exit_code, 4)
                self.assertEqual(len(result.checks), 1)
                self.assertTrue((root / path).exists())

    def test_failure_and_timeout_are_not_passed_and_keep_outputs(self):
        for suffix, timeout, expected in (("; raise SystemExit(1)", 10, 1), ("; import time; time.sleep(5)", 1, 1)):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as d:
                root = Path(d).resolve()
                gates, digest = policy(root, [("tests", WRITE + suffix, True, timeout)])
                result = run_check(root, gates, policy_digest=digest)
                self.assertNotEqual(result.exit_code, 0)
                self.assertEqual(result.receipt.outputs["bounded_outputs"][0]["scope"], "passed")
                self.assertEqual(len(result.receipt.outputs["bounded_outputs"][0]["files"]), 2)

    def test_other_gate_cannot_write_previous_output(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            attack = "from pathlib import Path; next(Path('.cache/gate-runs/a').glob('*/probe.log')).write_text('changed')"
            gates, digest = policy(root, [("a", WRITE, True, 10), ("b", attack, True, 10)])
            self.assertEqual(run_check(root, gates, policy_digest=digest).exit_code, 4)

    def test_policy_drift_invalidates_binding(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            gates, digest = policy(root, [("tests", WRITE, True, 10)])
            policy(root, [("changed", WRITE, True, 10)])
            with self.assertRaises(ValueError):
                run_check(root, gates, policy_digest=digest)
            self.assertFalse((root / ".cache").exists())

    def test_link_output_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "source.txt").write_text("original")
            command = "import os,pathlib; os.link('source.txt', pathlib.Path(os.environ['APG_GATE_OUTPUT_DIR'])/'link.txt')"
            gates, digest = policy(root, [("tests", command, True, 10)])
            result = run_check(root, gates, policy_digest=digest)
            self.assertEqual(result.exit_code, 4)
            self.assertEqual((root / "source.txt").read_text(), "original")
