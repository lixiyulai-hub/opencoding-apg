from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from opencoding.agent_adapter import AgentAdapterError, LocalAgentAdapter
from opencoding.transactions import apply_changes, preview_changes, rollback_changes


class Stage16ReplayPolicyTests(unittest.TestCase):
    def test_confirmation_binding_is_consumed_once_per_local_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = LocalAgentAdapter(root)
            action = {"type": "write_text", "path": "once.txt", "content": "first"}
            authorization = adapter.authorization_for(action, confirmation_id="one-time", scope="stage16")
            first = adapter.execute(action, authorization=authorization, run_id="stage16-first")
            self.assertEqual(first["status"], "succeeded")
            with self.assertRaises(AgentAdapterError) as ctx:
                adapter.execute(action, authorization=authorization, run_id="stage16-replay")
            self.assertEqual(ctx.exception.code, "authorization_replayed")
            claim_dir = root / ".opencoding" / "authorizations"
            self.assertEqual(len(list(claim_dir.glob("*.json"))), 1)

    def test_rollback_does_not_restore_a_consumable_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = LocalAgentAdapter(root)
            action = {"type": "write_text", "path": "rollback.txt", "content": "first"}
            authorization = adapter.authorization_for(action, confirmation_id="rollback-once", scope="stage16")
            adapter.execute(action, authorization=authorization, run_id="stage16-rollback-first")
            plan = preview_changes(root, {"rollback.txt": "changed"})
            applied = apply_changes(root, plan, approved_digest=plan["plan_digest"])
            self.assertEqual(rollback_changes(root, applied["transaction_id"])["status"], "rolled_back")
            with self.assertRaises(AgentAdapterError) as ctx:
                adapter.execute(action, authorization=authorization, run_id="stage16-rollback-replay")
            self.assertEqual(ctx.exception.code, "authorization_replayed")


if __name__ == "__main__":
    unittest.main()
