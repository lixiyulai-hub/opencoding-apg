"""Offline trusted-adapter fixtures: live labels below are not real provider evidence."""
import copy
import os
import unittest
import threading
import time
from unittest import mock

from opencoding.host_review import ReviewRegistry, _digest
from opencoding.host_candidates import apply_host_candidates
from opencoding.transactions import rollback_changes
from tests import test_product_host_candidates as fixtures
from opencoding.host import HostRegistry


def passed(request, cancel):
    return {'request_digest': _digest(request), 'status': 'passed', 'findings': [],
            'evidence': 'offline trusted-adapter test fixture'}


@unittest.skipUnless(os.name == 'nt', 'Windows snapshot support')
class HostReviewTests(unittest.TestCase):
    setUp = fixtures.HostCandidateTests.setUp
    enqueue = fixtures.HostCandidateTests.enqueue
    mutate_receipt = fixtures.HostCandidateTests.mutate_receipt

    def setup_review(self, *, live=True, validate=passed, review=passed):
        if live:
            hosts = HostRegistry()
            hosts.register('mock', self.host, capabilities=['task_candidate', 'cancel', 'timeout'],
                           verification='live', evidence='explicit offline live-label fixture')
            self.scheduler.host_registry = hosts
        self.enqueue()
        registry = ReviewRegistry()
        verification = 'live' if live else 'simulated'
        registry.register('validator', validate, role='validator', principal='test-runner', verification=verification)
        registry.register('reviewer', review, role='reviewer', principal='independent-reviewer', verification=verification)
        self.registry = registry
        return registry

    def review(self, **kwargs):
        return self.registry.review(self.root, self.digest, validator='validator', reviewer='reviewer', **kwargs)

    def apply(self, result):
        with mock.patch('opencoding.host_workflow.capture_binding', side_effect=lambda *a: copy.deepcopy(self.binding)):
            return apply_host_candidates(self.root, self.digest, review_registry=self.registry,
                                         grant=result.get('grant'), approved_digest=result.get('approved_digest'))

    def test_review_apply_and_transaction_rollback(self):
        self.setup_review()
        result = self.review()
        self.assertEqual(result['status'], 'review_passed', result)
        applied = self.apply(result)
        self.assertEqual(applied['status'], 'applied', applied)
        self.assertEqual((self.root / 'second.txt').read_text(), 'version-2')
        self.assertEqual(self.apply(result)['status'], 'blocked')
        restored = rollback_changes(self.root, applied['transaction_id'])
        self.assertEqual(restored['status'], 'rolled_back', restored)
        self.assertFalse((self.root / 'first.txt').exists())

    def test_simulation_never_issues_apply_grant(self):
        self.setup_review(live=False)
        result = self.review()
        self.assertEqual(result['status'], 'reviewed_simulated', result)
        self.assertNotIn('grant', result)
        self.assertEqual(self.apply(result)['status'], 'blocked')

    def test_blocker_unavailable_wrong_digest_fail_closed(self):
        self.setup_review()
        for response in ({'status': 'blocker'}, {'status': 'unavailable'},
                         {'request_digest': 'a'*64}, {'findings': ['blocker']}):
            with self.subTest(response=response):
                adapter = lambda r,c: {**passed(r,c), **response}
                self.registry._adapters['reviewer'] = (adapter, 'reviewer', 'independent-reviewer', 'live')
                self.assertEqual(self.review()['status'], 'blocked')
        self.assertFalse((self.root / 'first.txt').exists())

    def test_self_review_denied(self):
        self.setup_review()
        for principal in ('mock', 'test-runner'):
            self.registry._adapters['reviewer'] = (passed, 'reviewer', principal, 'live')
            self.assertEqual(self.review()['reason'], 'independent_review_required')

    def test_drift_during_review_rejects_grant(self):
        def changed(request, cancel):
            (self.root / 'first.txt').write_text('user change')
            return passed(request, cancel)
        self.setup_review(review=changed)
        self.assertEqual(self.review()['reason'], 'review_candidate_drift')

    def test_drift_after_review_preserves_user_file(self):
        self.setup_review()
        result = self.review()
        (self.root / 'first.txt').write_text('user change')
        self.assertEqual(self.apply(result)['status'], 'blocked')
        self.assertEqual((self.root / 'first.txt').read_text(), 'user change')

    def test_revoked_grant_and_foreign_registry_rejected(self):
        self.setup_review()
        result = self.review()
        self.registry.revoke(result['grant'])
        self.assertEqual(self.apply(result)['status'], 'blocked')
        result = self.review()
        self.registry = ReviewRegistry()
        self.assertEqual(self.apply(result)['status'], 'blocked')

    def test_timeout_and_late_pass_cannot_issue_grant(self):
        def slow(request, cancel):
            cancel.wait(.2)
            return passed(request, cancel)
        self.setup_review(review=slow)
        result = self.review(timeout_seconds=.03)
        self.assertEqual(result['reason'], 'review_timed_out')
        self.assertEqual(self.registry._grants, {})

    def test_cancel_before_review(self):
        self.setup_review()
        event = threading.Event(); event.set()
        self.assertEqual(self.review(cancel_event=event)['reason'], 'review_cancelled')

    def test_callback_mutation_does_not_change_bound_candidate(self):
        def mutate(request, cancel):
            original = passed(request, cancel)
            request['preview']['file_plan']['entries'][0]['content'] = 'injected'
            return original
        self.setup_review(review=mutate)
        result = self.review()
        applied = self.apply(result)
        self.assertEqual(applied['status'], 'applied', applied)
        self.assertEqual((self.root / 'first.txt').read_text(), 'version-1')

    def test_approval_digest_cannot_be_forged(self):
        self.setup_review()
        result = self.review(); result['approved_digest'] = 'a'*64
        self.assertEqual(self.apply(result)['reason'], 'review_candidate_drift')

    def test_revoke_between_writes_rolls_back_partial_application(self):
        import opencoding.transactions as transactions
        self.setup_review()
        result = self.review()
        original = transactions._atomic_write
        def write_then_revoke(path, content):
            original(path, content)
            self.registry.revoke(result['grant'])
        with mock.patch.object(transactions, '_atomic_write', side_effect=write_then_revoke):
            applied = self.apply(result)
        self.assertNotEqual(applied['status'], 'applied', applied)
        self.assertFalse((self.root / 'first.txt').exists(), applied)
        self.assertFalse((self.root / 'second.txt').exists(), applied)

    def test_cancelled_review_never_calls_adapter(self):
        callback = mock.Mock(side_effect=passed)
        self.setup_review(validate=callback)
        event = threading.Event(); event.set()
        self.review(cancel_event=event)
        callback.assert_not_called()

    def test_duplicate_registration_rejected(self):
        self.setup_review()
        with self.assertRaises(ValueError):
            self.registry.register('validator', passed, role='validator', principal='another')


