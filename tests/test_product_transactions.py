import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from opencoding.transactions import apply_changes, preview_changes, rollback_changes


class TransactionContractTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "existing.txt").write_text("before", encoding="utf-8")

    def test_preview_is_read_only_and_apply_then_rollback_is_auditable(self):
        plan = preview_changes(self.root, {"existing.txt": "after", "nested/new.txt": "created"})
        self.assertEqual(plan["status"], "preview")
        self.assertFalse((self.root / ".opencoding").exists())
        result = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        self.assertEqual(result["status"], "applied")
        self.assertEqual((self.root / "existing.txt").read_text(encoding="utf-8"), "after")
        self.assertEqual((self.root / "nested/new.txt").read_text(encoding="utf-8"), "created")
        receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
        self.assertNotIn("after", json.dumps(receipt))
        rollback = rollback_changes(self.root, result["transaction_id"])
        self.assertEqual(rollback["status"], "rolled_back")
        self.assertEqual((self.root / "existing.txt").read_text(encoding="utf-8"), "before")
        self.assertFalse((self.root / "nested/new.txt").exists())

    def test_digest_or_preimage_drift_blocks_without_creating_evidence(self):
        plan = preview_changes(self.root, {"existing.txt": "after"})
        self.assertEqual(apply_changes(self.root, plan, approved_digest="wrong")["status"], "blocked")
        self.assertFalse((self.root / ".opencoding").exists())
        (self.root / "existing.txt").write_text("user edit", encoding="utf-8")
        blocked = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        self.assertEqual(blocked["status"], "blocked")
        self.assertFalse((self.root / ".opencoding").exists())

    def test_postimage_drift_refuses_to_overwrite_user_change(self):
        plan = preview_changes(self.root, {"existing.txt": "after"})
        result = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        (self.root / "existing.txt").write_text("later user edit", encoding="utf-8")
        blocked = rollback_changes(self.root, result["transaction_id"])
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual((self.root / "existing.txt").read_text(encoding="utf-8"), "later user edit")

    def test_internal_evidence_link_blocks_apply_without_external_write(self):
        opencoding = self.root / ".opencoding"
        outside = Path(tempfile.mkdtemp())
        try:
            opencoding.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink creation is unavailable")
        plan = preview_changes(self.root, {"new.txt": "new"})
        result = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        self.assertEqual(result["status"], "blocked")
        self.assertFalse((outside / "transactions").exists())
        self.assertFalse((self.root / "new.txt").exists())

    def test_evidence_creation_failure_blocks_without_target_write(self):
        plan = preview_changes(self.root, {"new.txt": "new"})
        with patch("opencoding.transactions._write_bytes", side_effect=OSError("evidence unavailable")):
            result = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        self.assertEqual(result["status"], "blocked")
        self.assertFalse((self.root / "new.txt").exists())

    def test_manifest_preimage_tamper_blocks_rollback_before_mutation(self):
        plan = preview_changes(self.root, {"existing.txt": "after"})
        result = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        evidence = Path(result["rollback_ref"])
        manifest_path = evidence / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["entries"][0]["preimage_file"] = "../../existing.txt"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        rollback = rollback_changes(self.root, result["transaction_id"])
        self.assertEqual(rollback["status"], "blocked")
        self.assertEqual((self.root / "existing.txt").read_text(encoding="utf-8"), "after")

    def test_receipt_corruption_blocks_rollback_before_mutation(self):
        plan = preview_changes(self.root, {"existing.txt": "after"})
        result = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        receipt_path = Path(result["receipt_path"])
        receipt_path.write_text('{"status":"applied"}\n', encoding="utf-8")
        rollback = rollback_changes(self.root, result["transaction_id"])
        self.assertEqual(rollback["status"], "blocked")
        self.assertEqual((self.root / "existing.txt").read_text(encoding="utf-8"), "after")

    def test_interrupted_target_replacement_is_recovered_from_write_intent(self):
        plan = preview_changes(self.root, {"existing.txt": "after"})
        original = __import__("opencoding.transactions", fromlist=["_atomic_write"])._atomic_write

        def interrupt_after_target(path, content):
            original(path, content)
            if path.resolve() == (self.root / "existing.txt").resolve():
                raise SystemExit("simulated process interruption")

        with patch("opencoding.transactions._atomic_write", side_effect=interrupt_after_target):
            with self.assertRaises(SystemExit):
                apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        transaction_id = next((self.root / ".opencoding" / "transactions").glob("tx-*")).name
        rolled = rollback_changes(self.root, transaction_id)
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertEqual((self.root / "existing.txt").read_text(encoding="utf-8"), "before")

    def test_receipt_storage_failure_reports_changed_subset_and_allows_later_recovery(self):
        plan = preview_changes(self.root, {"existing.txt": "after"})
        original = __import__("opencoding.transactions", fromlist=["_write_json"])._write_json
        writes = {"count": 0}

        def fail_receipt_updates(path, value):
            if path.name == "receipt.json":
                writes["count"] += 1
                if writes["count"] > 1:
                    raise OSError("persistent evidence failure")
            return original(path, value)

        with patch("opencoding.transactions._write_json", side_effect=fail_receipt_updates):
            result = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        self.assertIn("existing.txt", result["changed_paths"])
        self.assertEqual(result["status"], "partial_failure")
        rolled = rollback_changes(self.root, result["transaction_id"])
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertEqual((self.root / "existing.txt").read_text(encoding="utf-8"), "before")

    def test_atomic_receipt_update_survives_interruption(self):
        plan = preview_changes(self.root, {"existing.txt": "after"})
        original = __import__("opencoding.transactions", fromlist=["_write_bytes"])._write_bytes
        writes = {"count": 0}

        def interrupt_receipt_temp(path, content):
            if path.name == "receipt.json":
                writes["count"] += 1
                if writes["count"] == 2:
                    path.write_bytes(b'{"schema_version":')
                    raise SystemExit("simulated receipt interruption")
            return original(path, content)

        with patch("opencoding.transactions._write_bytes", side_effect=interrupt_receipt_temp):
            with self.assertRaises(SystemExit):
                apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        transaction_id = next((self.root / ".opencoding" / "transactions").glob("tx-*")).name
        rolled = rollback_changes(self.root, transaction_id)
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertEqual((self.root / "existing.txt").read_text(encoding="utf-8"), "before")

    def test_multi_file_intervening_edit_is_not_overwritten(self):
        plan = preview_changes(self.root, {"a.txt": "a", "b.txt": "b"})
        original = __import__("opencoding.transactions", fromlist=["_atomic_write"])._atomic_write
        def edit_before_second_write(path, content):
            result = original(path, content)
            if path.name == "a.txt":
                (self.root / "b.txt").write_text("user edit", encoding="utf-8")
            return result

        with patch("opencoding.transactions._atomic_write", side_effect=edit_before_second_write):
            result = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), "user edit")
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "a")

    def test_cooperative_lock_blocks_competing_apply(self):
        plan = preview_changes(self.root, {"existing.txt": "after"})
        ready = self.root / "lock-ready"
        done = self.root / "lock-done"
        code = (
            "import sys,time;from pathlib import Path;"
            "sys.path.insert(0,bytes.fromhex(sys.argv[1]).decode());from opencoding.transactions import _cooperative_lock;"
            "r=Path(bytes.fromhex(sys.argv[2]).decode());ready=Path(bytes.fromhex(sys.argv[3]).decode());done=Path(bytes.fromhex(sys.argv[4]).decode());"
            "exec(\"with _cooperative_lock(r):\\n"
            "    ready.write_text('ready')\\n"
            "    while not done.exists(): time.sleep(0.01)\")"
        )
        encode_path = lambda value: str(value).encode("utf-8").hex()
        child = subprocess.Popen([sys.executable, "-B", "-c", code, encode_path(Path(__file__).parents[1]), encode_path(self.root), encode_path(ready), encode_path(done)])
        try:
            deadline = time.time() + 10
            while not ready.exists() and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            result = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
            self.assertEqual(result["status"], "blocked")
            self.assertEqual((self.root / "existing.txt").read_text(encoding="utf-8"), "before")
        finally:
            done.write_text("done", encoding="utf-8")
            child.wait(timeout=10)

    def test_rollback_failure_is_resumable_and_idempotent(self):
        (self.root / "a.txt").write_text("before-a", encoding="utf-8")
        (self.root / "b.txt").write_text("before-b", encoding="utf-8")
        plan = preview_changes(self.root, {"a.txt": "a", "b.txt": "b"})
        result = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        original = __import__("opencoding.transactions", fromlist=["_atomic_write"])._atomic_write
        def fail_once(path, content):
            if path.name == "b.txt":
                raise OSError("simulated rollback failure")
            return original(path, content)

        with patch("opencoding.transactions._atomic_write", side_effect=fail_once):
            partial = rollback_changes(self.root, result["transaction_id"])
        self.assertEqual(partial["status"], "partial_failure")
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "before-a")
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), "b")
        resumed = rollback_changes(self.root, result["transaction_id"])
        self.assertEqual(resumed["status"], "rolled_back")
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), "before-b")
        self.assertEqual(rollback_changes(self.root, result["transaction_id"])["status"], "rolled_back")

    def test_path_traversal_and_symlink_are_rejected(self):
        with self.assertRaises(ValueError):
            preview_changes(self.root, {"../outside.txt": "no"})
        link = self.root / "link.txt"
        try:
            link.symlink_to(self.root / "existing.txt")
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        with self.assertRaises(ValueError):
            preview_changes(self.root, {"link.txt": "no"})

    def test_partial_failure_is_reported_and_changed_subset_can_roll_back(self):
        plan = preview_changes(self.root, {"a.txt": "a", "b.txt": "b"})
        original = __import__("opencoding.transactions", fromlist=["_atomic_write"])._atomic_write
        calls = {"count": 0}

        def fail_second(path, content):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("simulated write failure")
            return original(path, content)

        with patch("opencoding.transactions._atomic_write", side_effect=fail_second):
            result = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        self.assertEqual(result["status"], "partial_failure")
        rolled = rollback_changes(self.root, result["transaction_id"])
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertFalse((self.root / "a.txt").exists())

    def test_same_content_postimage_identity_drift_blocks_rollback(self):
        plan = preview_changes(self.root, {"new.txt": "new"})
        target = (self.root / "new.txt").resolve()
        original_replace = os.replace

        def interrupt_after_target_replace(source, destination):
            original_replace(source, destination)
            if Path(destination).resolve() == target:
                raise SystemExit("simulated os.replace interruption")

        with patch("opencoding.transactions.os.replace", side_effect=interrupt_after_target_replace):
            with self.assertRaises(SystemExit):
                apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        transaction_id = next((self.root / ".opencoding" / "transactions").glob("tx-*")).name
        replacement = self.root / "replacement.tmp"
        replacement.write_text("new", encoding="utf-8")
        original_replace(replacement, self.root / "new.txt")
        blocked = rollback_changes(self.root, transaction_id)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual((self.root / "new.txt").read_text(encoding="utf-8"), "new")

    def test_r4_crash_after_apply_replace_is_idempotent(self):
        plan = preview_changes(self.root, {"existing.txt": "after"})
        target = (self.root / "existing.txt").resolve()
        original_replace = os.replace
        raised = {"value": False}

        def interrupt_after_apply_replace(source, destination):
            original_replace(source, destination)
            if Path(destination).resolve() == target and not raised["value"]:
                raised["value"] = True
                raise SystemExit("simulated apply replace interruption")

        with patch("opencoding.transactions.os.replace", side_effect=interrupt_after_apply_replace):
            with self.assertRaises(SystemExit):
                apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        transaction_id = next((self.root / ".opencoding" / "transactions").glob("tx-*"), None).name
        first = rollback_changes(self.root, transaction_id)
        second = rollback_changes(self.root, transaction_id)
        self.assertEqual(first["status"], "rolled_back")
        self.assertEqual(second["status"], "rolled_back")
        receipt = json.loads(
            ((self.root / ".opencoding" / "transactions" / transaction_id) / "receipt.json").read_text(encoding="utf-8")
        )
        self.assertEqual(receipt["rollback_changed_paths"], ["existing.txt"])
        self.assertEqual((self.root / "existing.txt").read_text(encoding="utf-8"), "before")

    def test_r4_crash_after_parent_mkdir_reports_unknown_residual(self):
        plan = preview_changes(self.root, {"nested/new.txt": "created"})
        original_mkdir = Path.mkdir
        parent = (self.root / "nested").resolve()
        raised = {"value": False}

        def interrupt_after_parent_mkdir(path, *args, **kwargs):
            result = original_mkdir(path, *args, **kwargs)
            if Path(path).resolve() == parent and not raised["value"]:
                raised["value"] = True
                raise SystemExit("simulated parent mkdir interruption")
            return result

        with patch("pathlib.Path.mkdir", autospec=True, side_effect=interrupt_after_parent_mkdir):
            with self.assertRaises(SystemExit):
                apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        transaction_id = next((self.root / ".opencoding" / "transactions").glob("tx-*"), None).name
        rolled = rollback_changes(self.root, transaction_id)
        self.assertEqual(rolled["status"], "blocked")
        self.assertEqual(rolled["rollback_residual_paths"], ["nested"])
        self.assertTrue((self.root / "nested").is_dir())
        self.assertFalse((self.root / "nested" / "new.txt").exists())

    def test_r4_crash_after_rollback_replace_resumes_from_rollback_intent(self):
        plan = preview_changes(self.root, {"existing.txt": "after"})
        applied = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        target = (self.root / "existing.txt").resolve()
        original_replace = os.replace
        raised = {"value": False}

        def interrupt_after_rollback_replace(source, destination):
            original_replace(source, destination)
            if Path(destination).resolve() == target and not raised["value"]:
                raised["value"] = True
                raise SystemExit("simulated rollback replace interruption")

        with patch("opencoding.transactions.os.replace", side_effect=interrupt_after_rollback_replace):
            with self.assertRaises(SystemExit):
                rollback_changes(self.root, applied["transaction_id"])
        resumed = rollback_changes(self.root, applied["transaction_id"])
        self.assertEqual(resumed["status"], "rolled_back")
        self.assertEqual((self.root / "existing.txt").read_text(encoding="utf-8"), "before")

    def test_r4_concurrent_roots_keep_recovery_identity_local(self):
        roots = [Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())]
        for root in roots:
            (root / "existing.txt").write_text("before", encoding="utf-8")
        plans = [preview_changes(root, {"existing.txt": "after"}) for root in roots]
        original_atomic = __import__("opencoding.transactions", fromlist=["_atomic_write"])._atomic_write
        original_replace = os.replace
        barrier = threading.Barrier(2)
        target = (roots[0] / "existing.txt").resolve()
        raised = {"value": False}
        outcomes = [None, None]

        def synchronized_atomic(path, content):
            if path.name == "existing.txt":
                barrier.wait(timeout=10)
            return original_atomic(path, content)

        def interrupt_one_root(source, destination):
            original_replace(source, destination)
            if Path(destination).resolve() == target and not raised["value"]:
                raised["value"] = True
                raise SystemExit("simulated concurrent apply interruption")

        def run(index):
            try:
                outcomes[index] = apply_changes(roots[index], plans[index], approved_digest=plans[index]["plan_digest"])
            except BaseException as error:
                outcomes[index] = error

        with patch("opencoding.transactions._atomic_write", side_effect=synchronized_atomic):
            with patch("opencoding.transactions.os.replace", side_effect=interrupt_one_root):
                threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertIsInstance(outcomes[0], BaseException)
        self.assertIsInstance(outcomes[1], dict)
        self.assertEqual(outcomes[1]["status"], "applied")
        tx0 = next((roots[0] / ".opencoding" / "transactions").glob("tx-*"), None).name
        tx1 = outcomes[1]["transaction_id"]
        self.assertEqual(rollback_changes(roots[0], tx0)["status"], "rolled_back")
        self.assertEqual(rollback_changes(roots[1], tx1)["status"], "rolled_back")

    def test_r4_parent_residual_classification_handles_absence_and_user_content(self):
        repeat_root = self.root / "repeat"
        repeat_root.mkdir()
        repeat_plan = preview_changes(repeat_root, {"nested/deep/new.txt": "after"})
        repeat_applied = apply_changes(repeat_root, repeat_plan, approved_digest=repeat_plan["plan_digest"])
        first = rollback_changes(repeat_root, repeat_applied["transaction_id"])
        second = rollback_changes(repeat_root, repeat_applied["transaction_id"])
        self.assertEqual(first["status"], "rolled_back")
        self.assertEqual(second["status"], "rolled_back")
        self.assertEqual(second["rollback_residual_paths"], [])
        self.assertFalse((repeat_root / "nested").exists())

        incomplete_root = self.root / "incomplete"
        incomplete_root.mkdir()
        incomplete_plan = preview_changes(incomplete_root, {"nested/deep/new.txt": "after"})
        original_mkdir = Path.mkdir
        nested = incomplete_root / "nested"

        def stop_after_first_parent(path, *args, **kwargs):
            result = original_mkdir(path, *args, **kwargs)
            if Path(path).resolve() == nested.resolve():
                raise SystemExit("simulated first parent creation")
            return result

        with patch.object(Path, "mkdir", new=stop_after_first_parent):
            with self.assertRaises(SystemExit):
                apply_changes(incomplete_root, incomplete_plan, approved_digest=incomplete_plan["plan_digest"])
        incomplete_id = next((incomplete_root / ".opencoding" / "transactions").glob("tx-*"), None).name
        interrupted = rollback_changes(incomplete_root, incomplete_id)
        self.assertEqual(interrupted["status"], "blocked")
        self.assertEqual(interrupted["rollback_residual_paths"], ["nested"])
        self.assertTrue(nested.is_dir())
        self.assertFalse((nested / "deep").exists())
        nested.rmdir()
        resumed = rollback_changes(incomplete_root, incomplete_id)
        self.assertEqual(resumed["status"], "rolled_back")
        self.assertEqual(resumed["rollback_residual_paths"], [])

        user_root = self.root / "user-content"
        user_root.mkdir()
        user_plan = preview_changes(user_root, {"nested/new.txt": "after"})
        user_applied = apply_changes(user_root, user_plan, approved_digest=user_plan["plan_digest"])
        user_file = user_root / "nested" / "unmanaged.txt"
        user_file.write_text("user data must stay", encoding="utf-8")
        user_rollback = rollback_changes(user_root, user_applied["transaction_id"])
        self.assertIn(user_rollback["status"], {"blocked", "partial_failure"})
        self.assertEqual(user_rollback["rollback_residual_paths"], ["nested"])
        self.assertEqual(user_file.read_text(encoding="utf-8"), "user data must stay")

    def test_parent_intent_failure_and_owned_parent_cleanup(self):
        plan = preview_changes(self.root, {"nested/new.txt": "created"})
        original_append = __import__("opencoding.transactions", fromlist=["_append_event"])._append_event
        failed = {"value": False}

        def fail_first_intent(directory, event):
            if event.get("status") == "write_intent" and not failed["value"]:
                failed["value"] = True
                raise OSError("synthetic intent persistence failure")
            return original_append(directory, event)

        with patch("opencoding.transactions._append_event", side_effect=fail_first_intent):
            result = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        self.assertEqual(result["status"], "failed")
        self.assertFalse((self.root / "nested").exists())

        applied = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        self.assertEqual(applied["status"], "applied")
        rolled = rollback_changes(self.root, applied["transaction_id"])
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertFalse((self.root / "nested").exists())

        user_parent = self.root / "user-owned"
        user_parent.mkdir()
        user_plan = preview_changes(self.root, {"user-owned/new.txt": "created"})
        user_applied = apply_changes(self.root, user_plan, approved_digest=user_plan["plan_digest"])
        self.assertEqual(rollback_changes(self.root, user_applied["transaction_id"])["status"], "rolled_back")
        self.assertTrue(user_parent.is_dir())

    def test_partial_failure_persists_terminal_receipt_state(self):
        plan = preview_changes(self.root, {"a.txt": "a", "b.txt": "b"})
        original = __import__("opencoding.transactions", fromlist=["_atomic_write"])._atomic_write
        calls = {"count": 0}

        def fail_second_target(path, content):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("synthetic target write failure")
            return original(path, content)

        with patch("opencoding.transactions._atomic_write", side_effect=fail_second_target):
            result = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        self.assertEqual(result["status"], "partial_failure")
        receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "partial_failure")
        self.assertEqual(receipt["changed_paths"], ["a.txt"])
        self.assertEqual(receipt["uncertain_paths"], [])
        self.assertIsInstance(receipt["finished_at"], str)
        self.assertIn("synthetic target write failure", receipt["reason_codes"])


if __name__ == "__main__":
    unittest.main()
