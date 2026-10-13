"""Read, review and apply bound Host candidates through a trusted review registry.

Digest consistency proves binding to the local records, not independent review.
A file digest alone never authorizes application.
"""
from __future__ import annotations

import json
import re
import sqlite3

from .host import candidate_digest, request_digest, request_identity, validate_request
from .planning import validate_task_plan
from .safety import SCHEMA_VERSION, canonical_json, safe_target, sha256_bytes
from .scheduler import SchedulerSnapshotError, _explicit_root, _readonly_snapshot_connection, _receipt_digest, action_digest
from .sessions import session_write_lock
from .transactions import preview_changes


class _Rejected(Exception):
    pass


def _require(condition, code='candidate_evidence_invalid'):
    if not condition:
        raise _Rejected(code)


def _same(left, right):
    return canonical_json(left) == canonical_json(right)


def _task_id(plan, digest, original):
    return 'host-' + sha256_bytes(canonical_json([plan['session_id'], digest, original]))


def _read_candidates(root, digest, connection):
    from .host_workflow import verify_binding

    records = []
    for row in connection.execute('SELECT * FROM tasks'):
        action = json.loads(row['action_json'])
        _require(type(action) is dict)
        if action.get('type') == 'host_task' and action.get('plan_digest') == digest:
            records.append(dict(row))
    _require(records, 'host_plan_not_found')
    first = json.loads(records[0]['input_json'])
    plan = first['plan']
    _require(validate_task_plan(plan)['valid'] and not plan['unresolved']
             and sha256_bytes(canonical_json(plan)) == digest, 'host_plan_binding_mismatch')
    binding = first.get('production_binding')
    _require(type(binding) is dict, 'production_binding_required')
    try:
        current = verify_binding(root, binding)
    except ValueError:
        raise _Rejected('production_binding_stale') from None
    _require(_same(current, binding), 'production_binding_stale')
    _require(binding['root'] == str(root) and binding['plan_digest'] == digest
             and binding['session_id'] == plan['session_id']
             and type(binding['revision']) is int and binding['revision'] == plan['revision'],
             'production_binding_mismatch')
    source = first['source_inputs']
    _require(type(source) is dict and all(type(k) is str and type(v) is str for k, v in source.items()))
    _require(sha256_bytes(canonical_json(source)) == binding['source_inputs_digest'],
             'production_input_mismatch')
    _require(set(binding['input_preimages']) == set(source), 'production_input_mismatch')
    for path, content in source.items():
        _require(binding['input_preimages'][path]['sha256'] == sha256_bytes(content.encode('utf-8')),
                 'production_input_mismatch')
    by_original = {item['id']: item for item in plan['tasks']}
    expected_ids = {_task_id(plan, digest, original) for original in by_original}
    _require({row['task_id'] for row in records} == expected_ids, 'host_plan_incomplete')
    rows = {row['task_id']: row for row in records}
    host_id = json.loads(records[0]['action_json'])['host_id']
    budget = first['max_requests']
    _require(type(budget) is int and 1 <= budget <= 10000)
    output_paths = {path for item in plan['tasks'] for path in item['outputs']}
    _require(set(binding['output_preimages']) == output_paths, 'production_output_mismatch')
    _require(not set(source) & output_paths, 'production_input_mismatch')
    allowed_source = {path for item in plan['tasks'] for path in item['inputs']}
    _require(set(source) <= allowed_source, 'production_input_mismatch')
    completed = {}
    files = {}
    receipt_digests = {}
    verification = set()
    used = 0
    for wave in plan['waves']:
        for original in wave:
            item = by_original[original]
            task_id = _task_id(plan, digest, original)
            row = rows[task_id]
            package = json.loads(row['input_json'])
            _require(_same(package['production_binding'], binding)
                     and _same(package['plan'], plan) and _same(package['source_inputs'], source)
                     and package['task_id'] == original and type(package['max_requests']) is int
                     and package['max_requests'] == budget
                     and package['delivery_gate'] == 'pending_independent_review')
            _require(row['state'] == 'succeeded' and not row['cancel_requested']
                     and row['runner_pid'] is None, 'host_candidates_incomplete')
            _require(not item['activation_gate']['required'])
            expected_dependencies = [_task_id(plan, digest, dep) for dep in item['depends_on']]
            stored_dependencies = [r['depends_on'] for r in connection.execute(
                'SELECT depends_on FROM task_dependencies WHERE task_id=?', (task_id,))]
            _require(json.loads(row['depends_json']) == expected_dependencies
                     and set(stored_dependencies) == set(expected_dependencies)
                     and row['idempotency_key'] == task_id
                     and row['max_attempts'] == min(item['retry']['max_attempts'], 100))
            action = {'type': 'host_task', 'host_id': host_id, 'session_id': plan['session_id'],
                      'plan_digest': digest, 'outputs': item['outputs']}
            _require(_same(json.loads(row['action_json']), action))
            runs = list(connection.execute('SELECT * FROM runs WHERE task_id=? ORDER BY attempt', (task_id,)))
            used += len(runs)
            _require(used <= budget and len(runs) == row['attempt']
                     and [r['attempt'] for r in runs] == list(range(1, row['attempt'] + 1))
                     and 1 <= row['attempt'] <= row['max_attempts'])
            run = runs[-1]
            _require(run['run_id'] == row['last_run_id'] and run['task_id'] == task_id
                     and run['status'] == 'succeeded' and run['finished_at']
                     and not run['timed_out'] and not run['cancelled'])
            receipt = json.loads(run['receipt_json'])
            input_digest = sha256_bytes(canonical_json(package))
            expected_action_digest = action_digest(action)
            _require(run['input_sha256'] == input_digest and run['action_digest'] == expected_action_digest
                     and run['receipt_digest'] == receipt['receipt_digest'] == _receipt_digest(receipt))
            _require(receipt['schema_version'] == SCHEMA_VERSION
                     and receipt['task_id'] == task_id and receipt['run_id'] == run['run_id']
                     and type(receipt['attempt']) is int and receipt['attempt'] == row['attempt']
                     and receipt['input_sha256'] == input_digest
                     and receipt['action_digest'] == expected_action_digest
                     and receipt['status'] == 'succeeded' and receipt['cancelled'] is False
                     and receipt['timed_out'] is False
                     and receipt['artifacts'] == json.loads(run['artifacts_json']) == []
                     and receipt['delivery_gate'] == 'pending_independent_review'
                     and receipt['execution_mode'] == 'production'
                     and _same(receipt['production_binding'], binding)
                     and receipt['validation']['status'] == 'pending'
                     and receipt['validation']['acceptance'] == item['acceptance']
                     and receipt['rollback']['status'] == 'not_applied'
                     and receipt['rollback']['instruction'] == item['rollback'])
            # Reconstruct the exact request using already checked ancestor receipts.
            ancestors = set()
            pending = list(item['depends_on'])
            while pending:
                ancestor = pending.pop()
                if ancestor not in ancestors:
                    ancestors.add(ancestor)
                    pending.extend(by_original[ancestor]['depends_on'])
            inputs = dict(source)
            dependencies = []
            for prior_wave in plan['waves']:
                for ancestor in prior_wave:
                    if ancestor not in ancestors:
                        continue
                    prior = completed[ancestor]
                    inputs.update(prior['host_result']['candidate']['files'])
                    dependencies.append({'task_id': prior['task_id'], 'receipt_digest': prior['receipt_digest'],
                                         'candidate_digest': prior['host_result']['candidate']['digest']})
            previous_outputs = {}
            previous_bytes = 0
            for path in item['outputs']:
                target = safe_target(root, path)
                raw = None
                if target.exists():
                    limit = 2 * 1024 * 1024 - previous_bytes
                    _require(target.stat().st_size <= limit, 'candidate_preimage_oversized')
                    with target.open('rb') as stream:
                        raw = stream.read(limit + 1)
                    _require(len(raw) <= limit, 'candidate_preimage_oversized')
                    previous_bytes += len(raw)
                _require((sha256_bytes(raw) if raw is not None else None) == binding['output_preimages'][path]['sha256'],
                         'candidate_preimage_drift')
                if raw is not None:
                    previous_outputs[path] = raw.decode('utf-8')
            _require(not (set(item['inputs']) - set(inputs) - set(item['outputs'])),
                     'production_input_mismatch')
            request = {'schema_version': '1.0', 'host_id': host_id, 'root': str(root),
                       'session_id': plan['session_id'], 'plan_digest': digest, 'task_id': task_id,
                       'run_id': run['run_id'], 'attempt': row['attempt'], 'outputs': item['outputs'],
                       'timeout_seconds': row['timeout_seconds'],
                       'input': {'task': item, 'files': {p: inputs[p] for p in item['inputs'] if p in inputs},
                                 'dependencies': dependencies, 'max_requests': budget,
                                 'new_outputs': [p for p in item['inputs'] if p in item['outputs']
                                                 and p not in inputs and p not in previous_outputs],
                                 'production_binding': binding, 'previous_outputs': previous_outputs}}
            validate_request(request)
            evidence = receipt['host_result']
            _require(evidence['schema_version'] == '1.0' and evidence['status'] == 'succeeded'
                     and evidence['error'] is None
                     and evidence['request_digest'] == request_digest(request)
                     and _same(evidence['identity'], request_identity(request)))
            candidate = evidence['candidate']
            _require(type(candidate) is dict and set(candidate) == {'files', 'digest'}
                     and type(candidate['files']) is dict and set(candidate['files']) == set(item['outputs'])
                     and all(type(value) is str for value in candidate['files'].values())
                     and candidate['digest'] == candidate_digest(candidate['files']))
            _require(evidence['verification'] in ('simulated', 'live')
                     and type(evidence['evidence']) is str and evidence['evidence'].strip())
            verification.add(evidence['verification'])
            completed[original] = receipt
            files.update(candidate['files'])
            receipt_digests[run['run_id']] = receipt['receipt_digest']
    return binding, files, receipt_digests, sorted(verification)


