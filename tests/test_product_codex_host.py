"""Offline subprocess fixtures; these tests are not live Codex evidence."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from opencoding.codex_host import CodexCLIHost


FIXTURE = r'''
import json, os, pathlib, sys, time
mode = sys.argv[1]
schema = json.loads(pathlib.Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
props = schema['properties']
request = json.loads(sys.stdin.read().split('context:\n',1)[1])
identity = {key: request[key] for key in props['identity']['properties']}
assert props['identity']['properties']['root'] == {'type': 'string'}
assert 'enum' not in props['identity']['properties']['outputs']
result = {'status': 'succeeded', 'request_digest': props['request_digest']['enum'][0],
          'identity': identity, 'files': {name: 'candidate' for name in identity['outputs']}}
assert sys.argv[sys.argv.index('--sandbox') + 1] == 'read-only'
assert '--ignore-user-config' in sys.argv and '--ignore-rules' in sys.argv
assert '--ephemeral' in sys.argv
assert pathlib.Path.cwd() != pathlib.Path(identity['root'])
assert pathlib.Path(os.environ['CODEX_HOME']).parent == pathlib.Path.cwd()
assert 'AUTH_TOKEN' not in os.environ
if mode.startswith('provider'):
    options = dict(sys.argv[i+1].split('=',1) for i,v in enumerate(sys.argv) if v == '-c')
    options = {key: json.loads(value) for key,value in options.items()}
    assert options['model_provider'] == 'opencoding_relay'
    assert options['model'] == 'relay-fixture-model'
    assert options['model_providers.opencoding_relay.base_url'] == 'https://relay.unit.test/v1'
    assert options['model_providers.opencoding_relay.wire_api'] == 'responses'
    secret = os.environ[options['model_providers.opencoding_relay.env_key']]
    assert 'CODEX_API_KEY' not in os.environ
    assert secret not in ' '.join(sys.argv)
    for path in pathlib.Path.cwd().rglob('*'):
        if path.is_file(): assert secret.encode() not in path.read_bytes()
    if mode == 'provider-error':
        print(secret, file=sys.stderr)
        sys.exit(1)
    if mode == 'provider-leak':
        result['files'] = {name: secret for name in identity['outputs']}
if mode == 'delay': time.sleep(20)
if mode == 'oversize': print('x' * 10000); sys.exit(0)
if mode == 'stderr': print('secret diagnostics', file=sys.stderr); sys.exit(1)
if mode == 'wrong-root': result['identity']['root'] = 'C:\\\\U'
if mode == 'wrong-id': result['identity']['run_id'] = 'wrong'
if mode == 'wrong-files': result['files'] = {'../outside': 'bad'}
if mode == 'wrong-type': result['identity']['attempt'] = True
if mode == 'not-json': print('done'); sys.exit(0)
if mode == 'duplicate': print('{"status":"failed",' + json.dumps(result)[1:]); sys.exit(0)
print(json.dumps(result))
'''


class CodexHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = self.root / 'fake_codex.py'
        self.fixture.write_text(FIXTURE, encoding='utf-8')
        self.request = {'schema_version': '1.0', 'host_id': 'codex-cli',
                        'root': str(self.root), 'session_id': 's1',
                        'plan_digest': 'a' * 64, 'task_id': 'task1', 'run_id': 'run1',
                        'attempt': 1, 'outputs': ['candidate.txt'],
                        'input': {'goal': 'fixture'}, 'timeout_seconds': 4}

    def execute(self, mode='ok', **kwargs):
        return CodexCLIHost([sys.executable, str(self.fixture), mode], **kwargs).execute(
            self.request, threading.Event())

    def test_candidate_only_and_disposable_environment(self):
        result = self.execute()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['files'], {'candidate.txt': 'candidate'})
        self.assertFalse((self.root / 'candidate.txt').exists())
        self.assertFalse(CodexCLIHost.live_verified)

    def test_untrusted_output_fails_closed(self):
        for mode in ('wrong-root', 'wrong-id', 'wrong-files', 'wrong-type', 'not-json', 'duplicate', 'stderr'):
            with self.subTest(mode=mode):
                result = self.execute(mode)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['files'], {})
                self.assertNotIn('secret', json.dumps(result))

    def test_failure_diagnostics_are_coarse_and_nonsecret(self):
        host = self.provider('provider-error')
        result = host.execute(self.request, threading.Event())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(host.last_diagnostics, {'code': 'process_failed', 'exit_code': 1})
        self.assertNotIn('fixture-secret-817262', json.dumps(host.last_diagnostics))

    def test_timeout_diagnostic_does_not_change_strict_response(self):
        self.request['timeout_seconds'] = .3
        host = CodexCLIHost([sys.executable, str(self.fixture), 'delay'])
        result = host.execute(self.request, threading.Event())
        self.assertEqual(result['status'], 'timed_out')
        self.assertEqual(host.last_diagnostics, {'code': 'timed_out'})

    def test_output_limit(self):
        self.assertEqual(self.execute('oversize', max_output_bytes=1000)['status'], 'failed')

    def test_timeout_stops_subprocess(self):
        self.request['timeout_seconds'] = .3
        start = time.monotonic()
        result = self.execute('delay')
        self.assertEqual(result['status'], 'timed_out')
        self.assertLess(time.monotonic() - start, 8)
        self.assertEqual(result['files'], {})

    def test_cancel_running_process(self):
        event = threading.Event()
        timer = threading.Timer(.3, event.set)
        timer.start()
        self.addCleanup(timer.cancel)
        host = CodexCLIHost([sys.executable, str(self.fixture), 'delay'])
        self.assertEqual(host.execute(self.request, event)['status'], 'cancelled')

    def test_cancel_before_launch(self):
        event = threading.Event()
        event.set()
        result = CodexCLIHost(['missing-executable']).execute(self.request, event)
        self.assertEqual(result['status'], 'cancelled')

    def test_missing_cli(self):
        result = CodexCLIHost(['missing-opencoding-codex-executable']).execute(self.request, threading.Event())
        self.assertEqual(result['status'], 'failed')

    def provider(self, mode='provider', **settings):
        config = {'schema_version': 1, 'provider': 'openai_compatible',
                  'base_url': 'https://relay.unit.test/v1', 'model': 'relay-fixture-model',
                  'api_key': 'fixture-secret-817262'}
        config.update(settings)
        return CodexCLIHost.from_provider_config(config, command=[sys.executable, str(self.fixture), mode])

    def test_provider_configuration_and_secret_only_environment(self):
        with mock.patch.dict('os.environ', {'CODEX_API_KEY': 'unrelated-default-secret'}):
            result = self.provider().execute(self.request, threading.Event())
        self.assertEqual(result['status'], 'succeeded')
        self.assertNotIn('fixture-secret-817262', json.dumps(result))
        self.assertFalse(CodexCLIHost.live_verified)

    def test_provider_error_and_candidate_leak_are_redacted(self):
        for mode in ('provider-error', 'provider-leak'):
            result = self.provider(mode).execute(self.request, threading.Event())
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['files'], {})
            self.assertNotIn('fixture-secret-817262', json.dumps(result))

    def test_escaped_secret_in_response_is_rejected(self):
        for secret in ('fixture"secret-817262', 'fixture\\secret-817262'):
            with self.subTest(secret=secret):
                result = self.provider('provider-leak', api_key=secret).execute(self.request, threading.Event())
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['files'], {})

    def test_secret_in_nested_request_values_or_keys_never_launches(self):
        for secret in ('fixture"secret-817262', 'fixture\\secret-817262'):
            for data in ({'nested': ['prefix' + secret + 'suffix']}, {secret: 'value'}):
                with self.subTest(secret=secret, data=data):
                    self.request['input'] = data
                    host = self.provider(api_key=secret)
                    with mock.patch('opencoding.codex_host.subprocess.Popen') as launch, \
                            mock.patch('opencoding.codex_host.tempfile.TemporaryDirectory') as temporary:
                        result = host.execute(self.request, threading.Event())
                    launch.assert_not_called()
                    temporary.assert_not_called()
                    self.assertEqual(result['status'], 'failed')
                    self.assertEqual(result['files'], {})

    def test_provider_rejects_invalid_or_placeholder_endpoints(self):
        for endpoint in ('https://user:secret@relay.unit.test/v1', 'https://relay.unit.test/v1?key=x',
                         'https://relay.unit.test/v1#fragment', 'http://relay.unit.test/v1',
                         'file:///tmp/model', 'https://example.com/v1', 'https://your-host/v1',
                         'https://relay.unit.test:bad/v1', 'https://relay.unit.test/v1/chat/completions',
                         'https://relay.unit.test/v1/responses', 'https://relay.unit.test/v1?',
                         'https://relay.unit.test/v1#', 'https://relay.unit.test/fixture-secret-817262'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError) as raised:
                self.provider(base_url=endpoint)
            self.assertNotIn(endpoint, str(raised.exception))

    def test_loopback_http_and_https_are_supported_without_probe(self):
        for endpoint in ('http://localhost:8080/v1', 'http://127.0.0.1:8080/v1',
                         'http://[::1]:8080/v1', 'https://relay.unit.test/v1'):
            self.provider(base_url=endpoint)

    def test_percent_encoded_endpoint_credentials_are_rejected(self):
        for secret in ('fixture-secret-817262', 'fixture"secret-817262', 'fixture\\secret-817262'):
            encoded = ''.join('%{:02X}'.format(byte) for byte in secret.encode('utf-8'))
            for _ in range(3):
                with self.subTest(secret=secret, encoded=encoded), self.assertRaises(ValueError):
                    self.provider(api_key=secret, base_url='https://relay.unit.test/prefix/' + encoded + '/v1')
                encoded = encoded.replace('%', '%25')
        # Escapes unrelated to credentials remain valid provider paths.
        self.provider(base_url='https://relay.unit.test/team%20one/v1')

    def test_missing_provider_configuration_does_not_fall_back(self):
        for settings in ({'api_key': None}, {'base_url': None, 'model': None, 'api_key': None},
                         {'api_key': 'your-api-key'}, {'model': '<model>'},
                         {'api_key': 'sk-placeholder'}, {'api_key': 'sk-your-key'},
                         {'api_key': '${API_KEY}'},
                         {'provider': 'workbuddy_gateway'}, {'schema_version': True}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.provider(**settings)


if __name__ == '__main__':
    unittest.main()
