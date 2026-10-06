"""Independent synthetic verification; all project writes remain in /tmp."""
import hashlib
import json
import sys
import time
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

sys.dont_write_bytecode = True
repo = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(repo))
from tests.test_product_service import _complete
from opencoding.service import ServiceError
from opencoding.taskplan_scheduler import (
    approve_task_plan, build_task_plan_confirmation,
    execute_task_plan, preview_task_plan,
)

records = []
def inventory(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}
def reject(name, fn, root, expected):
    before = inventory(root)
    try:
        fn()
    except ServiceError as error:
        assert error.code == expected, (name, error.code)
        assert inventory(root) == before, name + ': wrote project files'
        records.append({'probe': name, 'code': error.code, 'zero_file_changes': True})
    else:
        raise AssertionError(name + ': incorrectly accepted')

with TemporaryDirectory(dir='/tmp') as d:
    root = Path(d)
    session = _complete(root)['session']['id']
    before = inventory(root)
    preview = preview_task_plan(root, session)
    context = build_task_plan_confirmation(preview, statement='Synthetic independent confirmation', actor='synthetic-review', expires_in_seconds=120)
    approval = approve_task_plan(preview, confirmation=context)
    assert before == inventory(root)
    records.append({'probe':'preview_build_approve_zero_write','zero_file_changes':True})
    # Original attack, both legacy top-level shape and new nested shape.
    legacy = deepcopy(approval)
    legacy['expires_at'] = '2999-01-01T00:00:00+00:00'
    reject('legacy_unbound_expiry', lambda: execute_task_plan(root, legacy, authorization_context=context), root, 'approval_fields_invalid')
    altered = deepcopy(approval)
    altered['confirmation']['expires_at'] = (datetime.fromisoformat(context['expires_at']) + timedelta(seconds=60)).isoformat()
    reject('deadline_extension_with_original_context', lambda: execute_task_plan(root, altered, authorization_context=context), root, 'human_confirmation_scope_mismatch')
    extreme = deepcopy(approval)
    extreme['confirmation']['expires_at'] = '2999-01-01T00:00:00+00:00'
    reject('oversized_deadline_even_with_matching_context', lambda: execute_task_plan(root, extreme, authorization_context=deepcopy(extreme['confirmation'])), root, 'approval_expiry_invalid')
    # Real wall-clock expiry, independent of author's mocked datetime tests.
    short = build_task_plan_confirmation(preview, statement='Synthetic short confirmation', actor='synthetic-review', expires_in_seconds=1)
    short_approval = approve_task_plan(preview, confirmation=short)
    time.sleep(1.1)
    reject('real_expiry_old_confirmation_reapproval', lambda: approve_task_plan(preview, confirmation=short), root, 'approval_expired')
    reject('real_expiry_execution', lambda: execute_task_plan(root, short_approval, authorization_context=short), root, 'approval_expired')
    # Original valid independent context still performs bounded expected work.
    result = execute_task_plan(root, approval, authorization_context=deepcopy(context))
    assert result['status'] == 'blocked'
    assert any(t['last_error'] == 'host_missing' for t in result['tasks'])
    assert not (root / 'src').exists()
    successes = sum(r['status'] == 'succeeded' for r in result['runs'])
    records.append({'probe':'valid_independent_context','result':result['status'],'successful_documents':successes,'source_directory_absent':True})

files = ['opencoding/taskplan_scheduler.py','opencoding/executor.py','opencoding/scheduler.py','tests/test_taskplan_scheduler.py','docs/product/TASKPLAN_SCHEDULER.md','docs/product/OFFLINE_INSTALLATION.md','docs/product/AGENT_NATIVE_USE.md']
print(json.dumps({'probes':records,'reviewed_file_sha256':{f:hashlib.sha256((repo/f).read_bytes()).hexdigest() for f in files}},ensure_ascii=False,indent=2))
