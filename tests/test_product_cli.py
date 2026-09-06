"""Real subprocess coverage for the Chinese W2 entry point."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from opencoding.intake import QUESTION_DEFINITIONS
from opencoding.service import create_session, preview_session, submit_answer


ROOT = Path(__file__).resolve().parents[1]


def inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def journey_input(goal: str, confirmation: str, *, audience: str = "社区居民") -> str:
    answers = [goal, audience, "网页", "登记借用并确认归还"]
    answers.extend(["不需要"] * (len(QUESTION_DEFINITIONS) - 3))
    answers.append(confirmation)
    return "\n".join(answers) + "\n"


def run_cli(root: Path, text: str = "", *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CARGO_NET_OFFLINE"] = "true"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "opencoding", "--root", str(root), *args],
        cwd=ROOT,
        input=text,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


class ProductCliSubprocessTests(unittest.TestCase):
    def test_help_and_host_boundary_are_available_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_cli(Path(directory), "", "--help")
            self.assertEqual(result.returncode, 0)
            self.assertIn("中文入口", result.stdout)

    def test_rejection_journey_writes_no_business_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_cli(root, journey_input("社区借还工具", "拒绝"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("已拒绝", result.stdout)
            self.assertIn("Host 未接通", result.stdout)
            self.assertFalse((root / "AGENTS.md").exists())
            self.assertFalse((root / "memory.md").exists())

    def test_confirmed_journey_generates_documents_and_can_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_cli(root, journey_input("社区借还工具", "确认"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("文档已生成", result.stdout)
            self.assertIn("外部能力仍未接通", result.stdout)
            match = re.search(r"事务：([^；\s]+)", result.stdout)
            self.assertIsNotNone(match, result.stdout)
            transaction_id = match.group(1)
            self.assertTrue((root / "AGENTS.md").exists())
            rolled = run_cli(root, "", "--rollback", transaction_id)
            self.assertEqual(rolled.returncode, 0, rolled.stderr)
            self.assertIn("rolled_back", rolled.stdout)
            self.assertFalse((root / "AGENTS.md").exists())

    def test_eof_pause_and_resume_keep_the_same_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paused = run_cli(root, "暂停目标\n社区居民\n")
            self.assertEqual(paused.returncode, 0, paused.stderr)
            self.assertIn("已暂停", paused.stdout)
            match = re.search(r"session-[0-9a-f]+", paused.stdout)
            self.assertIsNotNone(match, paused.stdout)
            session_id = match.group(0)
            remaining = "网页\n登记借用并确认归还\n" + "\n".join(["不需要"] * (len(QUESTION_DEFINITIONS) - 3)) + "\n拒绝\n"
            resumed = run_cli(root, remaining, "--resume", session_id)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertIn("已拒绝", resumed.stdout)
            listing = run_cli(root, "", "--list")
            self.assertEqual(listing.returncode, 0)
            self.assertIn(session_id, listing.stdout)

    def test_change_command_shows_updated_diff_and_invalidates_old_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = create_session(root, "差异工具")
            session_id = view["session"]["id"]
            changed = run_cli(root, "", "--change", session_id, "audience", "学生")
            self.assertEqual(changed.returncode, 0, changed.stderr)
            self.assertIn("答案已修改", changed.stdout)
            self.assertIn("更新后的方案与差异", changed.stdout)

    def test_preview_command_is_zero_write_including_existing_guard_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = create_session(root, "只读预览")
            before = inventory(root)
            result = run_cli(root, "", "--preview", view["session"]["id"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("只读预览（未写入）", result.stdout)
            self.assertEqual(inventory(root), before)


if __name__ == "__main__":
    unittest.main()
