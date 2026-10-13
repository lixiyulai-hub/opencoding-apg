"""Trusted local validation/review adapters and single-use candidate grants.

Registration is a local trust decision, not proof of organizational independence.
Grants live only in this registry: restart requires fresh validation and review.
Callbacks must be read-only; timeouts seal results but cannot stop callback effects.
"""
from __future__ import annotations

import copy
import math
import queue
import secrets
import sqlite3
import threading
import time

from .host import _json_copy
from .safety import canonical_json, sha256_bytes
from .scheduler import _explicit_root, _readonly_snapshot_connection
from .sessions import session_write_lock
from .transactions import apply_changes, rollback_changes, _secure_dir, _write_json


def _digest(value):
    return sha256_bytes(canonical_json(value))


def _blocked(reason):
    return {'status': 'blocked', 'reason': reason, 'delivery_gate': 'pending_independent_review'}


def _database_digest(root):
    with _readonly_snapshot_connection(root) as connection:
        if connection is None:
            raise ValueError('host_plan_not_found')
        return _digest({table: [dict(row) for row in connection.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                        for table in ('tasks', 'runs', 'task_dependencies')})


class ReviewRegistry:
    def __init__(self):
        self._adapters = {}
        self._grants = {}
        self._lock = threading.RLock()

    def register(self, name, adapter, *, role, principal, verification='simulated'):
        if (type(name) is not str or not name.strip() or len(name) > 128
                or type(principal) is not str or not principal.strip() or len(principal) > 128
                or role not in ('validator', 'reviewer') or verification not in ('simulated', 'live')
                or not callable(adapter)):
            raise ValueError('invalid review registration')
        with self._lock:
            if name in self._adapters:
                raise ValueError('duplicate review registration')
            self._adapters[name] = (adapter, role, principal, verification)

    def revoke(self, grant):
        with self._lock:
            self._grants.pop(grant, None)

    def _call(self, registration, request, deadline, cancel):
        if cancel.is_set():
            raise ValueError('review_cancelled')
        if time.monotonic() >= deadline:
            raise ValueError('review_timed_out')
        results = queue.Queue(maxsize=1)
        def invoke():
            try:
                results.put(('ok', _json_copy(registration[0](copy.deepcopy(request), cancel))[0]))
            except Exception:
                results.put(('error', None))
        threading.Thread(target=invoke, daemon=True).start()
        while not cancel.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                cancel.set()
                raise ValueError('review_timed_out')
            try:
                state, result = results.get(timeout=min(.05, remaining))
            except queue.Empty:
                continue
            if cancel.is_set():
                raise ValueError('review_cancelled')
            if time.monotonic() >= deadline:
                cancel.set()
                raise ValueError('review_timed_out')
            if state != 'ok':
                raise ValueError('review_unavailable')
            if (type(result) is not dict or set(result) != {'request_digest', 'status', 'findings', 'evidence'}
                    or result['request_digest'] != _digest(request)
                    or result['status'] != 'passed' or result['findings'] != []
                    or type(result['evidence']) is not str or not result['evidence'].strip()
                    or len(result['evidence']) > 16384):
                raise ValueError('review_not_passed')
            return result
        raise ValueError('review_cancelled')

    def review(self, root, plan_digest, *, validator, reviewer, timeout_seconds=180, cancel_event=None):
        from .host_candidates import preview_host_candidates
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 3600):
            return _blocked('invalid_review_timeout')
        cancel = cancel_event or threading.Event()
        try:
            with self._lock:
                validation = self._adapters[validator]
                review = self._adapters[reviewer]
            if validation[1] != 'validator' or review[1] != 'reviewer' or validation[2] == review[2]:
                return _blocked('independent_review_required')
            preview = preview_host_candidates(root, plan_digest)
            if preview['status'] != 'preview_ready':
                return preview
            generator = preview['review_context']['generator_id']
            if generator in (reviewer, review[2], validator, validation[2]):
                return _blocked('independent_review_required')
            binding_digest = _digest(preview)
            deadline = time.monotonic() + timeout_seconds
            request = {'schema_version': '1.0', 'role': 'validator', 'preview': preview,
                       'binding_digest': binding_digest}
            validated = self._call(validation, request, deadline, cancel)
            request = {**request, 'role': 'reviewer', 'validation': validated}
            reviewed = self._call(review, request, deadline, cancel)
            if cancel.is_set():
                return _blocked('review_cancelled')
            current = preview_host_candidates(root, plan_digest)
            if _digest(current) != binding_digest:
                return _blocked('review_candidate_drift')
            live = preview['verification'] == ['live'] and validation[3] == review[3] == 'live'
            evidence = {'schema_version': '1.0', 'binding_digest': binding_digest,
                        'validator': validator, 'reviewer': reviewer,
                        'validation': validated, 'review': reviewed,
                        'verification': 'live' if live else 'simulated'}
            if not live:
                return {'status': 'reviewed_simulated', 'evidence': evidence,
                        'delivery_gate': 'pending_live_validation_review'}
            grant = secrets.token_urlsafe(32)
            with self._lock:
                self._grants[grant] = {'preview': copy.deepcopy(preview), 'evidence': evidence,
                                       'state': 'ready', 'expires': time.monotonic() + 900}
            return {'status': 'review_passed', 'grant': grant, 'evidence': copy.deepcopy(evidence),
                    'approved_digest': preview['file_plan']['plan_digest'], 'delivery_gate': 'ready_to_apply'}
        except KeyError:
            return _blocked('review_adapter_unavailable')
        except ValueError as error:
            return _blocked(str(error) if str(error).startswith('review_') else 'review_evidence_invalid')
        except (OSError, RuntimeError, TypeError, sqlite3.Error):
            return _blocked('review_evidence_invalid')

    def apply(self, root, plan_digest, grant, *, approved_digest):
        from .host_candidates import _preview_locked
        from .host_workflow import capture_binding
        claimed = False
        try:
            if type(grant) is not str:
                return _blocked('invalid_review_grant')
            with self._lock:
                record = self._grants.get(grant)
                if not record or record['state'] != 'ready' or time.monotonic() >= record['expires']:
                    return _blocked('invalid_review_grant')
                record['state'] = 'applying'
                claimed = True
            root = _explicit_root(root)
            with session_write_lock(root):
                current = _preview_locked(root, plan_digest)
                if (_digest(current) != record['evidence']['binding_digest']
                        or approved_digest != current['file_plan']['plan_digest']):
                    return _blocked('review_candidate_drift')
                database = _database_digest(root)
                binding = copy.deepcopy(current['binding'])
                binding.pop('output_preimages')
                def guard():
                    with self._lock:
                        if self._grants.get(grant) is not record or time.monotonic() >= record['expires']:
                            raise ValueError('review_grant_revoked')
                    now = capture_binding(root, current['binding']['session_id'])
                    now.pop('output_preimages')
                    if _digest(now) != _digest(binding) or _database_digest(root) != database:
                        raise ValueError('review_binding_drift')
                # Persist the bound review before any candidate writes. This is audit
                # evidence only; a disk record cannot reconstitute an apply grant.
                evidence_id = _digest(record['evidence'])
                evidence_dir = _secure_dir(root, ('.opencoding', 'host-reviews'), create=True)
                evidence_path = evidence_dir / (evidence_id + '.json')
                _write_json(evidence_path, {'evidence': record['evidence'],
                                          'candidate_digest': current['candidate_digest'],
                                          'plan_digest': plan_digest,
                                          'file_plan_digest': approved_digest,
                                          'run_receipt_digests': current['run_receipt_digests']})
                plan = {**current['file_plan'], 'commit_guard': guard}
                result = apply_changes(root, plan, approved_digest=approved_digest)
                if result['status'] == 'applied':
                    try:
                        guard()
                    except Exception:
                        result = {**result, 'status': 'partial_failure', 'reason': 'review_binding_drift'}
                if result['status'] != 'applied' and result.get('transaction_id'):
                    result['rollback'] = rollback_changes(root, result['transaction_id'])
                return {**result, 'review_evidence_path': str(evidence_path), 'review_evidence': copy.deepcopy(record['evidence']),
                        'delivery_gate': 'applied' if result['status'] == 'applied' else 'application_failed'}
        except Exception:
            return _blocked('review_application_rejected')
        finally:
            if claimed:
                with self._lock:
                    self._grants.pop(grant, None)
