"""Authorization identity and real multi-process transaction regressions."""

import json
import multiprocessing
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest import mock

from opencoding import grants


def _grant(root, **overrides):
    options = dict(goal="授权竞争回归", allowed_paths=["app"],
                   action_kinds=["local_write", "local_run", "ai_request"],
                   issued_by="test-user",
                   budget={"max_ai_requests": 1, "max_repair_rounds": 1,
                           "max_no_progress_rounds": 1})
    options.update(overrides)
    return grants.issue_batch_grant(root, **options)


def _issue(root, grant_id, kind="local_write"):
    return grants.issue_step_credential(
        root, grant_id, task_id="task-1", attempt=1, action_kind=kind,
        targets=["app/main.py"], action_digest="action-1", input_digest="input-1")


def _compete(root, operation, identity, barrier, results):
    """Spawn-safe child; all competitors use the actual public API and filesystem."""
    try:
        barrier.wait(timeout=20)
        if operation == "issue":
            _issue(root, identity, "ai_request")
        elif operation == "consume":
            grants.check_step_credential(root, identity, expect_task_id="task-1")
        elif operation in ("repair_rounds", "no_progress_rounds"):
            grants.consume_budget_counter(root, identity, counter=operation)
        elif operation == "revoke":
            grants.revoke_batch_grant(root, identity, reason="并发撤销")
        elif operation == "batch":
            identity = _grant(root)["grant_id"]
        else:
            raise AssertionError(operation)
        results.put({"pid": os.getpid(), "status": "accepted", "id": identity})
    except grants.GrantError as exc:
        results.put({"pid": os.getpid(), "status": exc.code})
    except BaseException as exc:
        results.put({"pid": os.getpid(), "status": "unexpected:" + repr(exc)})


def _hold_lock(root, ready, results):
    try:
        with grants._grant_store_lock(root):
            ready.set()
            # The parent intentionally terminates us. Avoid waiting on an Event's
            # condition lock: terminating its owner can deadlock test cleanup.
            time.sleep(60)
        results.put("released")
    except BaseException as exc:
        results.put("unexpected:" + repr(exc))


class GrantIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.grant = _grant(self.root)
        self.store = self.root / ".opencoding" / "grants"

    def test_grant_copy_cannot_issue_consume_budget_or_revoke(self):
        alias = "grant-" + "a" * 32
        original = self.store / (self.grant["grant_id"] + ".json")
        shutil.copyfile(original, self.store / (alias + ".json"))
        before = original.read_bytes()
        operations = (
            lambda: grants.load_grant(self.root, alias),
            lambda: _issue(self.root, alias, "ai_request"),
            lambda: grants.consume_budget_counter(self.root, alias, counter="repair_rounds"),
            lambda: grants.revoke_batch_grant(self.root, alias, reason="alias"),
        )
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(grants.GrantError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "grant_identity_mismatch")
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(grants.revocation_entries(self.root), [])
        self.assertEqual(grants.grant_consumption_journal(self.root, alias), [])
        _issue(self.root, self.grant["grant_id"], "ai_request")
        self.assertEqual(grants.load_grant(self.root, self.grant["grant_id"])
                         ["budget_used"]["ai_requests"], 1)

    def test_copy_before_revocation_cannot_restore_revoked_grant(self):
        alias = "grant-" + "b" * 32
        shutil.copyfile(self.store / (self.grant["grant_id"] + ".json"),
                        self.store / (alias + ".json"))
        grants.revoke_batch_grant(self.root, self.grant["grant_id"], reason="撤销原批次")
        with self.assertRaises(grants.GrantError) as caught:
            _issue(self.root, alias, "ai_request")
        self.assertEqual(caught.exception.code, "grant_identity_mismatch")
        with self.assertRaises(grants.GrantError) as caught:
            _issue(self.root, self.grant["grant_id"], "ai_request")
        self.assertEqual(caught.exception.code, "grant_revoked")

    def test_credential_copy_is_rejected_before_and_after_original_consumption(self):
        credential = _issue(self.root, self.grant["grant_id"])
        alias = "cred-" + "c" * 32
        original = self.store / "credentials" / (credential["credential_id"] + ".json")
        clone = self.store / "credentials" / (alias + ".json")
        shutil.copyfile(original, clone)
        for consume in (False, True):
            with self.subTest(consume=consume), self.assertRaises(grants.GrantError) as caught:
                grants.check_step_credential(self.root, alias, consume=consume)
            self.assertEqual(caught.exception.code, "credential_identity_mismatch")
        self.assertFalse(json.loads(original.read_text(encoding="utf-8"))["used"])
        self.assertEqual(grants.grant_consumption_journal(self.root, self.grant["grant_id"]), [])
        grants.check_step_credential(self.root, credential["credential_id"])
        with self.assertRaises(grants.GrantError) as caught:
            grants.check_step_credential(self.root, alias)
        self.assertEqual(caught.exception.code, "credential_identity_mismatch")
        journal = grants.grant_consumption_journal(self.root, self.grant["grant_id"])
        self.assertEqual([item["credential_id"] for item in journal], [credential["credential_id"]])

    def test_interrupted_consumption_remains_consumed(self):
        credential = _issue(self.root, self.grant["grant_id"])
        with mock.patch.object(grants, "_atomic_write_json", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                grants.check_step_credential(self.root, credential["credential_id"])
        with self.assertRaises(grants.GrantError) as caught:
            grants.check_step_credential(self.root, credential["credential_id"])
        self.assertEqual(caught.exception.code, "credential_journal_mismatch")

    def test_interrupted_revocation_epoch_cache_cannot_hide_revocation(self):
        grants.revoke_batch_grant(self.root, self.grant["grant_id"], reason="初次撤销")
        another = _grant(self.root)
        before = grants.revocation_epoch(self.root)
        with mock.patch.object(grants, "_atomic_write_json", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                grants.revoke_batch_grant(self.root, another["grant_id"], reason="流水已写，缓存中断")
        self.assertEqual(grants.revocation_epoch(self.root), before + 1)
        self.assertTrue(grants.load_grant(self.root, another["grant_id"])["revoked"])
        with self.assertRaises(grants.GrantError) as caught:
            _issue(self.root, another["grant_id"])
        self.assertEqual(caught.exception.code, "grant_revoked")

    def test_partial_consumption_journal_is_fail_closed(self):
        credential = _issue(self.root, self.grant["grant_id"])
        journal = grants._journal_path(self.root, self.grant["grant_id"])
        journal.parent.mkdir(exist_ok=True)
        journal.write_text('{"kind":"credential_consumed"', encoding="utf-8")
        with self.assertRaises(grants.GrantError) as caught:
            grants.check_step_credential(self.root, credential["credential_id"])
        self.assertEqual(caught.exception.code, "grant_journal_invalid")

    def test_partial_revocation_journal_is_fail_closed(self):
        journal = grants._revocation_journal_path(self.root)
        journal.parent.mkdir(exist_ok=True)
        journal.write_text('{"grant_id":', encoding="utf-8")
        for operation in (lambda: grants.load_grant(self.root, self.grant["grant_id"]),
                          lambda: grants.revocation_epoch(self.root)):
            with self.assertRaises(grants.GrantError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "grant_revocation_journal_invalid")

    def test_legacy_store_reads_do_not_create_lock_or_rewrite_revoked_grant(self):
        credential = _issue(self.root, self.grant["grant_id"])
        another = _grant(self.root)
        # Emulate a legacy writer interrupted after its durable revocation log.
        grants._append_revocation(self.root, another["grant_id"], "未补写授权文件")
        (self.store / ".state.lock").unlink()

        def snapshot():
            return {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in self.root.rglob("*") if p.is_file()}

        before = snapshot()
        self.assertFalse(grants.load_grant(self.root, self.grant["grant_id"])["revoked"])
        self.assertTrue(grants.load_grant(self.root, another["grant_id"])["revoked"])
        self.assertFalse(grants.check_step_credential(
            self.root, credential["credential_id"], consume=False)["credential"]["used"])
        self.assertEqual(grants.grant_consumption_journal(self.root, self.grant["grant_id"]), [])
        self.assertEqual(len(grants.revocation_entries(self.root)), 1)
        self.assertEqual(grants.revocation_epoch(self.root), 1)
        self.assertEqual(snapshot(), before)
        self.assertFalse((self.store / ".state.lock").exists())

    def test_legacy_read_rejects_snapshot_if_a_writer_creates_lock_mid_read(self):
        (self.store / ".state.lock").unlink()
        original_read = grants._read_json
        armed = [True]

        def concurrent_write(path):
            doc = original_read(path)
            if armed[0]:
                armed[0] = False
                _grant(self.root)
            return doc

        with mock.patch.object(grants, "_read_json", side_effect=concurrent_write):
            with self.assertRaises(grants.GrantError) as caught:
                grants.load_grant(self.root, self.grant["grant_id"])
        self.assertEqual(caught.exception.code, "grant_store_busy")


class GrantProcessConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ctx = multiprocessing.get_context("spawn")

    def _race(self, operation, identities):
        barrier = self.ctx.Barrier(len(identities) + 1)
        results = self.ctx.Queue()
        processes = [self.ctx.Process(target=_compete,
                                      args=(self.root, operation, identity, barrier, results))
                     for identity in identities]
        try:
            for process in processes:
                process.start()
            barrier.wait(timeout=20)
            received = [results.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(20)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(len({item["pid"] for item in received}), len(processes))
            self.assertFalse(any(item["status"].startswith("unexpected:") for item in received), received)
            return received
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                process.close()
            results.close()
            results.join_thread()

    def test_concurrent_ai_issue_has_one_success_and_one_persisted_charge(self):
        grant = _grant(self.root)
        received = self._race("issue", [grant["grant_id"]] * 6)
        self.assertCountEqual([r["status"] for r in received],
                              ["accepted"] + ["step_budget_exhausted"] * 5)
        loaded = grants.load_grant(self.root, grant["grant_id"])
        self.assertEqual(loaded["budget_used"]["ai_requests"], 1)
        journal = grants.grant_consumption_journal(self.root, grant["grant_id"])
        self.assertEqual(sum(item["kind"] == "ai_request_issue" for item in journal), 1)

    def test_legacy_first_writers_create_one_lock_before_consuming_budget(self):
        grant = _grant(self.root)
        (self.root / ".opencoding" / "grants" / ".state.lock").unlink()
        received = self._race("issue", [grant["grant_id"]] * 6)
        self.assertCountEqual([r["status"] for r in received],
                              ["accepted"] + ["step_budget_exhausted"] * 5)
        self.assertEqual(grants.load_grant(self.root, grant["grant_id"])
                         ["budget_used"]["ai_requests"], 1)
        self.assertEqual(len(grants.grant_consumption_journal(self.root, grant["grant_id"])), 1)

    def test_concurrent_credential_consume_succeeds_exactly_once(self):
        grant = _grant(self.root)
        credential = _issue(self.root, grant["grant_id"])
        received = self._race("consume", [credential["credential_id"]] * 6)
        self.assertCountEqual([r["status"] for r in received],
                              ["accepted"] + ["credential_replay"] * 5)
        journal = grants.grant_consumption_journal(self.root, grant["grant_id"])
        self.assertEqual(sum(item["kind"] == "credential_consumed" for item in journal), 1)

    def test_concurrent_repair_and_no_progress_counters_never_exceed_budget(self):
        for counter in ("repair_rounds", "no_progress_rounds"):
            with self.subTest(counter=counter):
                grant = _grant(self.root)
                received = self._race(counter, [grant["grant_id"]] * 6)
                self.assertCountEqual([r["status"] for r in received],
                                      ["accepted"] + ["budget_exhausted"] * 5)
                self.assertEqual(grants.load_grant(self.root, grant["grant_id"])
                                 ["budget_used"][counter], 1)
                journal = grants.grant_consumption_journal(self.root, grant["grant_id"])
                self.assertEqual(sum(item["amount"] for item in journal
                                     if item["kind"] == "counter_consumed"), 1)

    def test_concurrent_batch_creation_uses_one_store_lock(self):
        received = self._race("batch", [None] * 6)
        self.assertTrue(all(r["status"] == "accepted" for r in received), received)
        self.assertEqual(len({r["id"] for r in received}), 6)
        for record in received:
            self.assertEqual(grants.load_grant(self.root, record["id"])["grant_id"], record["id"])

    def test_concurrent_revocations_keep_complete_journal_and_monotonic_epoch(self):
        ids = [_grant(self.root)["grant_id"] for _ in range(6)]
        received = self._race("revoke", ids)
        self.assertTrue(all(r["status"] == "accepted" for r in received), received)
        self.assertEqual(grants.revocation_epoch(self.root), 6)
        self.assertCountEqual([r["grant_id"] for r in grants.revocation_entries(self.root)], ids)
        for identity in ids:
            self.assertTrue(grants.load_grant(self.root, identity)["revoked"])
            with self.assertRaises(grants.GrantError) as caught:
                _issue(self.root, identity)
            self.assertEqual(caught.exception.code, "grant_revoked")

    def test_busy_store_is_bounded_and_process_exit_releases_permanent_lock(self):
        grant = _grant(self.root)
        ready = self.ctx.Event()
        results = self.ctx.Queue()
        owner = self.ctx.Process(target=_hold_lock, args=(self.root, ready, results))
        owner.start()
        try:
            self.assertTrue(ready.wait(20), "child must own the real OS lock")
            lock_path = self.root / ".opencoding" / "grants" / ".state.lock"
            before = lock_path.stat()
            with mock.patch.object(grants, "_STORE_LOCK_TIMEOUT_SECONDS", 0.05):
                with self.assertRaises(grants.GrantError) as caught:
                    _issue(self.root, grant["grant_id"], "ai_request")
            self.assertEqual(caught.exception.code, "grant_store_busy")
            owner.terminate()
            owner.join(5)
            self.assertFalse(owner.is_alive())
            _issue(self.root, grant["grant_id"], "ai_request")
            after = lock_path.stat()
            self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
            self.assertEqual(grants.load_grant(self.root, grant["grant_id"])
                             ["budget_used"]["ai_requests"], 1)
        finally:
            if owner.is_alive():
                owner.terminate()
                owner.join(5)
            owner.close()
            results.close()
            results.join_thread()

    def test_missing_store_read_operations_do_not_initialize_metadata(self):
        self.assertEqual(grants.revocation_entries(self.root), [])
        self.assertEqual(grants.revocation_epoch(self.root), 0)
        self.assertEqual(grants.grant_consumption_journal(self.root, "grant-" + "d" * 32), [])
        with self.assertRaises(grants.GrantError) as caught:
            grants.load_grant(self.root, "grant-" + "d" * 32)
        self.assertEqual(caught.exception.code, "grant_unknown")
        self.assertFalse((self.root / ".opencoding").exists())


if __name__ == "__main__":
    unittest.main()
