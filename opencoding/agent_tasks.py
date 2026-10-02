"""Reviewed Python CLI tasks for the product loop; no model or action synthesis.

Confirmations/receipts are local same-user integrity records, not signatures.
Python remains arbitrary same-user code. File transactions cover reviewed
writes only; subprocess effects are not rolled back automatically.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .agent_adapter import AgentAdapterError, LocalAgentAdapter
from .executor import action_digest
from .safety import canonical_json, safe_target, _root_path, sha256_bytes
from .service import adoption_input_status, evaluate_session
from .source_identity import source_fingerprint

SOURCE_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = '.agents/skills/opencoding/SKILL.md'


def digest(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def _file_hash(root: Path, name: str) -> str | None:
    path = safe_target(root, name)
    return sha256_bytes(path.read_bytes()) if path.is_file() else None


def skill_identity() -> dict[str, str]:
    resource = SOURCE_ROOT / SKILL_PATH
    manifest = json.loads((resource.parent / 'skill.json').read_text(encoding='utf-8'))
    if not resource.read_text(encoding='utf-8').startswith('---\nname: opencoding\n') or manifest.get('entrypoint') != 'opencoding.agent_adapter:LocalAgentAdapter':
        raise AgentAdapterError('skill_invalid', '项目 skill 入口无效')
    return {'resource': SKILL_PATH, 'sha256': sha256_bytes(resource.read_bytes()),
            'manifest_sha256': sha256_bytes((resource.parent / 'skill.json').read_bytes()),
            'source_sha256': source_fingerprint(SOURCE_ROOT), 'entrypoint': manifest['entrypoint']}


def preview_agent_tasks(root: str | Path, session_id: str, tasks: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only review of exact actions, before images, plan, and skill source."""
    if not Path(root).is_absolute():
        raise AgentAdapterError("root_invalid", "必须使用绝对项目根目录")
    root = _root_path(Path(root))
    status = adoption_input_status(root, session_id)
    if not status.get('adopted') or not status.get('matches'):
        raise AgentAdapterError('adoption_required', '必须先采用与当前输入一致的计划')
    evaluation = evaluate_session(root, session_id)
    plan = evaluation['task_plan']
    by_id = {t['id']: t for t in plan['tasks']}
    if not isinstance(tasks, Mapping) or not tasks:
        raise AgentAdapterError('tasks_invalid', '必须提供明确的任务动作')
    prepared = json.loads(canonical_json(tasks))
    paths: set[str] = set()
    for task_id, actions in prepared.items():
        task = by_id.get(task_id)
        if task is None or task['action']['type'] not in {'implement_feature', 'verify_feature'} or task['action']['platform'] not in {'cli', 'web'}:
            raise AgentAdapterError('task_unsupported', '本入口仅支持已采用计划中的 Python CLI 或本地 Web 实现/测试节点')
        if not isinstance(actions, list) or not actions or len(actions) > 32:
            raise AgentAdapterError('actions_invalid', '任务动作必须为非空且有界列表')
        is_test = task['action']['type'] == 'verify_feature'
        allowed = set(task['outputs']) | (set(task['inputs']) if is_test else set())
        platform = task['action']['platform']
        allowed_suffixes = ('.py',) if platform == 'cli' else ('.ts', '.js', '.mjs', '.cjs', '.html', '.css')
        if not all(path.endswith(allowed_suffixes) for path in allowed):
            raise AgentAdapterError('task_unsupported', '当前桥接只支持 Python CLI 或 TypeScript/JavaScript Web 文件')
        writes = set()
        output = task['outputs'][0] if is_test and task['outputs'] else None
        for index, action in enumerate(actions):
            action_digest(action)  # strict shape/content checks, including secrets
            if action['type'] == 'write_text':
                path = action['path']
                if path not in allowed or path in writes:
                    raise AgentAdapterError('task_target_mismatch', '写入目标必须是该任务输出或测试节点的实现输入，且不能重复')
                safe_target(root, path)
                writes.add(path)
                paths.add(path)
            elif action['type'] == 'python_module':
                # Bounded first profile. Do not call an arbitrary "exit 0" a test.
                output = task['outputs'][0]
                expected = ['discover', '-s', str(Path(output).parent), '-p', Path(output).name, '-v']
                if not is_test or index != len(actions) - 1 or action['module'] != 'unittest' or action['args'] != expected or '-' in Path(output).stem:
                    raise AgentAdapterError('test_command_invalid', '验证任务必须以精确输出文件的 unittest discover 结束')
            elif action['type'] == 'node_script':
                if not is_test or index != len(actions) - 1 or action['path'] != output or not action['path'].endswith(('.ts', '.js', '.mjs', '.cjs')):
                    raise AgentAdapterError('test_command_invalid', 'Web 验证任务必须以精确输出文件的 node_script 结束')
                if action['path'].endswith('.ts') and action['args'] != ['--experimental-strip-types']:
                    raise AgentAdapterError('test_command_invalid', 'TypeScript Web 测试必须显式使用 node --experimental-strip-types')
            else:
                raise AgentAdapterError('action_invalid', '仅支持 write_text、python_module 或 node_script')
        if not set(task['outputs']).issubset(writes):
            raise AgentAdapterError('task_outputs_missing', '必须预览该任务的全部输出')
        if is_test and actions[-1]['type'] not in {'python_module', 'node_script'}:
            raise AgentAdapterError('test_command_missing', '验证节点必须实际运行 Python 或 Node 测试')
    # Freeze all adopted design and feature paths, so drift anywhere invalidates
    # confirmation before dispatch. Missing future outputs are valid snapshots.
    for task in plan['tasks']:
        paths.update(task['outputs'])
    payload = {'schema': 'opencoding-agent-task-preview-v1', 'root': str(root),
               'session_id': session_id, 'plan_digest': evaluation['adopted_plan_digest'],
               'tasks': prepared, 'before': {p: _file_hash(root, p) for p in sorted(paths)},
               'skill': skill_identity(), 'boundary': {'sandbox': False, 'managed_loader': None,
               'provider_used': False, 'python_effects_rollback': False}}
    return {**payload, 'preview_digest': digest(payload)}


