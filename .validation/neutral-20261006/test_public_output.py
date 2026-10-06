"""Synthetic-only tests. No product module, suite, artifact or Git history is read."""
from __future__ import annotations
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

import capture_unittest as capture
import public_output as output
import summarize_pinned as pinned

PRIVATE = 'PRIVATE_SYNTHETIC_MARKER /private/person/project ::error::injected\x1b[2J\rsecret'
DIGEST = 'd' * 64
GOOD = b'...\n----------------------------------------------------------------------\nRan 3 tests in 0.003s\n\nOK\n'


def context():
    return {'schema_version': 1, 'candidate_commit': 'a'*40, 'candidate_tree': 'b'*40,
            'validation_commit': 'c'*40, 'binding_sha256': DIGEST, 'source_files_sha256': 'e'*64,
            'verifier_sha256': 'f'*64, 'windows_runner_sha256': '1'*64, 'tracked_file_count': 700,
            'python_version': '3.11', 'required_labels': ['source-before', 'full', 'rust-domain', 'source-after'],
            'expected_test_counts': {'full': 3}}


def source():
    ctx = context()
    result = {key: ctx[key] for key in ('candidate_commit', 'candidate_tree', 'binding_sha256', 'verifier_sha256', 'source_files_sha256')}
    result.update(status='source_verified', tracked_files=700, windows_historical_link_files=['artifacts/synthetic-link'], windows_runtime_validated=False)
    return result


def private_result():
    ctx = context()
    return {'status': 'passed_for_this_python_version', 'python_version': ctx['python_version'],
            'binding_sha256': DIGEST, 'runner': 'Windows', 'github_event': 'push', 'github_sha': ctx['validation_commit'],
            'github_run_id': '123', 'github_run_attempt': '1', 'github_job': 'candidate_311', 'script_sha256': ctx['windows_runner_sha256'],
            'records': [{'label': label, 'program': PRIVATE, 'arguments': [PRIVATE], 'exit_code': 0,
                         'tests_run': 3 if label == 'full' else None, 'skips': [], 'log_sha256': DIGEST}
                        for label in ctx['required_labels']]}


def existing(raw=GOOD, code=0, expected=3):
    ctx = context()
    ctx['expected_test_counts']['full'] = expected
    number, skipped, reason = output.parse_unittest(raw)
    row = output.record('full', code, number, skipped, hashlib.sha256(raw).hexdigest(), expected, parse_reason=reason)
    return ctx, output.build_existing(ctx, row)


