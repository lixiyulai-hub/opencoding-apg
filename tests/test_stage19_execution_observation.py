from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from opencoding.agent_adapter import LocalAgentAdapter


class Stage19ExecutionObservationTests(unittest.TestCase):
    def test_local_write_observes_target_without_claiming_toolchain(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalAgentAdapter(Path(directory).resolve())
            action = {"type": "write_text", "path": "notes.txt", "content": "offline\n"}
            authorization = adapter.authorization_for(action, scope="stage19-synthetic")
            result = adapter.execute(
                action,
                authorization=authorization,
                target_platform="windows",
                run_id="stage19-observe-write",
            )
            observation = result["capability_observation"]
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(observation["host"]["family"], "linux")
            self.assertEqual(observation["target"]["normalized"], "windows")
            self.assertTrue(observation["target"]["recognized"])
            self.assertFalse(observation["target_toolchain_verified"])
            self.assertEqual(observation["toolchain"]["status"], "unverified")
            self.assertFalse(observation["model"]["available"])
            self.assertFalse(observation["external"]["adapter_calls"])
            self.assertIsNone(observation["managed_loader"]["observed"])

    def test_python_module_observation_preserves_uncontrolled_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalAgentAdapter(Path(directory).resolve())
            action = {"type": "python_module", "module": "unittest", "args": ["--help"]}
            authorization = adapter.authorization_for(action, scope="stage19-synthetic")
            result = adapter.execute(
                action,
                authorization=authorization,
                target_platform="cli",
                run_id="stage19-observe-python",
            )
            observation = result["capability_observation"]
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(observation["action_type"], "python_module")
            self.assertEqual(observation["target"]["normalized"], "cli")
            self.assertEqual(observation["external"]["python_module_side_effects"], "not-controlled")
            self.assertFalse(observation["sandbox"]["enabled"])
            self.assertEqual(observation["process_boundary"], "same-user-subprocess")


if __name__ == "__main__":
    unittest.main()
