import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opencoding.intake import answer_question, new_session
from opencoding.sessions import SessionConflictError, SessionLockError, load_session, save_session


def _save_from_process(root_text, session, queue, label, ready=None, start=None):
    if ready is not None:
        ready.set()
    if start is not None and not start.wait(10):
        raise RuntimeError("writer start barrier timed out")
    try:
        queue.put((label, "saved", save_session(Path(root_text), session)["status"]))
    except (SessionConflictError, SessionLockError) as exc:  # pragma: no cover - child process
        queue.put((label, "rejected", type(exc).__name__))
    except BaseException as exc:  # pragma: no cover - unexpected child failure
        queue.put((label, "unexpected_error", type(exc).__name__, str(exc)))
        raise


def _save_with_lock_checkpoint(root_text, session, queue, label, lock_held, continue_event):
    import opencoding.sessions as session_module

    original_read = session_module._read_existing

    def read_after_checkpoint(destination):
        lock_held.set()
        if not continue_event.wait(10):
            raise RuntimeError("lock-held checkpoint timed out")
        return original_read(destination)

    session_module._read_existing = read_after_checkpoint
    _save_from_process(root_text, session, queue, label)


def _crash_during_load(root_text, session_id):
    import opencoding.sessions as session_module

    def crash(_destination):
        os._exit(23)

    session_module._read_existing = crash
    session_module.load_session(Path(root_text), session_id)


def _crash_during_lock_initialization(root_text, session_id):
    import opencoding.sessions as session_module

    session_module._lock_owner_payload = lambda: os._exit(31)
    base = session_module.load_session(Path(root_text), session_id)
    session_module.save_session(Path(root_text), answer_question(base, "platform", "网页"))


def _crash_during_owner_write(root_text, session_id):
    import opencoding.sessions as session_module

    session_module._write_lock_owner = lambda *_args: os._exit(32)
    base = session_module.load_session(Path(root_text), session_id)
    session_module.save_session(Path(root_text), answer_question(base, "platform", "网页"))


def _crash_before_replace(root_text, session_id):
    import opencoding.sessions as session_module

    def crash(*_args):
        os._exit(27)

    session_module.os.replace = crash
    base = session_module.load_session(Path(root_text), session_id)
    session_module.save_session(Path(root_text), answer_question(base, "platform", "网页"))


def _hold_lock(root_text, ready, release, released=None):
    import opencoding.sessions as session_module

    lock, handle, payload = session_module._acquire_lock(Path(root_text))
    ready.set()
    try:
        if not release.wait(10):
            raise RuntimeError("lock release barrier timed out")
    finally:
        session_module._release_lock(lock, handle, payload)
        if released is not None:
            released.set()


def _crash_after_replace(root_text, session_id):
    import opencoding.sessions as session_module

    real_replace = session_module.os.replace

    def replace_then_crash(*args):
        real_replace(*args)
        os._exit(28)

    session_module.os.replace = replace_then_crash
    base = session_module.load_session(Path(root_text), session_id)
    session_module.save_session(Path(root_text), answer_question(base, "platform", "网页"))


