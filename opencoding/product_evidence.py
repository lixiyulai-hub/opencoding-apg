"""Check this run's local evidence, never promote historical tests to observed."""
from pathlib import Path
import re
from typing import Any

from .agent_adapter import _confirmation_binding
from .agent_tasks import skill_identity
from .executor import action_digest
from .safety import safe_target, sha256_bytes
from .service import adoption_input_status
from .transactions import _load_receipt_manifest


def verify_product_evidence(root: str | Path, run_id: str, expected_digest: str) -> dict[str, Any]:
    # Lazy import avoids the product_loop -> executor -> evidence cycle.
    from .product_loop import read_product_run
    import json
    root = Path(root)
    run = read_product_run(root, run_id)
    root = root.resolve()
    if not expected_digest or run.get('record_digest') != expected_digest or run.get('root') != str(root):
        raise ValueError('run_digest_or_root_mismatch')
    if run.get('status') != 'succeeded' or run.get('pending_action') or run.get('file_rollbacks'):
        raise ValueError('run_not_accepted')
    adoption = adoption_input_status(root, run['session_id'])
    if not adoption.get('matches') or adoption.get('current', {}).get('plan_digest') != run['plan_digest']:
        raise ValueError('plan_drifted')
    skill = skill_identity()
    latest: dict[str, str] = {}
    completed = []
    for event in run.get('action_events', []):
        if event.get('phase') != 'completed':
            continue
        action, receipt, auth = event['action'], event['receipt'], event['authorization']
        if event['skill'] != skill or action_digest(action) != receipt.get('action_digest'):
            raise ValueError('source_or_action_drifted')
        binding = _confirmation_binding(confirmation_id=auth['confirmation_id'], root=str(root),
            action_digest_value=action_digest(action), targets=[action['path']] if action['type'] in {'write_text', 'node_script'} else [],
            scope=auth['scope'], expires_at=auth['expires_at'])
        if auth.get('root') != str(root) or auth.get('confirmation_binding') != binding or receipt.get('authorization_binding', {}).get('confirmation_binding') != binding:
            raise ValueError('authorization_binding_invalid')
        claim_path = root / '.opencoding' / 'authorizations' / (binding + '.json')
        from .safety import _reject_linked_ancestors
        _reject_linked_ancestors(claim_path)
        claim = json.loads(claim_path.read_text(encoding='utf-8'))
        if claim.get('confirmation_binding') != binding or claim.get('root') != str(root):
            raise ValueError('claim_missing_or_changed')
        if action['type'] == 'write_text' and receipt['status'] == 'succeeded':
            tx_id = receipt.get('transaction', {}).get('transaction_id')
            _, tx_receipt, _, entries = _load_receipt_manifest(root, tx_id)
            expected = sha256_bytes(action['content'].encode('utf-8'))
            if tx_receipt['status'] != 'applied' or tx_receipt.get('rollback_status') is not None or len(entries) != 1 or entries[0]['path'] != action['path'] or entries[0]['after_sha256'] != expected:
                raise ValueError('transaction_evidence_invalid')
            if receipt.get('artifacts') != [{'path': action['path'], 'sha256': expected}]:
                raise ValueError('artifact_evidence_invalid')
            latest[action['path']] = expected
        completed.append(event)
    if not latest:
        raise ValueError('write_evidence_missing')
    for path, expected in latest.items():
        if sha256_bytes(safe_target(root, path, allow_missing=False).read_bytes()) != expected:
            raise ValueError('output_drifted:' + path)
    verification = [t for t in run['tasks'] if t['task']['action']['type'] == 'verify_feature']
    if not verification:
        raise ValueError('test_evidence_missing')
    test_receipts = []
    for task in verification:
        matches = [e for e in completed if e['task_id'] == task['id'] and e['action']['type'] in {'python_module', 'node_script'}]
        if not matches:
            raise ValueError('test_evidence_missing')
        event = matches[-1]
        receipt = event['receipt']
        output = task['task']['outputs'][0]
        if event['action']['type'] == 'python_module':
            command = {'type': 'python_module', 'module': 'unittest', 'args': ['discover', '-s', str(Path(output).parent), '-p', Path(output).name, '-v']}
            command_ok = event['action'] == command
        else:
            command_ok = event['action'].get('type') == 'node_script' and event['action'].get('path') == output
        if not command_ok or receipt.get('status') != 'succeeded' or receipt.get('exit_code') != 0 or type(receipt.get('tests_run')) is not int or receipt['tests_run'] <= 0:
            raise ValueError('test_failed_or_empty')
        combined = receipt.get('stdout_summary', '') + "\n" + receipt.get('stderr_summary', '')
        match = re.search(r'\bRan (\d+) tests? in |OPENCODING_TESTS_RUN=(\d+)', combined)
        if not match or int(match[1] or match[2]) != receipt['tests_run']:
            raise ValueError('test_output_mismatch')
        for path, expected in latest.items():
            if event['files_before'].get(path) != expected:
                # Be conservative: any later feature writes require revalidation.
                raise ValueError('tested_source_drifted')
        test_receipts.append({'task_id': task['id'], 'run_id': receipt['run_id'], 'tests_run': receipt['tests_run']})
    return {'run_id': run_id, 'run_digest': expected_digest, 'plan_digest': run['plan_digest'],
            'skill': skill, 'files': latest, 'tests': test_receipts,
            'scope': 'current Python CLI product run; local integrity, not identity proof or sandbox',
            'managed_loader': None}
