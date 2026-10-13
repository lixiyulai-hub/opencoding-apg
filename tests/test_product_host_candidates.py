"""Candidate evidence tests use real scheduler records and a stub authority verifier."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from opencoding.host import HostRegistry, request_digest, request_identity
from opencoding.host_candidates import apply_host_candidates, preview_host_candidates
from opencoding.safety import canonical_json, sha256_bytes
from opencoding.scheduler import Scheduler, _connect, _receipt_digest
from opencoding.task_execution import enqueue_task_plan
from tests.test_product_task_execution import two_task_plan


class CandidateHost:
    def __init__(self):
        self.calls = 0

    def execute(self, request, cancel_event):
        self.calls += 1
        return {'status': 'succeeded', 'request_digest': request_digest(request),
                'identity': request_identity(request),
                'files': {path: 'version-' + str(self.calls) for path in request['outputs']}}


@unittest.skipUnless(os.name == 'nt', 'Scheduler zero-write snapshot supports Windows only')
class HostCandidateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='host-candidate-preview-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        env = mock.patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'local-app-data')})
        env.start()
        self.addCleanup(env.stop)
        self.plan = two_task_plan()
        self.source = {'requirements.txt': 'source text'}
        (self.root / 'requirements.txt').write_text('source text', encoding='utf-8')
        self.host = CandidateHost()
        registry = HostRegistry()
        registry.register('mock', self.host, capabilities=['task_candidate', 'cancel', 'timeout'],
                          evidence='offline candidate fixture')
        self.scheduler = Scheduler(self.root, host_registry=registry)
        verifier = mock.patch('opencoding.host_workflow.verify_binding', side_effect=lambda root, binding: copy.deepcopy(binding))
        self.verify = verifier.start()
        self.addCleanup(verifier.stop)

    def enqueue(self, *, bound=True, complete=True):
        self.digest = sha256_bytes(canonical_json(self.plan))
        paths = {p for t in self.plan['tasks'] for p in t['outputs']}
        self.binding = {'schema_version': '1.0', 'root': str(self.root),
                        'session_id': self.plan['session_id'], 'revision': self.plan['revision'],
                        'plan_digest': self.digest, 'input_digest': 'b' * 64,
                        'adoption_digest': 'c' * 64,
                        'source_inputs_digest': sha256_bytes(canonical_json(self.source)),
                        'input_preimages': {p: {'sha256': sha256_bytes(v.encode())} for p, v in self.source.items()},
                        'output_preimages': {p: {'sha256': sha256_bytes((self.root / p).read_bytes())
                                               if (self.root / p).exists() else None} for p in paths}}
        self.batch = enqueue_task_plan(self.scheduler, self.plan, host_id='mock', max_requests=6,
                                       inputs=self.source, production_binding=self.binding if bound else None)
        if complete:
            for _ in self.plan['tasks']:
                result = self.scheduler.run_next()
                self.assertEqual(result['run']['status'], 'succeeded', result)

    def preview(self):
        return preview_host_candidates(self.root, self.digest)

    def mutate_receipt(self, mutate, original='second'):
        with _connect(self.root) as connection:
            row = connection.execute('SELECT * FROM runs WHERE task_id=? ORDER BY attempt DESC',
                                     (self.batch['task_ids'][original],)).fetchone()
            receipt = json.loads(row['receipt_json'])
            mutate(receipt)
            receipt['receipt_digest'] = _receipt_digest(receipt)
            connection.execute('UPDATE runs SET receipt_json=?,receipt_digest=? WHERE run_id=?',
                               (json.dumps(receipt), receipt['receipt_digest'], row['run_id']))

    def test_preview_preserves_pending_gate_and_never_writes_outputs(self):
        self.enqueue()
        result = self.preview()
        self.assertEqual(result['status'], 'preview_ready', result)
        self.assertEqual(result['delivery_gate'], 'pending_independent_review')
        self.assertEqual(result['verification'], ['simulated'])
        self.assertEqual(len(result['run_receipt_digests']), 2)
        self.assertEqual({e['path']: e['content'] for e in result['file_plan']['entries']},
                         {'first.txt': 'version-1', 'second.txt': 'version-2'})
        self.assertFalse((self.root / 'first.txt').exists())
        self.assertFalse((self.root / 'second.txt').exists())
        self.verify.assert_called()

    def test_final_candidate_uses_topological_latest_writer(self):
        self.plan['tasks'][1]['outputs'] = ['first.txt']
        self.enqueue()
        result = self.preview()
        self.assertEqual(result['status'], 'preview_ready', result)
        self.assertEqual(result['file_plan']['entries'][0]['content'], 'version-2')

    def test_existing_output_is_bound_as_previous_output(self):
        raw = b'original\r\ntext'
        (self.root / 'first.txt').write_bytes(raw)
        self.enqueue()
        result = self.preview()
        self.assertEqual(result['status'], 'preview_ready', result)
        entry = next(e for e in result['file_plan']['entries'] if e['path'] == 'first.txt')
        self.assertEqual(entry['before_sha256'], sha256_bytes(raw))
        self.assertEqual((self.root / 'first.txt').read_bytes(), raw)

    def test_new_self_output_input_is_reconstructed(self):
        self.plan['tasks'][1]['inputs'].append('second.txt')
        self.enqueue()
        result = self.preview()
        self.assertEqual(result['status'], 'preview_ready', result)

    def test_existing_self_output_input_is_reconstructed(self):
        self.plan['tasks'][1]['inputs'].append('second.txt')
        (self.root / 'second.txt').write_text('old second', encoding='utf-8')
        self.enqueue()
        result = self.preview()
        self.assertEqual(result['status'], 'preview_ready', result)

    def test_receipt_binding_cannot_be_replaced(self):
        self.enqueue()
        self.mutate_receipt(lambda r: r['production_binding'].update(adoption_digest='f' * 64))
        self.assertEqual(self.preview()['status'], 'blocked')

    def test_nonproduction_receipt_cannot_preview(self):
        self.enqueue()
        self.mutate_receipt(lambda r: r.update(execution_mode='simulation'))
        self.assertEqual(self.preview()['status'], 'blocked')

    def test_oversized_preimage_is_bounded(self):
        self.enqueue()
        (self.root / 'first.txt').write_bytes(b'x' * (2 * 1024 * 1024 + 1))
        self.assertEqual(self.preview()['reason'], 'candidate_preimage_oversized')

    def test_malformed_database_action_is_blocked(self):
        self.enqueue()
        with _connect(self.root) as connection:
            connection.execute("UPDATE tasks SET action_json='[]'")
        self.assertEqual(self.preview()['status'], 'blocked')

    def test_incomplete_tasks_are_blocked(self):
        self.enqueue(complete=False)
        self.scheduler.run_next()
        self.assertEqual(self.preview()['status'], 'blocked')

    def test_unbound_internal_candidates_cannot_preview_as_production(self):
        self.enqueue(bound=False)
        self.assertEqual(self.preview()['reason'], 'production_binding_required')

    def test_stale_authority_is_blocked(self):
        self.enqueue()
        self.verify.side_effect = ValueError('private source diagnostics')
        result = self.preview()
        self.assertEqual(result['reason'], 'production_binding_stale')
        self.assertNotIn('private', json.dumps(result))

    def test_changed_target_preimage_is_blocked_even_with_stub_verifier(self):
        self.enqueue()
        (self.root / 'first.txt').write_text('user edit', encoding='utf-8')
        self.assertEqual(self.preview()['reason'], 'candidate_preimage_drift')
        self.assertEqual((self.root / 'first.txt').read_text(), 'user edit')

    def test_rehashed_wrong_host_identity_is_rejected(self):
        self.enqueue()
        self.mutate_receipt(lambda r: r['host_result']['identity'].update(run_id='other-run'))
        self.assertEqual(self.preview()['status'], 'blocked')

    def test_rehashed_wrong_request_digest_is_rejected(self):
        self.enqueue()
        self.mutate_receipt(lambda r: r['host_result'].update(request_digest='f' * 64))
        self.assertEqual(self.preview()['status'], 'blocked')

    def test_rehashed_wrong_candidate_digest_is_rejected(self):
        self.enqueue()
        self.mutate_receipt(lambda r: r['host_result']['candidate']['files'].update({'second.txt': 'tampered'}))
        self.assertEqual(self.preview()['status'], 'blocked')

    def test_database_input_binding_is_checked(self):
        self.enqueue()
        with _connect(self.root) as connection:
            connection.execute("UPDATE runs SET input_sha256=?", ('d' * 64,))
        self.assertEqual(self.preview()['status'], 'blocked')

    def test_database_dependency_binding_is_checked(self):
        self.enqueue()
        with _connect(self.root) as connection:
            connection.execute('DELETE FROM task_dependencies')
        self.assertEqual(self.preview()['status'], 'blocked')

    def test_ordinary_digest_approval_cannot_open_publication_gate(self):
        self.enqueue()
        result = self.preview()
        blocked = apply_host_candidates(self.root, self.digest,
                                        approved_digest=result['file_plan']['plan_digest'])
        self.assertEqual(blocked['reason'], 'missing_independent_validation_review_gate')
        self.assertFalse((self.root / 'first.txt').exists())


if __name__ == '__main__':
    unittest.main()
