from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from opencoding.agent_adapter import AgentAdapterError, LocalAgentAdapter


class AgentAdapterTests(unittest.TestCase):
    def test_capabilities_separate_host_and_target_and_mark_model_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            report = LocalAgentAdapter(Path(directory)).capabilities(target_platform="web")
            self.assertEqual(report["target_platform"], "web")
            self.assertIn("os", report["host"])
            self.assertFalse(report["model"]["available"])
            self.assertFalse(report["external"])
            self.assertFalse(report["sandbox"])
            self.assertEqual(report["process_boundary"], "same-user-subprocess")

    def test_real_local_write_requires_authorization_and_records_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalAgentAdapter(Path(directory).resolve())
            action = {"type": "write_text", "path": "hello.txt", "content": "你好"}
            with self.assertRaises(AgentAdapterError):
                adapter.execute(action, authorization={"approved": False})
            authorization = adapter.authorization_for(action, scope="synthetic-user-confirmation")
            result = adapter.execute(action, authorization=authorization, run_id="adapter-write")
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["adapter_id"], "local-structured-python")
            self.assertEqual(result["authorization_binding"]["action_digest"], authorization["action_digest"])
            self.assertEqual((Path(directory) / "hello.txt").read_text(encoding="utf-8"), "你好")

    def test_malformed_actions_return_structured_adapter_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalAgentAdapter(Path(directory).resolve())
            for action in ({"type": "write_text"}, {"type": "shell", "command": "echo unsafe"}):
                with self.assertRaises(AgentAdapterError) as ctx:
                    adapter.execute(action, authorization={"approved": True})
                self.assertEqual(ctx.exception.code, "action_invalid")

    def test_binding_rejects_wrong_root_target_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalAgentAdapter(Path(directory).resolve())
            action = {"type": "write_text", "path": "bound.txt", "content": "bound"}
            authorization = adapter.authorization_for(action)
            for mutate in (
                {"root": str(Path(directory).parent)},
                {"targets": ["other.txt"]},
                {"expires_at": "2000-01-01T00:00:00Z"},
                {"action_digest": "0" * 64},
                {"confirmation_id": "changed-confirmation"},
                {"scope": "changed-scope"},
            ):
                bad = dict(authorization)
                bad.update(mutate)
                with self.assertRaises(AgentAdapterError) as ctx:
                    adapter.execute(action, authorization=bad)
                self.assertIn(ctx.exception.code, {"authorization_binding_invalid", "authorization_expired"})
            self.assertFalse((Path(directory) / "bound.txt").exists())

    def test_authorization_cannot_cross_projects_or_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            first_root, second_root = Path(directory) / "first", Path(directory) / "second"
            first_root.mkdir()
            second_root.mkdir()
            first, second = LocalAgentAdapter(first_root), LocalAgentAdapter(second_root)
            action = {"type": "write_text", "path": "bound.txt", "content": "first"}
            authorization = first.authorization_for(action, scope="project-first", confirmation_id="synthetic-first")
            self.assertIn("confirmation_binding", authorization)
            with self.assertRaises(AgentAdapterError) as ctx:
                second.execute(action, authorization=authorization)
            self.assertEqual(ctx.exception.code, "authorization_binding_invalid")
            changed = dict(action, content="second")
            with self.assertRaises(AgentAdapterError):
                first.execute(changed, authorization=authorization)
            self.assertFalse((first_root / "bound.txt").exists())
            self.assertFalse((second_root / "bound.txt").exists())
            receipt = first.execute(action, authorization=authorization)
            self.assertEqual(receipt["authorization_binding"]["confirmation_binding"], authorization["confirmation_binding"])
            self.assertEqual(receipt["authorization_binding"]["scope"], "project-first")

    def test_symlink_root_is_rejected_before_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "real"
            real.mkdir()
            alias = Path(directory) / "alias"
            try:
                alias.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation unavailable")
            with self.assertRaises(AgentAdapterError) as ctx:
                LocalAgentAdapter(alias)
            self.assertEqual(ctx.exception.code, "root_invalid")


if __name__ == "__main__":
    unittest.main()
