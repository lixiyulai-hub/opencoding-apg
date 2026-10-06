"""Independent original-commit probes, using synthetic /tmp projects only."""
import hashlib
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
sys.dont_write_bytecode = True
source = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(source))
from tests.test_product_service import _complete
from opencoding.service import build_caller_confirmation
from opencoding.taskplan_scheduler import approve_task_plan, execute_task_plan, preview_task_plan

def prepare(root):
    session_id = _complete(root)['session']['id']
    preview = preview_task_plan(root, session_id)
    context = build_caller_confirmation(preview, statement='Synthetic independent original review', actor='synthetic-review')
    approval = approve_task_plan(preview, confirmation=context, expires_in_seconds=1)
    return approval, context

records = []
with TemporaryDirectory(dir='/tmp') as t:
    root = Path(t)
    approval, context = prepare(root)
    approval['expires_at'] = '2999-01-01T00:00:00+00:00'
    result = execute_task_plan(root, approval, authorization_context=context)
    successes = sum(run['status'] == 'succeeded' for run in result['runs'])
    assert successes == 9
    records.append({'probe':'P1_expiry_mutation_preserving_original_context', 'modified_expiry_accepted':True, 'successful_document_runs':successes, 'result_status':result['status']})
with TemporaryDirectory(dir='/tmp') as t, TemporaryDirectory(dir='/tmp') as outside:
    root = Path(t)
    sentinel = Path(outside) / 'outside.md'
    sentinel.write_text('outside-sentinel', encoding='utf-8')
    approval, context = prepare(root)
    (root / 'PRG.md').symlink_to(sentinel)
    result = execute_task_plan(root, approval, authorization_context=context)
    task = next(x for x in result['tasks'] if x['input']['plan_task_id'] == 'requirements-confirmed')
    assert result['status'] == 'blocked' and task['state'] == 'failed'
    assert sentinel.read_text(encoding='utf-8') == 'outside-sentinel'
    records.append({'probe':'symlink_replacement_after_approval', 'result_status':result['status'], 'requirement_task_state':task['state'], 'requirement_task_error':task['last_error'], 'outside_sentinel_unchanged':True})
print(json.dumps({'reviewed_commit':'afc1614e3106b8d72adda35d92f8b70dc570c2a4','adapter_sha256':hashlib.sha256((source/'opencoding/taskplan_scheduler.py').read_bytes()).hexdigest(),'probes':records},indent=2))
