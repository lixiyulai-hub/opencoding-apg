from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProjectEntryTests(unittest.TestCase):
    def _run(self, root: Path, *args: str) -> dict:
        result = subprocess.run(
            [sys.executable, "-m", "opencoding", "project", *args, "--root", str(root)],
            cwd=ROOT,
            env={**__import__("os").environ, "PYTHONPATH": str(ROOT)},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_init_and_status_are_restartable_cli_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            created = self._run(root, "init", "--idea", "一个离线命令行清单")
            self.assertEqual(created["status"], "needs_answers")
            status = self._run(root, "status")
            self.assertEqual(status["status"], "project_found")
            self.assertEqual(status["run"]["status"], "not_started")
            self.assertTrue((root / ".opencoding" / "project-entry.json").is_file())

    def test_plan_requires_all_answers_and_declares_fixture_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self._run(root, "init", "--idea", "一个离线命令行清单")
            answers = root / "answers.json"
            answers.write_text(json.dumps({"audience": "我"}, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "opencoding", "project", "plan", "--root", str(root), "--answers", str(answers), "--answers-origin", "fixture"],
                cwd=ROOT,
                env={**__import__("os").environ, "PYTHONPATH": str(ROOT)},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("answers 必须包含全部澄清问题", result.stderr)


if __name__ == "__main__":
    unittest.main()
