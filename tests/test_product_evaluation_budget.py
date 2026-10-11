"""Request accounting and late-response probes use only isolated fake adapters."""
import concurrent.futures
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from opencoding import advisor, request_budget, service
from opencoding.facts import FactError, add_fact
from opencoding.aiadapter import AIRequestError
from opencoding.sessions import SessionLockError, session_write_lock
from tests.test_product_full_chain_v5 import FakeAdapter, eval_response
from tests.test_product_service import _complete


def _reserve_process(root):
    try:
        request_budget.reserve(root, "race", limit=1)
        return "ok"
    except request_budget.RequestBudgetError as exc:
        return exc.code


class EvaluationBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(Path(self.temp.name) / "config")})
        env.start()
        self.addCleanup(env.stop)

    def session(self):
        self.sid = _complete(self.root)["session"]["id"]
        self.revision = service.session_view(self.root, self.sid)["session"]["revision"]

    def test_failed_unknown_and_new_run_ids_consume_same_budget(self):
        first = request_budget.reserve(self.root, "scope", limit=2, run_id="first")
        request_budget.finish(self.root, "scope", first["reservation_id"], status="failed")
        request_budget.reserve(self.root, "scope", run_id="second")
        with self.assertRaisesRegex(request_budget.RequestBudgetError, "累计"):
            request_budget.reserve(self.root, "scope", limit=100, run_id="third")
        self.assertEqual(request_budget.read(self.root, "scope")["used"], 2)

    def test_explicit_extension_preserves_spent_and_records_reason(self):
        request_budget.reserve(self.root, "scope", limit=1)
        for confirm in (False, "true", 1):
            with self.assertRaises(request_budget.RequestBudgetError):
                request_budget.extend(self.root, "scope", additional=2, confirmed=confirm,
                                      reason="user request", issued_by="local-user")
        book = request_budget.extend(self.root, "scope", additional=2, confirmed=True,
                                     reason="user request", issued_by="local-user")
        self.assertEqual((book["used"], book["limit"]), (1, 3))
        self.assertEqual(book["extensions"][0]["issued_by"], "local-user")

    def test_window_is_persistent_and_extension_does_not_reset_it(self):
        with mock.patch("opencoding.request_budget.time.time", return_value=100):
            request_budget.reserve(self.root, "scope", window_limit=1)
        request_budget.extend(self.root, "scope", additional=2, confirmed=True,
                              reason="more tests", issued_by="user")
        with mock.patch("opencoding.request_budget.time.time", return_value=130):
            with self.assertRaises(request_budget.RequestBudgetError) as caught:
                request_budget.reserve(self.root, "scope", window_limit=100)
        self.assertEqual(caught.exception.code, "request_budget_rate_limited")
        with mock.patch("opencoding.request_budget.time.time", return_value=161):
            request_budget.reserve(self.root, "scope")

    def test_cross_process_reservations_do_not_overdraw(self):
        with concurrent.futures.ProcessPoolExecutor(max_workers=3,
                mp_context=multiprocessing.get_context("spawn")) as executor:
            outcomes = list(executor.map(_reserve_process, [str(self.root)] * 3))
        self.assertEqual(outcomes.count("ok"), 1, outcomes)
        self.assertEqual(outcomes.count("request_budget_exhausted"), 2, outcomes)

    def test_corrupt_or_copied_ledger_fails_closed(self):
        request_budget.reserve(self.root, "scope")
        path = request_budget._path(self.root, "scope")
        original = json.loads(path.read_text(encoding="utf-8"))
        mutations = [
            lambda b: b.update(root="different-project"),
            lambda b: b.update(scope_digest="different-scope"),
            lambda b: b["requests"].append(dict(b["requests"][0])),
            lambda b: b["requests"][0].pop("request_id"),
            lambda b: b["requests"][0].update(reserved_at=float("nan")),
            lambda b: b["requests"][0].update(reserved_at=True),
            lambda b: b["requests"][0].update(finished_at=float("inf")),
        ]
        for mutation in mutations:
            book = json.loads(json.dumps(original))
            mutation(book)
            path.write_text(json.dumps(book), encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaises(request_budget.RequestBudgetError) as caught:
                request_budget.reserve(self.root, "scope")
            self.assertEqual(caught.exception.code, "request_budget_invalid")
            self.assertEqual(path.read_bytes(), before)

    def test_cooldown_status_is_computable(self):
        with mock.patch("opencoding.request_budget.time.time", return_value=100):
            request_budget.reserve(self.root, "scope", window_limit=1)
        with mock.patch("opencoding.request_budget.time.time", return_value=110):
            book = request_budget.read(self.root, "scope")
        self.assertEqual(book["window_remaining"], 0)
        self.assertEqual(book["retry_after_seconds"], 50)

    def test_reject_linked_budget_directory(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.root / ".opencoding").mkdir()
        try:
            (self.root / ".opencoding" / "request-budgets").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlink privilege unavailable")
        try:
            with self.assertRaises(ValueError):
                request_budget.reserve(self.root, "scope")
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            (self.root / ".opencoding" / "request-budgets").unlink()

    def test_evaluation_failures_are_counted_before_adapter_dispatch(self):
        self.session()
        adapter = FakeAdapter([AIRequestError("response_schema_invalid", "fake failure")] * 6)
        for index in range(5):
            with self.assertRaises(AIRequestError):
                advisor.run_ai_evaluation(self.root, self.sid, adapter, run_id="run-" + str(index))
        with self.assertRaises(request_budget.RequestBudgetError):
            advisor.run_ai_evaluation(self.root, self.sid, adapter, run_id="new-run")
        self.assertEqual(len(adapter.calls), 5)
        book = request_budget.read(self.root, "evaluation:" + self.sid)
        self.assertEqual(book["used"], 5)
        self.assertTrue(all(item["request_id"] and item["status"] == "failed" for item in book["requests"]))

    def _pending_call(self, mutation):
        entered, release = threading.Event(), threading.Event()
        class PausedAdapter:
            def complete(inner, *args, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test timed out")
                return eval_response()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(advisor.run_ai_evaluation, self.root, self.sid, PausedAdapter())
            self.assertTrue(entered.wait(10))
            try:
                mutation()
            finally:
                release.set()
            with self.assertRaises(service.ServiceError) as caught:
                future.result(10)
            self.assertEqual(caught.exception.code, "evaluation_input_drift")

    def test_transport_errors_and_interruptions_keep_unknown_reservations(self):
        self.session()
        errors = [AIRequestError("read_idle_timeout", "timeout"),
                  AIRequestError("network_unavailable", "disconnected"),
                  KeyboardInterrupt(), SystemExit()]
        class RaisingAdapter:
            def complete(inner, *args, **kwargs):
                raise error
        for error in errors:
            with self.assertRaises(type(error)):
                advisor.run_ai_evaluation(self.root, self.sid, RaisingAdapter())
        book = request_budget.read(self.root, "evaluation:" + self.sid)
        self.assertEqual(book["used"], 4)
        self.assertTrue(all(item["status"] == "unknown" for item in book["requests"]))

    def test_accounting_failure_never_masks_dispatch_error(self):
        self.session()
        original = AIRequestError("network_unavailable", "original transport error")
        with mock.patch("opencoding.request_budget.finish", side_effect=OSError("accounting error")):
            with self.assertRaises(AIRequestError) as caught:
                advisor.run_ai_evaluation(self.root, self.sid, FakeAdapter([original]))
        self.assertIs(caught.exception, original)
        self.assertEqual(request_budget.read(self.root, "evaluation:" + self.sid)["used"], 1)

    def test_accounting_failure_does_not_turn_saved_evaluation_into_failure(self):
        self.session()
        with mock.patch("opencoding.request_budget.finish", side_effect=OSError("accounting error")) as finish:
            record = advisor.run_ai_evaluation(self.root, self.sid, FakeAdapter([eval_response()]))
        self.assertEqual(finish.call_count, 1)
        self.assertEqual(record["request_budget_status"], "unknown")
        self.assertEqual(advisor.get_evaluation(self.root, record["evaluation_id"])["status"], "pending_confirmation")
        self.assertEqual(request_budget.read(self.root, "evaluation:" + self.sid)["requests"][0]["status"], "unknown")

    def test_saved_evaluation_uses_local_dispatch_and_reservation_identity(self):
        self.session()
        payload = eval_response()
        payload["request_id"] = "adapter-reported-identity"
        adapter = FakeAdapter([payload])
        record = advisor.run_ai_evaluation(self.root, self.sid, adapter, run_id="audit-run")
        saved = advisor.get_evaluation(self.root, record["evaluation_id"])
        reserved = request_budget.read(self.root, "evaluation:" + self.sid)["requests"][0]
        self.assertEqual(saved["request_id"], adapter.calls[0]["request_id"])
        self.assertEqual(saved["request_id"], reserved["request_id"])
        self.assertEqual(saved["request_budget_reservation_id"], reserved["reservation_id"])
        self.assertEqual(saved["adapter_request_id"], "adapter-reported-identity")
        self.assertEqual(saved["run_id"], "audit-run")
        self.assertEqual(saved["task_id"], "ai-evaluation")

    def test_contended_session_locks_have_public_errors(self):
        self.session()
        adapter = FakeAdapter([eval_response()])
        with session_write_lock(self.root):
            with self.assertRaises(service.ServiceError):
                advisor.run_ai_evaluation(self.root, self.sid, adapter)
            with self.assertRaises(service.ServiceError):
                advisor.adopt_confirmed_evaluation(self.root, self.sid)
            with self.assertRaises(FactError):
                add_fact(self.root, self.sid, content="not written", source_type="user")
        self.assertEqual(adapter.calls, [])

    def test_late_response_cannot_supersede_confirmed_evaluation(self):
        self.session()
        record = advisor.run_ai_evaluation(self.root, self.sid, FakeAdapter([eval_response()]))
        self._pending_call(lambda: advisor.confirm_evaluation(self.root, self.sid, record["evaluation_id"],
                            expected_revision=self.revision, accepted=True))
        records = advisor.list_evaluations(self.root, self.sid)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "confirmed")

    def test_fact_update_during_network_discards_response(self):
        self.session()
        self._pending_call(lambda: add_fact(self.root, self.sid, content="只在手机使用", source_type="user"))
        self.assertEqual(advisor.list_evaluations(self.root, self.sid), [])

    def test_session_update_during_network_discards_response(self):
        self.session()
        self._pending_call(lambda: service.submit_answer(self.root, self.sid,
                            self.revision, "audience", "只有管理员"))
        self.assertEqual(advisor.list_evaluations(self.root, self.sid), [])

    def test_adoption_during_network_discards_response(self):
        self.session()
        record = advisor.run_ai_evaluation(self.root, self.sid, FakeAdapter([eval_response()]))
        advisor.confirm_evaluation(self.root, self.sid, record["evaluation_id"],
                                    expected_revision=self.revision, accepted=True)
        self._pending_call(lambda: advisor.adopt_confirmed_evaluation(self.root, self.sid))
        self.assertEqual(advisor.latest_evaluation(self.root, self.sid)["evaluation_id"], record["evaluation_id"])

    def test_fact_update_after_confirmation_prevents_adoption(self):
        self.session()
        record = advisor.run_ai_evaluation(self.root, self.sid, FakeAdapter([eval_response()]))
        advisor.confirm_evaluation(self.root, self.sid, record["evaluation_id"],
                                    expected_revision=self.revision, accepted=True)
        add_fact(self.root, self.sid, content="预算已经改变", source_type="user")
        with self.assertRaises(service.ServiceError):
            advisor.adopt_confirmed_evaluation(self.root, self.sid)
        self.assertFalse((self.root / ".opencoding" / "adoptions" / (self.sid + ".json")).exists())

    def test_fact_update_after_response_prevents_confirmation(self):
        self.session()
        record = advisor.run_ai_evaluation(self.root, self.sid, FakeAdapter([eval_response()]))
        add_fact(self.root, self.sid, content="预算已经改变", source_type="user")
        with self.assertRaises(service.ServiceError):
            advisor.confirm_evaluation(self.root, self.sid, record["evaluation_id"],
                                        expected_revision=self.revision, accepted=True)
        self.assertEqual(advisor.get_evaluation(self.root, record["evaluation_id"])["status"], "pending_confirmation")

    def test_fact_text_is_present_in_provider_input(self):
        self.session()
        add_fact(self.root, self.sid, content="只有十位使用者", source_type="user")
        captured = []
        class CapturingAdapter:
            def complete(inner, messages, **kwargs):
                captured.extend(messages)
                return eval_response()
        advisor.run_ai_evaluation(self.root, self.sid, CapturingAdapter())
        self.assertIn("只有十位使用者", json.dumps(captured, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
