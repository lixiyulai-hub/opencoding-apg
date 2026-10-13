import copy
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from opencoding.host import HostRegistry, MockHost, request_digest, request_identity
from opencoding.planning import validate_task_plan
from opencoding.scheduler import Scheduler
from opencoding.task_execution import enqueue_task_plan


def two_task_plan():
    def task(name, inputs, outputs, dependencies):
        return {'id': name, 'title': name, 'description': 'Review candidate',
                'depends_on': dependencies, 'inputs': inputs, 'outputs': outputs,
                'action': {'type': 'security_review'}, 'acceptance': ['Candidate meets requirement'],
                'rollback': 'Discard unapplied candidate', 'retry': {'max_attempts': 2},
                'activation_gate': {'required': False, 'reason': 'Local candidate only'}}
    return {'schema_version': '1.1', 'session_id': 'host-review', 'revision': 1,
            'unresolved': [], 'tasks': [task('first', ['requirements.txt'], ['first.txt'], []),
                                     task('second', ['first.txt'], ['second.txt'], ['first'])],
            'waves': [['first'], ['second']]}


class RecordingHost:
    def __init__(self):
        self.requests = []

    def execute(self, request, cancel_event):
        self.requests.append(copy.deepcopy(request))
        return {'status': 'succeeded', 'request_digest': request_digest(request),
                'identity': request_identity(request),
                'files': {p: 'candidate:' + p for p in request['outputs']}}


class TaskExecutionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='host-task-review-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        local = self.root / 'local-app-data'
        local.mkdir()
        patcher = mock.patch.dict(os.environ, {'LOCALAPPDATA': str(local)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.adapter = RecordingHost()
        self.registry = HostRegistry()
        self.registry.register('mock', self.adapter, capabilities=['task_candidate', 'cancel', 'timeout'], evidence='independent simulated fixture')
        self.scheduler = Scheduler(self.root, host_registry=self.registry)
        self.plan = two_task_plan()
        self.assertTrue(validate_task_plan(self.plan)['valid'])

    def enqueue(self, budget=2):
        return enqueue_task_plan(self.scheduler, self.plan, host_id='mock', max_requests=budget,
                                 inputs={'requirements.txt': 'Build candidates'})

    def test_dependency_candidate_propagation_and_receipts_remain_pending(self):
        batch = self.enqueue()
        self.assertEqual(self.scheduler.run_next(batch['task_ids']['second'])['status'], 'idle')
        first = self.scheduler.run_next()
        second = self.scheduler.run_next()
        self.assertEqual(first['run']['status'], 'succeeded')
        self.assertEqual(second['run']['status'], 'succeeded')
        downstream = self.adapter.requests[1]['input']
        self.assertEqual(downstream['files'], {'first.txt': 'candidate:first.txt'})
        self.assertEqual(downstream['dependencies'][0]['receipt_digest'], first['run']['receipt_digest'])
        self.assertEqual(downstream['dependencies'][0]['candidate_digest'], first['run']['host_result']['candidate']['digest'])
        for result in (first, second):
            self.assertEqual(result['run']['validation']['status'], 'pending')
            self.assertEqual(result['run']['rollback']['status'], 'not_applied')
            self.assertEqual(result['run']['delivery_gate'], 'pending_independent_review')
            self.assertEqual(result['run']['host_result']['verification'], 'simulated')
        self.assertFalse((self.root / 'first.txt').exists())
        self.assertFalse((self.root / 'second.txt').exists())

    def test_cumulative_budget_survives_restart(self):
        batch = self.enqueue(budget=1)
        self.scheduler.run_next()
        self.scheduler = Scheduler(self.root, host_registry=self.registry)
        result = self.scheduler.run_next()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'host_request_budget_exhausted')
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(self.scheduler.get_task(batch['task_ids']['second'])['attempt'], 0)

    def test_restart_reenqueue_is_idempotent(self):
        self.enqueue()
        self.scheduler.run_next()
        self.scheduler = Scheduler(self.root, host_registry=self.registry)
        again = self.enqueue()
        self.assertTrue(all(t['status'] == 'already_enqueued' for t in again['tasks']))
        self.scheduler.run_next()
        self.assertEqual(self.scheduler.run_next()['status'], 'idle')
        self.assertEqual(len(self.adapter.requests), 2)

    def test_failed_requests_are_not_refunded(self):
        failing = HostRegistry()
        adapter = MockHost({}, status='failed')
        failing.register('mock', adapter, capabilities=['task_candidate', 'cancel', 'timeout'], evidence='failure fixture')
        self.scheduler.host_registry = failing
        self.enqueue(budget=1)
        self.assertEqual(self.scheduler.run_next()['run']['status'], 'failed')
        self.assertEqual(self.scheduler.run_next()['reason'], 'host_request_budget_exhausted')
        self.assertEqual(adapter.calls, 1)

    def test_activation_gate_prevents_all_enqueues(self):
        self.plan['tasks'][1]['activation_gate'] = {'required': True, 'reason': 'External authorization required'}
        with self.assertRaises(ValueError):
            self.enqueue()
        self.assertEqual(self.scheduler.run_next()['status'], 'idle')
        self.assertEqual(self.adapter.requests, [])

    def test_unverified_host_is_rejected_before_consuming_attempt(self):
        self.scheduler.host_registry = HostRegistry()
        batch = self.enqueue()
        result = self.scheduler.run_next()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.scheduler.get_task(batch['task_ids']['first'])['attempt'], 0)
        self.assertEqual(self.scheduler.list_runs(), [])

    def test_missing_registry_is_rejected_without_dispatch(self):
        self.scheduler.host_registry = None
        self.enqueue()
        self.assertEqual(self.scheduler.run_next()['reason'], 'host_registry_unavailable')
        self.assertEqual(self.scheduler.list_runs(), [])

    def test_cancel_between_host_response_and_finish_discards_candidate(self):
        batch = self.enqueue()
        original = self.scheduler._finish
        def cancel_then_finish(task, result, run_id):
            self.scheduler.cancel(task['task_id'])
            return original(task, result, run_id)
        with mock.patch.object(self.scheduler, '_finish', side_effect=cancel_then_finish):
            result = self.scheduler.run_next()
        self.assertEqual(result['run']['status'], 'cancelled')
        self.assertIsNone(result['run']['host_result']['candidate'])
        self.assertEqual(self.scheduler.get_task(batch['task_ids']['second'])['state'], 'frozen')

    def test_direct_enqueue_cannot_strip_plan_dependencies(self):
        # A caller of the public scheduler must not bypass the TaskPlan wrapper.
        class Capture:
            def __init__(self):
                self.tasks = []
            def enqueue(self, task):
                self.tasks.append(copy.deepcopy(task))
                return {'status': 'enqueued'}
        capture = Capture()
        enqueue_task_plan(capture, self.plan, host_id='mock', max_requests=2,
                          inputs={'requirements.txt': 'Build candidates'})
        forged = capture.tasks[1]
        forged['depends_on'] = []
        forged['input']['source_inputs']['first.txt'] = 'fabricated prior candidate'
        try:
            self.scheduler.enqueue(forged)
        except ValueError:
            return
        result = self.scheduler.run_next()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.adapter.requests, [])

    def test_cancelled_adapter_late_response_cannot_restart_dependency(self):
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()
        class LateHost(RecordingHost):
            def execute(self, request, cancel_event):
                entered.set()
                release.wait(5)
                response = super().execute(request, cancel_event)
                exited.set()
                return response
        late = LateHost()
        registry = HostRegistry()
        registry.register('mock', late, capabilities=['task_candidate', 'cancel', 'timeout'], evidence='late response fixture')
        self.scheduler.host_registry = registry
        batch = self.enqueue()
        results = []
        worker = threading.Thread(target=lambda: results.append(self.scheduler.run_next()))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            self.scheduler.cancel(batch['task_ids']['first'])
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results[0]['run']['status'], 'cancelled')
            self.assertIsNone(results[0]['run']['host_result']['candidate'])
        finally:
            release.set()
            worker.join(5)
            exited.wait(5)
        self.assertEqual(self.scheduler.run_next()['status'], 'idle')
        self.assertEqual(self.scheduler.get_task(batch['task_ids']['second'])['state'], 'frozen')

    def test_wave_execution_advances_only_after_prior_candidates(self):
        from opencoding.task_execution import run_task_wave
        batch = self.enqueue()
        first = run_task_wave(self.scheduler, batch['plan_digest'])
        self.assertEqual((first['status'], first['wave']), ('wave_dispatched', 0))
        self.assertEqual(len(self.adapter.requests), 1)
        second = run_task_wave(self.scheduler, batch['plan_digest'])
        self.assertEqual((second['status'], second['wave']), ('wave_dispatched', 1))
        self.assertEqual(len(self.adapter.requests), 2)
        complete = run_task_wave(self.scheduler, batch['plan_digest'])
        self.assertEqual(complete['status'], 'candidates_complete')
        self.assertEqual(complete['delivery_gate'], 'pending_independent_review')

    def test_tampered_dependency_candidate_is_not_consumed(self):
        import json
        from opencoding.scheduler import _connect
        batch = self.enqueue()
        first = self.scheduler.run_next()
        receipt = copy.deepcopy(first['run'])
        receipt['host_result']['candidate']['files']['first.txt'] = 'altered after dispatch'
        with _connect(self.root) as connection:
            connection.execute('UPDATE runs SET receipt_json=? WHERE run_id=?',
                               (json.dumps(receipt), receipt['run_id']))
        result = self.scheduler.run_next(batch['task_ids']['second'])
        self.assertEqual(result['run']['status'], 'failed')
        self.assertEqual(len(self.adapter.requests), 1)

    def test_transitive_ancestor_input_is_available_to_final_task(self):
        final = copy.deepcopy(self.plan['tasks'][1])
        final.update(id='third', title='third', depends_on=['second'],
                     inputs=['first.txt', 'second.txt'], outputs=['third.txt'])
        self.plan['tasks'].append(final)
        self.plan['waves'].append(['third'])
        self.assertTrue(validate_task_plan(self.plan)['valid'])
        batch = self.enqueue(budget=3)
        for _ in range(3):
            self.assertEqual(self.scheduler.run_next()['run']['status'], 'succeeded')
        payload = self.adapter.requests[2]['input']
        self.assertEqual(payload['files'], {'first.txt': 'candidate:first.txt',
                                           'second.txt': 'candidate:second.txt'})
        self.assertEqual([dep['task_id'] for dep in payload['dependencies']],
                         [batch['task_ids']['first'], batch['task_ids']['second']])

    def test_revised_ancestor_output_uses_latest_wave_not_dependency_order(self):
        self.plan['tasks'][1]['outputs'] = ['first.txt']
        final = copy.deepcopy(self.plan['tasks'][1])
        final.update(id='third', title='third', depends_on=['second', 'first'],
                     inputs=['first.txt'], outputs=['third.txt'])
        self.plan['tasks'].append(final)
        self.plan['waves'].append(['third'])
        self.assertTrue(validate_task_plan(self.plan)['valid'])
        class RevisingHost(RecordingHost):
            def execute(self, request, cancel_event):
                response = super().execute(request, cancel_event)
                response['files'] = {p: request['input']['task']['id'] for p in request['outputs']}
                return response
        self.adapter = RevisingHost()
        registry = HostRegistry()
        registry.register('mock', self.adapter, capabilities=['task_candidate', 'cancel', 'timeout'],
                          evidence='ordered revision fixture')
        self.scheduler.host_registry = registry
        self.enqueue(budget=3)
        for _ in range(3):
            self.assertEqual(self.scheduler.run_next()['run']['status'], 'succeeded')
        self.assertEqual(self.adapter.requests[2]['input']['files'], {'first.txt': 'second'})

    def test_same_plan_receipt_substitution_is_rejected(self):
        import json
        from opencoding.scheduler import _connect
        # Revisions share an output, so only identity binding detects this swap.
        self.plan['tasks'][1]['outputs'] = ['first.txt']
        final = copy.deepcopy(self.plan['tasks'][1])
        final.update(id='third', title='third', depends_on=['second'],
                     inputs=['first.txt'], outputs=['third.txt'])
        self.plan['tasks'].append(final)
        self.plan['waves'].append(['third'])
        batch = self.enqueue(budget=3)
        first = self.scheduler.run_next()
        second = self.scheduler.run_next()
        # Keep the first receipt internally hash-consistent, but place it in the second run.
        with _connect(self.root) as connection:
            connection.execute('UPDATE runs SET receipt_json=? WHERE run_id=?',
                               (json.dumps(first['run']), second['run']['run_id']))
        result = self.scheduler.run_next(batch['task_ids']['third'])
        self.assertEqual(result['run']['status'], 'failed')
        self.assertEqual(len(self.adapter.requests), 2)
        self.assertIn('host dependency evidence mismatch', result['run']['stderr_summary'])
