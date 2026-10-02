import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import opencoding.executor as executor_module
from opencoding.executor import Executor, action_digest


def _context(root, action):
    return {
        "schema_version": "1.0",
        "root": str(root.resolve()),
        "action_digest": action_digest(action),
        "targets": [action["path"]] if action["type"] in {"write_text", "node_script"} else [],
        "external": False,
        "cost_limit": 0,
        "data_scope": "synthetic",
        "irreversible": False,
    }


def _wait_for(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return path.exists()


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.executor = Executor(self.root)

    def test_write_text_is_real_and_hashed(self):
        action = {"type": "write_text", "path": "artifacts/result.txt", "content": "合成结果"}
        result = self.executor.execute(action, _context(self.root, action), run_id="run-write")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((self.root / "artifacts/result.txt").read_text(encoding="utf-8"), "合成结果")
        self.assertEqual(result["artifacts"][0]["sha256_kind"], "file_bytes")
        self.assertEqual(len(result["input_sha256"]), 64)
        self.assertFalse(result["live_verified"])

    def test_node_script_runs_existing_node_without_shell(self):
        script = {"type": "write_text", "path": "checks/test.mjs", "content": "console.log('OPENCODING_TESTS_RUN=1')\n"}
        self.executor.execute(script, _context(self.root, script), run_id="run-node-write")
        action = {"type": "node_script", "path": "checks/test.mjs", "args": []}
        result = self.executor.execute(action, _context(self.root, action), run_id="run-node-script")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("OPENCODING_TESTS_RUN=1", result["stdout_summary"])

    def test_unknown_shell_and_context_mismatch_are_rejected(self):
        for action in (
            {"type": "shell", "command": "echo unsafe"},
            {"type": "python_module", "module": "fixture_task", "args": [], "command": "unsafe"},
        ):
            with self.assertRaises(ValueError):
                self.executor.execute(action, {}, run_id="run-bad")
        action = {"type": "write_text", "path": "ok.txt", "content": "ok"}
        bad = dict(_context(self.root, action), external=True)
        with self.assertRaises(ValueError):
            self.executor.execute(action, bad)

    def test_context_scope_run_id_and_total_argument_size_are_bounded(self):
        action = {"type": "write_text", "path": "ok.txt", "content": "ok"}
        invalid_scope = dict(_context(self.root, action), data_scope={"unbounded": True})
        with self.assertRaises(ValueError):
            self.executor.execute(action, invalid_scope)
        with self.assertRaises(ValueError):
            self.executor.execute(action, _context(self.root, action), run_id="bad run id")
        module_action = {"type": "python_module", "module": "fixture_task", "args": ["x" * 4096] * 5}
        with self.assertRaises(ValueError):
            self.executor.execute(module_action, _context(self.root, module_action))

    def test_paths_links_and_secrets_are_rejected(self):
        action = {"type": "write_text", "path": "../outside.txt", "content": "x"}
        with self.assertRaises(ValueError):
            self.executor.execute(action, _context(self.root, action))
        secret = {"type": "write_text", "path": "secret.txt", "content": "Bearer fake-super-secret-token-123456"}
        with self.assertRaises(ValueError):
            self.executor.execute(secret, _context(self.root, secret))
        with self.assertRaises(ValueError):
            Executor(".")

    def test_pre_cancelled_actions_have_no_side_effect_or_spawn(self):
        cancelled = threading.Event()
        cancelled.set()
        write_action = {"type": "write_text", "path": "should-not-exist.txt", "content": "synthetic"}
        with mock.patch.object(executor_module, "_atomic_write", side_effect=AssertionError("pre-cancelled write was attempted")):
            write_result = self.executor.execute(write_action, _context(self.root, write_action), run_id="run-pre-cancel-write", cancel_event=cancelled)
        self.assertEqual(write_result["status"], "cancelled")
        self.assertTrue(write_result["cancelled"])
        self.assertFalse(write_result["timed_out"])
        self.assertIsNone(write_result["exit_code"])
        self.assertEqual(write_result["artifacts"], [])
        self.assertFalse((self.root / write_action["path"]).exists())

        module_action = {"type": "python_module", "module": "synthetic_fixture", "args": []}
        with mock.patch.object(executor_module.subprocess, "Popen", side_effect=AssertionError("pre-cancelled module was started")):
            module_result = self.executor.execute(module_action, _context(self.root, module_action), run_id="run-pre-cancel-module", cancel_event=cancelled)
        self.assertEqual(module_result["status"], "cancelled")
        self.assertTrue(module_result["cancelled"])
        self.assertFalse(module_result["timed_out"])
        self.assertIsNone(module_result["exit_code"])
        self.assertEqual(module_result["artifacts"], [])

    def test_nonzero_taskkill_falls_back_to_owned_process_with_bounded_wait(self):
        cancelled = threading.Event()

        class FakeProcess:
            def __init__(self):
                self.pid = 12345
                self.stdout = tempfile.TemporaryFile()
                self.stderr = tempfile.TemporaryFile()
                self.killed = False
                self.wait_timeouts = []

            def poll(self):
                if self.killed:
                    return -9
                cancelled.set()
                return None

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                self.wait_timeouts.append(timeout)
                if timeout is None:
                    raise AssertionError("process wait must be bounded")
                return -9

        process = FakeProcess()
        action = {"type": "python_module", "module": "synthetic_fixture", "args": []}
        with (
            mock.patch.object(executor_module.os, "name", "nt"),
            mock.patch.object(executor_module.subprocess, "Popen", return_value=process),
            mock.patch.object(executor_module.subprocess, "run", return_value=mock.Mock(returncode=1)),
            mock.patch.object(executor_module, "_windows_pipe_available", return_value=None),
        ):
            result = self.executor.execute(action, _context(self.root, action), run_id="run-taskkill-fallback", cancel_event=cancelled)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(process.killed)
        self.assertTrue(process.wait_timeouts)
        self.assertTrue(all(timeout is not None for timeout in process.wait_timeouts))

    def test_inherited_output_pipes_finalize_without_waiting_for_descendant(self):
        module = self.root / "inherited_pipe_fixture.py"
        module.write_text(
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'])\n"
            "print('parent stdout', flush=True)\n"
            "print('parent stderr', file=sys.stderr, flush=True)\n",
            encoding="utf-8",
        )
        action = {"type": "python_module", "module": "inherited_pipe_fixture", "args": []}
        started = time.monotonic()
        with mock.patch.object(executor_module, "_cancel_windows_synchronous_io", create=True) as cancel_io:
            result = self.executor.execute(action, _context(self.root, action), run_id="run-inherited-pipes", timeout_seconds=0.5)
        cancel_io.assert_not_called()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.5)
        self.assertEqual(result["status"], "succeeded")
        self.assertIn("parent stdout", result["stdout_summary"])
        self.assertIn("parent stderr", result["stderr_summary"])
        self.assertFalse(any(thread.is_alive() and thread.name.startswith("opencoding-output-") for thread in threading.enumerate()))

    def test_idle_windows_pipe_stops_without_starting_a_read(self):
        stream = mock.Mock()
        stream.fileno.return_value = 123
        collector = executor_module._Collector(stream)
        inspected = threading.Event()

        def no_data(_fd):
            inspected.set()
            return 0

        with (
            mock.patch.object(executor_module.os, "name", "nt"),
            mock.patch.object(executor_module, "_windows_pipe_available", side_effect=no_data),
            mock.patch.object(executor_module.os, "read", side_effect=AssertionError("empty pipe read")),
        ):
            reader = threading.Thread(target=collector.run)
            reader.start()
            try:
                self.assertTrue(inspected.wait(1))
            finally:
                collector.stop()
                reader.join(1)
            self.assertFalse(reader.is_alive())
            self.assertIsNone(collector.error)
            collector.close_after_join()
        stream.close.assert_called_once()

    def test_reader_failure_closes_both_streams_before_reporting_error(self):
        streams = [tempfile.TemporaryFile(), tempfile.TemporaryFile()]
        collectors = [executor_module._Collector(stream) for stream in streams]
        readers = [threading.Thread(target=collector.run) for collector in collectors]
        try:
            with (
                mock.patch.object(executor_module.os, "name", "nt"),
                mock.patch.object(executor_module, "_windows_pipe_available", side_effect=OSError("synthetic unavailable")),
            ):
                for reader in readers:
                    reader.start()
                with self.assertRaisesRegex(RuntimeError, "output collector failed"):
                    executor_module._finalize_collectors(list(zip(collectors, readers)))
            self.assertTrue(all(stream.closed for stream in streams))
            self.assertFalse(any(reader.is_alive() for reader in readers))
        finally:
            for collector, reader in zip(collectors, readers):
                collector.stop()
                reader.join(1)
            for stream in streams:
                stream.close()

    def test_large_output_remains_bounded(self):
        module = self.root / "large_output_fixture.py"
        module.write_text("import sys\nsys.stdout.write('x' * 20000)\n", encoding="utf-8")
        action = {"type": "python_module", "module": "large_output_fixture", "args": []}
        result = self.executor.execute(action, _context(self.root, action), run_id="run-large-output")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(result["stdout_summary"]), executor_module.MAX_OUTPUT_BYTES)

    def test_python_module_nonzero_timeout_and_cancel(self):
        module = self.root / "fixture_task.py"
        module.write_text(
            "import sys\n"
            "if sys.argv[1] == 'fail': print('failure'); raise SystemExit(7)\n"
            "if sys.argv[1] == 'spin':\n import time\n while True: time.sleep(0.01)\n"
            "print('ok')\n",
            encoding="utf-8",
        )
        action = {"type": "python_module", "module": "fixture_task", "args": ["fail"]}
        result = self.executor.execute(action, _context(self.root, action), run_id="run-fail")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 7)
        action = {"type": "python_module", "module": "fixture_task", "args": ["spin"]}
        result = self.executor.execute(action, _context(self.root, action), run_id="run-timeout", timeout_seconds=0.05)
        self.assertEqual(result["status"], "timed_out")
        self.assertTrue(result["timed_out"])
        event = threading.Event()
        thread_result = []
        thread = threading.Thread(target=lambda: thread_result.append(self.executor.execute(action, _context(self.root, action), run_id="run-cancel", timeout_seconds=5, cancel_event=event)))
        try:
            thread.start()
            event.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(thread_result), 1)
            self.assertEqual(thread_result[0]["status"], "cancelled")
        finally:
            event.set()
            thread.join(3)

    def test_python_environment_blocks_injection_without_clearing_platform_variables(self):
        module = self.root / "fixture_env.py"
        module.write_text("import os\nprint(os.environ.get('PYTHONPATH'))\n", encoding="utf-8")
        action = {"type": "python_module", "module": "fixture_env", "args": []}
        environment = dict(os.environ)
        environment["PYTHONPATH"] = "outside-root"
        environment["SYSTEMROOT"] = "fixture-system-root"
        with mock.patch.dict(os.environ, environment, clear=True):
            result = self.executor.execute(action, _context(self.root, action), run_id="run-environment")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["stdout_summary"].strip(), "None")

    def test_timeout_terminates_a_spawned_child_tree(self):
        module = self.root / "fixture_tree.py"
        module.write_text(
            "from pathlib import Path\n"
            "import subprocess, sys\n"
            "root = Path.cwd()\n"
            "release = root / 'release-child'\n"
            "result = root / 'child-survived'\n"
            "code = \"from pathlib import Path; import sys, time; release=Path(sys.argv[1]); result=Path(sys.argv[2]);\\nwhile not release.exists(): time.sleep(0.01)\\nresult.write_text('survived', encoding='utf-8')\"\n"
            "subprocess.Popen([sys.executable, '-c', code, str(release), str(result)])\n"
            "(root / 'child-ready').write_text('ready', encoding='utf-8')\n"
            "import time\n"
            "while True: time.sleep(0.01)\n",
            encoding="utf-8",
        )
        action = {"type": "python_module", "module": "fixture_tree", "args": []}
        cancelled = threading.Event()
        result_holder = []
        thread = threading.Thread(
            target=lambda: result_holder.append(
                self.executor.execute(action, _context(self.root, action), run_id="run-tree", timeout_seconds=5, cancel_event=cancelled)
            )
        )
        release = self.root / "release-child"
        try:
            thread.start()
            self.assertTrue(_wait_for(self.root / "child-ready", 2), "fixture child did not start")
            cancelled.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(result_holder), 1)
            self.assertEqual(result_holder[0]["status"], "cancelled")
            release.write_text("release", encoding="utf-8")
            self.assertFalse(_wait_for(self.root / "child-survived", 1), "child survived cancellation")
        finally:
            cancelled.set()
            if not release.exists():
                release.write_text("release", encoding="utf-8")
            thread.join(5)
            self.assertFalse(thread.is_alive(), "executor thread did not finish")


if __name__ == "__main__":
    unittest.main()
