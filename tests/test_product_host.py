import copy
import math
import tempfile
import threading
import time
import unittest
from pathlib import Path

from opencoding.host import HostRegistry, HostProtocolError, MockHost, candidate_digest


class HostProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.request = {'schema_version': '1.0', 'host_id': 'mock', 'root': str(Path(self.tmp.name).resolve()),
                        'session_id': 'session-1', 'plan_digest': 'a' * 64, 'task_id': 'task-1',
                        'run_id': 'run-1', 'attempt': 1, 'outputs': ['src/app.py'],
                        'input': {'description': 'write candidate'}, 'timeout_seconds': 1}
        self.host = MockHost({'src/app.py': 'print(42)\n'})
        self.registry = HostRegistry()
        self.register(self.host)

    def register(self, host, registry=None, **kwargs):
        (registry or self.registry).register('mock', host, capabilities={'task_candidate', 'cancel', 'timeout'},
                                            evidence='local contract tests', **kwargs)

    def test_candidate_is_bound_and_does_not_write(self):
        result = self.registry.execute(self.request)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['verification'], 'simulated')
        self.assertEqual(result['candidate']['digest'], candidate_digest(self.host.files))
        self.assertEqual(result['identity']['outputs'], self.request['outputs'])
        self.assertFalse((Path(self.tmp.name) / 'src').exists())

    def test_missing_registration_and_capabilities_rejected(self):
        registry = HostRegistry()
        with self.assertRaises(HostProtocolError):
            registry.execute(self.request)
        registry.register('mock', self.host, capabilities=['task_candidate'], evidence='partial')
        with self.assertRaises(HostProtocolError):
            registry.execute(self.request)
        self.assertEqual(self.host.calls, 0)

    def test_unverified_registration_rejected(self):
        for kwargs in ({'verification': 'unverified', 'evidence': 'test'}, {'evidence': ''}, {'evidence': ' '}):
            with self.subTest(kwargs=kwargs), self.assertRaises(HostProtocolError):
                HostRegistry().register('mock', self.host, capabilities=['task_candidate'], **kwargs)

    def test_duplicate_registration_rejected(self):
        with self.assertRaises(HostProtocolError):
            self.register(self.host)

    def test_preflight_is_read_only_and_detached(self):
        metadata = self.registry.require('mock')
        self.assertEqual(metadata['verification'], 'simulated')
        metadata['capabilities'].clear()
        self.assertEqual(len(self.registry.require('mock')['capabilities']), 3)
        self.assertEqual(self.host.calls, 0)
        with self.assertRaises(HostProtocolError):
            self.registry.require('unknown')
        registry = HostRegistry()
        registry.register('partial', self.host, capabilities=['task_candidate'], evidence='partial')
        with self.assertRaises(HostProtocolError):
            registry.require('partial')

    def test_strict_request_validation(self):
        mutations = [('attempt', True), ('attempt', 0), ('timeout_seconds', True),
                     ('timeout_seconds', math.nan), ('timeout_seconds', math.inf),
                     ('timeout_seconds', 0), ('timeout_seconds', 3601), ('plan_digest', 'bad'),
                     ('schema_version', '2.0'), ('root', '.'), ('input', []), ('outputs', []),
                     ('session_id', 'bad id'), ('outputs', ('a.py',)), ('input', {1: 'value'})]
        for key, value in mutations:
            with self.subTest(key=key, value=value), self.assertRaises(HostProtocolError):
                self.registry.execute({**self.request, key: value})
        with self.assertRaises(HostProtocolError):
            self.registry.execute({**self.request, 'unknown': True})
        self.assertEqual(self.host.calls, 0)

    def test_nonportable_or_escaping_paths_rejected(self):
        for output in ('../out', '/out', 'C:/out', 'a\\b', 'a//b', './a', 'a/../b', 'a.', 'a ', 'NUL.py', 'com1', 'a\x00b', '.git/config', 'a/.opencoding/config', '.governance/file', 'a?b', 'a\x7fb', '．．/out', 'a／../b', 'ＮＵＬ.py'):
            with self.subTest(output=output), self.assertRaises(HostProtocolError):
                self.registry.execute({**self.request, 'outputs': [output]})
        for outputs in (['a', 'a'], ['A.py', 'a.py'], ['a', 'a/b.py'], ['Ａ.py', 'a.py']):
            with self.assertRaises(HostProtocolError):
                self.registry.execute({**self.request, 'outputs': outputs})

    def test_request_size_and_depth_limits(self):
        with self.assertRaises(HostProtocolError):
            self.registry.execute({**self.request, 'input': {'text': 'x' * (2 * 1024 * 1024)}})
        nested = {}
        for _ in range(45):
            nested = {'nested': nested}
        with self.assertRaises(HostProtocolError):
            self.registry.execute({**self.request, 'input': nested})

    def test_same_request_executes_once_and_returns_detached_copy(self):
        first = self.registry.execute(self.request)
        first['candidate']['files']['src/app.py'] = 'tampered'
        first['identity']['outputs'].append('extra')
        second = self.registry.execute(self.request)
        self.assertEqual(self.host.calls, 1)
        self.assertEqual(second['candidate']['files'], self.host.files)
        self.assertEqual(second['identity']['outputs'], self.request['outputs'])

    def test_same_run_cannot_change_any_request_field(self):
        self.registry.execute(self.request)
        for key, value in [('task_id', 'task-2'), ('session_id', 's2'), ('plan_digest', 'b' * 64),
                           ('attempt', 2), ('outputs', ['other.py']), ('input', {}), ('timeout_seconds', 2)]:
            with self.subTest(key=key), self.assertRaises(HostProtocolError):
                self.registry.execute({**self.request, key: value})
        self.assertEqual(self.host.calls, 1)

    def test_concurrent_duplicates_join_one_dispatch(self):
        self.host.delay_seconds = .05
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.registry.execute(self.request))) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 6)
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.host.calls, 1)

    def test_pre_cancelled_request_never_dispatches(self):
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(self.registry.execute(self.request, cancel_event=cancel)['status'], 'cancelled')
        self.assertEqual(self.host.calls, 0)

    def test_running_cancellation(self):
        self.host.delay_seconds = 1
        cancel = threading.Event()
        timer = threading.Timer(.03, cancel.set)
        timer.start()
        self.addCleanup(timer.join)
        result = self.registry.execute(self.request, cancel_event=cancel)
        self.assertEqual(result['status'], 'cancelled')
        self.assertIsNone(result['candidate'])
        self.assertIn('not guaranteed', result['error'])

    def test_timeout_ignores_late_result_and_prevents_redispatch(self):
        returned = threading.Event()
        class LateHost(MockHost):
            def execute(self, request, cancel_event):
                time.sleep(.1)
                result = super().execute(request, threading.Event())
                returned.set()
                return result
        host = LateHost(self.host.files)
        registry = HostRegistry()
        self.register(host, registry)
        request = {**self.request, 'timeout_seconds': .02}
        result = registry.execute(request)
        self.assertEqual(result['status'], 'timed_out')
        self.assertTrue(returned.wait(1))
        self.assertEqual(registry.execute(request), result)
        self.assertEqual(host.calls, 1)

    def tampered(self, mutate):
        class BadHost(MockHost):
            def execute(self, request, cancel_event):
                response = super().execute(request, cancel_event)
                mutate(response)
                return response
        registry = HostRegistry()
        self.register(BadHost(self.host.files), registry)
        return registry.execute(self.request)

    def test_identity_fields_all_bound(self):
        changes = {'root': '.', 'session_id': 'other', 'plan_digest': 'b' * 64, 'task_id': 'other',
                   'run_id': 'other', 'attempt': True, 'outputs': ['other'], 'host_id': 'other'}
        for key, value in changes.items():
            with self.subTest(key=key):
                result = self.tampered(lambda response: response['identity'].__setitem__(key, value))
                self.assertEqual(result['status'], 'failed')
                self.assertIsNone(result['candidate'])

    def test_invalid_response_rejected(self):
        mutations = [lambda r: r.update(request_digest='b' * 64), lambda r: r.update(verification='live'),
                     lambda r: r.update(status='unknown'), lambda r: r.update(files={'other.py': 'text'}),
                     lambda r: r.update(files={'src/app.py': 42}), lambda r: r.update(status='failed'),
                     lambda r: r.update(files={'src/app.py': 'x' * (2 * 1024 * 1024)})]
        for mutate in mutations:
            result = self.tampered(mutate)
            self.assertEqual(result['status'], 'failed')
            self.assertIsNone(result['candidate'])

    def test_adapter_failure_is_cached_and_sanitized(self):
        class BrokenHost:
            calls = 0
            def execute(self, request, cancel_event):
                self.calls += 1
                raise RuntimeError('secret-credential')
        host = BrokenHost()
        registry = HostRegistry()
        self.register(host, registry)
        result = registry.execute(self.request)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('secret-credential', str(result))
        self.assertEqual(registry.execute(self.request), result)
        self.assertEqual(host.calls, 1)

    def test_live_provenance_only_from_registry(self):
        registry = HostRegistry()
        self.register(self.host, registry, verification='live')
        self.assertEqual(registry.execute(self.request)['verification'], 'live')

    def test_failed_candidate_empty(self):
        self.host.status = 'failed'
        result = self.registry.execute(self.request)
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['candidate'])


if __name__ == '__main__':
    unittest.main()
