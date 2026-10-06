"""Offline installed-wheel journeys for the combined preview and evidence APIs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
import zipfile

from tests.test_product_cli import inventory
from tests.test_product_packaging import (
    CREATE_NO_WINDOW, DIST_INFO_FILES, PACKAGE_FILES, ROOT,
    _copy_fixture, _environment, _retained_workspace, _run,
)


_CREATE_RETAINED_PREVIEW = """
import json,sys
from opencoding.intake import QUESTION_DEFINITIONS
from opencoding.service import create_session,submit_answer
from opencoding.taskplan_scheduler import preview_task_plan
root=sys.argv[1]
view=create_session(root,'安装后的离线借还登记')
answers={'audience':'社区居民','platform':'网页','outcome':'登记借用并确认归还','data_persistence':'需要'}
for question in QUESTION_DEFINITIONS:
    view=submit_answer(root,view['session']['id'],view['session']['revision'],question['id'],answers.get(question['id'],'不需要'))
print(json.dumps(preview_task_plan(root,view['session']['id']),ensure_ascii=False))
"""

_ORIGINS = """
import hashlib,importlib,json,pathlib,sys
prefix=pathlib.Path(sys.prefix).resolve()
result={}
for name in ('cli','taskplan_scheduler','taskplan_evidence'):
    module=importlib.import_module('opencoding.'+name)
    path=pathlib.Path(module.__file__).resolve()
    assert path.is_relative_to(prefix), (name,path,prefix)
    assert 'site-packages' in path.parts
    result[name]={'origin':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
print(json.dumps(result))
"""

_INSTALLED_PROJECTION = """
import json,sys
from contextlib import ExitStack
from copy import deepcopy
from unittest import mock
from opencoding.executor import action_digest
from opencoding.safety import canonical_json,sha256_bytes
from opencoding.scheduler import _receipt_digest
from opencoding.taskplan_evidence import project_taskplan_evidence

# The original preview was captured earlier by the installed public API.
# Expected values are separately retained by the test driver, not defaulted by
# the projection. The successful-looking snapshot below is explicitly synthetic.
retained=json.loads(sys.stdin.read())
preview=retained['preview']
root=retained['expected_root']
empty={'schema_version':'1.0','root':root,'status':'not_initialized','tasks':[],'runs':[]}
args={'expected_root':root,'expected_preview_digest':retained['expected_preview_digest'],
      'original_preview':preview,'scheduler_snapshot':empty,'transaction_evidence':{},'file_evidence':{}}
snapshot={'schema_version':'1.0','root':root,'status':'ready','tasks':[],'runs':[]}
transactions,files={},{}
for index,original in enumerate(preview['tasks']):
    run_id='run-installed-fixture-'+str(index)
    tx_id='tx-20261006T000000000000Z-'+format(index,'012x')
    artifacts=[]
    if original['action']['type']=='document':
        for entry in original['action']['plan']['entries']:
            artifacts.append({'path':entry['path'],'sha256':entry['after_sha256'],'sha256_kind':'file_bytes','transaction_id':tx_id})
            files[entry['path']]={'root':root,'path':entry['path'],'content_bytes':entry['content'].encode('utf-8'),'acquisition_error':None}
        transactions[tx_id]={'root':root,'transaction_id':tx_id,'raw_files':{'receipt.json':b'{"status":"applied"}'},'acquisition_error':None}
    task={**deepcopy(original),'schema_version':'1.0','timeout_seconds':30.0,'state':'succeeded','attempt':1,'last_run_id':run_id,'last_error':'succeeded'}
    receipt={'schema_version':'1.0','run_id':run_id,'task_id':original['task_id'],'attempt':1,
      'input_sha256':sha256_bytes(canonical_json(original['input'])),'action_digest':action_digest(original['action']),
      'status':'succeeded','exit_code':0,'timed_out':False,'cancelled':False,'stdout_summary':'private fixture body',
      'stderr_summary':'','artifacts':artifacts,'reason':'succeeded'}
    receipt['receipt_digest']=_receipt_digest(receipt)
    snapshot['tasks'].append(task)
    snapshot['runs'].append({**deepcopy(receipt),'started_at':'2026-10-06T00:00:00+00:00',
        'finished_at':'2026-10-06T00:00:01+00:00','receipt':receipt})
captured={**args,'scheduler_snapshot':snapshot,'transaction_evidence':transactions,'file_evidence':files}
before=deepcopy(captured)
with ExitStack() as stack:
    calls=[]
    for target in ('builtins.open','pathlib.Path.read_bytes','pathlib.Path.read_text','pathlib.Path.resolve',
                   'os.open','os.stat','os.lstat','os.scandir','sqlite3.connect','subprocess.Popen',
                   'opencoding.scheduler.Scheduler.__init__','opencoding.service.preview_session',
                   'opencoding.taskplan_scheduler.preview_task_plan','opencoding.taskplan_scheduler.execute_task_plan',
                   'opencoding.transactions._load_receipt_manifest','opencoding.transactions._cooperative_lock'):
        calls.append(stack.enter_context(mock.patch(target,side_effect=AssertionError(target))))
    absent=project_taskplan_evidence(**args)
    projected=project_taskplan_evidence(**captured)
    bad_binding=project_taskplan_evidence(**{**captured,'expected_preview_digest':'0'*64})
    bad_snapshot=project_taskplan_evidence(**{**captured,'scheduler_snapshot':None})
    forged=deepcopy(captured)
    next(iter(forged['file_evidence'].values()))['trusted_validated']=True
    bad_capture=project_taskplan_evidence(**forged)
    assert captured==before
    for call in calls:
        call.assert_not_called()
assert absent['binding']['status']=='matched'
assert absent['scheduler']['status']=='not_initialized'
assert projected['binding']['status']=='matched'
for result in (absent,projected,bad_snapshot,bad_capture):
    assert not result['live_verified'] and result['activation_status']=='not_activated'
    for task in result['tasks']:
        if task['classification']!='host_missing':
            assert task['evidence_status']=='unverified'
            assert 'strict_transaction_validator_unavailable' in task['reason_codes']
assert any(output['status']=='byte_match' for task in projected['tasks'] for output in task['outputs'])
assert bad_binding['binding']['status']=='rejected' and bad_binding['tasks']==[]
assert bad_snapshot['binding']['status']=='matched' and bad_snapshot['scheduler']['reason_codes']==['snapshot_invalid']
assert bad_capture['binding']['status']=='matched' and bad_capture['capture_reason_codes']==['capture_invalid']
serialized=json.dumps(projected,ensure_ascii=False)
assert root not in serialized and 'private fixture body' not in serialized and 'observed' not in serialized
print(json.dumps({'absent_status':absent['scheduler']['status'],'binding':projected['binding']['status'],
    'scope':'synthetic-memory-capture','documents_unverified':True,'live_verified':False,
    'bad_binding':bad_binding['binding']['status'],'bad_snapshot':bad_snapshot['scheduler']['reason_codes'],
    'bad_capture':bad_capture['capture_reason_codes'],'io_calls':0}))
"""


class InstalledTaskPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = _retained_workspace("combined-taskplan")
        scratch = cls.workspace / "source-fixture"
        scratch.mkdir()
        _copy_fixture(ROOT, scratch)
        dist = cls.workspace / "dist"
        dist.mkdir()
        build = "import setuptools.build_meta as backend,sys; print(backend.build_wheel(sys.argv[1]))"
        _run([sys.executable, "-I", "-B", "-X", "utf8", "-c", build, str(dist)],
             cwd=scratch, workspace=cls.workspace, label="combined-wheel-build")
        cls.wheel = next(dist.glob("*.whl"))
        cls.outside = cls.workspace / "outside-source"
        cls.outside.mkdir()
        venv = cls.workspace / "venv"
        _run([sys.executable, "-I", "-B", "-X", "utf8", "-m", "venv", str(venv)],
             cwd=cls.outside, workspace=cls.workspace, label="combined-venv")
        cls.python = venv / "Scripts" / "python.exe"
        cls.console = venv / "Scripts" / "opencoding.exe"
        if not cls.python.exists():
            cls.python, cls.console = venv / "bin" / "python", venv / "bin" / "opencoding"
        _run([str(cls.python), "-I", "-B", "-X", "utf8", "-m", "pip", "install", "--no-index", "--no-deps", "--no-cache-dir", "--no-compile", str(cls.wheel)],
             cwd=cls.outside, workspace=cls.workspace, label="combined-install")
        origin = _run([str(cls.python), "-I", "-B", "-X", "utf8", "-c", _ORIGINS],
                      cwd=cls.outside, workspace=cls.workspace, label="combined-origins")
        cls.origins = json.loads(origin.stdout)
        cls.projects = cls.workspace / "projects"
        cls.projects.mkdir()
        cls.project = cls.projects / "authorized"
        cls.project.mkdir()
        (cls.project / "empty-directory").mkdir()
        (cls.project / "sentinel.bin").write_bytes(b"unchanged\x00\xff")
        result = _run([str(cls.python), "-I", "-B", "-X", "utf8", "-c", _CREATE_RETAINED_PREVIEW, str(cls.project)],
                      cwd=cls.outside, workspace=cls.workspace, label="combined-retained-preview")
        cls.preview = json.loads(result.stdout)
        cls.session_id = cls.preview["session"]["id"]
        cls.session_path = cls.project / ".opencoding" / "sessions" / (cls.session_id + ".json")
        cls.sequence = 0

    def command(self, command, *, text="确认\n", expected_exit=0):
        type(self).sequence += 1
        label = f"installed-journey-{self.sequence:03d}"
        environment = _environment(self.workspace)
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        before = inventory(self.projects)
        result = subprocess.run(command, cwd=self.outside, env=environment, input=text,
                                text=True, encoding="utf-8", capture_output=True, check=False,
                                creationflags=CREATE_NO_WINDOW, timeout=120)
        log = self.workspace / "logs" / label
        log.with_suffix(".command.json").write_text(json.dumps(command, indent=2) + "\n", encoding="utf-8")
        log.with_suffix(".stdout.log").write_text(result.stdout, encoding="utf-8")
        log.with_suffix(".stderr.log").write_text(result.stderr, encoding="utf-8")
        log.with_suffix(".exit.txt").write_text(str(result.returncode) + "\n", encoding="utf-8")
        self.assertEqual(result.returncode, expected_exit, result.stderr)
        self.assertEqual(inventory(self.projects), before)
        return result

    def cli(self, mode, *arguments, root=None, expected_exit=0):
        command = [str(self.console)] if mode == "console" else [str(self.python), "-I", "-B", "-X", "utf8", "-m", "opencoding"]
        return self.command([*command, "--root", str(self.project if root is None else root), *arguments], expected_exit=expected_exit)

    def test_wheel_exact_modules_readme_and_installed_origins(self):
        with zipfile.ZipFile(self.wheel) as archive:
            names = set(archive.namelist())
            self.assertEqual(len(names), len(archive.namelist()))
            dist_info = next(name.rsplit("/", 1)[0] for name in names if name.endswith(".dist-info/METADATA"))
            self.assertEqual(names, set(PACKAGE_FILES) | {f"{dist_info}/{name}" for name in DIST_INFO_FILES})
            for relative in PACKAGE_FILES:
                self.assertEqual(archive.read(relative), (ROOT / relative).read_bytes())
            metadata = archive.read(dist_info + "/METADATA").decode("utf-8")
            self.assertEqual(metadata.split("\n\n", 1)[1].strip(), (ROOT / "docs/product/OFFLINE_INSTALLATION.md").read_text(encoding="utf-8").strip())
            for document in ("TASKPLAN_EVIDENCE.md", "TASKPLAN_SCHEDULER.md", "AGENT_NATIVE_USE.md"):
                self.assertFalse(any(name.endswith(document) for name in names))
        for name in ("cli", "taskplan_scheduler", "taskplan_evidence"):
            installed = self.origins[name]
            self.assertNotIn(str(ROOT), installed["origin"])
            self.assertEqual(installed["sha256"], hashlib.sha256((ROOT / "opencoding" / (name + ".py")).read_bytes()).hexdigest())

    def test_module_and_console_preview_match_retained_api_and_preserve_project(self):
        for mode in ("module", "console"):
            with self.subTest(mode=mode):
                for _ in range(2):
                    structured = self.cli(mode, "--task-preview", self.session_id, "--json")
                    self.assertEqual(json.loads(structured.stdout), self.preview)
                    self.assertEqual(structured.stderr, "")
                human = self.cli(mode, "--task-preview", self.session_id)
                for text in (self.session_id, "未执行", "仅表示方案状态", "预计效果", "offline_design", "host_missing"):
                    self.assertIn(text, human.stdout)
                self.assertFalse((self.project / ".opencoding" / "scheduler").exists())

    def test_installed_errors_and_legacy_status_protocol_are_zero_write(self):
        missing = self.projects / "missing-root"
        for mode in ("module", "console"):
            status = self.cli(mode, "--status", "--json")
            self.assertEqual(json.loads(status.stdout), {"schema_version": "1.0", "root": str(self.project), "status": "not_initialized", "tasks": [], "runs": []})
            bad_status = self.cli(mode, "--status", "--json", root=missing, expected_exit=2)
            self.assertEqual(json.loads(bad_status.stderr), {"error": {"code": "invalid_root"}})
            for root, session in ((missing, self.session_id), (self.project, "missing-session"), (self.project, "")):
                error = self.cli(mode, "--task-preview", session, "--json", root=root, expected_exit=2)
                self.assertEqual(error.stdout, "")
                self.assertEqual(set(json.loads(error.stderr)), {"error"})
            conflict = self.cli(mode, "--task-preview", self.session_id, "--status", expected_exit=2)
            self.assertEqual(conflict.stdout, "")
            self.assertIn("not allowed", conflict.stderr)
        original = self.session_path.read_bytes()
        try:
            self.session_path.write_bytes(b"invalid-session-json")
            for mode in ("module", "console"):
                error = self.cli(mode, "--task-preview", self.session_id, "--json", expected_exit=2)
                self.assertEqual(json.loads(error.stderr), {"error": {"code": "session_read_failed"}})
        finally:
            self.session_path.write_bytes(original)
        self.assertFalse(missing.exists())

    def test_installed_pure_projection_of_retained_preview_and_explicit_memory_captures(self):
        retained = {"expected_root": self.preview["root"], "expected_preview_digest": self.preview["service_digest"], "preview": self.preview}
        result = self.command([str(self.python), "-I", "-B", "-X", "utf8", "-c", _INSTALLED_PROJECTION],
                              text=json.dumps(retained, ensure_ascii=False))
        self.assertEqual(json.loads(result.stdout), {
            "absent_status": "not_initialized", "binding": "matched", "scope": "synthetic-memory-capture",
            "documents_unverified": True, "live_verified": False, "bad_binding": "rejected",
            "bad_snapshot": ["snapshot_invalid"], "bad_capture": ["capture_invalid"], "io_calls": 0})

    def test_installed_preview_preserves_symlink_targets_and_guard_bytes(self):
        outside = self.projects / "outside-sentinel.bin"
        outside.write_bytes(b"outside-stays-unchanged")
        link = self.project / "unrelated-link"
        try:
            link.symlink_to(outside)
        except OSError as error:
            self.skipTest("symlink creation unavailable: " + str(error))
        guard = self.project / ".opencoding" / ".session-write.lock"
        original = guard.read_bytes()
        try:
            for mode in ("module", "console"):
                self.cli(mode, "--task-preview", self.session_id, "--json")
                self.assertEqual(guard.read_bytes(), original)
                self.assertEqual(outside.read_bytes(), b"outside-stays-unchanged")
        finally:
            link.unlink()


if __name__ == "__main__":
    unittest.main()
