"""第一批安全返修的隔离反例回归测试（F03–F07/F09；R03–R20）。

每条测试对应源码复核报告的一个发现/复现编号：修复前该测试失败，
修复后必须通过。全部使用临时目录与测试剧本响应器，不触网、不触真实项目。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
import unittest.mock
import uuid
from pathlib import Path

from opencoding import cli as cli_module
from opencoding import grants, uxtext
from opencoding import autorun as autorun_module
from opencoding import sandbox as sandbox_module
from opencoding import service as service_module
from opencoding.aiadapter import MockAdapter
from opencoding.autorun import LENDREG_SCENARIO, AutorunError
from tests import REVIEWED_FIXTURE


def run_batch(*args, **kwargs):
    """N01-a：受信任合成测试显式绑定已审查夹具身份（不再使用环境变量豁免）。"""

    kwargs.setdefault("trusted_fixture", REVIEWED_FIXTURE)
    from opencoding.autorun import run_batch as _run_batch_impl

    return _run_batch_impl(*args, **kwargs)

from tests.test_product_autorun import APP, INIT, MODELS, MockResponder, TASK_FILES


def make_grant(root: Path):
    return grants.issue_batch_grant(
        root,
        goal="安全反例测试批次",
        allowed_paths=["lendreg", "tests", "scripts", "data", "RECOVERY.md"],
        action_kinds=["local_write", "local_run", "ai_request"],
        issued_by="安全回归测试替身",
        budget={"max_ai_requests": 40, "max_repair_rounds": 12},
    )


def scenario(*task_ids: str) -> dict:
    tasks = [task for task in LENDREG_SCENARIO["tasks"] if task["task_id"] in task_ids]
    return {
        "name": "安全反例迷你场景",
        "goal": LENDREG_SCENARIO["goal"],
        "tasks": tasks,
        "verifiers": LENDREG_SCENARIO["verifiers"],
    }


def load_ledger(root: Path, run_id: str) -> dict:
    return json.loads((root / ".opencoding" / "autoruns" / (run_id + ".json")).read_text(encoding="utf-8"))


def save_ledger(root: Path, run_id: str, ledger: dict) -> None:
    path = root / ".opencoding" / "autoruns" / (run_id + ".json")
    path.write_text(json.dumps(ledger, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def response_for_request(run_id: str, request: dict) -> dict:
    adapter = MockAdapter(responder=MockResponder())
    result = adapter.complete(
        [{"role": "user", "content": "任务：x（d01-models）"}],
        request_kind=request.get("kind", "implement"),
        run_id=run_id,
        task_id=D01,
        attempt=request["attempt"],
        request_id=request["request_id"],
        nonce=request["nonce"],
        input_digest=request["input_digest"],
        allowed_outputs=request["allowed_outputs"],
    )
    return {
        **result,
        "bound_request_id": request["request_id"],
        "bound_run_id": run_id,
        "bound_task_id": D01,
        "bound_attempt": request["attempt"],
        "bound_input_digest": request["input_digest"],
    }


D01 = "d01-models"


class CancelAfterFirstResponse:
    """在第一次模型响应返回前调用 cancel_run，模拟用户在请求途中取消。"""

    def __init__(self, root: Path, run_id: str):
        self.root = root
        self.run_id = run_id
        self.inner = MockResponder()

    def __call__(self, messages, *, request_kind, nonce):
        result = self.inner(messages, request_kind=request_kind, nonce=nonce)
        if request_kind == "implement":
            autorun_module.cancel_run(self.root, self.run_id, reason="检查点测试取消")
        return result


class OutOfScopeResponder:
    """返回一个合法文件 + 一个越权文件的多文件候选（R04 原样反例）。"""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, messages, *, request_kind, nonce):
        self.calls.append("d01")
        return json.dumps({
            "nonce": nonce,
            "summary": "含越权文件的候选",
            "files": [
                {"path": "lendreg/models.py", "content": MODELS},
                {"path": "REVENGE.md", "content": "越权写入"},
            ],
        }, ensure_ascii=False)


class R16BadReturnResponder:
    """d06 返回"重复新增不拒绝"的坏 app.py；其余任务返回剧本（R16）。"""

    def __init__(self):
        self.inner = MockResponder()

    def __call__(self, messages, *, request_kind, nonce):
        result = self.inner(messages, request_kind=request_kind, nonce=nonce)
        if request_kind == "implement" and "（d06-return）" in messages[-1]["content"]:
            payload = json.loads(result)
            broken = APP.replace('if item_id in data["items"]:', 'if False and item_id in data["items"]:')
            payload["files"] = [{"path": "lendreg/app.py", "content": broken}]
            return json.dumps(payload, ensure_ascii=False)
        return result


class EscapedInitResponder:
    """d01 候选的 __init__.py 在被导入时向工作目录上层写标记文件（R17）。"""

    def __init__(self):
        self.inner = MockResponder()

    def __call__(self, messages, *, request_kind, nonce):
        result = self.inner(messages, request_kind=request_kind, nonce=nonce)
        if request_kind == "implement" and "（d01-models）" in messages[-1]["content"]:
            payload = json.loads(result)
            payload["files"] = [
                {"path": "lendreg/__init__.py", "content": INIT + '\nopen(__import__("os").path.join("..", "escaped.marker"), "w").write("escape-test")\n'},
                {"path": "lendreg/models.py", "content": MODELS},
            ]
            return json.dumps(payload, ensure_ascii=False)
        return result


class BadContentResponder:
    """前 N 次对指定任务返回"路径合法但必然验收失败"的候选（R06）。

    与 MockResponder 的 broken 注入不同：坏内容放在任务允许产物路径内，
    走正常的"验收失败 → 修复 → 冻结"路径，而不是被候选校验直接拒绝。
    """

    def __init__(self, bad_rounds: dict[str, int]):
        self._bad = dict(bad_rounds)
        self.calls: list[str] = []
        self._inner = MockResponder()

    def __call__(self, messages, *, request_kind, nonce):
        if request_kind != "implement":
            return json.dumps({"nonce": nonce, "summary": "s", "files": []})
        text = messages[-1]["content"]
        match = re.search(r"（(d\d{2}-[a-z-]+)）", text)
        task_id = match.group(1) if match else "unknown"
        self.calls.append(task_id)
        bad_round = self._bad.get(task_id)
        if bad_round is not None and self.calls.count(task_id) <= bad_round:
            return json.dumps({
                "nonce": nonce,
                "summary": "坏实现",
                "files": [{"path": "lendreg/models.py", "content": "raise ValueError('broken candidate')\n"}],
            }, ensure_ascii=False)
        return self._inner(messages, request_kind=request_kind, nonce=nonce)


class InterruptedRepairResponder:
    def __init__(self):
        self.calls = 0
        self.repair_message = ""
        self.inner = MockResponder()

    def __call__(self, messages, *, request_kind, nonce):
        self.calls += 1
        prompt = messages[-1]["content"]
        if self.calls == 1:
            payload = json.loads(self.inner(messages, request_kind=request_kind, nonce=nonce))
            payload["files"] = [
                {"path": "lendreg/__init__.py", "content": INIT},
                {"path": "lendreg/models.py", "content": "def new_item(*args): return {}\ndef new_loan(*args): return {}\n"},
            ]
            return json.dumps(payload, ensure_ascii=False)
        if self.calls == 2:
            raise RuntimeError("synthetic interruption during repair dispatch")
        self.repair_message = prompt
        return self.inner(messages, request_kind=request_kind, nonce=nonce)


class SecurityRegressionTests(unittest.TestCase):
    def test_d01_non_object_output_and_missing_business_functions_freeze_without_escape(self):
        class InvalidD01Responder:
            def __call__(self, messages, *, request_kind, nonce):
                if request_kind != "implement":
                    return MockResponder()(messages, request_kind=request_kind, nonce=nonce)
                return json.dumps({
                    "nonce": nonce,
                    "summary": "无业务函数",
                    "files": [
                        {"path": "lendreg/__init__.py", "content": ""},
                        {"path": "lendreg/models.py", "content": "print([])\n"},
                    ],
                })

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=InvalidD01Responder()), run_id="run-d01-invalid",
            )
            self.assertEqual(summary["states"][D01], "frozen")
            self.assertFalse(summary["deliverable"])
            self.assertFalse((root / "lendreg" / "models.py").exists())
            self.assertGreater(
                grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"],
                0,
            )

    def test_d01_controller_does_not_accept_candidate_printed_result_rows(self):
        class NoisyD01Responder(MockResponder):
            def __call__(self, messages, *, request_kind, nonce):
                result = super().__call__(messages, request_kind=request_kind, nonce=nonce)
                if request_kind == "implement" and "d01-models" in messages[-1]["content"]:
                    payload = json.loads(result)
                    for entry in payload["files"]:
                        if entry["path"] == "lendreg/models.py":
                            entry["content"] = (
                                "def new_item(item_id, name):\n"
                                "    print('[]')\n"
                                "    return {'id': item_id, 'name': name, 'status': 'available'}\n"
                                "def new_loan(loan_id, item_id, borrower):\n"
                                "    return {'id': loan_id, 'item_id': item_id, 'closed': False}\n"
                            )
                    return json.dumps(payload, ensure_ascii=False)
                return result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=NoisyD01Responder()), run_id="run-d01-noisy",
            )
            self.assertEqual(summary["states"][D01], "frozen")
            self.assertFalse(summary["deliverable"])

    def test_status_does_not_hide_corrupted_recovery_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run_id = "run-corrupt-recovery"
            ledger_dir = root / ".opencoding" / "autoruns"
            recovery_dir = root / ".opencoding" / "dev-runs" / run_id / "recovery"
            ledger_dir.mkdir(parents=True)
            recovery_dir.mkdir(parents=True)
            (ledger_dir / (run_id + ".json")).write_text(json.dumps({
                "schema_version": autorun_module.AUTORUN_SCHEMA_VERSION,
                "run_id": run_id,
                "started_at": "2026-09-24T00:00:00Z",
                "tasks": {"d01-models": {"state": "succeeded", "requests": [{"attempt": 1}], "attempts": [{"attempt": 1}]}}
            }), encoding="utf-8")
            (recovery_dir / "d01-models-1-tx.json").write_text("{broken", encoding="utf-8")
            with self.assertRaises(AutorunError) as caught:
                autorun_module.read_status_snapshot(root)
            self.assertEqual(caught.exception.code, "autorun_status_store_invalid")

    def test_status_attempt_is_derived_from_requests_not_reverification_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run_id = "run-request-vs-event"
            ledger_dir = root / ".opencoding" / "autoruns"
            ledger_dir.mkdir(parents=True)
            (ledger_dir / (run_id + ".json")).write_text(json.dumps({
                "schema_version": autorun_module.AUTORUN_SCHEMA_VERSION,
                "run_id": run_id,
                "started_at": "2026-09-24T00:00:00Z",
                "tasks": {"d01-models": {
                    "state": "succeeded",
                    "requests": [{"attempt": 1, "request_id": "req-1", "kind": "implement"}],
                    "attempts": [{"attempt": 2, "kind": "reverify", "verification_ok": True}],
                }}
            }), encoding="utf-8")
            snapshot = autorun_module.read_status_snapshot(root)
            self.assertEqual(snapshot["runs"][0]["attempt"], 1)
            self.assertEqual(snapshot["tasks"][0]["attempt"], 1)
            self.assertEqual(snapshot["events"][0]["event_type"], "request")

    def test_status_rejects_legal_but_empty_rollback_evidence_for_success(self):
        for malformed in ("[]", "{}"):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                run_id = "run-empty-recovery"
                ledger_dir = root / ".opencoding" / "autoruns"
                recovery_dir = root / ".opencoding" / "dev-runs" / run_id / "recovery"
                ledger_dir.mkdir(parents=True)
                recovery_dir.mkdir(parents=True)
                (ledger_dir / (run_id + ".json")).write_text(json.dumps({
                    "schema_version": autorun_module.AUTORUN_SCHEMA_VERSION,
                    "run_id": run_id,
                    "started_at": "2026-09-25T00:00:00Z",
                    "tasks": {"d01-models": {
                        "state": "succeeded",
                        "file_hashes": {"lendreg/models.py": "a" * 64},
                        "attempts": [{"attempt": 1, "committed": True}],
                        "requests": [{"attempt": 1}],
                    }},
                }), encoding="utf-8")
                (recovery_dir / "d01-models-1-tx.json").write_text(malformed, encoding="utf-8")
                with self.assertRaises(AutorunError) as caught:
                    autorun_module.read_status_snapshot(root)
                self.assertEqual(caught.exception.code, "autorun_status_store_invalid")
    def test_f08_d01_spoofed_early_exit_freezes_and_valid_models_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)

            def spoofed(messages, *, request_kind, nonce):
                return json.dumps({
                    "nonce": nonce,
                    "summary": "仅打印成功标记",
                    "files": [
                        {"path": "lendreg/__init__.py", "content": ""},
                        {
                            "path": "lendreg/models.py",
                            "content": "print('D01 OK')\nraise SystemExit(0)\n",
                        },
                    ],
                }, ensure_ascii=False)

            rejected = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=spoofed), run_id="run-f08-d01-spoof",
            )
            self.assertEqual(rejected["states"][D01], "frozen")
            self.assertEqual(rejected["succeeded"], 0)
            self.assertFalse(rejected["deliverable"])
            self.assertTrue((root / "lendreg" / "models.py").exists() is False)

            accepted = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-f08-d01-valid",
            )
            self.assertEqual(accepted["states"][D01], "succeeded")
            self.assertEqual(accepted["succeeded"], 1)
            self.assertTrue(accepted["deliverable"])

    def test_f08_declared_test_and_recovery_artifacts_must_be_executable_and_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for mapping in TASK_FILES.values():
                for entry in mapping:
                    path = root / entry["path"]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(entry["content"], encoding="utf-8")
            (root / "tests" / "test_borrow_guard.py").write_text("class TestBroken:\n  ???\n", encoding="utf-8")
            (root / "tests" / "test_restart_persistence.py").write_text("class TestBroken:\n  ???\n", encoding="utf-8")
            (root / "scripts" / "full_flow.py").write_text("def main(:\n", encoding="utf-8")
            autorun_module._set_trusted_fixture(root, REVIEWED_FIXTURE)
            try:
                self.assertFalse(autorun_module._verify_python_test_artifact(
                    root, "tests/test_borrow_guard.py", "test_borrow_guard"
                )[0])
                self.assertFalse(autorun_module._verify_python_test_artifact(
                    root, "tests/test_restart_persistence.py", "test_restart_persistence"
                )[0])

                zero_test_script = (
                    "import subprocess, unittest\n"
                    "class TestNeverRun(unittest.TestCase):\n"
                    "    def test_assertion(self):\n"
                    "        self.assertTrue(subprocess.run([\"python\", \"-c\", \"pass\"]).returncode == 0)\n"
                    "print('Ran 0 tests in 0.000s')\n"
                )
                (root / "tests" / "test_borrow_guard.py").write_text(zero_test_script, encoding="utf-8")
                self.assertFalse(autorun_module._verify_python_test_artifact(
                    root, "tests/test_borrow_guard.py", "test_borrow_guard"
                )[0])
                self.assertFalse(autorun_module._verify_d10_full_flow(root)[0])
                (root / "scripts" / "full_flow.py").write_text(TASK_FILES["d10-full-flow"][0]["content"], encoding="utf-8")
                (root / "RECOVERY.md").write_text("", encoding="utf-8")
                self.assertFalse(autorun_module._verify_d10_full_flow(root)[0])
            finally:
                autorun_module._TRUSTED_FIXTURES.pop(str(root), None)

    # R03：取消发生在"接收模型结果之后、候选落地之前"→ 任何文件都不得写入。
    def test_r03_cancel_after_response_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            responder = CancelAfterFirstResponse(root, "run-sec-cancel")
            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=responder), run_id="run-sec-cancel",
            )
            self.assertEqual(summary["states"][D01], "cancelled")
            self.assertFalse((root / "lendreg" / "models.py").exists())
            self.assertFalse((root / "lendreg" / "__init__.py").exists())
            self.assertTrue((root / ".opencoding" / "autoruns" / "run-sec-cancel.cancel.json").exists())

    # R03/R07 接续：已取消运行不得自动复活，也不得补写候选。
    def test_r03_cancelled_run_does_not_revive_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=CancelAfterFirstResponse(root, "run-sec-cancel2")),
                run_id="run-sec-cancel2",
            )
            fresh = MockResponder()
            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=fresh), run_id="run-sec-cancel2",
            )
            self.assertEqual(summary["states"][D01], "cancelled")
            self.assertEqual(fresh.calls, [])
            self.assertFalse((root / "lendreg" / "models.py").exists())

    # R03b：提交中授权被撤销 → 写入必须整体回滚，账本不得记成功。
    def test_r03b_revoke_mid_commit_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            original_apply = autorun_module.transactions.apply_changes

            def revoked_apply(project, plan, approved_digest):
                grants.revoke_batch_grant(project, grant["grant_id"], reason="测试：提交中撤销")
                return original_apply(project, plan, approved_digest=approved_digest)

            autorun_module.transactions.apply_changes = revoked_apply
            try:
                summary = run_batch(
                    root, scenario(D01), grant_id=grant["grant_id"],
                    adapter=MockAdapter(responder=MockResponder()), run_id="run-sec-revoke",
                )
            finally:
                autorun_module.transactions.apply_changes = original_apply
            self.assertEqual(summary["states"][D01], "frozen")
            ledger = load_ledger(root, "run-sec-revoke")
            record = ledger["tasks"][D01]
            self.assertEqual(record["attempts"][-1]["committed"], False)
            self.assertEqual(record["attempts"][-1]["error_code"], "grant_revoked")
            self.assertFalse((root / "lendreg" / "models.py").exists())
            self.assertFalse((root / "lendreg" / "__init__.py").exists())

    # R04：多文件候选含越权文件 → 整体校验先于任何写入，无部分覆盖。
    def test_r04_out_of_scope_candidate_rejected_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            responder = OutOfScopeResponder()
            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=responder), run_id="run-sec-scope",
            )
            self.assertEqual(summary["states"][D01], "frozen")
            ledger = load_ledger(root, "run-sec-scope")
            record = ledger["tasks"][D01]
            self.assertEqual(record["attempts"][-1]["error_code"], "rejected_before_verification")
            self.assertEqual(record["attempts"][-1]["file_hashes"], {})
            self.assertFalse((root / "lendreg").exists())
            self.assertFalse((root / "REVENGE.md").exists())

    # R09：请求之后、提交之前用户改了既有文件 → 精确前像比对拒绝覆盖。
    def test_r09_user_edit_between_request_and_commit_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            task = next(t for t in LENDREG_SCENARIO["tasks"] if t["task_id"] == "d03-add")
            target = root / "lendreg" / "app.py"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"user before edit")
            expected = {
                "lendreg/app.py": hashlib.sha256(b"user before edit").hexdigest(),
                "lendreg/__main__.py": None,
            }
            target.write_bytes(b"user after edit")
            with self.assertRaises(AutorunError) as caught:
                autorun_module._commit_candidate(
                    root, grant, task, TASK_FILES["d03-add"],
                    "run-sec-drift", "d03-add", 1, expected=expected,
                )
            self.assertEqual(caught.exception.code, "preimage_drift")
            self.assertEqual(target.read_bytes(), b"user after edit")
            self.assertFalse((root / "lendreg" / "__main__.py").exists())

    # R05：同一 run-id 换新授权接续 → 拒绝（运行与授权持久绑定）。
    def test_r05_resume_with_new_grant_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant1 = make_grant(root)
            run_batch(
                root, scenario(D01), grant_id=grant1["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-sec-bind",
            )
            self.assertEqual(autorun_module.existing_run_grant_id(root, "run-sec-bind"), grant1["grant_id"])
            grant2 = make_grant(root)
            with self.assertRaises(AutorunError) as caught:
                run_batch(
                    root, scenario(D01), grant_id=grant2["grant_id"],
                    adapter=MockAdapter(responder=MockResponder()), run_id="run-sec-bind",
                )
            self.assertEqual(caught.exception.code, "run_grant_mismatch")

    # R05（CLI 层）：接续时原授权已撤销 → 退出码 2，且不换发新授权。
    def test_r05_cli_resume_after_revoke_exits_without_reissue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-sec-cli",
            )
            grants.revoke_batch_grant(root, grant["grant_id"], reason="测试撤销")
            grants_dir = root / ".opencoding" / "grants"
            before = sorted(path.name for path in grants_dir.glob("*.json"))
            args = argparse.Namespace(
                root=str(root), autorun="lendreg", grant_ttl=1, mock_ai=True, run_id="run-sec-cli",
            )
            exit_code = cli_module._autorun_mode(args)
            self.assertEqual(exit_code, 2)
            after = sorted(path.name for path in grants_dir.glob("*.json"))
            self.assertEqual(before, after)

    # R06：冻结任务接续不复活、尝试历史与尝试号原样保留。
    def test_r06_frozen_task_not_revived_and_history_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=BadContentResponder({D01: 3})), run_id="run-sec-frozen",
            )
            self.assertEqual(summary["states"][D01], "frozen")
            ledger = load_ledger(root, "run-sec-frozen")
            self.assertEqual([item["attempt"] for item in ledger["tasks"][D01]["attempts"]], [1, 2, 3])
            fresh = MockResponder()
            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=fresh), run_id="run-sec-frozen",
            )
            self.assertEqual(summary["states"][D01], "frozen")
            self.assertEqual(fresh.calls, [])
            ledger = load_ledger(root, "run-sec-frozen")
            self.assertEqual([item["attempt"] for item in ledger["tasks"][D01]["attempts"]], [1, 2, 3])

    # R06b：running 记录接续时尝试号按历史续计，不重置为 1。
    def test_r06b_running_attempt_number_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-sec-run",
            )
            ledger = load_ledger(root, "run-sec-run")
            ledger["tasks"][D01]["state"] = "running"
            save_ledger(root, "run-sec-run", ledger)
            fresh = MockResponder()
            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=fresh), run_id="run-sec-run",
            )
            self.assertEqual(summary["states"][D01], "succeeded")
            # N02：已保存响应优先消费，接续不重新请求适配器（原期望 fresh.calls==[D01] 随之更新）。
            self.assertEqual(fresh.calls, [], "结果已保存却被重新请求")
            ledger = load_ledger(root, "run-sec-run")
            attempts = ledger["tasks"][D01]["attempts"]
            self.assertEqual(len(attempts), 2)
            # 恢复已持久化响应沿用原请求尝试号；只有明确新重试才递增。
            self.assertEqual(attempts[-1]["attempt"], 1)

    # R10：授权原字节复制到其他项目根 → 根绑定校验拒绝。
    def test_r10_grant_copied_to_other_root_rejected(self):
        with tempfile.TemporaryDirectory() as outer:
            root_a = (Path(outer) / "a").resolve()
            root_b = (Path(outer) / "b").resolve()
            root_a.mkdir()
            root_b.mkdir()
            grant = make_grant(root_a)
            import shutil

            shutil.copytree(root_a / ".opencoding" / "grants", root_b / ".opencoding" / "grants")
            with self.assertRaises(grants.GrantError) as caught:
                grants.load_grant(root_b, grant["grant_id"])
            self.assertEqual(caught.exception.code, "grant_root_mismatch")
            with self.assertRaises(grants.GrantError):
                run_batch(
                    root_b, scenario(D01), grant_id=grant["grant_id"],
                    adapter=MockAdapter(responder=MockResponder()), run_id="run-sec-root",
                )

    # R15a：把已消费凭据的 used 标记重置 → 消费日志识破，拒绝使用。
    def test_r15_reset_used_flag_detected_by_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-sec-used",
            )
            ledger = load_ledger(root, "run-sec-used")
            credential_id = ledger["tasks"][D01]["attempts"][-1]["ai_credential_id"]
            credential_path = root / ".opencoding" / "grants" / "credentials" / (credential_id + ".json")
            payload = json.loads(credential_path.read_text(encoding="utf-8"))
            payload["used"] = False
            credential_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaises(grants.GrantError) as caught:
                grants.check_step_credential(root, credential_id, expect_task_id=D01, consume=False)
            self.assertEqual(caught.exception.code, "credential_journal_mismatch")

    # R15b：把授权的已用预算重置为 0 → 追加型日志识破，授权整体拒绝。
    def test_r15_reset_budget_detected_by_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-sec-budget",
            )
            grant_path = root / ".opencoding" / "grants" / (grant["grant_id"] + ".json")
            payload = json.loads(grant_path.read_text(encoding="utf-8"))
            payload["budget_used"]["ai_requests"] = 0
            grant_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaises(grants.GrantError) as caught:
                grants.load_grant(root, grant["grant_id"])
            self.assertEqual(caught.exception.code, "grant_budget_tampered")

    # R08：产物被合法后继任务更新 → 血缘识别为 successor，不误判用户修改。
    def test_r08_legitimate_successor_not_misjudged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            chain = scenario("d01-models", "d02-storage", "d03-add", "d04-lend", "d06-return")
            summary = run_batch(
                root, chain, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-sec-succ",
            )
            self.assertEqual(summary["succeeded"], 5, json.dumps(summary, ensure_ascii=False))
            fresh = MockResponder()
            summary = run_batch(
                root, chain, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=fresh), run_id="run-sec-succ",
            )
            self.assertEqual(summary["succeeded"], 5)
            self.assertEqual(summary["frozen"], 0)
            self.assertEqual(fresh.calls, [])

    # R12a：成功记录的 file_hashes 残缺 → 隔离重验补证，不请求 AI、不自动通过。
    def test_r12_incomplete_success_record_reverified_without_ai(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-sec-hashes",
            )
            ledger = load_ledger(root, "run-sec-hashes")
            ledger["tasks"][D01]["file_hashes"] = {}
            save_ledger(root, "run-sec-hashes", ledger)
            fresh = MockResponder()
            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=fresh), run_id="run-sec-hashes",
            )
            self.assertEqual(summary["states"][D01], "succeeded")
            self.assertEqual(fresh.calls, [])
            ledger = load_ledger(root, "run-sec-hashes")
            attempts = ledger["tasks"][D01]["attempts"]
            self.assertEqual(attempts[-1]["kind"], "reverify")
            self.assertTrue(ledger["tasks"][D01]["file_hashes"])

    # R12b：产物实际已被删除的残缺成功记录 → 重验失败，冻结而非假装成功。
    def test_r12_deleted_output_reverified_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-sec-del",
            )
            (root / "lendreg" / "models.py").unlink()
            ledger = load_ledger(root, "run-sec-del")
            ledger["tasks"][D01]["file_hashes"] = {}
            save_ledger(root, "run-sec-del", ledger)
            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-sec-del",
            )
            self.assertEqual(summary["states"][D01], "frozen")
            ledger = load_ledger(root, "run-sec-del")
            self.assertEqual(ledger["tasks"][D01]["reason"], "receipt_invalid")

    # R19/R20：候选与验收器全部在隔离副本运行 → 真实项目的数据哨兵字节不变。
    def test_r19_r20_verifier_side_effects_stay_in_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            sentinel_dir = root / "data"
            sentinel_dir.mkdir()
            sentinel = sentinel_dir / "store.json"
            # 合法结构哨兵：若验收器跑在真实项目，add 命令会改写它（字节变化可检测）。
            sentinel_bytes = json.dumps({"items": {}, "loans": []}, sort_keys=True).encode("utf-8")
            sentinel.write_bytes(sentinel_bytes)
            summary = run_batch(
                root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-sec-scratch",
            )
            self.assertEqual(summary["succeeded"], 10, json.dumps(summary, ensure_ascii=False))
            self.assertEqual(sentinel.read_bytes(), sentinel_bytes)

    # R17：候选代码的导入副作用只能落在隔离区，不得出现在真实项目根。
    def test_r17_candidate_side_effect_confined_to_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=EscapedInitResponder()), run_id="run-sec-escape",
            )
            # S01/Q06：候选向工作副本之外（含控制元数据区）写标记必须被拒绝，
            # 因此该候选无法验收通过；标记不得出现在项目内任何位置。
            self.assertEqual(summary["states"][D01], "frozen")
            self.assertFalse((root / "escaped.marker").exists())
            self.assertFalse((root / "lendreg" / "escaped.marker").exists())
            self.assertEqual(list(root.rglob("escaped.marker")), [])

    # F09a：回滚一次运行的受控写入 → 建文件删除、恢复记录闭环。
    def test_f09_rollback_full_restoration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-sec-rb1",
            )
            self.assertTrue((root / "lendreg" / "models.py").exists())
            result = autorun_module.rollback_task_files(root, "run-sec-rb1")
            self.assertEqual(result["status"], "rolled_back")
            self.assertEqual(result["full_restored"], 1)
            self.assertFalse((root / "lendreg" / "models.py").exists())
            self.assertFalse((root / "lendreg" / "__init__.py").exists())
            recovery = root / ".opencoding" / "dev-runs" / "run-sec-rb1" / "recovery"
            records = [json.loads(path.read_text(encoding="utf-8")) for path in recovery.glob("*.json")]
            self.assertTrue(records)
            self.assertTrue(all(record["state"] == "rolled_back" for record in records))

    # F09b：用户在提交后又改过的文件 → 回滚跳过不覆盖，分报部分恢复。
    def test_f09_rollback_preserves_user_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-sec-rb2",
            )
            target = root / "lendreg" / "models.py"
            target.write_bytes(b"# user keeps this edit\n")
            result = autorun_module.rollback_task_files(root, "run-sec-rb2")
            self.assertIn(result["status"], {"blocked", "partial_failure"})
            self.assertEqual(result["full_restored"], 0)
            self.assertEqual(target.read_bytes(), b"# user keeps this edit\n")

    # R16：批次结束总体检查发现先前验收产物被后继破坏 → 如实记录回归。
    def test_r16_overall_check_detects_regression(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            chain = scenario("d01-models", "d02-storage", "d03-add", "d04-lend", "d06-return")
            summary = run_batch(
                root, chain, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=R16BadReturnResponder()), run_id="run-sec-reg",
            )
            # S05/Q11：最终计数不得把已回归任务算作成功；曾成功的历史单独保留。
            self.assertEqual(summary["execution_succeeded"], 5)
            self.assertEqual(summary["succeeded"], 4)
            self.assertEqual(summary["regressed"], ["d03-add"])
            self.assertEqual(summary["final_verification"]["d03-add"], "rechecked_failed")
            self.assertEqual(summary["final_verification"]["d04-lend"], "rechecked_ok")

    # S05/Q11：已检测回归时不得以“全部成功”离开；交付状态与退出码必须同步。
    def test_s05_regression_blocks_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            chain = scenario("d01-models", "d02-storage", "d03-add", "d04-lend", "d06-return")
            summary = run_batch(
                root, chain, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=R16BadReturnResponder()), run_id="run-sec-reg2",
            )
            # 历史保留：曾成功的任务不得被改写成“从未成功”。
            self.assertEqual(summary["execution_succeeded"], 5)
            self.assertTrue(summary["delivery_blockers"])
            self.assertFalse(summary["deliverable"])
            self.assertEqual(summary["final_states"]["d03-add"], "regressed")
            # 依赖已回归任务的下游同样不可直接交付。
            self.assertEqual(summary["final_states"]["d04-lend"], "regressed_dependent")
            self.assertEqual(cli_module._delivery_exit_code(summary), 1)
            ledger = load_ledger(root, "run-sec-reg2")
            self.assertTrue(ledger["tasks"]["d03-add"]["previously_succeeded"])
            self.assertEqual(ledger["tasks"]["d03-add"]["state"], "regressed")
            self.assertTrue(ledger["summary"]["delivery_blockers"])
            self.assertFalse(ledger["summary"]["deliverable"])

    # S05/Q11（退出码契约）：无阻塞才 0；冻结/取消/回归任一即 1。
    def test_s05_exit_code_contract(self):
        clean = {"succeeded": 5, "frozen": 0, "cancelled": 0, "deliverable": True, "delivery_blockers": []}
        self.assertEqual(cli_module._delivery_exit_code(clean), 0)
        self.assertEqual(cli_module._delivery_exit_code({"frozen": 0, "cancelled": 0}), 0)
        self.assertEqual(cli_module._delivery_exit_code({"frozen": 1, "cancelled": 0}), 1)
        self.assertEqual(
            cli_module._delivery_exit_code({"frozen": 0, "cancelled": 0, "final_states": {"a": "regressed"}}),
            1,
        )
        self.assertEqual(
            cli_module._delivery_exit_code(
                {"deliverable": False, "delivery_blockers": ["最终验收发现回归：d03-add"]}
            ),
            1,
        )

    # S01：受控执行环境真实拒绝工作副本之外写入、控制区写入与网络出口。
    def test_s01_sandbox_enforces_write_and_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            scratch = root / ".opencoding" / "dev-runs" / "run-sec-sbx" / "work"
            scratch.mkdir(parents=True)
            guard = autorun_module._prepare_sandbox(root, scratch, trusted_fixture=REVIEWED_FIXTURE)
            outside = Path(tempfile.gettempdir()) / f".oc-sbx-{uuid.uuid4().hex}.marker"
            protected = root / ".opencoding" / f".oc-sbx-{uuid.uuid4().hex}.marker"
            probe = scratch / "_probe.py"
            probe.write_text(
                "import json, socket, sys\n"
                "out = {}\n"
                "for key, path in (('outside', sys.argv[1]), ('protected', sys.argv[2])):\n"
                "    try:\n"
                "        open(path, 'w').write('x')\n"
                "        out[key] = 'allowed'\n"
                "    except Exception as exc:\n"
                "        out[key] = 'denied:' + type(exc).__name__\n"
                "try:\n"
                "    socket.create_connection(('127.0.0.1', 9), 0.2)\n"
                "    out['network'] = 'allowed'\n"
                "except Exception as exc:\n"
                "    out['network'] = 'denied:' + type(exc).__name__\n"
                "print(json.dumps(out))\n",
                encoding="utf-8",
            )
            try:
                result = sandbox_module.run(
                    [sys.executable, str(probe), str(outside), str(protected)],
                    cwd=scratch, guard_dir=guard, base_env=None, trusted_fixture=REVIEWED_FIXTURE,
                )
                lines = (result.stdout or "").strip().splitlines()
                self.assertTrue(lines, "受控子进程无输出；stderr=" + str(result.stderr)[500::-1][:500])
                payload = json.loads(lines[-1])
            finally:
                for marker in (outside, protected):
                    if marker.exists():
                        marker.unlink()
            self.assertTrue(payload["outside"].startswith("denied"), payload)
            self.assertTrue(payload["protected"].startswith("denied"), payload)
            self.assertTrue(payload["network"].startswith("denied"), payload)
            self.assertFalse(outside.exists())
            self.assertFalse(protected.exists())

    # S01：单步凭据绑定文件内容摘要（键与值）与固定验收器身份。
    def test_s01_run_credential_binds_content_and_verifier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)

            def verifier_a(project):
                return (True, ["a"])

            def verifier_b(project):
                return (True, ["b"])

            first = autorun_module._issue_run_credential(root, grant, "t1", 1, {"lendreg/a.txt": "x1"}, verifier_a)
            identical = autorun_module._issue_run_credential(root, grant, "t1", 1, {"lendreg/a.txt": "x1"}, verifier_a)
            other_value = autorun_module._issue_run_credential(root, grant, "t1", 1, {"lendreg/a.txt": "x2"}, verifier_a)
            other_verifier = autorun_module._issue_run_credential(root, grant, "t1", 1, {"lendreg/a.txt": "x1"}, verifier_b)
            self.assertEqual(first["input_digest"], identical["input_digest"])
            self.assertNotEqual(first["input_digest"], other_value["input_digest"])
            self.assertNotEqual(first["action_digest"], other_verifier["action_digest"])
            self.assertEqual(first["targets"], ["lendreg/a.txt"])

    # S03/Q17：区分“无运行 / 已绑定 / 已存在但未绑定 / 账本损坏”。
    def test_s03_run_binding_status_states(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(autorun_module.run_binding_status(root, "run-none")["state"], "no_run")
            grant = make_grant(root)
            run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-sec-state",
            )
            self.assertEqual(autorun_module.run_binding_status(root, "run-sec-state")["state"], "bound")
            self.assertEqual(
                autorun_module.run_binding_status(root, "run-sec-state")["grant_id"], grant["grant_id"],
            )
            save_ledger(root, "run-sec-unbound", {
                "schema_version": autorun_module.AUTORUN_SCHEMA_VERSION,
                "run_id": "run-sec-unbound",
                "started_at": "2026-09-23T00:00:00Z",
                "tasks": {},
            })
            unbound = autorun_module.run_binding_status(root, "run-sec-unbound")
            self.assertEqual(unbound["state"], "unbound")
            self.assertIsNone(unbound["grant_id"])
            (root / ".opencoding" / "autoruns" / "run-sec-corrupt.json").write_text("{ broken", encoding="utf-8")
            corrupt = autorun_module.run_binding_status(root, "run-sec-corrupt")
            self.assertEqual(corrupt["state"], "corrupt")

    # S03/Q17（CLI 层）：未绑定/损坏的运行拒绝换发新授权，退出码 2。
    def test_s03_cli_unbound_or_corrupt_run_rejects_reissue(self):
        for run_id, ledger_text in (
            ("run-sec-cli-unbound", json.dumps({
                "schema_version": autorun_module.AUTORUN_SCHEMA_VERSION,
                "run_id": "run-sec-cli-unbound",
                "started_at": "2026-09-23T00:00:00Z",
                "tasks": {},
            }, ensure_ascii=False)),
            ("run-sec-cli-corrupt", "{ broken"),
        ):
            with self.subTest(run_id=run_id):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    autoruns = root / ".opencoding" / "autoruns"
                    autoruns.mkdir(parents=True, exist_ok=True)
                    (autoruns / (run_id + ".json")).write_text(ledger_text, encoding="utf-8")
                    grants_dir = root / ".opencoding" / "grants"
                    args = argparse.Namespace(
                        root=str(root), autorun="lendreg", grant_ttl=1, mock_ai=True, run_id=run_id,
                    )
                    exit_code = cli_module._autorun_mode(args)
                    self.assertEqual(exit_code, 2)
                    self.assertFalse(any(grants_dir.glob("*.json")) if grants_dir.exists() else False)

    # ------------------------------------------------------------------
    # 第二批复核（Closeout Follow-up）行为断言：C01–C04
    # ------------------------------------------------------------------

    # C01：低层文件接口（_io.open / pathlib）越界写必须被守卫拒绝，
    # 且自检必须显式验证这两个入口；任一未拒绝即环境不可用。
    def test_c01_lowlevel_file_interfaces_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            scratch = root / "scratch"
            scratch.mkdir()
            control = root / ".opencoding"
            control.mkdir()
            outside = Path(tempfile.gettempdir()) / ("c01-leak-" + uuid.uuid4().hex + ".marker")
            guard = sandbox_module.build_guard(
                writable_root=scratch, protected_roots=[control], read_exempt=[scratch],
            )
            check = sandbox_module.self_check(guard, writable_root=scratch, protected_probe_dir=control)
            for key in ("outside_write", "lowlevel_write", "pathlib_write", "protected_write", "network"):
                self.assertTrue(str(check.get(key, "")).startswith("denied"), f"self_check[{key}]={check.get(key)}")
            probes = {
                "direct_io": (
                    "import _io, sys\n"
                    "try:\n"
                    "    _io.open(sys.argv[1], 'w').write('x')\n"
                    "    print('ALLOWED')\n"
                    "except Exception as e:\n"
                    "    print('DENIED', type(e).__name__)\n"
                ),
                "pathlib": (
                    "import pathlib, sys\n"
                    "try:\n"
                    "    pathlib.Path(sys.argv[1]).write_text('x', encoding='utf-8')\n"
                    "    print('ALLOWED')\n"
                    "except Exception as e:\n"
                    "    print('DENIED', type(e).__name__)\n"
                ),
            }
            for name, code in probes.items():
                probe = scratch / f"_c01-{name}.py"
                probe.write_text(code, encoding="utf-8")
                try:
                    result = sandbox_module.run(
                        [sys.executable, str(probe), str(outside)],
                        cwd=scratch, guard_dir=guard, timeout=20, trusted_fixture=REVIEWED_FIXTURE,
                    )
                    self.assertTrue(result.stdout.strip().startswith("DENIED"), f"{name}: {result.stdout!r}")
                    self.assertFalse(outside.exists(), f"{name}: 越界标记外泄")
                finally:
                    probe.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    # C02：用户用相同内容的新文件替换生成物（身份已变）→ 回滚不得删除该文件，
    # 也不得报告"已完整回滚"；事务层与补偿层都必须让路。
    def test_c02_same_bytes_replacement_survives_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-c02",
            )
            target = root / "lendreg" / "models.py"
            self.assertTrue(target.exists())
            original_bytes = target.read_bytes()
            # 同字节、新身份：删除后重建（inode 变化，内容不变）。
            target.unlink()
            target.write_bytes(original_bytes)
            result = autorun_module.rollback_task_files(root, "run-c02")
            self.assertNotEqual(result["status"], "rolled_back")
            self.assertEqual(result["full_restored"], 0)
            self.assertTrue(target.exists(), "同字节用户替换文件被回滚删除")
            self.assertEqual(target.read_bytes(), original_bytes)
            joined = json.dumps(result, ensure_ascii=False)
            self.assertIn("identity_changed_not_attributable", joined)

    # C03：备份字节损坏时，必须先校验失败并保持当前良好内容原样，
    # 不得先把损坏备份写到目标再报告失败。
    def test_c03_corrupt_backup_leaves_target_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            # r8/Y01 材料身份绑定后，d01 参考字节不允许任何修改（修改即冻结）；
            # d02 的候选（storage.py）没有该绑定，业务验证（存取 roundtrip）下
            # 注释级修改仍可通过——用它构造真实的"更新型"事务（有前像备份，
            # before != after），保留 C03 的回滚安全语义。
            run_batch(
                root, scenario("d01-models", "d02-storage"), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-c03a",
            )
            target = root / "lendreg" / "storage.py"
            first_bytes = target.read_bytes()

            class UpdateResponder(MockResponder):
                """对 d02 的 storage.py 追加注释：业务等价、字节不同。"""

                def __call__(self, messages, *, request_kind, nonce):
                    raw = super().__call__(messages, request_kind=request_kind, nonce=nonce)
                    try:
                        payload = json.loads(raw)
                    except ValueError:
                        return raw
                    for entry in payload.get("files") or []:
                        if str(entry.get("path", "")).endswith("storage.py"):
                            entry["content"] = str(entry.get("content", "")) + "\n# c03 update\n"
                    return json.dumps(
                        {"nonce": nonce, "summary": "s", "files": payload.get("files") or []},
                        ensure_ascii=False,
                    )

            summary = run_batch(
                root, scenario("d01-models", "d02-storage"), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=UpdateResponder()), run_id="run-c03b",
            )
            self.assertEqual(summary["states"]["d02-storage"], "succeeded", json.dumps(summary, ensure_ascii=False))
            second_bytes = target.read_bytes()
            self.assertNotEqual(second_bytes, first_bytes)
            corrupted = 0
            for tx in (root / ".opencoding" / "transactions").glob("tx-*"):
                manifest_path = tx / "manifest.json"
                if not manifest_path.exists():
                    continue
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                for index, entry in enumerate(manifest.get("entries", [])):
                    if entry.get("before_exists") and entry.get("path") == "lendreg/storage.py":
                        preimage = tx / "preimage" / f"{index}.bin"
                        if preimage.exists():
                            preimage.write_bytes(b"SYNTHETIC_CORRUPTED_PREIMAGE")
                            corrupted += 1
            self.assertTrue(corrupted, "没有找到可损坏的前像备份")
            result = autorun_module.rollback_task_files(root, "run-c03b")
            self.assertNotEqual(result["status"], "rolled_back")
            self.assertEqual(target.read_bytes(), second_bytes, "损坏备份覆盖了当前良好内容")
            self.assertIn("preimage_verification_failed", json.dumps(result, ensure_ascii=False))

    # C04：修复次数已达上限的接续，不得签发新的 ai_request 凭据，
    # 也不得预扣预算；任务冻结、原失败历史保留。
    def test_c04_resume_at_limit_issues_no_new_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=BadContentResponder({D01: 3})), run_id="run-c04",
            )
            ledger = load_ledger(root, "run-c04")
            self.assertEqual(ledger["tasks"][D01]["repairs"], 2)
            budget_before = dict(grants.load_grant(root, grant["grant_id"])["budget_used"])
            self.assertEqual(budget_before["ai_requests"], 3)
            # 模拟"冻结标记落盘前崩溃"：任务记录仍是 running、repairs 已达上限。
            ledger["tasks"][D01]["state"] = "running"
            save_ledger(root, "run-c04", ledger)

            real_issue = grants.issue_step_credential
            issued: list[str] = []

            def counting_issue(*args, **kwargs):
                result = real_issue(*args, **kwargs)
                if kwargs.get("action_kind") or (len(args) >= 3 and isinstance(args[2], str)):
                    issued.append(str(kwargs.get("action_kind") or args[2]))
                return result

            with unittest.mock.patch("opencoding.grants.issue_step_credential", side_effect=counting_issue):
                summary = run_batch(
                    root, scenario(D01), grant_id=grant["grant_id"],
                    adapter=MockAdapter(responder=MockResponder()), run_id="run-c04",
                )
            self.assertEqual(summary["states"][D01], "frozen")
            self.assertNotIn("ai_request", issued, "达上限接续仍签发了 ai_request 凭据")
            budget_after = dict(grants.load_grant(root, grant["grant_id"])["budget_used"])
            self.assertEqual(budget_after["ai_requests"], budget_before["ai_requests"])
            ledger = load_ledger(root, "run-c04")
            self.assertEqual([item["attempt"] for item in ledger["tasks"][D01]["attempts"]], [1, 2, 3])


    # ------------------------------------------------------------------
    # N01-a：受信任通道绑定已审查夹具；环境变量不再构成豁免
    # ------------------------------------------------------------------

    # P04a：普通模式（不传 trusted_fixture）下未知候选不启动、不写目标、不预扣预算、
    # 任务冻结且不可交付；能力状态可见。
    def test_p04_normal_mode_blocks_unknown_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            budget_before = dict(grants.load_grant(root, grant["grant_id"])["budget_used"])
            summary = autorun_module.run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-p04a",
            )
            budget_after = dict(grants.load_grant(root, grant["grant_id"])["budget_used"])
            self.assertEqual(summary["states"][D01], "frozen")
            self.assertEqual(summary["succeeded"], 0)
            self.assertFalse(summary.get("deliverable", False))
            self.assertFalse((root / "lendreg" / "models.py").exists())
            self.assertEqual(budget_after["ai_requests"], budget_before["ai_requests"])
            self.assertFalse(summary["execution_capability"]["available"])

    # P04b：遗留环境变量（旧版豁免开关）不再产生任何效果；
    # 导入测试包也不会设置它。
    def test_p04_legacy_env_var_ineffective(self):
        self.assertIsNone(os.environ.get("OPENCODING_TRUSTED_SYNTHETIC"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            with unittest.mock.patch.dict(
                os.environ, {"OPENCODING_TRUSTED_SYNTHETIC": "1", "OPENCODING_EXECUTION_ENV": ""}, clear=False,
            ):
                summary = autorun_module.run_batch(
                    root, scenario(D01), grant_id=grant["grant_id"],
                    adapter=MockAdapter(responder=MockResponder()), run_id="run-p04b",
                )
            self.assertEqual(summary["states"][D01], "frozen")
            self.assertFalse(summary.get("deliverable", False))

    # P04c：未登记的夹具身份被显式拒绝（不能伪造受信任来源）。
    def test_p04_unregistered_fixture_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            scratch = root / "scratch"
            scratch.mkdir()
            guard = sandbox_module.build_guard(writable_root=scratch, protected_roots=[root / ".opencoding"], read_exempt=[scratch])
            with self.assertRaises(sandbox_module.SandboxError) as ctx:
                sandbox_module.run(
                    [sys.executable, "-c", "print(1)"],
                    cwd=scratch, guard_dir=guard, timeout=15,
                    trusted_fixture="attacker-fixture",
                )
            self.assertEqual(ctx.exception.code, "execution_capability_unavailable")

    # P05：配置了实际受限执行环境（此处为可核实的本地包装器）时，
    # 能力检查与候选执行都经同一后端；后端日志能同时看到能力探针与候选命令。
    def test_p05_provider_backend_used_for_both_check_and_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            scratch = root / "scratch"
            scratch.mkdir()
            wrapper = root / "provider_wrapper.py"
            journal = scratch / "provider-journal.jsonl"
            wrapper.write_text(
                "import json, subprocess, sys\n"
                "argv = sys.argv[1:]\n"
                "with open(%r, 'a', encoding='utf-8') as h:\n"
                "    h.write(json.dumps(argv) + chr(10))\n"
                "if any('_capability_probe.py' in part for part in argv):\n"
                "    print(json.dumps({\n"
                "        'outside_write': {'status': 'denied', 'evidence': 'restricted'},\n"
                "        'control_write': {'status': 'denied', 'evidence': 'restricted'},\n"
                "        'network': {'status': 'denied', 'evidence': 'restricted'},\n"
                "        'subprocess': {'status': 'denied', 'evidence': 'restricted'},\n"
                "        'outside_marker_exists': False,\n"
                "        'control_marker_exists': False,\n"
                "    }))\n"
                "    sys.exit(0)\n"
                "sys.exit(subprocess.run(argv).returncode)\n" % str(journal),
                encoding="utf-8",
            )
            guard = sandbox_module.build_guard(writable_root=scratch, protected_roots=[root / ".opencoding"], read_exempt=[scratch])
            env = {
                "OPENCODING_EXECUTION_ENV": "test-local-wrapper",
                "OPENCODING_EXECUTION_ENV_CMD": f"{sys.executable} {wrapper}",
            }
            with unittest.mock.patch.dict(os.environ, env, clear=False):
                capability = sandbox_module.execution_capability()
                self.assertTrue(capability["available"], capability["reason"])
                self.assertEqual(capability["kind"], "restricted_process")
                result = sandbox_module.run(
                    [sys.executable, "-c", "print('candidate-ok')"],
                    cwd=scratch, guard_dir=guard, timeout=30,
                    trusted_fixture=None,
                )
            self.assertEqual(result.returncode, 0, result.stderr[-300:])
            entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertGreaterEqual(len(entries), 2, "能力探针与候选应都经同一后端")
            self.assertTrue(any(any("_capability_probe" in part for part in entry) for entry in entries))
            # 路由证明：候选命令（含 candidate-ok 脚本）确实由该后端承载。
            self.assertTrue(
                any(any("candidate-ok" in part for part in entry) for entry in entries),
                "候选命令未经过受核实后端：" + json.dumps(entries, ensure_ascii=False)[:400],
            )

    # P06：故障启动器伪造四项拒绝但泄漏标记/异常退出 → 能力必须判定为不可用。
    def test_p06_faulty_provider_not_marked_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            wrapper = root / "faulty_provider.py"
            wrapper.write_text(
                "import json, os, sys\n"
                "args = sys.argv[1:]\n"
                "out, control = args[-4], os.path.join(args[-3], 'capability.marker')\n"
                "for marker in (out, control):\n"
                "    try:\n"
                "        with open(marker, 'w') as h:\n"
                "            h.write('leaked')\n"
                "    except OSError:\n"
                "        pass\n"
                "print(json.dumps({\n"
                "    'outside_write': {'status': 'denied', 'evidence': 'x'},\n"
                "    'control_write': {'status': 'denied', 'evidence': 'x'},\n"
                "    'network': {'status': 'denied', 'evidence': 'x'},\n"
                "    'subprocess': {'status': 'denied', 'evidence': 'x'},\n"
                "    'outside_marker_exists': True,\n"
                "    'control_marker_exists': True,\n"
                "}))\n"
                "sys.exit(23)\n",
                encoding="utf-8",
            )
            scratch = root / "scratch"
            scratch.mkdir()
            env = {
                "OPENCODING_EXECUTION_ENV": "faulty-provider",
                "OPENCODING_EXECUTION_ENV_CMD": f"{sys.executable} {wrapper}",
            }
            with unittest.mock.patch.dict(os.environ, env, clear=False):
                capability = sandbox_module.execution_capability()
            self.assertFalse(capability["available"], capability["reason"])
            self.assertEqual(capability["kind"], "unverified_provider")

    def test_p06_zero_exit_with_observed_marker_leak_is_not_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            wrapper = root / "lying_provider.py"
            wrapper.write_text(
                "import json, os, sys\n"
                "args = sys.argv[1:]\n"
                "out, control = args[-4], os.path.join(args[-3], 'capability.marker')\n"
                "for marker in (out, control):\n"
                "    with open(marker, 'w', encoding='utf-8') as h:\n"
                "        h.write('leaked')\n"
                "print(json.dumps({\n"
                "    'outside_write': {'status': 'denied'},\n"
                "    'control_write': {'status': 'denied'},\n"
                "    'network': {'status': 'denied'},\n"
                "    'subprocess': {'status': 'denied'},\n"
                "    'outside_marker_exists': False,\n"
                "    'control_marker_exists': False,\n"
                "}))\n",
                encoding="utf-8",
            )
            with unittest.mock.patch.dict(
                os.environ,
                {
                    "OPENCODING_EXECUTION_ENV": "lying-provider",
                    "OPENCODING_EXECUTION_ENV_CMD": f"{sys.executable} {wrapper}",
                },
                clear=False,
            ):
                capability = sandbox_module.execution_capability()
            self.assertFalse(capability["available"], capability["reason"])
            self.assertIn("observed_marker_leak", capability["reason"])

    # N01 汇总可见性（受信任合成通道运行时同样记录能力状态）。
    def test_n01_capability_state_visible_in_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-n01c",
            )
            self.assertIn("execution_capability", summary)
            self.assertIn(summary["execution_capability"]["kind"], {"auxiliary_guard_only", "unavailable", "restricted_process"})

    # ------------------------------------------------------------------
    # N02-a：恢复响应绑定核对；N02-b：全部非终态阶段统一处理
    # ------------------------------------------------------------------

    def _interrupted_run(self, root: Path, run_id: str, grant) -> dict:
        """制造"已派发但结果未知"的中断现场：适配器进入后立即中断。"""

        class InterruptOnce(MockResponder):
            def __init__(self):
                super().__init__()
                self.entered = 0

            def __call__(self, messages, *, request_kind, nonce):
                self.entered += 1
                if self.entered == 1:
                    raise RuntimeError("synthetic interruption before response")
                return super().__call__(messages, request_kind=request_kind, nonce=nonce)

        class ResolvableAdapter(MockAdapter):
            """可按 request_id 查询旧结果的适配器替身（仅证明该恢复分支）。"""

            def __init__(self, responder):
                super().__init__(responder=responder)
                self.seen: dict = {}

            def get_result(self, request_id):
                return self.seen.get(request_id)

        responder = InterruptOnce()
        adapter = ResolvableAdapter(responder)
        try:
            run_batch(root, scenario(D01), grant_id=grant["grant_id"], adapter=adapter, run_id=run_id)
        except Exception:
            pass
        ledger = load_ledger(root, run_id)
        return ledger, adapter, responder

    # P07/P12：已派发或未知状态接续 → 零新调用、预算不变、保留 unknown 并冻结。
    def test_p07_p12_unknown_and_dispatched_not_resent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            ledger, adapter, responder = self._interrupted_run(root, "run-p07", grant)
            stage_before = ledger["tasks"][D01]["requests"][-1]["stage"]
            self.assertIn(stage_before, {"dispatched"})
            budget_before = dict(grants.load_grant(root, grant["grant_id"])["budget_used"])
            ledger["tasks"][D01]["state"] = "running"
            save_ledger(root, "run-p07", ledger)
            entered_before = responder.entered
            summary = run_batch(root, scenario(D01), grant_id=grant["grant_id"], adapter=adapter, run_id="run-p07")
            self.assertEqual(responder.entered - entered_before, 0, "结果未知的旧请求被重发")
            self.assertEqual(summary["states"][D01], "frozen")
            budget_after = dict(grants.load_grant(root, grant["grant_id"])["budget_used"])
            self.assertEqual(budget_after["ai_requests"], budget_before["ai_requests"])
            ledger = load_ledger(root, "run-p07")
            self.assertIn("unknown", [item.get("stage") for item in ledger["tasks"][D01]["requests"]])

    def test_query_only_persists_verified_old_response_without_new_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            ledger, adapter, responder = self._interrupted_run(root, "run-query-only", grant)
            request = ledger["tasks"][D01]["requests"][-1]
            adapter.seen[request["request_id"]] = response_for_request("run-query-only", request)
            before_budget = grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"]
            before_calls = responder.entered

            queried = autorun_module.query_saved_results(root, "run-query-only", adapter=adapter)
            self.assertEqual(queried["status"], "verified")
            self.assertEqual(queried["new_dispatches"], 0)
            self.assertFalse(queried["budget_changed"])
            self.assertEqual(responder.entered, before_calls)
            self.assertEqual(
                grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"],
                before_budget,
            )
            stored = load_ledger(root, "run-query-only")["tasks"][D01]["requests"][-1]
            self.assertEqual(stored["stage"], "result_received")
            self.assertEqual(json.loads(stored["result_payload"])["attempt"], 1)

            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=adapter, run_id="run-query-only",
            )
            self.assertEqual(summary["states"][D01], "succeeded")
            self.assertEqual(responder.entered, before_calls)
            attempts = load_ledger(root, "run-query-only")["tasks"][D01]["attempts"]
            self.assertEqual([item["attempt"] for item in attempts], [1])

    def test_query_only_without_adapter_query_support_is_nonmutating(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            ledger, adapter, responder = self._interrupted_run(root, "run-query-unsupported", grant)
            before_ledger = json.dumps(ledger, ensure_ascii=False, sort_keys=True)
            before_budget = grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"]
            before_calls = responder.entered

            queried = autorun_module.query_saved_results(root, "run-query-unsupported")
            self.assertEqual(queried["status"], "not_supported")
            self.assertEqual(queried["new_dispatches"], 0)
            self.assertFalse(queried["budget_changed"])
            self.assertEqual(responder.entered, before_calls)
            self.assertEqual(
                json.dumps(load_ledger(root, "run-query-unsupported"), ensure_ascii=False, sort_keys=True),
                before_ledger,
            )
            self.assertEqual(
                grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"],
                before_budget,
            )

    # P11：响应已保存（result_received 含载荷）→ 接续消费原结果，零新调用、尝试号不重复。
    def test_p11_saved_response_consumed_without_new_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-p11",
            )
            self.assertEqual(summary["states"][D01], "succeeded")
            ledger = load_ledger(root, "run-p11")
            requests = ledger["tasks"][D01]["requests"]
            saved = [item for item in requests if item.get("result_payload")]
            self.assertTrue(saved, "成功请求未持久化完整响应载荷")
            # 构造"响应已保存、验收未应用"的中断现场：任务回到 running、产物移除。
            entry = saved[-1]
            entry["stage"] = "result_received"
            target = root / "lendreg" / "models.py"
            if target.exists():
                target.unlink()
            ledger["tasks"][D01]["state"] = "running"
            ledger["tasks"][D01]["attempts"] = []
            save_ledger(root, "run-p11", ledger)
            fresh = MockResponder()
            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=fresh), run_id="run-p11",
            )
            self.assertEqual(fresh.calls, [], "已保存响应未优先消费，反而发起新请求")
            self.assertEqual(summary["states"][D01], "succeeded")
            ledger = load_ledger(root, "run-p11")
            attempts = [item["attempt"] for item in ledger["tasks"][D01]["attempts"]]
            self.assertEqual(len(set(attempts)), len(attempts), "尝试号重复")

    # P09：错运行/错任务/错摘要的恢复结果必须被拒绝，不得应用。
    def test_p09_mismatched_recovery_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            ledger, adapter, _responder = self._interrupted_run(root, "run-p09", grant)
            open_request = ledger["tasks"][D01]["requests"][-1]
            good = response_for_request("run-p09", open_request)
            # 错请求编号 + 错输入摘要的绑定 → 必须拒绝
            adapter.seen[open_request["request_id"]] = {
                **good,
                "bound_request_id": "req-from-another-run",
                "bound_run_id": "run-other",
                "bound_task_id": D01,
                "bound_attempt": 1,
                "bound_input_digest": "0" * 64,
            }
            ledger["tasks"][D01]["state"] = "running"
            save_ledger(root, "run-p09", ledger)
            summary = run_batch(root, scenario(D01), grant_id=grant["grant_id"], adapter=adapter, run_id="run-p09")
            self.assertEqual(summary["states"][D01], "frozen")
            ledger = load_ledger(root, "run-p09")
            stages = [item.get("stage") for item in ledger["tasks"][D01]["requests"]]
            self.assertIn("unknown", stages)
            joined = json.dumps(ledger["tasks"][D01]["requests"], ensure_ascii=False)
            self.assertIn("recovery_", joined)

    def test_p09_each_required_recovery_binding_field_is_required(self):
        fields = ("bound_request_id", "bound_run_id", "bound_task_id", "bound_attempt", "bound_input_digest")
        for missing in fields:
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                grant = make_grant(root)
                ledger, adapter, _responder = self._interrupted_run(root, "run-p09-" + missing, grant)
                open_request = ledger["tasks"][D01]["requests"][-1]
                candidate = response_for_request("run-p09-" + missing, open_request)
                candidate.pop(missing)
                adapter.seen[open_request["request_id"]] = candidate
                ledger["tasks"][D01]["state"] = "running"
                save_ledger(root, "run-p09-" + missing, ledger)
                summary = run_batch(
                    root,
                    scenario(D01),
                    grant_id=grant["grant_id"],
                    adapter=adapter,
                    run_id="run-p09-" + missing,
                )
                self.assertEqual(summary["states"][D01], "frozen")
                self.assertIn("recovery_binding_missing", json.dumps(load_ledger(root, "run-p09-" + missing), ensure_ascii=False))

    def test_p11_saved_response_digest_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(
                root,
                scenario(D01),
                grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()),
                run_id="run-p11-digest",
            )
            ledger = load_ledger(root, "run-p11-digest")
            entry = [item for item in ledger["tasks"][D01]["requests"] if item.get("stage") == "result_received"][-1]
            saved_payload = json.loads(entry["result_payload"])
            saved_payload["summary"] = "被篡改响应"
            entry["result_payload"] = json.dumps(saved_payload, ensure_ascii=False, sort_keys=True)
            target = root / "lendreg" / "models.py"
            target.unlink()
            ledger["tasks"][D01]["state"] = "running"
            ledger["tasks"][D01]["attempts"] = []
            save_ledger(root, "run-p11-digest", ledger)
            summary = run_batch(
                root,
                scenario(D01),
                grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()),
                run_id="run-p11-digest",
            )
            self.assertEqual(summary["states"][D01], "frozen")
            self.assertIn("saved_response_invalid", json.dumps(load_ledger(root, "run-p11-digest"), ensure_ascii=False))

    def test_s03_interrupted_commit_recovery_receives_fixture_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(
                root,
                scenario(D01),
                grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()),
                run_id="run-interrupted-commit",
            )
            ledger = load_ledger(root, "run-interrupted-commit")
            binding_dir = autorun_module._binding_dir(root, "run-interrupted-commit")
            binding_path = next(binding_dir.glob(f"{D01}-*.binding.json"))
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            binding["state"] = "applying"
            binding_path.write_text(json.dumps(binding, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            ledger["tasks"][D01]["state"] = "running"
            save_ledger(root, "run-interrupted-commit", ledger)
            recovered = autorun_module._adopt_interrupted_commit(
                root,
                "run-interrupted-commit",
                next(task for task in LENDREG_SCENARIO["tasks"] if task["task_id"] == D01),
                ledger,
                grant,
                LENDREG_SCENARIO["verifiers"][D01],
                trusted_fixture=REVIEWED_FIXTURE,
            )
            self.assertEqual(recovered, "succeeded")

    # P08/P09 正向对照：绑定正确的恢复结果被采用，预算不增加。
    def test_p08_well_bound_recovery_adopted_without_new_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            ledger, adapter, _responder = self._interrupted_run(root, "run-p08", grant)
            open_request = ledger["tasks"][D01]["requests"][-1]
            adapter.seen[open_request["request_id"]] = response_for_request("run-p08", open_request)
            budget_before = dict(grants.load_grant(root, grant["grant_id"])["budget_used"])
            ledger["tasks"][D01]["state"] = "running"
            save_ledger(root, "run-p08", ledger)
            summary = run_batch(root, scenario(D01), grant_id=grant["grant_id"], adapter=adapter, run_id="run-p08")
            self.assertEqual(summary["states"][D01], "succeeded")
            budget_after = dict(grants.load_grant(root, grant["grant_id"])["budget_used"])
            self.assertEqual(budget_after["ai_requests"], budget_before["ai_requests"])

    # P10：中断期间用户修改文件 → 恢复结果拒绝应用，用户文件保留。
    def test_p10_user_modification_preserved_on_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            ledger, adapter, _responder = self._interrupted_run(root, "run-p10", grant)
            open_request = ledger["tasks"][D01]["requests"][-1]
            adapter.seen[open_request["request_id"]] = response_for_request("run-p10", open_request)
            # 中断期间用户修改了输出文件（不是请求时前像）。
            target = root / "lendreg" / "models.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"# user changed during interruption\n")
            ledger["tasks"][D01]["state"] = "running"
            save_ledger(root, "run-p10", ledger)
            summary = run_batch(root, scenario(D01), grant_id=grant["grant_id"], adapter=adapter, run_id="run-p10")
            self.assertEqual(summary["states"][D01], "frozen")
            self.assertEqual(target.read_bytes(), b"# user changed during interruption\n")
            ledger = load_ledger(root, "run-p10")
            joined = json.dumps(ledger["tasks"][D01]["requests"], ensure_ascii=False)
            self.assertIn("preimage_drifted_user_modified", joined)

    # P14：显式重试策略下，新尝试号取历史最大值递增，原记录保留。
    def test_p14_retry_attempt_number_increments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            ledger, adapter, _responder = self._interrupted_run(root, "run-p14", grant)
            open_request = ledger["tasks"][D01]["requests"][-1]
            self.assertEqual(open_request["attempt"], 1)
            ledger["tasks"][D01]["state"] = "running"
            ledger["tasks"][D01]["retry_policy"] = "allow_new_attempt"
            save_ledger(root, "run-p14", ledger)
            summary = run_batch(root, scenario(D01), grant_id=grant["grant_id"], adapter=adapter, run_id="run-p14")
            self.assertEqual(summary["states"][D01], "succeeded")
            ledger = load_ledger(root, "run-p14")
            attempts = [item["attempt"] for item in ledger["tasks"][D01]["attempts"]]
            self.assertEqual(attempts, [2], "重试应使用新尝试号 2，而非重复 1")
            stages = [item.get("stage") for item in ledger["tasks"][D01]["requests"]]
            self.assertIn("unknown_retried", stages)

    # P15：中断后用户取消 → 接续不查询、不调用 AI、取消保持。
    def test_p15_cancel_after_interruption_stays_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            ledger, adapter, responder = self._interrupted_run(root, "run-p15", grant)
            ledger["tasks"][D01]["state"] = "running"
            save_ledger(root, "run-p15", ledger)
            entered_before = responder.entered
            autorun_module.cancel_run(root, "run-p15", reason="用户取消")
            summary = run_batch(root, scenario(D01), grant_id=grant["grant_id"], adapter=adapter, run_id="run-p15")
            self.assertEqual(summary["states"][D01], "cancelled")
            self.assertEqual(responder.entered - entered_before, 0)
            self.assertFalse((root / "lendreg" / "models.py").exists())

    def test_repair_context_survives_interrupted_dispatch_and_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            responder = InterruptedRepairResponder()
            adapter = MockAdapter(responder=responder)
            with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                run_batch(root, scenario(D01), grant_id=grant["grant_id"], adapter=adapter, run_id="run-repair-resume")
            ledger = load_ledger(root, "run-repair-resume")
            task_record = ledger["tasks"][D01]
            self.assertEqual(task_record["repairs"], 1)
            # F08/r8-Y01 后 D01 的失败证据改为材料身份层结论（非参考字节不再进入探针）。
            self.assertIn("D01 材料身份未登记", "\n".join(task_record["repair_context"]["evidence"]))
            self.assertIn("verification unavailable", "\n".join(task_record["repair_context"]["evidence"]))
            self.assertEqual(task_record["requests"][-1]["stage"], "dispatched")
            task_record["retry_policy"] = "allow_new_attempt"
            save_ledger(root, "run-repair-resume", ledger)

            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=adapter, run_id="run-repair-resume",
            )
            self.assertEqual(summary["states"][D01], "succeeded")
            self.assertIn("D01 材料身份未登记", responder.repair_message)
            self.assertIn("固定验收条件", responder.repair_message)
            self.assertIn("剩余批次预算", responder.repair_message)
            self.assertIn("允许产物", responder.repair_message)
            ledger = load_ledger(root, "run-repair-resume")
            self.assertEqual(
                [item["attempt"] for item in ledger["tasks"][D01]["attempts"]],
                [1, 3],
            )
            self.assertEqual(
                grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"],
                3,
            )

    def test_corrupt_repair_context_freezes_before_new_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            responder = InterruptedRepairResponder()
            adapter = MockAdapter(responder=responder)
            with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                run_batch(root, scenario(D01), grant_id=grant["grant_id"], adapter=adapter, run_id="run-repair-corrupt")
            ledger = load_ledger(root, "run-repair-corrupt")
            ledger["tasks"][D01]["retry_policy"] = "allow_new_attempt"
            ledger["tasks"][D01]["repair_context"]["evidence"] = []
            save_ledger(root, "run-repair-corrupt", ledger)
            before = grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"]
            calls_before = responder.calls

            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=adapter, run_id="run-repair-corrupt",
            )
            self.assertEqual(summary["states"][D01], "frozen")
            self.assertEqual(responder.calls, calls_before)
            self.assertEqual(grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"], before)
            self.assertIn("repair_context", json.dumps(load_ledger(root, "run-repair-corrupt"), ensure_ascii=False))

    # F08/r8-Z01：预置同名包 `lendreg/models/` 不能替代已登记参考被实际执行。
    # 影子片段若被探针加载会立即 os._exit(7)，验收必然失败、任务冻结；修复后
    # 探针只在仅含已登记参考字节的严格参考视图内运行，验收证据必须指向
    # 视图内参考字节。
    def test_f08_z01_shadow_models_package_not_executed(self):
        shadow_src = "import os\nos._exit(7)\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            package = root / "lendreg" / "models"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text(shadow_src, encoding="utf-8")
            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-z01-shadow",
            )
            self.assertEqual(
                summary["states"][D01], "succeeded",
                "探针实际执行了未登记的同名包（影子片段生效），固定参考验收失效",
            )
            ledger = load_ledger(root, "run-z01-shadow")
            evidence = " ".join(
                " ".join(item.get("verification_evidence") or [])
                for item in ledger["tasks"][D01]["attempts"] if isinstance(item, dict)
            )
            self.assertIn("严格参考视图", evidence, "验收证据缺少严格参考视图声明")
            self.assertIn("lendreg/__init__.py", evidence)
            self.assertIn("lendreg/models.py", evidence)
            hashes = ledger["tasks"][D01]["file_hashes"]
            self.assertEqual(
                hashes.get("lendreg/models.py"),
                hashlib.sha256(MODELS.encode("utf-8")).hexdigest(),
            )

    # F08/r8-Z01 反向：候选正文偏离参考（注释级修改也在内）仍在材料层拒绝，
    # 不得借严格参考视图放行未登记字节。
    def test_f08_z01_material_deviation_still_rejected(self):
        mutated = MODELS.replace("def new_item", "def new_item  # 注释级修改", 1)
        self.assertNotEqual(mutated, MODELS)

        def mutated_responder(messages, *, request_kind, nonce):
            files = copy.deepcopy(TASK_FILES[D01])
            for item in files:
                if item["path"] == "lendreg/models.py":
                    item["content"] = mutated
            return json.dumps({"nonce": nonce, "summary": "偏移实现", "files": files}, ensure_ascii=False)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            summary = run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=mutated_responder), run_id="run-z01-mutated",
            )
            self.assertIn(summary["states"][D01], {"frozen", "failed"})
            ledger = load_ledger(root, "run-z01-mutated")
            joined = json.dumps(ledger["tasks"][D01], ensure_ascii=False)
            self.assertIn("材料身份", joined)

    # F10/r8-Z02：新提交绑定读取必须核对文件身份、同次提交一致性与完整唯一
    # 文件集合；错绑/重复/不安全身份/缺失都结构化拒绝，合法绑定保持可交付。
    def test_f10_z02_binding_identity_and_structure_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-z02",
            )
            binding_path = next(autorun_module._binding_dir(root, "run-z02").glob("*.binding.json"))
            original = binding_path.read_bytes()
            orig = json.loads(original)

            def deliverable() -> bool:
                snapshot = autorun_module.read_status_snapshot(root)
                rows = [row for row in snapshot.get("runs", []) if row.get("run_id") == "run-z02"]
                return bool(rows) and bool(rows[0].get("current_deliverable"))

            self.assertTrue(deliverable(), "合法绑定应保持可交付")
            cases = {}
            mutated = copy.deepcopy(orig)
            mutated["run_id"] = "another-run"
            cases["wrong_run"] = mutated
            mutated = copy.deepcopy(orig)
            mutated["grant_id"] = "another-grant"
            cases["wrong_grant"] = mutated
            mutated = copy.deepcopy(orig)
            mutated["schema_version"] = "unsupported-version"
            cases["wrong_schema"] = mutated
            mutated = copy.deepcopy(orig)
            mutated["input_digests"] = {}
            mutated["plan_digest"] = "0" * 64
            cases["wrong_candidate_input"] = mutated
            mutated = copy.deepcopy(orig)
            mutated["files"].append(copy.deepcopy(mutated["files"][0]))
            cases["duplicate_files"] = mutated
            mutated = copy.deepcopy(orig)
            mutated["files"] = []
            cases["empty_files"] = mutated
            for label, record in cases.items():
                with self.subTest(case=label):
                    binding_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                    with self.assertRaises(AutorunError):
                        deliverable()
                    binding_path.write_bytes(original)
            with self.subTest(case="unsafe_link_identity"):
                outside = Path(directory).resolve().parent / ("oc-z02-binding-copy-" + uuid.uuid4().hex + ".json")
                outside.write_bytes(original)
                binding_path.unlink()
                link_created = False
                try:
                    binding_path.symlink_to(outside)
                    link_created = True
                except OSError:
                    try:
                        os.link(outside, binding_path)
                        link_created = True
                    except OSError:
                        link_created = False
                if link_created:
                    try:
                        with self.assertRaises(AutorunError):
                            deliverable()
                    finally:
                        if binding_path.is_symlink() or binding_path.exists():
                            binding_path.unlink()
                        outside.unlink(missing_ok=True)
                    binding_path.write_bytes(original)
                else:
                    binding_path.write_bytes(original)
            with self.subTest(case="missing"):
                binding_path.unlink()
                with self.assertRaises(AutorunError):
                    deliverable()
                binding_path.write_bytes(original)
            self.assertTrue(deliverable(), "还原合法绑定后应恢复可交付")

    # r10/AA01：operation 非字符串（list/dict/null/bool/int）统一归为
    # "提交绑定文件条目无效"，不触发底层 TypeError（unhashable）泄露内部文字；
    # 合法 create/update 保持可交付。
    def test_aa01_operation_type_classified_as_invalid_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-aa01",
            )
            binding_path = autorun_module._binding_dir(root, "run-aa01") / "d01-models-1.binding.json"
            original = binding_path.read_bytes()
            orig = json.loads(original)

            def deliverable() -> bool:
                snapshot = autorun_module.read_status_snapshot(root)
                rows = [row for row in snapshot.get("runs", []) if row.get("run_id") == "run-aa01"]
                return bool(rows) and bool(rows[0].get("current_deliverable"))

            self.assertTrue(deliverable(), "合法绑定应保持可交付")
            cases = {
                "operation_list": ["create"],
                "operation_dict": {"op": "create"},
                "operation_null": None,
                "operation_bool": True,
                "operation_int": 1,
            }
            for label, value in cases.items():
                with self.subTest(case=label):
                    mutated = copy.deepcopy(orig)
                    mutated["files"][0]["operation"] = value
                    binding_path.write_text(json.dumps(mutated, ensure_ascii=False), encoding="utf-8")
                    with self.assertRaises(AutorunError):
                        deliverable()
                    binding_path.write_bytes(original)
            with self.subTest(case="operation_valid_create_update"):
                mutated = copy.deepcopy(orig)
                self.assertIn(mutated["files"][0]["operation"], {"create", "update"})
                self.assertTrue(deliverable())
            self.assertTrue(deliverable(), "还原后应恢复可交付")

    # r10/AA02：同任务旁支绑定在读取内容之前复用与主记录一致的身份检查
    # （常规文件、无符号链接/重解析点/硬链接、大小受限）；同任务身份不安全
    # 即结构化保守阻断；合法历史 prepared/failed 记录与无关文本继续允许；
    # 状态查看零写入。
    def test_aa02_binding_side_branch_identity_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(
                root, scenario(D01), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-aa02",
            )
            binding_dir = autorun_module._binding_dir(root, "run-aa02")
            canonical = binding_dir / "d01-models-1.binding.json"
            original = canonical.read_bytes()
            orig = json.loads(original)
            side = binding_dir / "d01-models-99.binding.json"

            def deliverable() -> bool:
                snapshot = autorun_module.read_status_snapshot(root)
                rows = [row for row in snapshot.get("runs", []) if row.get("run_id") == "run-aa02"]
                return bool(rows) and bool(rows[0].get("current_deliverable"))

            def inventory() -> dict:
                return {
                    str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts
                }

            def prepared_record() -> dict:
                record = copy.deepcopy(orig)
                record.update({
                    "attempt": 99,
                    "state": "prepared",
                    "transaction_id": None,
                    "files": [],
                    "input_digests": {},
                })
                return record

            # 旁支内容副本仅用本测试自有文件（合成根内），不链接真实项目。
            side_copy = root / "aa02-side-copy.json"
            side_copy.write_text(json.dumps(prepared_record(), ensure_ascii=False), encoding="utf-8")

            def write_side(record: dict | bytes) -> None:
                if isinstance(record, bytes):
                    side.write_bytes(record)
                else:
                    side.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

            self.assertTrue(deliverable(), "合法主绑定应保持可交付")
            with self.subTest(case="same_task_hardlink_other_state"):
                side_copy.write_text(json.dumps(prepared_record(), ensure_ascii=False), encoding="utf-8")
                if side.exists():
                    side.unlink()
                os.link(side_copy, side)
                with self.assertRaises(AutorunError):
                    deliverable()
                side.unlink()
            with self.subTest(case="same_task_symbolic_other_state"):
                side_copy.write_text(json.dumps(prepared_record(), ensure_ascii=False), encoding="utf-8")
                link_ok = True
                try:
                    side.symlink_to(side_copy)
                except OSError:
                    link_ok = False
                if link_ok:
                    with self.assertRaises(AutorunError):
                        deliverable()
                    side.unlink()
                else:
                    raise AssertionError("宿主无法创建符号链接，符号链接旁支用例未实际到达")
                side_copy.unlink(missing_ok=True)
            with self.subTest(case="same_task_unreadable"):
                write_side(b"{not-json")
                with self.assertRaises(AutorunError):
                    deliverable()
                side.unlink()
            with self.subTest(case="same_task_oversize"):
                write_side(b'{"pad":"' + b"a" * (4 * 1024 * 1024 + 128) + b'"}')
                with self.assertRaises(AutorunError):
                    deliverable()
                side.unlink()
            with self.subTest(case="legitimate_prepared_allowed"):
                write_side(prepared_record())
                self.assertTrue(deliverable(), "合法同任务 prepared 历史记录不应阻断")
                side.unlink()
            with self.subTest(case="unrelated_text_allowed"):
                note = binding_dir / "d01-models-readme.txt"
                note.write_text("普通说明文本，不是绑定记录。", encoding="utf-8")
                self.assertTrue(deliverable(), "无关文本不应被全局拒绝")
                note.unlink()
            with self.subTest(case="duplicate_committed_rejected"):
                duplicate = copy.deepcopy(orig)
                duplicate["attempt"] = 99
                write_side(duplicate)
                with self.assertRaises(AutorunError):
                    deliverable()
                side.unlink()
            with self.subTest(case="canonical_hardlink_rejected"):
                own_copy = root / "aa02-canonical-copy.json"
                own_copy.write_bytes(original)
                canonical.unlink()
                os.link(own_copy, canonical)
                try:
                    with self.assertRaises(AutorunError):
                        deliverable()
                finally:
                    if canonical.is_symlink() or canonical.exists():
                        canonical.unlink()
                    own_copy.unlink(missing_ok=True)
                canonical.write_bytes(original)
            self.assertTrue(deliverable(), "还原合法主绑定后应恢复可交付")
            before = inventory()
            deliverable()
            self.assertEqual(inventory(), before, "状态查看产生写入")


if __name__ == "__main__":
    unittest.main()
