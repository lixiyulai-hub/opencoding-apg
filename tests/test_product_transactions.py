import json
import os
import shutil
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

def evidence_json(value):
    """Independent synthetic-fixture encoding; never repairs API input."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def evidence_hash(raw):
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def evidence_events(payloads):
    rows = []
    for payload in payloads:
        row = dict(payload, previous_event_sha256=evidence_hash(b"".join(rows)) if rows else None,
                   event_sha256=evidence_hash(evidence_json(payload)[:-1]))
        rows.append(evidence_json(row))
    return b"".join(rows)


def transaction_bytes_fixture(*, before=b"before\r\n", create_only=False):
    """Caller-supplied bytes are content fixtures, not a capture or execution proof."""
    transaction_id = "tx-20261006T000000000000Z-000000000001"
    entries = [
        {"path": "existing.txt", "before_exists": True, "before_sha256": evidence_hash(before),
         "after_sha256": evidence_hash(b"after"), "preimage_file": "preimage/0.bin", "before_identity": [17, 23]},
        {"path": "nested/new.txt", "before_exists": False, "before_sha256": None,
         "after_sha256": evidence_hash(b"created"), "preimage_file": None, "before_identity": None},
    ]
    if create_only:
        entries = entries[1:]
    paths = [item["path"] for item in entries]
    manifest = {"schema_version": "1.0", "transaction_id": transaction_id, "plan_digest": "1" * 64, "entries": entries}
    receipt = {"schema_version": "1.0", "transaction_id": transaction_id, "plan_digest": "1" * 64,
               "status": "applied", "planned_paths": paths, "changed_paths": paths, "uncertain_paths": [],
               "started_at": "", "manifest_sha256": evidence_hash(evidence_json(manifest)[:-1]),
               "rollback_status": None, "rollback_changed_paths": []}
    payloads = [{"transaction_id": transaction_id, "status": state, "changed_paths": paths, "at": ""}
                for state in ("failed", "partial_failure", "applied")]
    return {"transaction_id": transaction_id, "receipt_bytes": evidence_json(receipt),
            "manifest_bytes": evidence_json(manifest), "events_bytes": evidence_events(payloads),
            "preimage_bytes": {} if create_only else {"preimage/0.bin": before}}


class TransactionReaderCharacterizationTests(unittest.TestCase):
    """Fixed legacy outcomes, established before extracting the pure fragments."""

    def fixture(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        values = transaction_bytes_fixture()
        evidence = root / ".opencoding" / "transactions" / values["transaction_id"]
        (evidence / "preimage").mkdir(parents=True)
        (root / "existing.txt").write_bytes(b"after")
        (root / "guard.bin").write_bytes(b"unchanged\x00")
        for key, name in (("receipt_bytes", "receipt.json"), ("manifest_bytes", "manifest.json"), ("events_bytes", "events.jsonl")):
            (evidence / name).write_bytes(values[key])
        for name, raw in values["preimage_bytes"].items():
            (evidence / name).write_bytes(raw)
        return root, evidence, values

    def test_canonical_values_and_encoding_failures(self):
        from opencoding import transactions as tx
        for value in ([], "值", 42, True, None):
            with self.subTest(value=value):
                self.assertEqual(tx._strict_json_bytes(evidence_json(value), "fixture"), value)
        failures = [(b'{"a":1,"a":2}\n', "fixture contains duplicate fields"),
                    (b'{"a": 1}\n', "fixture is not canonical JSON"),
                    (b'{"b":2,"a":1}\n', "fixture is not canonical JSON"),
                    (b'"\xff"\n', "fixture is not valid JSON"),
                    (b'\xef\xbb\xbf{}\n', "fixture is not valid JSON"),
                    (b'{}', "fixture is not canonical JSON"),
                    (b'{}\n\n', "fixture is not canonical JSON"),
                    (b'{}\r\n', "fixture is not canonical JSON")]
        for raw, message in failures:
            with self.subTest(raw=raw), self.assertRaises(ValueError) as caught:
                tx._strict_json_bytes(raw, "fixture")
            self.assertEqual(str(caught.exception), message)

    def test_compound_fault_priority_and_stopping_point(self):
        from opencoding import transactions as tx
        from tests.test_product_cli import inventory
        messages = {"C01": "manifest is not valid JSON", "C02": "receipt status is invalid",
                    "C03": "transaction evidence file is missing or unsafe",
                    "C04": "target path contains a link or reparse point: existing.txt",
                    "C05": "preimage digest mismatch", "C06": "manifest digest mismatch",
                    "C07": "transaction preimage inventory disagrees", "C08": "event chain is broken",
                    "C09": "transaction evidence file is missing or unsafe"}
        for case, message in messages.items():
            with self.subTest(case=case):
                root, evidence, values = self.fixture()
                receipt, manifest = json.loads(values["receipt_bytes"]), json.loads(values["manifest_bytes"])
                if case == "C01":
                    receipt = []
                elif case == "C02":
                    receipt.update(status="invalid", manifest_sha256="0" * 64)
                elif case == "C04":
                    (root / "existing.txt").unlink()
                    (root / "existing.txt").symlink_to(root / "guard.bin")
                    manifest["entries"][0]["after_sha256"] = "bad"
                elif case == "C05":
                    (evidence / "preimage/0.bin").write_bytes(b"wrong")
                    (evidence / "preimage/extra.bin").symlink_to(root / "guard.bin")
                elif case == "C06":
                    manifest["unknown"] = True
                elif case in {"C07", "C09"}:
                    (evidence / "preimage/extra.bin").write_bytes(b"extra")
                    receipt["planned_paths"] = ["different.txt"]
                    if case == "C09":
                        (evidence / "preimage/0.bin").unlink()
                elif case == "C08":
                    rows = values["events_bytes"].splitlines(keepends=True)
                    last = json.loads(rows[2])
                    last.update(previous_event_sha256="0" * 64, event_sha256="0" * 64)
                    (evidence / "events.jsonl").write_bytes(b"".join(rows[:2]) + evidence_json(last))
                if isinstance(receipt, dict):
                    receipt["manifest_sha256"] = "0" * 64 if case in {"C02", "C06"} else evidence_hash(evidence_json(manifest)[:-1])
                (evidence / "receipt.json").write_bytes(evidence_json(receipt))
                (evidence / "manifest.json").write_bytes(b"{bad-json}\n" if case == "C01" else evidence_json(manifest))
                if case == "C03":
                    (evidence / "receipt.json").write_bytes(b"bad receipt")
                    (evidence / "manifest.json").unlink()
                    (evidence / "manifest.json").symlink_to(root / "guard.bin")
                before, reads, scans = inventory(root), [], []
                original_read, original_iterdir = Path.read_bytes, Path.iterdir
                def read(path):
                    reads.append(path)
                    return original_read(path)
                def scan(path):
                    scans.append(path)
                    return original_iterdir(path)
                with patch.object(Path, "read_bytes", read), patch.object(Path, "iterdir", scan), self.assertRaises(ValueError) as caught:
                    if case == "C08":
                        tx._load_events(evidence, values["transaction_id"])
                    else:
                        tx._load_receipt_manifest(root, values["transaction_id"])
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(inventory(root), before)
                if case in {"C02", "C03"}:
                    self.assertEqual(reads.count(evidence / "manifest.json"), 1 if case == "C02" else 0)
                if case in {"C05", "C09"}:
                    self.assertNotIn(evidence / "preimage", scans)
                if case != "C08":
                    self.assertNotIn(evidence / "events.jsonl", reads)

    def test_dual_manifest_reads_keep_first_parse_and_second_digest(self):
        from opencoding import transactions as tx
        for case in ("D01", "D02", "D03", "D04", "D05"):
            with self.subTest(case=case):
                root, evidence, values = self.fixture()
                first = values["manifest_bytes"]
                second_doc = json.loads(first)
                if case != "D01":
                    second_doc["entries"][0]["after_sha256"] = "2" * 64
                second = evidence_json(second_doc)
                receipt = json.loads(values["receipt_bytes"])
                if case == "D02":
                    receipt["manifest_sha256"] = evidence_hash(second[:-1])
                if case == "D05":
                    receipt["unknown"] = True
                (evidence / "receipt.json").write_bytes(evidence_json(receipt))
                original, calls = Path.read_bytes, []
                def read(path):
                    if path != evidence / "manifest.json":
                        return original(path)
                    calls.append(path)
                    if len(calls) == 1:
                        return first
                    if case == "D04":
                        raise OSError("synthetic second manifest read failure")
                    if case == "D05":
                        raise AssertionError("second manifest read must not occur")
                    return second
                with patch.object(Path, "read_bytes", read):
                    if case in {"D01", "D02"}:
                        result = tx._load_receipt_manifest(root, values["transaction_id"])
                        self.assertEqual(result[2], json.loads(first))
                        self.assertEqual(result[1]["manifest_sha256"], evidence_hash(second[:-1]))
                    else:
                        kind = OSError if case == "D04" else ValueError
                        with self.assertRaises(kind) as caught:
                            tx._load_receipt_manifest(root, values["transaction_id"])
                        self.assertEqual(str(caught.exception), {"D03": "manifest digest mismatch", "D04": "synthetic second manifest read failure", "D05": "receipt fields are incompatible"}[case])
                self.assertEqual(len(calls), 1 if case == "D05" else 2)

    def test_events_require_all_previous_raw_lines(self):
        from opencoding import transactions as tx
        root, evidence, values = self.fixture()
        rows = values["events_bytes"].splitlines(keepends=True)
        self.assertEqual(len(tx._load_events(evidence, values["transaction_id"])), 3)
        for wrong in (evidence_hash(rows[1]), json.loads(rows[1])["event_sha256"]):
            last = json.loads(rows[2])
            last["previous_event_sha256"] = wrong
            (evidence / "events.jsonl").write_bytes(b"".join(rows[:2]) + evidence_json(last))
            with self.assertRaisesRegex(ValueError, "^event chain is broken$"):
                tx._load_events(evidence, values["transaction_id"])

    def test_legacy_loose_content_rules_remain_accepted(self):
        from opencoding import transactions as tx
        root, evidence, values = self.fixture()
        manifest, receipt = json.loads(values["manifest_bytes"]), json.loads(values["receipt_bytes"])
        manifest["entries"][0]["before_identity"] = ["legacy", {"anything": True}]
        receipt.update(finished_at="not-ISO", rollback_status="any-old-string", rollback_changed_paths=["existing.txt", "existing.txt"])
        receipt["manifest_sha256"] = evidence_hash(evidence_json(manifest)[:-1])
        (evidence / "manifest.json").write_bytes(evidence_json(manifest))
        (evidence / "receipt.json").write_bytes(evidence_json(receipt))
        self.assertEqual(tx._load_receipt_manifest(root, values["transaction_id"])[1], receipt)
        payload = {"transaction_id": values["transaction_id"], "status": "write_intent", "changed_paths": ["existing.txt"], "at": "",
                   "before_sha256": None, "after_sha256": "1" * 64, "before_identity": [True, False], "after_identity": [False, True]}
        (evidence / "events.jsonl").write_bytes(evidence_events([payload]))
        self.assertEqual(tx._load_events(evidence, values["transaction_id"])[0]["before_identity"], [True, False])

    def test_rollback_reads_before_and_inside_real_lock(self):
        from contextlib import contextmanager
        from opencoding import transactions as tx
        from tests.test_product_cli import inventory
        for fault in (None, "before", "inside"):
            with self.subTest(fault=fault):
                root = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, root)
                (root / "existing.txt").write_bytes(b"before")
                plan = preview_changes(root, {"existing.txt": "after", "nested/new.txt": "created"})
                applied = apply_changes(root, plan, approved_digest=plan["plan_digest"])
                self.assertEqual(applied["status"], "applied")
                evidence = Path(applied["rollback_ref"])
                if fault == "before":
                    receipt = json.loads((evidence / "receipt.json").read_bytes())
                    receipt["status"] = "invalid"
                    (evidence / "receipt.json").write_bytes(evidence_json(receipt))
                before, order, reads = inventory(root), [], []
                original_receipt, original_events, original_lock, original_read = tx._load_receipt_manifest, tx._load_events, tx._cooperative_lock, Path.read_bytes
                def receipt_reader(*args):
                    order.append("receipt")
                    return original_receipt(*args)
                def events_reader(*args):
                    order.append("events")
                    return original_events(*args)
                def read(path):
                    reads.append(path)
                    if fault == "inside" and path == evidence / "receipt.json" and reads.count(path) == 2:
                        raise OSError("synthetic receipt read failure inside lock")
                    return original_read(path)
                @contextmanager
                def lock(path):
                    order.append("lock-enter")
                    with original_lock(path):
                        order.append("lock-held")
                        try:
                            yield
                        finally:
                            order.append("lock-exit")
                with patch.object(tx, "_load_receipt_manifest", receipt_reader), patch.object(tx, "_load_events", events_reader), patch.object(tx, "_cooperative_lock", lock), patch.object(Path, "read_bytes", read):
                    result = rollback_changes(root, applied["transaction_id"])
                expected = ["receipt"] if fault == "before" else ["receipt", "events", "lock-enter", "lock-held", "receipt"] + ([] if fault else ["events"]) + ["lock-exit"]
                self.assertEqual(order, expected)
                self.assertEqual(reads.count(evidence / "manifest.json"), {None: 4, "before": 1, "inside": 2}[fault])
                self.assertEqual(result["status"], "blocked" if fault else "rolled_back")
                if fault:
                    self.assertEqual(inventory(root), before)
                else:
                    self.assertEqual((root / "existing.txt").read_bytes(), b"before")
                    self.assertFalse((root / "nested/new.txt").exists())


if __name__ == "__main__":
    unittest.main()
