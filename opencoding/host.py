"""Candidate-only Host contract. Registration is a local trust decision, not a sandbox.

Adapters must not write project outputs. Cancellation/timeout seals the local result;
cooperative cancellation cannot guarantee that an external action has stopped.
Idempotency is scoped to the lifetime of this registry; durable dispatch belongs to
 the scheduler. Simulated verification must never be promoted to live evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import queue
import re
import threading
import time
from .planning import _canonical_path, _path_error, _related

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_FILES = 256
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_IDENTITY = ('root', 'session_id', 'plan_digest', 'task_id', 'run_id', 'attempt', 'outputs', 'host_id')
_FIELDS = set(_IDENTITY) | {'schema_version', 'input', 'timeout_seconds'}
_CAPABILITIES = {'task_candidate', 'cancel', 'timeout'}


class HostProtocolError(ValueError):
    """Invalid or untrusted Host protocol data."""


def _json_copy(value):
    def check(item, depth=0):
        if depth > 40:
            raise HostProtocolError('JSON nesting exceeds limit')
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child, depth + 1)
            return
        raise HostProtocolError('strict JSON data required')
    check(value)
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HostProtocolError('invalid JSON data') from exc
    if len(raw) > MAX_JSON_BYTES:
        raise HostProtocolError('JSON payload exceeds limit')
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def request_digest(request):
    return _json_copy(request)[1]


def candidate_digest(files):
    return _json_copy(files)[1]


def request_identity(request):
    return {key: copy.deepcopy(request[key]) for key in _IDENTITY}


def _path(value):
    if type(value) is not str or not value or len(value) > 1024:
        raise HostProtocolError('invalid output path')
    if _path_error(value):
        raise HostProtocolError('output path must be portable project-relative path')
    return value


def validate_request(request):
    request, _ = _json_copy(request)
    if type(request) is not dict or set(request) != _FIELDS or request['schema_version'] != '1.0':
        raise HostProtocolError('invalid request schema')
    for key in ('host_id', 'session_id', 'task_id', 'run_id'):
        if type(request[key]) is not str or not _ID.fullmatch(request[key]):
            raise HostProtocolError('invalid ' + key)
    if type(request['plan_digest']) is not str or not _HASH.fullmatch(request['plan_digest']):
        raise HostProtocolError('invalid plan_digest')
    root = request['root']
    if type(root) is not str or not root or not Path(root).is_absolute() or not Path(root).is_dir():
        raise HostProtocolError('root must be an existing absolute directory')
    if type(request['attempt']) is not int or not 1 <= request['attempt'] <= 1000:
        raise HostProtocolError('invalid attempt')
    timeout = request['timeout_seconds']
    if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
        raise HostProtocolError('invalid timeout_seconds')
    outputs = request['outputs']
    if type(outputs) is not list or not 1 <= len(outputs) <= MAX_FILES:
        raise HostProtocolError('invalid outputs')
    for output in outputs:
        _path(output)
    canonical = [_canonical_path(p) for p in outputs]
    if any(_related(left, right) for i, left in enumerate(canonical) for right in canonical[i + 1:]):
        raise HostProtocolError('duplicate or overlapping outputs')
    if type(request['input']) is not dict:
        raise HostProtocolError('input must be object')
    return request


class HostRegistry:
    """Trusted adapter registration with bounded, identity-bound candidate dispatch.

    Verification/evidence are assertions by the trusted registering caller, not
    independent review or evidence that the registry itself exercised a live Host.
    """

    def __init__(self):
        self._hosts = {}
        self._runs = {}
        self._lock = threading.Lock()

    def register(self, host_id, adapter, *, capabilities, verification='simulated', evidence=''):
        if type(host_id) is not str or not _ID.fullmatch(host_id):
            raise HostProtocolError('invalid host_id')
        if verification not in ('simulated', 'live') or type(evidence) is not str or not evidence.strip() or len(evidence) > 4096:
            raise HostProtocolError('verification and evidence required')
        if not isinstance(capabilities, (list, tuple, set, frozenset)) or any(type(c) is not str or c not in _CAPABILITIES for c in capabilities):
            raise HostProtocolError('unknown capabilities')
        if not callable(getattr(adapter, 'execute', None)):
            raise HostProtocolError('adapter.execute required')
        with self._lock:
            if host_id in self._hosts:
                raise HostProtocolError('host already registered')
            self._hosts[host_id] = (adapter, frozenset(capabilities), verification, evidence)

    def require(self, host_id):
        """Read-only capability preflight; returns trusted registration metadata."""
        if type(host_id) is not str or not _ID.fullmatch(host_id):
            raise HostProtocolError('invalid host_id')
        with self._lock:
            host = self._hosts.get(host_id)
            if host is None or not _CAPABILITIES.issubset(host[1]):
                raise HostProtocolError('host or required capabilities unverified')
            return {'host_id': host_id, 'capabilities': sorted(host[1]),
                    'verification': host[2], 'evidence': host[3]}

    def execute(self, request, *, cancel_event=None):
        request = validate_request(request)
        digest = request_digest(request)
        with self._lock:
            host = self._hosts.get(request['host_id'])
            if host is None or not _CAPABILITIES.issubset(host[1]):
                raise HostProtocolError('host or required capabilities unverified')
            prior = self._runs.get(request['run_id'])
            if prior is not None:
                if prior['digest'] != digest:
                    raise HostProtocolError('run_id reused with different request')
                owner = False
                record = prior
            else:
                owner = True
                record = {'digest': digest, 'done': threading.Event()}
                self._runs[request['run_id']] = record
        if not owner:
            record['done'].wait()
            return copy.deepcopy(record['result'])
        adapter, _, verification, evidence = host
        result = {'schema_version': '1.0', 'status': 'failed', 'request_digest': digest,
                  'identity': request_identity(request), 'candidate': None,
                  'verification': verification, 'evidence': evidence, 'error': None}
        stop = threading.Event()
        mailbox = queue.Queue(maxsize=1)
        def run():
            try:
                response = adapter.execute(copy.deepcopy(request), stop)
                mailbox.put_nowait(('result', _json_copy(response)[0]))
            except BaseException:
                mailbox.put_nowait(('error', None))
        try:
            if cancel_event is not None and cancel_event.is_set():
                result['status'] = 'cancelled'
            else:
                started = time.monotonic()
                threading.Thread(target=run, daemon=True).start()
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        result['status'] = 'cancelled'
                        break
                    remaining = request['timeout_seconds'] - (time.monotonic() - started)
                    if remaining <= 0:
                        result['status'] = 'timed_out'
                        break
                    try:
                        kind, response = mailbox.get(timeout=min(remaining, .01))
                    except queue.Empty:
                        continue
                    if cancel_event is not None and cancel_event.is_set():
                        result['status'] = 'cancelled'
                        break
                    if time.monotonic() - started >= request['timeout_seconds']:
                        result['status'] = 'timed_out'
                        break
                    if kind == 'error':
                        raise HostProtocolError('adapter failed')
                    required = {'status', 'request_digest', 'identity', 'files'}
                    if type(response) is not dict or set(response) != required:
                        raise HostProtocolError('invalid adapter result schema')
                    if response['request_digest'] != digest or response['identity'] != result['identity'] or request_digest(response['identity']) != request_digest(result['identity']):
                        raise HostProtocolError('adapter result identity mismatch')
                    if response['status'] not in ('succeeded', 'failed', 'cancelled', 'timed_out'):
                        raise HostProtocolError('invalid adapter status')
                    files = response['files']
                    if type(files) is not dict or any(type(v) is not str for v in files.values()):
                        raise HostProtocolError('candidate files must contain text')
                    if response['status'] == 'succeeded':
                        if set(files) != set(request['outputs']):
                            raise HostProtocolError('candidate outputs mismatch')
                        result['candidate'] = {'files': files, 'digest': candidate_digest(files)}
                    elif files:
                        raise HostProtocolError('unsuccessful result cannot carry files')
                    result['status'] = response['status']
                    break
            if result['status'] in ('cancelled', 'timed_out'):
                stop.set()
                result['error'] = 'dispatch sealed; external action termination is not guaranteed'
        except Exception as exc:
            stop.set()
            result['status'] = 'failed'
            result['candidate'] = None
            result['error'] = str(exc) if isinstance(exc, HostProtocolError) else 'host dispatch failed'
        finally:
            record['result'] = copy.deepcopy(result)
            record['done'].set()
        return copy.deepcopy(result)


class MockHost:
    """Cooperative simulated adapter; never modifies project files."""

    def __init__(self, files, delay_seconds=0, status='succeeded'):
        self.files = copy.deepcopy(files)
        self.delay_seconds = delay_seconds
        self.status = status
        self.calls = 0

    def execute(self, request, cancel_event):
        self.calls += 1
        cancelled = cancel_event.wait(self.delay_seconds)
        status = 'cancelled' if cancelled else self.status
        return {'status': status, 'request_digest': request_digest(request),
                'identity': request_identity(request),
                'files': copy.deepcopy(self.files) if status == 'succeeded' else {}}
