from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from opencoding.agent_adapter import AgentAdapterError, LocalAgentAdapter
from opencoding.transactions import apply_changes, preview_changes, rollback_changes


class Stage15AuthorizationRecoveryTests(unittest.TestCase):
    def test_confirmation_binding_rejects_tampering_and_cross_project_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first_root, second_root = base / "first", base / "second"
            first_root.mkdir()
            second_root.mkdir()
            first, second = LocalAgentAdapter(first_root), LocalAgentAdapter(second_root)
            action = {"type": "write_text", "path": "bound.txt", "content": "first"}
            authorization = first.authorization_for(action, confirmation_id="synthetic-first", scope="project-first")
            with self.assertRaises(AgentAdapterError):
                first.execute(action, authorization={**authorization, "confirmation_id": "tampered"})
            with self.assertRaises(AgentAdapterError):
                second.execute(action, authorization=authorization)
            self.assertFalse((first_root / "bound.txt").exists())
            self.assertFalse((second_root / "bound.txt").exists())

    def test_transaction_root_binding_rejects_moved_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original, moved = base / "original", base / "moved"
            original.mkdir()
            (original / "state.txt").write_text("before", encoding="utf-8")
            plan = preview_changes(original, {"state.txt": "after"})
            applied = apply_changes(original, plan, approved_digest=plan["plan_digest"])
            original.rename(moved)
            blocked = rollback_changes(moved, applied["transaction_id"])
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual((moved / "state.txt").read_text(encoding="utf-8"), "after")
            receipt = json.loads((moved / ".opencoding" / "transactions" / applied["transaction_id"] / "manifest.json").read_text())
            self.assertEqual(receipt["root"], str(original.resolve()))

    def test_recovery_helper_reports_legacy_receipt_as_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.mkdir(exist_ok=True)
            (root / "state.txt").write_text("before", encoding="utf-8")
            plan = preview_changes(root, {"state.txt": "after"})
            applied = apply_changes(root, plan, approved_digest=plan["plan_digest"])
            transaction = root / ".opencoding" / "transactions" / applied["transaction_id"]
            manifest_path = transaction / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest.pop("root")
            from opencoding.safety import canonical_json, sha256_bytes
            manifest_bytes = canonical_json(manifest) + b"\n"
            manifest_path.write_bytes(manifest_bytes)
            receipt_path = transaction / "receipt.json"
            receipt = json.loads(receipt_path.read_text())
            receipt["manifest_sha256"] = sha256_bytes(manifest_bytes.rstrip(b"\n"))
            receipt_path.write_text(json.dumps(receipt, separators=(",", ":")) + "\n")
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "recover_skill_transaction.py"),
                 "--execution-root", str(root), "--transaction-id", applied["transaction_id"], "--rollback"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
