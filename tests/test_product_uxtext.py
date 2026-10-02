"""中文出口、错误码映射与中断来源诊断的契约测试。"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from opencoding import uxtext


def inventory(root: Path) -> dict[str, str]:
    entries: dict[str, str] = {}

    def visit(directory: Path) -> None:
        with os.scandir(directory) as children:
            for child in children:
                path = Path(child.path)
                relative = path.relative_to(root).as_posix()
                metadata = child.stat(follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    entries[relative] = "directory"
                    visit(path)
                elif stat.S_ISREG(metadata.st_mode):
                    entries[relative] = "file"

    visit(root)
    return entries


class ErrorMessageContractTests(unittest.TestCase):
    def test_known_status_codes_have_chinese_messages(self):
        for code in (
            "invalid_root",
            "execution_status_database_busy",
            "execution_status_unsafe_journal_state",
            "execution_status_database_unavailable",
            "status_read_failed",
        ):
            with self.subTest(code=code):
                message = uxtext.error_message(code)
                self.assertTrue(message)
                self.assertNotEqual(message, code)

    def test_unknown_code_falls_back_to_chinese_with_sanitized_code(self):
        message = uxtext.error_message("token=sk-test-1234567890")
        self.assertNotIn("sk-test-1234567890", message)

    def test_empty_code_falls_back_to_generic_chinese(self):
        self.assertTrue(uxtext.error_message(""))

    def test_render_progress_uses_chinese_labels(self):
        text = uxtext.render_progress("先沿用本地保存。", "没有跨设备要求。", "查询实现与测试。", "补充中文错误提示。", "暂无")
        for label in ("当前判断：", "依据：", "已完成：", "正在继续：", "需要你处理："):
            self.assertIn(label, text)

    def test_render_pause_uses_chinese_labels(self):
        text = uxtext.render_pause("下一步需要外发资料。", "先完成不联网部分。", "是否允许发送列明数据。")
        for label in ("当前暂停原因：", "我的建议：", "需要你决定："):
            self.assertIn(label, text)


class SourceClassificationTests(unittest.TestCase):
    def test_product_codes_classify_to_product_layer(self):
        self.assertEqual(uxtext.classify_source("execution_status_database_busy"), "product")
        self.assertEqual(uxtext.classify_source("session_revision_stale"), "product")
        self.assertEqual(uxtext.classify_source("approval_expired"), "product")

    def test_os_errors_classify_to_os_layer(self):
        self.assertEqual(uxtext.classify_source("", OSError("denied")), "os")
        self.assertEqual(uxtext.classify_source("invalid_root"), "os")

    def test_unknown_codes_stay_unknown(self):
        self.assertEqual(uxtext.classify_source("mystery_code"), "unknown")
        self.assertEqual(uxtext.classify_source(""), "unknown")


class InterruptionLedgerTests(unittest.TestCase):
    def test_record_and_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            event = uxtext.record_interruption(
                root,
                category="business_question",
                action="确认业务范围",
                reason="数据范围影响方案。",
                reason_code="outcome:needs_clarification",
                source_layer="product",
                auto_path="回答后继续生成方案。",
            )
            self.assertEqual(event["source_layer"], "product")
            events = uxtext.read_interruptions(root)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["category"], "business_question")
            self.assertEqual(events[0]["category_label"], "必要业务问题")

    def test_record_sanitizes_secret_shaped_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            event = uxtext.record_interruption(
                root,
                category="system_error",
                action="测试",
                reason="token=sk-test-1234567890",
            )
            self.assertNotIn("sk-test-1234567890", event["reason"])

    def test_unknown_category_or_layer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaises(ValueError):
                uxtext.record_interruption(root, category="nope", action="a", reason="b")
            with self.assertRaises(ValueError):
                uxtext.record_interruption(root, category="user_pause", action="a", reason="b", source_layer="nope")

    def test_read_on_empty_root_is_zero_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            before = inventory(root)
            self.assertEqual(uxtext.read_interruptions(root), [])
            self.assertEqual(uxtext.interruption_stats(root)["total"], 0)
            self.assertEqual(inventory(root), before)

    def test_stats_aggregate_by_category_and_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            uxtext.record_interruption(root, category="repeat_question", action="回答问题", reason="空回答。")
            uxtext.record_interruption(root, category="tool_forced", action="确认写入", reason="宿主工具要求确认。", source_layer="tool")
            stats = uxtext.interruption_stats(root)
            self.assertEqual(stats["total"], 2)
            self.assertEqual(stats["by_category"]["repeat_question"], 1)
            self.assertEqual(stats["by_category"]["tool_forced"], 1)
            self.assertEqual(stats["by_layer"]["tool"], 1)
            self.assertEqual(stats["unnecessary_questions"], 1)
            self.assertEqual(stats["tool_forced_confirmations"], 1)


if __name__ == "__main__":
    unittest.main()