class LocalAgentTaskExecutor:
    """Explicit confirmed bundle injected into start/resume_product_run."""
    executor_id = 'local-structured-python-product-loop'

    def __init__(self, root: str | Path, preview: Mapping[str, Any], *,
                 expected_digest: str, confirmation_id: str, confirmed: bool,
                 synthetic: bool = True, ttl_seconds: int = 300):
        if confirmed is not True or type(synthetic) is not bool:
            raise AgentAdapterError('confirmation_required', '执行必须明确确认；合成确认必须标注')
        if not isinstance(confirmation_id, str) or not confirmation_id.strip():
            raise AgentAdapterError('confirmation_required', '确认标识不能为空')
        if type(ttl_seconds) is not int or not 0 < ttl_seconds <= 3600:
            raise AgentAdapterError('expiry_invalid', '授权最多有效一小时')
        self.root = _root_path(Path(root))
        self.preview = deepcopy(dict(preview))
        current = preview_agent_tasks(self.root, self.preview['session_id'], self.preview['tasks'])
        if current != self.preview or expected_digest != current['preview_digest']:
            raise AgentAdapterError('preview_drifted', '计划、源码或文件已改变，请重新预览确认')
        self.adapter = LocalAgentAdapter(root)
        self.confirmation_id = confirmation_id
        self.synthetic = synthetic
        self.expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        self.expected_files = dict(current['before'])
        # Nonce/expiry in each authorization are frozen once for this object.
        self.specs = {}
        for task_id, actions in self.preview['tasks'].items():
            self.specs[task_id] = [(action, self.adapter.authorization_for(action,
                scope=('synthetic' if synthetic else 'caller-confirmed') + '-product-preview:' + expected_digest,
                confirmation_id=confirmation_id + ':' + task_id + ':' + str(index), ttl_seconds=ttl_seconds))
                for index, action in enumerate(actions)]

    def validate(self, root: Path, session_id: str, plan_digest: str) -> None:
        if root != self.root or session_id != self.preview['session_id'] or plan_digest != self.preview['plan_digest']:
            raise AgentAdapterError('executor_binding_invalid', '执行器与根目录/会话/采用计划不匹配')
        if digest({k: v for k, v in self.preview.items() if k != 'preview_digest'}) != self.preview['preview_digest']:
            raise AgentAdapterError('preview_drifted', '预览对象已改变')
        if datetime.fromisoformat(self.expires_at) <= datetime.now(timezone.utc):
            raise AgentAdapterError('authorization_expired', '预览确认已过期')
        if skill_identity() != self.preview['skill']:
            raise AgentAdapterError('source_drifted', '确认后的执行源码/skill 已变化')
        for path, expected in self.expected_files.items():
            if _file_hash(self.root, path) != expected:
                raise AgentAdapterError('preview_drifted', '确认后的文件已变化：' + path)

    def __call__(self, root: Path, task: Mapping[str, Any], *, run_id: str, attempt: int, checkpoint) -> dict[str, Any]:
        self.validate(root, self.preview['session_id'], self.preview['plan_digest'])
        task_id = task['id']
        if task_id not in self.specs:
            return {'status': 'blocked_capability', 'errors': ['task_actions_missing'],
                    'human_gate': {'required': True, 'recorded': False, 'reason': '请由 Agent 提供该任务动作并预览确认'}}
        receipts = []
        test_count = None
        for index, (action, authorization) in enumerate(self.specs[task_id]):
            self.validate(root, self.preview['session_id'], self.preview['plan_digest'])
            before_exists = False
            if action['type'] == 'write_text':
                before_exists = safe_target(self.root, action['path'], allow_missing=True).exists()
            event = {'action': action, 'authorization': authorization, 'preview_digest': self.preview['preview_digest'],
                     'confirmation_id': self.confirmation_id, 'synthetic_confirmation': self.synthetic,
                     'skill': self.preview['skill'], 'index': index, 'attempt': attempt,
                     'files_before': dict(self.expected_files), 'before_exists': before_exists}
            checkpoint({**event, 'phase': 'dispatching'})
            try:
                receipt = self.adapter.execute(action, authorization=authorization,
                    data_scope='synthetic-local-documents' if self.synthetic else 'local',
                    run_id=f'{run_id}-{task_id}-{attempt}-{index}', target_platform=task.get('action', {}).get('platform'),
                    transactional_write=action['type'] == 'write_text',
                    expected_before_sha256=self.expected_files.get(action.get('path')))
            except (AgentAdapterError, ValueError) as error:
                checkpoint({**event, 'phase': 'rejected', 'error': getattr(error, 'code', type(error).__name__)})
                return {'status': 'failed', 'errors': [getattr(error, 'code', type(error).__name__)], 'evidence': receipts}
            if action['type'] == 'write_text' and receipt['status'] == 'succeeded':
                self.expected_files[action['path']] = sha256_bytes(action['content'].encode('utf-8'))
            if action['type'] in {'python_module', 'node_script'}:
                combined = receipt.get('stdout_summary', '') + "\n" + receipt.get('stderr_summary', '')
                match = re.search(r'\bRan (\d+) tests? in |OPENCODING_TESTS_RUN=(\d+)', combined)
                test_count = int(match[1] or match[2]) if match else 0
                receipt['tests_run'] = test_count
            checkpoint({**event, 'phase': 'completed', 'receipt': receipt})
            receipts.append(receipt)
            self.validate(root, self.preview['session_id'], self.preview['plan_digest'])
            if receipt['status'] != 'succeeded' or (action['type'] in {'python_module', 'node_script'} and not test_count):
                return {'status': 'failed', 'errors': ['test_failed_or_empty' if action['type'] in {'python_module', 'node_script'} else 'write_failed'], 'evidence': receipts}
        return {'status': 'succeeded', 'errors': [], 'evidence': receipts,
                'executor_id': self.executor_id, 'test': 'node_local_web' if any(a.get('type') == 'node_script' for a in self.preview['tasks'].get(task_id, [])) else ('python_unittest' if test_count is not None else 'transactional_write'), 'preview_digest': self.preview['preview_digest'],
                'tests_run': test_count, 'synthetic_confirmation': self.synthetic,
                'skill': self.preview['skill']}


__all__ = ['LocalAgentTaskExecutor', 'preview_agent_tasks']