class AdoptedHostReviewTests(unittest.TestCase):
    from tests.test_product_host_workflow import HostWorkflowTests as _Workflow
    setUp = _Workflow.setUp

    def prepare(self):
        from opencoding.host_workflow import enqueue_adopted_plan
        from opencoding.scheduler import Scheduler
        from opencoding.task_execution import run_task_wave
        hosts = HostRegistry()
        hosts.register('mock', self.adapter, capabilities=['task_candidate', 'cancel', 'timeout'],
                       verification='live', evidence='offline integration fixture, not real live evidence')
        batch = enqueue_adopted_plan(self.root, self.sid, hosts, 'mock', 20)
        scheduler = Scheduler(self.root, host_registry=hosts)
        for wave in self.adoption['task_plan']['waves']:
            result = run_task_wave(scheduler, batch['plan_digest'])
            self.assertEqual(result['status'], 'wave_dispatched', result)
        registry = ReviewRegistry()
        registry.register('validation', passed, role='validator', principal='validation', verification='live')
        registry.register('review', passed, role='reviewer', principal='review', verification='live')
        result = registry.review(self.root, batch['plan_digest'], validator='validation', reviewer='review')
        self.assertEqual(result['status'], 'review_passed', result)
        return batch, registry, result

    def test_real_adoption_apply_rollback_and_restart_requires_review(self):
        batch, registry, result = self.prepare()
        applied = apply_host_candidates(self.root, batch['plan_digest'], review_registry=registry,
                                        grant=result['grant'], approved_digest=result['approved_digest'])
        self.assertEqual(applied['status'], 'applied', applied)
        self.assertTrue((self.root / 'PRG.md').is_file())
        self.assertTrue(__import__('pathlib').Path(applied['review_evidence_path']).is_file())
        rollback = rollback_changes(self.root, applied['transaction_id'])
        self.assertEqual(rollback['status'], 'rolled_back', rollback)
        self.assertFalse((self.root / 'PRG.md').exists())
        self.assertEqual(apply_host_candidates(self.root, batch['plan_digest'], review_registry=ReviewRegistry(),
                         grant=result['grant'], approved_digest=result['approved_digest'])['status'], 'blocked')

    def test_authority_drift_after_review_blocks_application(self):
        from opencoding.facts import add_fact
        batch, registry, result = self.prepare()
        add_fact(self.root, self.sid, content='New user constraint', source_type='user')
        applied = apply_host_candidates(self.root, batch['plan_digest'], review_registry=registry,
                                        grant=result['grant'], approved_digest=result['approved_digest'])
        self.assertEqual(applied['status'], 'blocked', applied)
        self.assertFalse((self.root / 'PRG.md').exists())
