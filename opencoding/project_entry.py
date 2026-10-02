"""Default project workflow for a host Agent; no model or code generator inside.

Each command is a fresh process. Session/run references survive restart, while
previews are recomputed and content-bound to the caller's reviewed digest.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import uuid

from .agent_tasks import LocalAgentTaskExecutor, digest, preview_agent_tasks
from .intake import QUESTION_DEFINITIONS
from .product_evidence import verify_product_evidence
from .product_loop import read_product_run, rollback_product_run, start_product_run, resume_product_run, _write_json
from .safety import _root_path, _reject_linked_ancestors, sanitize_text
from .service import (create_session, session_view, submit_answer, evaluate_session,
                      adopt_evaluation_plan, preview_session, approve_preview, apply_approved)
from .sessions import _lock_os, _unlock_os


def _read_json(path: Path):
    if not path.is_absolute():
        raise ValueError('输入文件必须为绝对路径')
    _reject_linked_ancestors(path)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('输入文件超过 16 MiB')
    return json.loads(path.read_text(encoding='utf-8'))


def _state_path(root: Path) -> Path:
    path = root / '.opencoding' / 'project-entry.json'
    _reject_linked_ancestors(path)
    return path


def _state(root: Path) -> dict:
    value = _read_json(_state_path(root))
    if (not isinstance(value, dict) or value.get('schema') != 'opencoding-project-entry-v1'
            or value.get('root') != str(root)
            or value.get('record_digest') != digest({k: v for k, v in value.items() if k != 'record_digest'})):
        raise ValueError('项目入口状态已改变或属于其他根目录；禁止跨根复用授权')
    return value


@contextmanager
def _entry_lock(root: Path):
    metadata = root / '.opencoding'
    _reject_linked_ancestors(metadata)
    metadata.mkdir(exist_ok=True)
    path = metadata / '.project-entry.lock'
    _reject_linked_ancestors(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    locked = False
    try:
        _lock_os(fd)
        locked = True
        yield
    finally:
        if locked:
            _unlock_os(fd)
        os.close(fd)


def _run_summary(root: Path, state: dict) -> dict:
    path = root / '.opencoding' / 'product_runs' / (state['run_id'] + '.json')
    _reject_linked_ancestors(path)
    if not path.exists():
        return {'status': 'not_started', 'run_id': state['run_id']}
    run = read_product_run(root, state['run_id'])
    result = {key: run[key] for key in ('status', 'run_id', 'record_digest', 'failure', 'human_gate', 'file_rollbacks') if key in run}
    result['tasks'] = [{'id': t['id'], 'status': t['status'], 'attempts': t.get('attempts', 0)} for t in run['tasks']]
    if run['status'] == 'succeeded':
        result['evidence'] = verify_product_evidence(root, run['run_id'], run['record_digest'])
    return result


def _confirm(args):
    if not args.authorized_local or not args.authorization_id or not args.authorization_id.strip():
        raise ValueError('需要已有本地任务授权：--authorized-local --authorization-id；不是新增逐动作人审')


def _operate(root: Path, args) -> dict:
    if args.command == 'init':
        if _state_path(root).exists():
            raise ValueError('此根目录已有入口状态；使用 status 接续，勿覆盖')
        view = create_session(root, args.idea)
        state = {'schema': 'opencoding-project-entry-v1', 'root': str(root),
                 'session_id': view['session']['id'], 'run_id': 'product-' + uuid.uuid4().hex[:12],
                 'understanding': 'host-agent; program uses explicit answers and rules, no built-in model',
                 'answers_origin': None}
        _write_json(_state_path(root), state)
        return {'status': 'needs_answers', 'session_id': state['session_id'],
                'questions': list(QUESTION_DEFINITIONS), 'next': '由宿主 Agent 澄清后提供 answers JSON；执行 project plan'}
    state = _state(root)
    session_id = state['session_id']
    if args.command == 'plan':
        if _run_summary(root, state)['status'] != 'not_started':
            raise ValueError('已启动项目不能覆盖方案；保留账本并使用 preview/run 修复或新根目录')
        answers = _read_json(args.answers)
        ids = {q['id'] for q in QUESTION_DEFINITIONS}
        if not isinstance(answers, dict) or set(answers) != ids or any(not isinstance(v, str) or not v.strip() for v in answers.values()):
            raise ValueError('answers 必须包含全部澄清问题的非空字符串，不能代用户默认为否')
        view = session_view(root, session_id)
        for q in QUESTION_DEFINITIONS:
            view = submit_answer(root, session_id, view['session']['revision'], q['id'], answers[q['id']])
            if view.get('status') in {'busy', 'stale'}:
                raise ValueError('会话忙或版本变化，请重新读取；未继续执行')
        evaluation = evaluate_session(root, session_id)
        state['answers_origin'] = args.answers_origin
        _write_json(_state_path(root), state)
        if evaluation['recommendation']['status'] != 'ready':
            return {'status': 'needs_clarification', 'evaluation': evaluation}
        adopt_evaluation_plan(root, session_id)
        preview = preview_session(root, session_id)
        return {'status': 'documents_preview', 'evaluation': evaluation, 'preview': preview,
                'expected_digest': preview['service_digest'], 'next': '复核方案与文档 diff，使用 project apply-docs'}
    if args.command == 'apply-docs':
        _confirm(args)
        preview = preview_session(root, session_id)
        if args.expected_digest != preview['service_digest']:
            raise ValueError('文档/方案已漂移；重新 plan 并复核，尚未写入文档')
        result = apply_approved(root, approve_preview(preview))
        state['documents'] = result
        state['documents_authorization'] = {'id': args.authorization_id, 'scope': 'existing-user-local-task', 'synthetic': args.synthetic}
        _write_json(_state_path(root), state)
        return result
    if args.command in {'preview', 'run'}:
        actions = _read_json(args.actions)
        preview = preview_agent_tasks(root, session_id, actions)
        if args.command == 'preview':
            return {'status': 'actions_preview', 'preview': preview, 'expected_digest': preview['preview_digest'],
                    'next': '宿主 Agent 复核动作后，在已有授权范围内使用 project run'}
        _confirm(args)
        executor = LocalAgentTaskExecutor(root, preview, expected_digest=args.expected_digest,
                    confirmation_id=args.authorization_id, confirmed=True, synthetic=args.synthetic)
        previous = _run_summary(root, state)
        # Existing successful runs are never dispatched again. Status validates hashes.
        if previous['status'] == 'succeeded':
            return previous
        if previous['status'] == 'not_started':
            start_product_run(root, session_id, run_id=state['run_id'], human_confirmed=True, project_executor=executor)
        else:
            resume_product_run(root, state['run_id'], human_confirmed=True, project_executor=executor)
        return _run_summary(root, state)
    if args.command == 'rollback':
        _confirm(args)
        return rollback_product_run(root, state['run_id'], reason=args.reason)
    if args.command == 'status':
        return {'status': 'project_found', 'state': state, 'run': _run_summary(root, state),
                'next': '根据 run 的失败任务准备修复动作并 preview/run；成功产物保留供直接运行'}
    raise ValueError('未知项目命令')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='opencoding project', description='当前 Agent 的默认项目入口：需求→方案→文档→受控执行→测试→恢复')
    subs = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'plan', 'apply-docs', 'preview', 'run', 'status', 'rollback'):
        sub = subs.add_parser(name)
        sub.add_argument('--root', type=Path, required=True, help='已有独立项目绝对目录')
        if name == 'init':
            sub.add_argument('--idea', required=True)
        if name == 'plan':
            sub.add_argument('--answers', type=Path, required=True)
            sub.add_argument('--answers-origin', choices=('user-conversation', 'agent-assumptions', 'fixture'), required=True)
        if name in {'preview', 'run'}:
            sub.add_argument('--actions', type=Path, required=True)
        if name in {'apply-docs', 'run'}:
            sub.add_argument('--expected-digest', required=True)
        if name in {'apply-docs', 'run', 'rollback'}:
            sub.add_argument('--authorized-local', action='store_true')
            sub.add_argument('--authorization-id')
            sub.add_argument('--synthetic', action='store_true')
        if name == 'rollback':
            sub.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    try:
        if not args.root.is_absolute():
            raise ValueError('--root 必须为绝对目录')
        root = _root_path(args.root)
        if args.command in {'status', 'preview'}:
            result = _operate(root, args)
        else:
            with _entry_lock(root):
                result = _operate(root, args)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 1 if result.get('status') in {'failed', 'blocked_human_gate', 'blocked_capability', 'rollback_blocked', 'busy', 'stale'} else 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({'status': 'error', 'error': sanitize_text(str(exc)), 'code': getattr(exc, 'code', type(exc).__name__)}, ensure_ascii=False), file=sys.stderr)
        return 2
