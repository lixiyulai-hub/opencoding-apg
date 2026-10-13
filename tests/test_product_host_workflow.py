"""Real session/adoption fixtures for the production candidate entry point."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from opencoding import service
from opencoding.host import HostRegistry
from opencoding.host_workflow import capture_binding, enqueue_adopted_plan, verify_binding
from opencoding.safety import canonical_json, sha256_bytes
from opencoding.scheduler import Scheduler
from opencoding.sessions import session_write_lock
from opencoding.task_execution import run_task_wave
from tests.test_product_service import _complete
from tests.test_product_task_execution import RecordingHost


class HostWorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='host-production-binding-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        local = self.root / 'local-app-data'
        local.mkdir()
        env = mock.patch.dict(os.environ, {'LOCALAPPDATA': str(local)})
        env.start()
        self.addCleanup(env.stop)
        self.sid = _complete(self.root)['session']['id']
        self.adoption = service.adopt_evaluation_plan(self.root, self.sid)['adoption']
        self.path = self.root / '.opencoding' / 'adoptions' / (self.sid + '.json')
        self.memory = self.root / 'memory.md'
        self.memory.write_bytes(b'Adopted source\r\nKeep exact bytes.\r\n')
        self.adapter = RecordingHost()
        self.registry = HostRegistry()
        self.registry.register('mock', self.adapter, capabilities=['task_candidate', 'cancel', 'timeout'],
                               evidence='offline real-session fixture')

    def capture(self):
        with session_write_lock(self.root):
            return capture_binding(self.root, self.sid)

    def verify(self, binding):
        with session_write_lock(self.root):
            return verify_binding(self.root, binding)

    def mutate_adoption(self, change):
        record = json.loads(self.path.read_text(encoding='utf-8'))
        change(record)
        self.path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')

    def test_capture_uses_full_adoption_and_actual_source_bytes(self):
        before = self.path.read_bytes()
        binding = self.capture()
        self.assertEqual(binding['adoption_digest'], sha256_bytes(canonical_json(self.adoption)))
        self.assertEqual(binding['source_inputs_digest'], sha256_bytes(canonical_json({'memory.md': self.memory.read_bytes().decode('utf-8')})))
        self.assertEqual(binding['input_preimages'], {'memory.md': {'sha256': sha256_bytes(self.memory.read_bytes())}})
        outputs = {p for t in self.adoption['task_plan']['tasks'] for p in t['outputs']}
        self.assertEqual(binding['output_preimages'], {p: {'sha256': None} for p in outputs})
        self.assertEqual(self.verify(binding), binding)
        self.assertEqual(self.path.read_bytes(), before)

    def test_rule_evaluation_without_explicit_adoption_is_rejected(self):
        self.path.unlink()
        service.evaluate_session(self.root, self.sid)
        with self.assertRaises(ValueError):
            self.capture()

    def test_all_authority_identity_and_digest_fields_fail_closed(self):
        before = self.path.read_bytes()
        mutations = [lambda r: r.update(root=str(self.root.parent)), lambda r: r.update(session_id='other'),
                     lambda r: r.update(revision=True), lambda r: r.update(validated=1),
                     lambda r: r.update(plan_digest='a' * 64), lambda r: r.update(input_digest='b' * 64),
                     lambda r: r['task_plan']['tasks'][0].update(description='tampered'),
                     lambda r: r['input_snapshot'].update(session_id='other'),
                     lambda r: r.update(executor_mapping={})]
        for change in mutations:
            with self.subTest(change=change):
                self.path.write_bytes(before)
                self.mutate_adoption(change)
                with self.assertRaises(ValueError):
                    self.capture()

    def test_self_consistent_tampered_plan_cannot_replace_current_snapshot(self):
        def change(record):
            record['task_plan']['tasks'][0]['description'] = 'unadopted alternate'
            record['plan_digest'] = sha256_bytes(canonical_json(record['task_plan']))
            record['input_snapshot']['plan_digest'] = record['plan_digest']
            record['input_digest'] = sha256_bytes(canonical_json(record['input_snapshot']))
        self.mutate_adoption(change)
        with self.assertRaises(ValueError):
            self.capture()

    def test_same_plan_readoption_changes_full_adoption_binding(self):
        binding = self.capture()
        self.mutate_adoption(lambda r: r.update(adopted_at='new-adoption-instance'))
        with self.assertRaises(ValueError):
            self.verify(binding)

    def test_current_session_drift_rejected(self):
        binding = self.capture()
        service.submit_answer(self.root, self.sid, self.adoption['revision'], 'outcome', '不同业务结果')
        with self.assertRaises(ValueError):
            self.verify(binding)

    def test_current_snapshot_compared_even_if_status_claims_match(self):
        status = service.adoption_input_status(self.root, self.sid)
        status['current']['requirement_digest'] = '0' * 64
        with mock.patch('opencoding.service.adoption_input_status', return_value=status):
            with self.assertRaises(ValueError):
                self.capture()

    def test_source_byte_drift_and_missing_source_rejected(self):
        binding = self.capture()
        self.memory.write_bytes(self.memory.read_bytes().replace(b'\r\n', b'\n'))
        with self.assertRaises(ValueError):
            self.verify(binding)
        self.memory.unlink()
        with self.assertRaises(ValueError):
            self.capture()

    def test_non_utf8_source_rejected(self):
        self.memory.write_bytes(b'\xff\xfe')
        with self.assertRaises(ValueError):
            self.capture()

    def test_oversized_source_and_deep_adoption_rejected(self):
        with mock.patch('opencoding.host_workflow._MAX_SOURCE_BYTES', 8):
            with self.assertRaises(ValueError):
                self.capture()
        self.path.write_text('[' * 1500 + '0' + ']' * 1500, encoding='utf-8')
        with self.assertRaises(ValueError):
            self.capture()

    def test_missing_modified_and_deleted_output_preimages_are_bound(self):
        absent = self.capture()
        target = self.root / 'PRG.md'
        target.write_bytes(b'user-owned original\r\n')
        with self.assertRaises(ValueError):
            self.verify(absent)
        present = self.capture()
        self.assertEqual(present['output_preimages']['PRG.md']['sha256'], sha256_bytes(target.read_bytes()))
        target.write_bytes(b'user-owned edit')
        with self.assertRaises(ValueError):
            self.verify(present)
        target.unlink()
        with self.assertRaises(ValueError):
            self.verify(present)

    def test_source_and_output_hardlinks_rejected(self):
        duplicate = self.root / 'linked-input.md'
        os.link(self.memory, duplicate)
        with self.assertRaises(ValueError):
            self.capture()
        duplicate.unlink()
        os.link(self.memory, self.root / 'PRG.md')
        with self.assertRaises(ValueError):
            self.capture()

    def test_adoption_hardlink_and_duplicate_json_keys_rejected(self):
        duplicate = self.root / 'adoption-copy.json'
        os.link(self.path, duplicate)
        with self.assertRaises(ValueError):
            self.capture()
        duplicate.unlink()
        self.path.write_text('{"validated": false,' + self.path.read_text(encoding='utf-8')[1:], encoding='utf-8')
        with self.assertRaises(ValueError):
            self.capture()

    def test_binding_tampering_and_unknown_fields_rejected(self):
        binding = self.capture()
        for key, value in [('root', str(self.root.parent)), ('adoption_digest', 'a' * 64),
                           ('source_inputs_digest', 'a' * 64), ('output_preimages', {}),
                           ('input_preimages', {}), ('extra', True), ('revision', True)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.verify({**binding, key: value})

    def test_enqueue_requires_registered_host_and_missing_source_fails_before_scheduler(self):
        with mock.patch('opencoding.scheduler.Scheduler', side_effect=AssertionError('must not create scheduler')):
            with self.assertRaises(ValueError):
                enqueue_adopted_plan(self.root, self.sid, HostRegistry(), 'mock', 20)
            self.memory.unlink()
            with self.assertRaises(ValueError):
                enqueue_adopted_plan(self.root, self.sid, self.registry, 'mock', 20)

    def test_full_real_adopted_plan_enqueues_and_runs_candidates_only(self):
        batch = enqueue_adopted_plan(self.root, self.sid, self.registry, 'mock', 20)
        scheduler = Scheduler(self.root, host_registry=self.registry)
        for _ in self.adoption['task_plan']['waves']:
            wave = run_task_wave(scheduler, batch['plan_digest'])
            self.assertEqual(wave['status'], 'wave_dispatched', wave)
            for result in wave['results']:
                self.assertEqual(result['run']['status'], 'succeeded', result)
                self.assertEqual(result['run']['execution_mode'], 'production')
        self.assertEqual(run_task_wave(scheduler, batch['plan_digest'])['status'], 'candidates_complete')
        self.assertEqual(len(self.adapter.requests), len(self.adoption['task_plan']['tasks']))
        for task in self.adoption['task_plan']['tasks']:
            for output in task['outputs']:
                self.assertFalse((self.root / output).exists())
        self.verify(self.capture())


if __name__ == '__main__':
    unittest.main()