class SessionPersistenceTests(unittest.TestCase):
    def test_revision_zero_save_load_and_secret_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = new_session("做一个工具 token=sk-test-1234567890")

            result = save_session(root, session)
            loaded = load_session(root, session["id"])
            on_disk = (root / ".opencoding" / "sessions" / f"{session['id']}.json").read_text(encoding="utf-8")

            self.assertEqual(result["status"], "saved")
            self.assertEqual(loaded["revision"], 0)
            self.assertNotIn("sk-test-1234567890", on_disk)
            self.assertEqual(save_session(root, loaded)["status"], "unchanged")

    def test_new_revision_save_is_allowed_but_stale_save_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = new_session("做一个工具")
            save_session(root, base)
            current = answer_question(base, "platform", "网页")
            save_session(root, current)

            stale = answer_question(base, "platform", "命令行")
            with self.assertRaises(SessionConflictError):
                save_session(root, stale)

    def test_changed_answer_extends_history_but_different_branch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = new_session("做一个工具")
            save_session(root, base)
            current = answer_question(base, "platform", "网页")
            save_session(root, current)

            changed = answer_question(current, "platform", "命令行")
            self.assertEqual(save_session(root, changed)["status"], "saved")

            branch = answer_question(answer_question(base, "audience", "管理员"), "platform", "命令行")
            with self.assertRaises(SessionConflictError):
                save_session(root, branch)

    def test_crashed_reader_leaves_recoverable_owned_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = new_session("做一个工具")
            save_session(root, session)
            context = multiprocessing.get_context("spawn")
            process = context.Process(target=_crash_during_load, args=(str(root), session["id"]))
            process.start()
            process.join(10)
            self.assertEqual(process.exitcode, 23)

            recovered = load_session(root, session["id"])
            self.assertEqual(recovered["id"], session["id"])
            self.assertTrue((root / ".opencoding" / ".session-write.lock").exists())

    def test_crash_during_lock_initialization_releases_guard_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = new_session("锁初始化中断")
            save_session(root, base)
            context = multiprocessing.get_context("spawn")
            process = context.Process(target=_crash_during_lock_initialization, args=(str(root), base["id"]))
            process.start()
            process.join(10)
            self.assertEqual(process.exitcode, 31)

            loaded = load_session(root, base["id"])
            self.assertEqual(loaded, base)
            self.assertEqual(save_session(root, answer_question(loaded, "platform", "网页"))["status"], "saved")

    def test_crash_during_owner_write_releases_guard_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = new_session("owner 写入中断")
            save_session(root, base)
            context = multiprocessing.get_context("spawn")
            process = context.Process(target=_crash_during_owner_write, args=(str(root), base["id"]))
            process.start()
            process.join(10)
            self.assertEqual(process.exitcode, 32)

            loaded = load_session(root, base["id"])
            self.assertEqual(loaded, base)
            self.assertEqual(save_session(root, answer_question(loaded, "platform", "网页"))["status"], "saved")

    def test_crash_before_replace_leaves_unknown_temp_but_allows_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = new_session("做一个工具")
            save_session(root, base)
            context = multiprocessing.get_context("spawn")
            process = context.Process(target=_crash_before_replace, args=(str(root), base["id"]))
            process.start()
            process.join(10)
            self.assertEqual(process.exitcode, 27)

            loaded = load_session(root, base["id"])
            self.assertEqual(loaded["revision"], 0)
            self.assertTrue(list((root / ".opencoding" / "sessions").glob(f".{base['id']}.json.tmp-*")))
            current = answer_question(loaded, "platform", "网页")
            self.assertEqual(save_session(root, current)["status"], "saved")
            self.assertEqual(load_session(root, base["id"])["revision"], 1)

    def test_crash_after_replace_recovers_new_revision_and_allows_next_save(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = new_session("做一个工具")
            save_session(root, base)
            context = multiprocessing.get_context("spawn")
            process = context.Process(target=_crash_after_replace, args=(str(root), base["id"]))
            process.start()
            process.join(10)
            self.assertEqual(process.exitcode, 28)

            loaded = load_session(root, base["id"])
            self.assertEqual(loaded["revision"], 1)
            self.assertEqual(save_session(root, answer_question(loaded, "platform", "命令行"))["status"], "saved")
            self.assertEqual(load_session(root, base["id"])["revision"], 2)

    def test_unknown_legacy_temp_is_preserved_while_new_attempt_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = new_session("做一个工具")
            save_session(root, base)
            sessions_dir = root / ".opencoding" / "sessions"
            legacy_temp = sessions_dir / f".{base['id']}.json.tmp"
            legacy_bytes = b"unknown stale bytes\n"
            legacy_temp.write_bytes(legacy_bytes)

            current = answer_question(base, "platform", "网页")
            self.assertEqual(save_session(root, current)["status"], "saved")
            self.assertEqual(legacy_temp.read_bytes(), legacy_bytes)
            self.assertEqual(load_session(root, base["id"])["revision"], 1)

    def test_lock_never_uses_process_signal_probe(self):
        import opencoding.sessions as session_module

        with tempfile.TemporaryDirectory() as directory, patch.object(session_module.os, "kill", side_effect=AssertionError("os.kill is forbidden")):
            root = Path(directory)
            self.assertEqual(save_session(root, new_session("无信号探活"))["status"], "saved")

    def test_divergent_same_revision_save_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = new_session("原始目标")
            save_session(root, original)
            divergent = dict(original)
            divergent["goal"] = "另一个目标"

            with self.assertRaises(SessionConflictError):
                save_session(root, divergent)

    def test_revision_cannot_replace_immutable_goal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = new_session("原始目标")
            save_session(root, base)
            candidate = answer_question(base, "platform", "网页")
            candidate["goal"] = "悄悄替换的目标"

            with self.assertRaises(SessionConflictError):
                save_session(root, candidate)
            self.assertEqual(load_session(root, base["id"]), base)

    def test_nested_unknown_field_and_incompatible_schema_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = new_session("做一个工具")
            invalid = dict(session)
            invalid["questions"] = [dict(session["questions"][0], unexpected=True), *session["questions"][1:]]
            with self.assertRaises(ValueError):
                save_session(root, invalid)

            destination = root / ".opencoding" / "sessions" / f"{session['id']}.json"
            destination.parent.mkdir(parents=True)
            destination.write_text(json.dumps({"schema_version": "1.0"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_session(root, session["id"])

    def test_root_and_internal_symlink_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            real = parent / "real"
            real.mkdir()
            linked_root = parent / "linked-root"
            try:
                linked_root.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            with self.assertRaises(ValueError):
                save_session(linked_root, new_session("工具"))

            ancestor_target = parent / "ancestor-target"
            (ancestor_target / "child").mkdir(parents=True)
            ancestor_alias = parent / "ancestor-alias"
            ancestor_alias.symlink_to(ancestor_target, target_is_directory=True)
            try:
                with self.assertRaises(ValueError):
                    save_session(ancestor_alias / "child", new_session("工具"))
            finally:
                ancestor_alias.unlink()

            root = parent / "root"
            root.mkdir()
            metadata = root / ".opencoding"
            metadata.symlink_to(real, target_is_directory=True)
            try:
                with self.assertRaises(ValueError):
                    save_session(root, new_session("工具"))
            finally:
                metadata.unlink()

    def test_corrupt_owner_record_does_not_authorize_without_os_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = new_session("做一个工具")
            save_session(root, base)
            lock = root / ".opencoding" / ".session-write.lock"
            lock.write_bytes(b"pid=fixture\n")
            self.assertEqual(save_session(root, answer_question(base, "platform", "网页"))["status"], "saved")

    def test_os_lock_competition_is_not_a_successful_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = new_session("做一个工具")
            save_session(root, base)
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            process = context.Process(target=_hold_lock, args=(str(root), ready, release))
            process.start()
            self.assertTrue(ready.wait(10))
            try:
                with self.assertRaises(SessionLockError):
                    save_session(root, answer_question(base, "platform", "网页"))
            finally:
                release.set()
                process.join(10)
            self.assertEqual(process.exitcode, 0)

    def test_competing_process_writes_cannot_both_succeed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = new_session("做一个工具")
            save_session(root, base)
            left = answer_question(base, "platform", "网页")
            right = answer_question(base, "platform", "命令行")
            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            left_lock_held = context.Event()
            left_continue = context.Event()
            right_ready = context.Event()
            right_start = context.Event()
            left_process = context.Process(
                target=_save_with_lock_checkpoint,
                args=(str(root), left, queue, "left", left_lock_held, left_continue),
            )
            right_process = context.Process(
                target=_save_from_process,
                args=(str(root), right, queue, "right", right_ready, right_start),
            )
            processes = [left_process, right_process]
            try:
                left_process.start()
                self.assertTrue(left_lock_held.wait(10))
                right_process.start()
                self.assertTrue(right_ready.wait(10))

                right_start.set()
                right_result = queue.get(timeout=10)
                left_continue.set()
                left_result = queue.get(timeout=10)

                for process in processes:
                    process.join(10)
                    self.assertEqual(process.exitcode, 0)
            finally:
                left_continue.set()
                right_start.set()
                for process in processes:
                    if process.pid is not None:
                        if process.is_alive():
                            process.terminate()
                        process.join(10)

            results = [left_result, right_result]
            self.assertEqual({item[0] for item in results}, {"left", "right"})
            self.assertEqual(left_result, ("left", "saved", "saved"))
            self.assertEqual(right_result, ("right", "rejected", "SessionLockError"))
            saved = [item for item in results if item[1:] == ("saved", "saved")]
            rejected = [item for item in results if item[1] == "rejected"]
            self.assertEqual(len(saved), 1)
            self.assertEqual(len(rejected), 1)
            self.assertIn(rejected[0][2], {"SessionLockError", "SessionConflictError"})

            winner = {"left": left, "right": right}[saved[0][0]]
            self.assertEqual(load_session(root, base["id"]), winner)

    def test_released_lock_rejects_stale_process_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = new_session("做一个工具")
            save_session(root, base)
            winner = answer_question(base, "platform", "网页")
            stale = answer_question(base, "platform", "命令行")
            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            stale_ready = context.Event()
            stale_start = context.Event()
            stale_process = context.Process(
                target=_save_from_process,
                args=(str(root), stale, queue, "stale", stale_ready, stale_start),
            )
            winner_process = context.Process(
                target=_save_from_process,
                args=(str(root), winner, queue, "winner"),
            )
            try:
                stale_process.start()
                self.assertTrue(stale_ready.wait(10))
                winner_process.start()
                winner_result = queue.get(timeout=10)
                winner_process.join(10)
                self.assertEqual(winner_process.exitcode, 0)

                stale_start.set()
                stale_result = queue.get(timeout=10)
                stale_process.join(10)
                self.assertEqual(stale_process.exitcode, 0)
            finally:
                stale_start.set()
                for process in (winner_process, stale_process):
                    if process.pid is not None:
                        if process.is_alive():
                            process.terminate()
                        process.join(10)

            self.assertEqual(winner_result, ("winner", "saved", "saved"))
            self.assertEqual(stale_result, ("stale", "rejected", "SessionConflictError"))
            self.assertEqual(load_session(root, base["id"]), winner)

    def test_read_missing_session_does_not_create_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(FileNotFoundError):
                load_session(root, "session-missing")
            self.assertFalse((root / ".opencoding").exists())

    def test_root_must_be_explicit_absolute_path(self):
        session = new_session("做一个工具")
        with self.assertRaises(TypeError):
            save_session(".", session)


if __name__ == "__main__":
    unittest.main()
