"""Production Host dispatch bound to an explicitly adopted, current task plan.

Capture/verification callers must hold session_write_lock. Filesystem checks
reject links and aliases; they are not a sandbox against same-user processes.
This workflow only queues candidates and never installs project outputs.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from . import service
from .planning import _canonical_path, validate_task_plan
from .safety import canonical_json, safe_target, sha256_bytes
from .sessions import _check_existing_chain, read_session_snapshot, session_write_lock

_SESSION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_BINDING_FIELDS = {'schema_version', 'root', 'session_id', 'revision', 'plan_digest',
                   'input_digest', 'adoption_digest', 'source_inputs_digest',
                   'input_preimages', 'output_preimages'}


def _digest(value):
    return sha256_bytes(canonical_json(value))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate adoption JSON key')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('nonfinite adoption JSON')


def _read_adoption(root, session_id):
    if type(session_id) is not str or not _SESSION_ID.fullmatch(session_id):
        raise ValueError('invalid adoption session identity')
    path = root / '.opencoding' / 'adoptions' / (session_id + '.json')
    _check_existing_chain(path, root=root)
    if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('adopted plan unavailable')
    try:
        record = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object,
                            parse_constant=_reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('adopted plan unreadable') from exc
    if type(record) is not dict:
        raise ValueError('adopted plan must be an object')
    return record


def _file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _file_state(root, plan):
    outputs = sorted({path for task in plan['tasks'] for path in task['outputs']})
    produced = {_canonical_path(path) for path in outputs}
    external = sorted({path for task in plan['tasks'] for path in task['inputs']
                       if _canonical_path(path) not in produced})
    source = {}
    inputs = {}
    total = 0
    for relative in external:
        target = safe_target(root, relative, allow_missing=False)
        if target.stat().st_size + total > _MAX_SOURCE_BYTES:
            raise ValueError('source input size limit exceeded')
        # read_bytes preserves CRLF and BOM; text decoding never normalizes bytes.
        with target.open('rb') as stream:
            raw = stream.read(_MAX_SOURCE_BYTES - total + 1)
        total += len(raw)
        if total > _MAX_SOURCE_BYTES:
            raise ValueError('source input size limit exceeded')
        try:
            source[relative] = raw.decode('utf-8')
        except UnicodeError as exc:
            raise ValueError('source input must be UTF-8 text') from exc
        inputs[relative] = {'sha256': sha256_bytes(raw)}
    preimages = {}
    for relative in outputs:
        target = safe_target(root, relative)
        preimages[relative] = {'sha256': _file_digest(target) if target.exists() else None}
    return source, inputs, preimages


def _capture(root, session_id):
    project = service._root_path(root)
    record = _read_adoption(project, session_id)
    session = read_session_snapshot(project, session_id)
    plan = record.get('task_plan')
    snapshot = record.get('input_snapshot')
    checked = validate_task_plan(plan)
    if (record.get('schema_version') != '1.0' or record.get('validated') is not True
            or record.get('root') != str(project) or record.get('session_id') != session_id
            or type(record.get('revision')) is not int or record['revision'] != session['revision']
            or type(plan) is not dict or not checked['valid'] or not plan['tasks'] or plan['unresolved']
            or plan['session_id'] != session_id or plan['revision'] != record['revision']
            or record.get('plan_digest') != _digest(plan)
            or type(snapshot) is not dict or record.get('input_digest') != _digest(snapshot)
            or snapshot.get('session_id') != session_id or snapshot.get('revision') != record['revision']
            or snapshot.get('plan_digest') != record['plan_digest']):
        raise ValueError('adopted plan identity or digest invalid')
    status = service.adoption_input_status(project, session_id)
    if (status.get('adopted') is not True or status.get('matches') is not True
            or status.get('input_snapshot_missing') is not False or status.get('drift')
            or canonical_json(status.get('current')) != canonical_json(snapshot)
            or canonical_json(status.get('adopted_snapshot')) != canonical_json(snapshot)):
        raise ValueError('adopted inputs no longer current')
    source, inputs, outputs = _file_state(project, plan)
    binding = {'schema_version': '1.0', 'root': str(project), 'session_id': session_id,
               'revision': record['revision'], 'plan_digest': record['plan_digest'],
               'input_digest': record['input_digest'], 'adoption_digest': _digest(record),
               'source_inputs_digest': _digest(source), 'input_preimages': inputs,
               'output_preimages': outputs}
    return binding, plan, source


def capture_binding(root, session_id):
    """Capture authority and file preimages; caller holds session_write_lock."""
    try:
        return _capture(root, session_id)[0]
    except (KeyError, TypeError, RecursionError) as exc:
        raise ValueError('adopted state has invalid structure') from exc


def verify_binding(root, binding):
    """Fail closed on any authority/file drift; caller holds session_write_lock."""
    if type(binding) is not dict or set(binding) != _BINDING_FIELDS:
        raise ValueError('production binding required')
    current = capture_binding(root, binding.get('session_id'))
    if canonical_json(current) != canonical_json(binding):
        raise ValueError('production binding no longer current')
    return current


def enqueue_adopted_plan(root, session_id, registry, host_id, max_requests, timeout_seconds=180):
    """Queue the authoritative adopted plan with byte-bound external inputs."""
    from .scheduler import Scheduler
    from .task_execution import enqueue_task_plan

    project = service._root_path(root)
    registry.require(host_id)
    with session_write_lock(project):
        try:
            binding, plan, source = _capture(project, session_id)
        except (KeyError, TypeError, RecursionError) as exc:
            raise ValueError('adopted state has invalid structure') from exc
        scheduler = Scheduler(project, host_registry=registry)
        return enqueue_task_plan(scheduler, plan, host_id=host_id, max_requests=max_requests,
                                 timeout_seconds=timeout_seconds, inputs=source,
                                 production_binding=binding)
