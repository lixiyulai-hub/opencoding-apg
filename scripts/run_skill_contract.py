#!/usr/bin/env python3
"""Install/load a local skill; run only an explicitly confirmed synthetic fixture.

Execution requires the SHA256 of the action-file bytes that were reviewed. This
is a caller-supplied fixture assertion, not human approval or a security sandbox.
The entire action array is validated before installation or project writes. A
failed action stops the batch; earlier writes remain and are reported. Repeat
means re-execution, not deduplication. Python modules run as the same OS user.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opencoding.agent_adapter import LocalAgentAdapter
from opencoding.codex_host import CodexHostError, CodexSkillHost, install_skill
from opencoding.executor import action_digest
from opencoding.source_identity import source_fingerprint
from opencoding.safety import _reject_linked_ancestors, _relative_parts, safe_target
from opencoding.transactions import apply_changes, preview_changes, read_receipt_state, rollback_changes


def _directory(value: str, *, required: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('directory must be an absolute path without traversal')
    _reject_linked_ancestors(path)
    for parent in (path, *path.parents):
        if parent.exists() and not parent.is_dir():
            raise ValueError(f'not a directory: {parent}')
    if required and not path.is_dir():
        raise ValueError(f'directory does not exist: {path}')
    return path.resolve(strict=False)


def _target(root: Path, relative: str) -> Path:
    # Validate even when the execution root does not yet exist, without mkdir.
    parts = _relative_parts(relative)
    ancestor = root
    while not ancestor.exists():
        ancestor = ancestor.parent
    prefix = root.relative_to(ancestor).parts
    return safe_target(ancestor, '/'.join((*prefix, *parts)))


def _load_actions(value: str, expected_sha: str) -> tuple[list[dict], str]:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('--action-file must be absolute')
    _reject_linked_ancestors(path)
    raw = path.read_bytes()  # Parse exactly these hashed bytes; never reread on repeat.
    digest = hashlib.sha256(raw).hexdigest()
    if not expected_sha or not re.fullmatch(r'[0-9a-f]{64}', expected_sha):
        raise ValueError('--expected-action-file-sha256 must name the reviewed bytes')
    if digest != expected_sha:
        raise ValueError('action_file_drift: reviewed SHA256 differs from action file')
    value = json.loads(raw)
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ValueError('action file must contain 1..100 action objects')
    for action in value:
        action_digest(action)  # Strict executor schema and content validation.
    return value, digest


def run(args: argparse.Namespace) -> dict[str, Any]:
    if type(args.repeat) is not int or not 1 <= args.repeat <= 10:
        raise ValueError('--repeat must be between 1 and 10')
    actions: list[dict] = []
    digest = None
    execution_root = None
    if args.run_actions:
        if args.confirm_synthetic is not True:
            raise ValueError('local execution requires --confirm-synthetic; proposal/unknown is not confirmation')
        if not args.execution_root or not args.action_file:
            raise ValueError('--run-actions requires --execution-root and --action-file')
        actions, digest = _load_actions(args.action_file, args.expected_action_file_sha256)
        execution_root = _directory(args.execution_root)
    elif any((args.action_file, args.execution_root, args.confirm_synthetic,
              args.rollback_probe_path, args.expected_action_file_sha256,
              args.expected_source_sha256, args.repeat != 1)):
        raise ValueError('execution options require --run-actions')

    project_root = _directory(args.project_root, required=True)
    if project_root != Path(__file__).resolve().parents[1]:
        raise ValueError('--project-root must match the source containing this runner')
    source_digest = source_fingerprint(project_root)
    if args.expected_source_sha256:
        if not re.fullmatch(r'[0-9a-f]{64}', args.expected_source_sha256):
            raise ValueError('--expected-source-sha256 must be a lowercase SHA256')
        if source_digest != args.expected_source_sha256:
            raise ValueError('source_drift: reviewed source SHA256 differs from source tree')
    elif args.run_actions:
        raise ValueError('--run-actions requires --expected-source-sha256')
    home = _directory(args.codex_home)
    roots = [project_root, home] + ([execution_root] if execution_root is not None else [])
    for i, left in enumerate(roots):
        for right in roots[i + 1:]:
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise ValueError('source, Codex home and execution roots must not overlap')
    if execution_root is not None:
        for action in actions:
            if action['type'] == 'write_text':
                _target(execution_root, action['path'])
        if args.rollback_probe_path:
            probe = _target(execution_root, args.rollback_probe_path)
            if probe.exists():
                raise ValueError('rollback probe must be a new file')
    # Read-only resource validation and destination checks precede all writes.
    CodexSkillHost(project_root).discover()
    destination = home / 'skills' / 'opencoding'
    _reject_linked_ancestors(destination)
    if destination.exists() and not args.overwrite:
        raise ValueError(f'destination already exists: {destination}')
    installed = install_skill(project_root, home, overwrite=args.overwrite)
    host = CodexSkillHost(project_root, codex_home=home)
    resource = host.discover()
    loaded = host.load(target_platform=args.target_platform)
    result = {
        'schema': 'opencoding-skill-runtime-contract-v2',
        'project_root': str(project_root), 'codex_home': str(home),
        'execution_root': str(execution_root) if execution_root else None,
        'installation': installed,
        'discovery': {'source': resource.source, 'resource': str(resource.directory)},
        'load': loaded, 'status': 'installed_and_loaded',
        'actions_executed': False, 'synthetic_confirmation': False,
        'reviewed_action_file_sha256': digest, 'repeat_count': 0,
        'source_fingerprint_sha256': source_digest,
        'reviewed_source_sha256': args.expected_source_sha256,
        'requested_repeat_count': args.repeat, 'actions': [], 'runs': [],
        'automatic_rollback': False,
    }
    if not args.run_actions:
        return result
    _directory(str(execution_root))
    execution_root.mkdir(parents=True, exist_ok=True)
    adapter = LocalAgentAdapter(execution_root)
    invocation = uuid.uuid4().hex
    result.update(status='executed', invocation_id=invocation, synthetic_confirmation=True)
    for repeat_index in range(args.repeat):
        executed = []
        result['runs'].append(executed)
        for index, action in enumerate(actions):
            run_id = f'skill-{invocation}-r{repeat_index:03d}-{index:03d}'
            try:
                authorization = adapter.authorization_for(
                    action, scope='synthetic-skill-contract', confirmation_id=f'synthetic-{run_id}')
                receipt = adapter.execute(action, authorization=authorization, run_id=run_id)
            except (OSError, ValueError) as exc:
                receipt = {'status': 'failed', 'run_id': run_id, 'error': str(exc)}
            executed.append(receipt)
            result['actions'].append(receipt)
            result['actions_executed'] = True
            if receipt['status'] != 'succeeded':
                result.update(status='failed', stopped_at={'repeat': repeat_index, 'action': index})
                return result
        result['repeat_count'] += 1
    if args.rollback_probe_path:
        probe = _target(execution_root, args.rollback_probe_path)
        if probe.exists():
            result.update(status='failed', error='rollback probe appeared during actions')
            return result
        plan = preview_changes(execution_root, {args.rollback_probe_path: 'skill contract rollback probe\n'})
        applied = apply_changes(execution_root, plan, approved_digest=plan['plan_digest'])
        rolled = rollback_changes(execution_root, applied['transaction_id'])
        state = read_receipt_state(execution_root, applied['transaction_id'])
        result['rollback'] = {'apply': applied, 'result': rolled, 'receipt': state, 'residual': probe.exists()}
        if rolled.get('status') != 'rolled_back' or state['residual_paths'] or probe.exists():
            result['status'] = 'failed'
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', required=True)
    parser.add_argument('--codex-home', required=True)
    parser.add_argument('--target-platform', default='unspecified')
    parser.add_argument('--overwrite', action='store_true')
    parser.add_argument('--run-actions', action='store_true')
    parser.add_argument('--execution-root')
    parser.add_argument('--confirm-synthetic', action='store_true')
    parser.add_argument('--action-file')
    parser.add_argument('--expected-action-file-sha256', help='SHA256 of the reviewed action file bytes')
    parser.add_argument('--expected-source-sha256', help='SHA256 fingerprint of the reviewed executable source tree')
    parser.add_argument('--rollback-probe-path')
    parser.add_argument('--repeat', type=int, default=1, help='repeat the same reviewed bytes 1..10 times; no deduplication')
    args = parser.parse_args(argv)
    try:
        report = run(args)
    except (CodexHostError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({'status': 'blocked', 'error': str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if report['status'] == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
