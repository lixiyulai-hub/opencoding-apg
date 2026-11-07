"""Cross-platform APG/native document integration; no installed APG required."""
from pathlib import Path
import tempfile
import unittest
import os
from unittest import mock

from opencoding import service
from tests.test_product_service import _complete


class UpstreamDocumentTests(unittest.TestCase):
    def test_concurrent_edit_during_merge_cannot_be_baked_into_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session = _complete(root)["session"]["id"]
            original = service.merge_documents
            def changed(*args):
                result = original(*args)
                (root / "AGENTS.md").write_text("concurrent user edit", encoding="utf-8")
                return result
            with mock.patch.object(service, "merge_documents", side_effect=changed):
                with self.assertRaisesRegex(service.ServiceError, "文档在合并读取后发生变化"):
                    service.preview_session(root, session)

    def test_hardlink_rejected_by_resolver(self):
        from opencoding.document_paths import resolve_document_paths
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "source.txt").write_text("user content")
            os.link(root / "source.txt", root / "ARCHITECTURE.md")
            with self.assertRaisesRegex(ValueError, "hardlink"):
                resolve_document_paths(root, ["architecture.md"])

    @unittest.skipIf(os.name == "nt", "distinct aliases require case-sensitive filesystem")
    def test_case_unicode_and_symlink_aliases_rejected(self):
        from opencoding.document_paths import resolve_document_paths
        for names in (("ARCHITECTURE.md", "architecture.md"), ("é.md", "e\u0301.md")):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in names:
                    (root / name).write_text("original")
                with self.assertRaisesRegex(ValueError, "ambiguous"):
                    resolve_document_paths(root, [names[0]])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("original")
            (root / "ARCHITECTURE.md").symlink_to(root / "source.txt")
            with self.assertRaisesRegex(ValueError, "link"):
                resolve_document_paths(root, ["architecture.md"])

    def test_edited_generated_block_requires_manual_merge(self):
        from opencoding.document_paths import merge_document
        merged = merge_document("user text\n", "generated")
        with self.assertRaisesRegex(ValueError, "edited"):
            merge_document(merged.replace("\ngenerated\n", "\nuser edited generated block\n"), "new generation")

    def test_existing_apg_documents_preview_apply_repeat_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            original = {
                "ARCHITECTURE.md": b"# Existing architecture\r\nUser choices.\r\n",
                "SECURITY.md": b"# Existing security\n",
                "AGENTS.md": b"User instructions.\r\n<!-- project-governance:begin -->\r\nManaged bytes.\r\n<!-- project-governance:end -->\r\n",
            }
            for name, content in original.items():
                (root / name).write_bytes(content)
            view = _complete(root)
            session = view["session"]["id"]
            preview = service.preview_session(root, session)
            self.assertIn("ARCHITECTURE.md", preview["documents"])
            self.assertNotIn("architecture.md", preview["documents"])
            architecture = next(t for t in preview["task_plan"]["tasks"] if t["id"] == "architecture")
            self.assertEqual(architecture["action"]["document"], "ARCHITECTURE.md")
            self.assertEqual(architecture["outputs"], ["ARCHITECTURE.md"])
            result = service.apply_approved(root, service.approve_preview(preview))
            self.assertEqual(result["status"], "applied")
            for name, content in original.items():
                self.assertTrue((root / name).read_bytes().startswith(content))
            again = service.preview_session(root, session)
            self.assertEqual(again["documents"], preview["documents"])
            rolled = service.rollback(root, result["transaction"]["transaction_id"])
            self.assertEqual(rolled["status"], "rolled_back")
            for name, content in original.items():
                self.assertEqual((root / name).read_bytes(), content)

    def test_user_edit_after_preview_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            view = _complete(root)
            approval = service.approve_preview(service.preview_session(root, view["session"]["id"]))
            (root / "AGENTS.md").write_text("new user instructions", encoding="utf-8")
            result = service.apply_approved(root, approval)
            self.assertEqual(result["status"], "stale")
            self.assertEqual((root / "AGENTS.md").read_text(), "new user instructions")
