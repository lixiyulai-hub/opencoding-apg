"""Cross-process budget regressions; no model calls or candidate execution."""
import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path

from opencoding import generic_run


def _mutate_budget(root, operation, index, start, results):
    project = Path(root)
    original = generic_run._require_readable_ledger

    def delayed_read(*args):
        value = original(*args)
        time.sleep(0.08)  # Widen the old read/write race, inside the new lock.
        return value

    generic_run._require_readable_ledger = delayed_read
    start.wait(15)
    try:
        if operation == "extend":
            generic_run.extend_task_budget(
                project, "task", "session", amount=1,
                reason="explicit test extension", issued_by="test")
        else:
            generic_run.record_dispatch(
                project, "task", "session", "run-" + str(index), dispatched=1)
        results.put("ok")
    except Exception as exc:
        results.put(getattr(exc, "code", type(exc).__name__))


class BudgetConcurrencyTests(unittest.TestCase):
    def _race(self, project, operation):
        context = multiprocessing.get_context("spawn")
        start, results = context.Event(), context.Queue()
        workers = [context.Process(target=_mutate_budget,
                   args=(str(project), operation, i, start, results)) for i in range(4)]
        try:
            for worker in workers:
                worker.start()
            start.set()
            outcomes = [results.get(timeout=30) for _ in workers]
            for worker in workers:
                worker.join(15)
                self.assertEqual(worker.exitcode, 0)
            return outcomes
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(5)
            results.close()

    def test_concurrent_extensions_preserve_every_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            generic_run.open_task_ledger(project, "task", "session", ceiling=1, run_id="initial")
            self.assertEqual(self._race(project, "extend"), ["ok"] * 4)
            state = generic_run.task_budget_state(project, "task", "session")
            self.assertEqual(state["ceiling"], 5)
            self.assertEqual(len(state["extensions"]), 4)

    def test_concurrent_dispatch_cannot_spend_the_last_request_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            generic_run.open_task_ledger(project, "task", "session", ceiling=1, run_id="initial")
            outcomes = self._race(project, "dispatch")
            self.assertEqual(outcomes.count("ok"), 1, outcomes)
            self.assertEqual(outcomes.count("task_budget_exhausted"), 3, outcomes)
            state = generic_run.task_budget_state(project, "task", "session")
            self.assertEqual(state["dispatched"], 1)
            self.assertEqual(state["remaining"], 0)

    def test_repeated_dispatch_record_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            generic_run.open_task_ledger(project, "task", "session", ceiling=1, run_id="run")
            for _ in range(2):
                generic_run.record_dispatch(project, "task", "session", "run", dispatched=1)
            self.assertEqual(generic_run.task_budget_state(project, "task", "session")["dispatched"], 1)