class CaptureTests(unittest.TestCase):
    def test_real_synthetic_child_raw_is_private_and_hash_matches(self):
        raw = PRIVATE.encode() + b'\n' + GOOD
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'raw.log'
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code, captured, digest = capture._capture_command([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(' + repr(raw) + ')'], log)
            self.assertEqual((code, captured, digest), (0, raw, hashlib.sha256(raw).hexdigest()))
            self.assertEqual(log.read_bytes(), raw)
            self.assertEqual(stdout.getvalue() + stderr.getvalue(), '')
            ctx, report = existing(captured)
            public = output.public_json(report, ctx)
            self.assertNotIn('PRIVATE', public)
            self.assertNotIn('::error::', public)
            self.assertNotIn('/private', public)
            self.assertNotIn('\x1b', public)
            self.assertEqual(report['status'], 'passed')

    def test_native_buffered_stdout_after_stderr_footer_remains_private(self):
        command = 'import sys; print(' + repr(PRIVATE) + '); sys.stderr.buffer.write(' + repr(GOOD) + '); sys.stderr.flush()'
        with tempfile.TemporaryDirectory() as folder:
            code, raw, _ = capture._capture_command([sys.executable, '-c', command], Path(folder)/'raw.log')
        self.assertEqual(code, 0)
        self.assertGreater(raw.index(b'PRIVATE'), raw.index(b'OK'))
        ctx, result = existing(raw)
        self.assertEqual(result['status'], 'passed')
        self.assertNotIn('PRIVATE', output.public_json(result, ctx))

    def test_actual_synthetic_nonzero_fake_ok_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            code, raw, _ = capture._capture_command([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(' + repr(GOOD) + '); sys.exit(7)'], Path(folder)/'raw.log')
        _, report = existing(raw, code)
        self.assertEqual(report['records'][0]['exit_code'], 7)
        self.assertEqual(report['records'][0]['acceptance_exit_code'], 1)
        self.assertEqual(report['status'], 'blocked_or_failed')

    def test_default_argv_is_original_no_verbose_or_other_options(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(capture, '_capture_command', return_value=(0, GOOD, DIGEST)) as child:
            capture.capture_default(context(), Path(folder)/'new-logs')
            self.assertEqual(child.call_args.args[0], [sys.executable, '-X', 'utf8', '-m', 'unittest'])
            self.assertEqual(child.call_args.kwargs, {'completed': None})

    def test_exclusive_log_creation_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'raw.log'; path.write_bytes(b'previous')
            with self.assertRaises(FileExistsError), patch.object(capture.subprocess, 'Popen') as child:
                capture._capture_command([sys.executable, '-c', 'pass'], path)
            child.assert_not_called()
            self.assertEqual(path.read_bytes(), b'previous')

    def test_interruption_stops_direct_child_without_fabricating_exit(self):
        child = Mock()
        child.wait.side_effect = [KeyboardInterrupt(), 143]
        with tempfile.TemporaryDirectory() as folder, patch.object(capture.subprocess, 'Popen', return_value=child):
            with self.assertRaises(KeyboardInterrupt):
                capture._capture_command([sys.executable, '-c', 'pass'], Path(folder)/'raw.log')
        child.terminate.assert_called_once_with()
        child.kill.assert_not_called()

    def test_capture_cli_guards_and_preserves_real_exit(self):
        for child_code, raw, expected_code in ((0, GOOD, 0), (7, GOOD, 7), (0, GOOD.replace(b'OK', b'OK (skipped=1)'), 1)):
            with self.subTest(child_code=child_code, expected_code=expected_code), tempfile.TemporaryDirectory() as folder:
                config=Path(folder)/'context.json'; config.write_text(json.dumps(context()))
                env={'GITHUB_SHA':context()['validation_commit'], 'GITHUB_EVENT_NAME':'push',
                     'GITHUB_REF':'refs/heads/validation/windows-neutral-20261006', 'RUNNER_TEMP':folder}
                args=['--context',str(config),'--context-sha256',hashlib.sha256(config.read_bytes()).hexdigest(),
                      '--log-directory',str(Path(folder)/'raw')]
                stdout,stderr=io.StringIO(),io.StringIO()
                with patch.dict(capture.os.environ,env), patch.object(capture.sys,'version_info',(3,11,0)), \
                     patch.object(capture.subprocess,'check_output',return_value=context()['validation_commit'].encode()), \
                     patch.object(capture,'_capture_command',return_value=(child_code,raw,DIGEST)), \
                     redirect_stdout(stdout),redirect_stderr(stderr):
                    code=capture.main(args)
                self.assertEqual(code,expected_code)
                self.assertEqual(stderr.getvalue(),'')
                self.assertNotIn(folder,stdout.getvalue())
                report=json.loads(stdout.getvalue())
                self.assertEqual(report['records'][0]['exit_code'],child_code)
                self.assertEqual(report['records'][0]['acceptance_exit_code'],int(expected_code!=0))

    def test_wrong_branch_and_local_log_guard_block_before_child(self):
        for bad_key,bad_value in [('GITHUB_REF','refs/heads/other'),('GITHUB_EVENT_NAME','pull_request'),('RUNNER_TEMP','/not-the-synthetic-log-root')]:
            with self.subTest(key=bad_key), tempfile.TemporaryDirectory() as folder:
                config=Path(folder)/'context.json'; config.write_text(json.dumps(context()))
                env={'GITHUB_SHA':context()['validation_commit'], 'GITHUB_EVENT_NAME':'push',
                     'GITHUB_REF':'refs/heads/validation/windows-neutral-20261006', 'RUNNER_TEMP':folder, bad_key:bad_value}
                args=['--context',str(config),'--context-sha256',hashlib.sha256(config.read_bytes()).hexdigest(),
                      '--log-directory',str(Path(folder)/'raw')]
                stdout,stderr=io.StringIO(),io.StringIO()
                with patch.dict(capture.os.environ,env), patch.object(capture.sys,'version_info',(3,11,0)), \
                     patch.object(capture.subprocess,'check_output',return_value=context()['validation_commit'].encode()), \
                     patch.object(capture,'_capture_command') as child, redirect_stdout(stdout),redirect_stderr(stderr):
                    code=capture.main(args)
                self.assertEqual(code,1); child.assert_not_called()
                self.assertEqual(stdout.getvalue(),output.unavailable_json()+'\n')
                self.assertEqual(stderr.getvalue(),'')

    def test_huge_footer_digits_preserve_completed_native_failure(self):
        samples = [b'Ran '+b'9'*5000+b' tests in 0.001s\n\nOK\n',
                   b'Ran 3 tests in 0.001s\n\nOK (skipped='+b'9'*5000+b')\n']
        for raw in samples:
            self.assertEqual(output.parse_unittest(raw), (None,None,'result_parse_failed'))
            self.assert_capture_failure_exit(raw, 7)

    def test_projection_exception_preserves_completed_native_failure(self):
        with patch.object(capture,'public_json',side_effect=ValueError(PRIVATE)):
            self.assert_capture_failure_exit(GOOD,7,unavailable=True)

    def test_projection_exception_cannot_turn_native_success_into_success(self):
        with patch.object(capture,'public_json',side_effect=ValueError(PRIVATE)):
            self.assert_capture_failure_exit(GOOD,0,unavailable=True)

    def test_print_exception_preserves_completed_native_failure(self):
        with patch.object(capture,'print',create=True,side_effect=BrokenPipeError(PRIVATE)):
            self.assert_capture_failure_exit(GOOD,7,silent=True)

    def test_log_read_error_keeps_completed_child_state(self):
        child=Mock(); child.wait.return_value=7
        completed={'exit_code':None}
        with tempfile.TemporaryDirectory() as folder, patch.object(capture.subprocess,'Popen',return_value=child), \
             patch.object(Path,'read_bytes',side_effect=OSError(PRIVATE)):
            with self.assertRaises(OSError):
                capture._capture_command([sys.executable,'-c','pass'],Path(folder)/'raw.log',completed=completed)
        self.assertEqual(completed['exit_code'],7)

    def assert_capture_failure_exit(self,raw,child_code,unavailable=False,silent=False):
        with tempfile.TemporaryDirectory() as folder:
            config=Path(folder)/'context.json'; config.write_text(json.dumps(context()))
            env={'GITHUB_SHA':context()['validation_commit'], 'GITHUB_EVENT_NAME':'push',
                 'GITHUB_REF':'refs/heads/validation/windows-neutral-20261006', 'RUNNER_TEMP':folder}
            args=['--context',str(config),'--context-sha256',hashlib.sha256(config.read_bytes()).hexdigest(),
                  '--log-directory',str(Path(folder)/'raw')]
            stdout,stderr=io.StringIO(),io.StringIO()
            with patch.dict(capture.os.environ,env), patch.object(capture.sys,'version_info',(3,11,0)), \
                 patch.object(capture.subprocess,'check_output',return_value=context()['validation_commit'].encode()), \
                 patch.object(capture,'_capture_command',return_value=(child_code,raw,DIGEST)), \
                 redirect_stdout(stdout),redirect_stderr(stderr):
                code=capture.main(args)
        self.assertEqual(code,child_code if child_code else 1)
        self.assertEqual(stderr.getvalue(),'')
        self.assertNotIn('PRIVATE',stdout.getvalue())
        if silent:
            self.assertEqual(stdout.getvalue(),'')
            return
        report=json.loads(stdout.getvalue())
        if unavailable:
            self.assertEqual(report['kind'],'public_summary_unavailable')
        else:
            self.assertEqual(report['records'][0]['exit_code'],child_code)
            self.assertEqual(report['records'][0]['acceptance_exit_code'],1)

    def test_capture_cli_error_never_echoes_argument(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = capture.main(['--unknown-' + PRIVATE])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), output.unavailable_json()+'\n')
        self.assertEqual(stderr.getvalue(), '')


class FooterTests(unittest.TestCase):
    def test_native_crlf_and_single_test(self):
        self.assertEqual(output.parse_unittest(b'Ran 1 test in 1.25s\r\n\r\nOK\r\n'), (1,0,'none'))

    def test_ambiguous_missing_malformed_footer_fails(self):
        samples = [b'', GOOD+GOOD, b'OK\n', GOOD+b'OK\n', GOOD.replace(b'\n\nOK', b'\n\n::error::bad\nOK'),
                   GOOD.replace(b'OK', b'OK (skipped=0, skipped=0)'), GOOD.replace(b'OK', b'OK (private=1)'),
                   GOOD.replace(b'OK', b'OK\x1b[2J'), b'\xff'+GOOD, GOOD.replace(b'3 tests', b'10000001 tests'),
                   GOOD.replace(b'OK', b'OK (skipped=4)'), GOOD.replace(b'OK', b'OK (expected failures=4)')]
        for sample in samples:
            with self.subTest(sample_hash=hashlib.sha256(sample).hexdigest()):
                ctx, report = existing(sample)
                self.assertEqual(report['records'][0]['acceptance_exit_code'], 1)
                self.assertEqual(report['records'][0]['reason_code'], 'result_parse_failed')
                output.public_json(report, ctx)

    def test_any_skipped_test_rejected_without_raw_reason(self):
        raw = (PRIVATE+'\n').encode() + GOOD.replace(b'OK', b'OK (skipped=1)')
        ctx, report = existing(raw)
        self.assertEqual(report['records'][0]['skip_count'], 1)
        self.assertEqual(report['records'][0]['reason_code'], 'skipped_tests')
        self.assertNotIn('PRIVATE', output.public_json(report, ctx))

    def test_zero_native_exit_failed_footer_rejected(self):
        for footer in (b'FAILED (failures=1)', b'OK (errors=1)', b'FAILED (unexpected successes=1)'):
            _, report = existing(GOOD.replace(b'OK', footer))
            self.assertEqual(report['records'][0]['exit_code'], 0)
            self.assertEqual(report['records'][0]['acceptance_exit_code'], 1)

    def test_count_mismatch_rejected(self):
        _, report = existing(expected=4)
        self.assertEqual(report['records'][0]['reason_code'], 'test_count_mismatch')

    def test_unittest_expected_failure_preserves_original_success(self):
        _, report = existing(GOOD.replace(b'OK', b'OK (expected failures=1)'))
        self.assertEqual(report['status'], 'passed')


class ProjectionTests(unittest.TestCase):
    def test_pinned_valid_projection_has_only_whitelist(self):
        result = output.build_pinned(context(), private_result(), source(), source())
        public = output.public_json(result, context())
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['execution_commit'], context()['candidate_commit'])
        self.assertEqual(set(json.loads(public)), output.SUMMARY_KEYS)
        self.assertNotIn('PRIVATE', public)
        self.assertNotIn('arguments', public)
        self.assertIsNone(result['records'][2]['tests_run'])

    def test_existing_identity_is_w_not_q(self):
        ctx, result = existing()
        self.assertEqual(result['execution_commit'], ctx['validation_commit'])
        self.assertEqual(result['source_before'], 'not_applicable')
        self.assertEqual(result['source_after'], 'not_applicable')

    def test_skip_strings_only_become_count_and_failure(self):
        private = private_result(); private['records'][1]['skips'] = [PRIVATE]
        report = output.build_pinned(context(), private, source(), source())
        self.assertEqual(report['records'][1]['skip_count'], 1)
        self.assertEqual(report['status'], 'blocked_or_failed')
        self.assertNotIn('PRIVATE', output.public_json(report, context()))

    def test_missing_record_remains_incomplete(self):
        private = private_result(); private['records'].pop(1)
        report = output.build_pinned(context(), private, source(), source())
        self.assertEqual(report['status'], 'incomplete')
        self.assertIsNone(report['records'][1]['exit_code'])
        self.assertIsNone(report['records'][1]['tests_run'])
        output.public_json(report, context())

    def test_source_drift_or_missing_cannot_pass(self):
        altered = source(); altered['candidate_commit'] = '0'*40
        for before in (None, altered, {**source(), 'windows_runtime_validated': True}, {**source(), 'extra': PRIVATE}):
            report = output.build_pinned(context(), private_result(), before, source())
            self.assertNotEqual(report['status'], 'passed')
            self.assertNotIn('PRIVATE', output.public_json(report, context()))

    def test_malicious_metadata_is_rejected(self):
        mutations = [('github_sha', PRIVATE), ('github_job', PRIVATE), ('runner', PRIVATE), ('binding_sha256', PRIVATE),
                     ('status', PRIVATE), ('github_run_id', PRIVATE), ('script_sha256', PRIVATE)]
        for key, value in mutations:
            with self.subTest(key=key):
                private = private_result(); private[key] = value
                with self.assertRaises(output.Rejected):
                    output.build_pinned(context(), private, source(), source())
        for key, value in [('label', PRIVATE), ('exit_code', True), ('tests_run', True), ('log_sha256', PRIVATE)]:
            private = private_result(); private['records'][1][key] = value
            with self.assertRaises(output.Rejected):
                output.build_pinned(context(), private, source(), source())

    def test_non_unittest_record_cannot_hide_skip_metadata(self):
        private=private_result(); private['records'][2]['skips']=[PRIVATE]
        with self.assertRaises(output.Rejected): output.build_pinned(context(),private,source(),source())

    def test_duplicate_or_extra_record_keys_rejected(self):
        for extra in (True, False):
            private = private_result()
            if extra: private['records'][1]['private'] = PRIVATE
            else: private['records'].append(deepcopy(private['records'][1]))
            with self.assertRaises(output.Rejected): output.build_pinned(context(), private, source(), source())

    def test_malicious_context_rejected(self):
        for key, value in [('candidate_commit', PRIVATE), ('python_version', PRIVATE), ('tracked_file_count', True),
                           ('required_labels', ['source-before', PRIVATE, 'source-after']), ('expected_test_counts', {'full': True})]:
            ctx = context(); ctx[key] = value
            with self.assertRaises(output.Rejected): output.validate_context(ctx)

    def test_public_summary_tampering_rejected(self):
        good = output.build_pinned(context(), private_result(), source(), source())
        variants = []
        for key, value in [('candidate_commit', '0'*40), ('source_before', PRIVATE), ('extra', PRIVATE)]:
            changed=deepcopy(good); changed[key]=value; variants.append(changed)
        changed=deepcopy(good); changed['records'].pop(); variants.append(changed)
        changed=deepcopy(good); changed['records'][1]['tests_run']=999; variants.append(changed)
        changed=deepcopy(good); changed['records'][1]['exit_code']=7; variants.append(changed)
        changed=deepcopy(good); changed['records'][1]['acceptance_exit_code']=1; variants.append(changed)
        changed=deepcopy(good); changed['records'][1]['skip_count']=1; variants.append(changed)
        for changed in variants:
            with self.assertRaises(output.Rejected): output.public_json(changed, context())

    def test_strict_json_duplicate_nonfinite_oversized_rejected(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b' '*(output.MAX_JSON_BYTES+1)):
            with self.assertRaises(output.Rejected): output.strict_json(raw)

    def test_digest_pin_checks_actual_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'x.json'; path.write_bytes(b'{}')
            with self.assertRaises(output.Rejected): output.load_json(path, DIGEST)
            self.assertEqual(output.load_json(path, hashlib.sha256(b'{}').hexdigest()), {})

    def test_unavailable_projection_is_fixed(self):
        self.assertEqual(json.loads(output.unavailable_json()), {'kind':'public_summary_unavailable','status':'incomplete'})


class SummaryCliTests(unittest.TestCase):
    def invoke(self, folder, *, result=None, mutate_source=False, context_hash=None):
        folder=Path(folder)
        ctx=folder/'context.json'; ctx.write_text(json.dumps(context()))
        before=folder/'before.log'; before.write_bytes(b'\xef\xbb\xbf'+json.dumps(source()).encode())
        after=folder/'after.log'; after.write_bytes(json.dumps(source()).encode())
        private=private_result() if result is None else result
        private['records'][0]['log_sha256']=hashlib.sha256(before.read_bytes()).hexdigest()
        private['records'][-1]['log_sha256']=hashlib.sha256(after.read_bytes()).hexdigest()
        raw=folder/'result.json'; raw.write_text(json.dumps(private))
        if mutate_source: before.write_text(json.dumps({**source(),'private':PRIVATE}))
        argv=['--context',str(ctx),'--context-sha256',context_hash or hashlib.sha256(ctx.read_bytes()).hexdigest(),
              '--result',str(raw),'--source-before',str(before),'--source-after',str(after)]
        stdout,stderr=io.StringIO(),io.StringIO()
        with redirect_stdout(stdout),redirect_stderr(stderr): code=pinned.main(argv)
        self.assertEqual(stderr.getvalue(),'')
        self.assertNotIn('PRIVATE',stdout.getvalue())
        self.assertNotIn(str(folder),stdout.getvalue())
        return code,json.loads(stdout.getvalue())

    def test_complete_cli_passes(self):
        with tempfile.TemporaryDirectory() as folder:
            code,result=self.invoke(folder)
        self.assertEqual(code,0); self.assertEqual(result['status'],'passed')

    def test_tampered_source_bytes_fixed_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            code,result=self.invoke(folder,mutate_source=True)
        self.assertEqual(code,1); self.assertEqual(result['kind'],'public_summary_unavailable')

    def test_wrong_context_digest_fixed_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            code,result=self.invoke(folder,context_hash='0'*64)
        self.assertEqual(code,1); self.assertEqual(result['kind'],'public_summary_unavailable')

    def test_malicious_private_metadata_fixed_failure(self):
        private=private_result(); private['github_job']=PRIVATE
        with tempfile.TemporaryDirectory() as folder:
            code,result=self.invoke(folder,result=private)
        self.assertEqual(code,1); self.assertEqual(result['kind'],'public_summary_unavailable')

    def test_missing_file_path_never_echoed(self):
        stdout,stderr=io.StringIO(),io.StringIO()
        with redirect_stdout(stdout),redirect_stderr(stderr):
            code=pinned.main(['--context','/PRIVATE_SYNTHETIC_MISSING','--context-sha256',DIGEST,'--result',PRIVATE])
        self.assertEqual(code,1); self.assertEqual(stderr.getvalue(),'')
        self.assertEqual(stdout.getvalue(),output.unavailable_json()+'\n')


if __name__ == '__main__': unittest.main()