def _review_context(connection, digest):
    for row in connection.execute('SELECT action_json,input_json FROM tasks'):
        action = json.loads(row['action_json'])
        if action.get('type') == 'host_task' and action.get('plan_digest') == digest:
            plan = json.loads(row['input_json'])['plan']
            return {'generator_id': action['host_id'],
                    'acceptance': {item['id']: item['acceptance'] for item in plan['tasks']}}
    raise _Rejected('host_plan_not_found')


def _preview_locked(root, plan_digest):
    with _readonly_snapshot_connection(root) as connection:
        _require(connection is not None, 'host_plan_not_found')
        binding, files, receipts, verification = _read_candidates(root, plan_digest, connection)
        file_plan = preview_changes(root, files)
        for entry in file_plan['entries']:
            _require(entry['before_sha256'] == binding['output_preimages'][entry['path']]['sha256'],
                     'candidate_preimage_drift')
        return {'status': 'preview_ready', 'plan_digest': plan_digest,
                'candidate_digest': candidate_digest(files), 'run_receipt_digests': receipts,
                'binding': binding, 'file_plan': file_plan, 'verification': verification,
                'delivery_gate': 'pending_independent_review',
                'review_context': _review_context(connection, plan_digest)}


def preview_host_candidates(root, plan_digest):
    """Preview only a complete, current, production-bound candidate set."""
    try:
        _require(type(plan_digest) is str and re.fullmatch(r'[a-f0-9]{64}', plan_digest),
                 'invalid_plan_digest')
        root = _explicit_root(root)
        with session_write_lock(root):
            return _preview_locked(root, plan_digest)
    except _Rejected as error:
        reason = str(error)
    except SchedulerSnapshotError as error:
        reason = error.code
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, sqlite3.Error):
        reason = 'candidate_evidence_invalid'
    return {'status': 'blocked', 'reason': reason, 'delivery_gate': 'pending_independent_review'}


def apply_host_candidates(root, plan_digest, *, approved_digest=None, review_registry=None, grant=None):
    """Apply only an unchanged candidate authorized by a trusted review registry."""
    from .host_review import ReviewRegistry
    if not isinstance(review_registry, ReviewRegistry) or grant is None:
        return {'status': 'blocked', 'reason': 'missing_independent_validation_review_gate',
                'delivery_gate': 'pending_independent_review'}
    return review_registry.apply(root, plan_digest, grant, approved_digest=approved_digest)
