# -*- coding: utf-8 -*-
"""R17-C 检查点5:正式产品链全链验证(审核 §F:不用局部字段测试代替整个入口)。

正向:创建项目→回答→当前评估→真实确认→权威采用(绑定 AI 评估)→生成(替身后端)
     →blocked_execution 真实状态→恢复链(累计预算继承)。
负向:权威判定三项不齐拒绝、新旧对象错配拒绝、根链接零 I/O、传输未知门+确认、
     worker 早期返回落权威状态、restart 断言未关联 main 记录拒绝。

替身边界:AI 适配器与受限后端为控制流替身(明确无外部副作用),只证明接口状态链;
不冒称真实模型或隔离。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from opencoding import aiconfig, advisor, generic_run, service
from opencoding.aiadapter import AIRequestError
from opencoding.generic_run import (
    GenericRunError,
    _validate_contract,
    load_receipt,
    run_generic_app,
    task_lineage,
    validate_runtime_roots,
    write_worker_outcome,
)
from opencoding.intake import QUESTION_DEFINITIONS
from opencoding.workbench import Workbench, WorkbenchError

from tests.test_product_service import _complete  # 复用既有完整会话构造

GOOD_CONTRACT = {
    "runtime": "python-stdlib",
    "entry_module": "app.main",
    "files": [{"path": "app/main.py", "purpose": "主程序"}],
    "functions": {"create_item": "创建记录", "list_items": "列出记录"},
    "data_dir": "app/data",
    "steps": [
        {"op": "call", "phase": "main", "function": "create_item", "args": ["锤子", 1]},
        {"op": "call", "phase": "main", "function": "list_items", "args": []},
        {"op": "call", "phase": "restart", "function": "list_items", "args": []},
        {"op": "assert", "phase": "restart", "saved": "restart_items",
         "contains": [{"name": "锤子"}]},
    ],
}


def eval_response(contract=None, choice="cli"):
    return {"real": True, "provider": "fake", "model": "fake-model",
            "request_id": "req-eval-" + os.urandom(4).hex(),
            "structured": {"request_kind": "evaluate", "choice": choice,
                           "summary": "测试评估", "reasons": ["r1"],
                           "recommendation": {"stack": "python",
                                              "rejected": [], "assumptions": [],
                                              "unknowns": [], "revisit_when": []},
                           "implementation_contract": contract or GOOD_CONTRACT}}


def impl_response():
    return {"real": True, "provider": "fake", "model": "fake-model",
            "request_id": "req-impl-" + os.urandom(4).hex(),
            "structured": {"summary": "实现", "files": [
                {"path": "app/main.py", "content": "VALUE = 1\n"}]}}


class FakeAdapter:
    """控制流替身:按脚本返回评估/实现响应;不发网络、不执行候选。"""

    real = True
    provider = "fake"
    model = "fake-model"

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete(self, messages, **kw):
        self.calls.append({"kind": kw.get("request_kind"),
                           "request_id": kw.get("request_id")})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class FullChainBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.project = self.workspace / "家庭借还登记"
        self.project.mkdir()
        self.view = _complete(self.project)
        self.sid = self.view["session"]["id"]
        self.bench = Workbench(self.workspace)

    def tearDown(self):
        self._tmp.cleanup()

    def _confirm_eval(self, adapter, contract=None):
        record = advisor.run_ai_evaluation(self.project, self.sid, adapter, run_id="t")
        current = service.session_view(self.project, self.sid)["session"]["revision"]
        record = advisor.confirm_evaluation(self.project, self.sid, record["evaluation_id"],
                                            expected_revision=current, accepted=True)
        return record

    def _adopt(self):
        return advisor.adopt_confirmed_evaluation(self.project, self.sid)

    def _generate(self, script, body=None):
        adapter = FakeAdapter(script)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.base / "appdata")}):
            aiconfig.save_config({"provider": "openai_compatible", "base_url": "https://x/v1",
                                  "api_key": "k", "model": "m"})
            with mock.patch("opencoding.aiadapter.adapter_from_config", return_value=adapter):
                start = self.bench.api(
                    "POST", f"/api/project/家庭借还登记/session/{self.sid}/generate", {}, body or {})
        return start, adapter


class AdoptionChainTests(FullChainBase):
    def test_adopt_binds_confirmed_ai_evaluation(self):
        """A 正向:采用记录必须绑定用户确认的 AI 评估(evaluation_id+契约摘要)。"""
        self._confirm_eval(FakeAdapter([eval_response()]))
        adopted = self._adopt()
        binding = adopted["adoption"]["ai_binding"]
        self.assertTrue(binding["evaluation_id"].startswith("eval-"))
        self.assertEqual(binding["source"], "confirmed_ai_evaluation")
        self.assertTrue(binding["contract_sha256"])
        saved = json.loads((self.project / ".opencoding" / "adoptions" / (self.sid + ".json"))
                           .read_text(encoding="utf-8"))
        self.assertEqual(saved["ai_binding"]["evaluation_id"], binding["evaluation_id"])

    def test_adopt_without_confirmed_evaluation_rejected(self):
        with self.assertRaises(ValueError):
            advisor.adopt_confirmed_evaluation(self.project, self.sid)

    def test_generate_rejects_adoption_without_ai_binding(self):
        """A 负向:旧世界采用记录(无 ai_binding)不能作为生成依据。"""
        self._confirm_eval(FakeAdapter([eval_response()]))
        self._adopt()
        path = self.project / ".opencoding" / "adoptions" / (self.sid + ".json")
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc.pop("ai_binding")
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(WorkbenchError) as ctx:
            self._generate([impl_response()])
        self.assertEqual(ctx.exception.code, "adoption_unbound")

    def test_generate_rejects_stale_evaluation_binding(self):
        """A 负向:采用绑定旧评估、当前是新评估 → 拒绝(新契约+旧计划错配)。"""
        self._confirm_eval(FakeAdapter([eval_response()]))
        self._adopt()
        self._confirm_eval(FakeAdapter([eval_response(contract=dict(GOOD_CONTRACT))]))
        with self.assertRaises(WorkbenchError) as ctx:
            self._generate([impl_response()])
        self.assertEqual(ctx.exception.code, "adoption_stale_evaluation")

    def test_generate_rejects_snapshot_missing_even_when_no_drift(self):
        """A 负向(S06):adopted=true+matches=false+input_snapshot_missing=true+drift=[] → 拒绝。"""
        self._confirm_eval(FakeAdapter([eval_response()]))
        self._adopt()
        path = self.project / ".opencoding" / "adoptions" / (self.sid + ".json")
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc.pop("input_snapshot")
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        status = service.adoption_input_status(self.project, self.sid)
        self.assertTrue(status["input_snapshot_missing"])
        self.assertFalse(status["matches"])
        self.assertEqual(status["drift"], [])
        with self.assertRaises(WorkbenchError) as ctx:
            self._generate([impl_response()])
        self.assertEqual(ctx.exception.code, "adoption_snapshot_missing")

    def test_generate_rejects_after_answer_changes(self):
        """A 负向:采用后用户又改了答案 → 漂移拒绝。"""
        self._confirm_eval(FakeAdapter([eval_response()]))
        self._adopt()
        service.submit_answer(self.project, self.sid,
                              service.session_view(self.project, self.sid)["session"]["revision"],
                              "outcome", "改主意了")
        with self.assertRaises(WorkbenchError) as ctx:
            self._generate([impl_response()])
        self.assertIn(ctx.exception.code, ("adoption_drifted", "evaluation_stale"))


class ForwardChainTests(FullChainBase):
    def test_full_chain_forward_stub_backend(self):
        """F 正向:评估→确认→采用→生成(替身后端)→真实 blocked_execution 状态。"""
        self._confirm_eval(FakeAdapter([eval_response()]))
        self._adopt()
        start, adapter = self._generate([impl_response()])
        self.assertEqual(start["status"], "running")
        run_id = start["run_id"]
        receipt = self._wait_done(run_id)
        self.assertEqual(receipt["status"], "blocked_execution")
        self.assertEqual(receipt["current_request"]["effect"], "received",
                         "已收到完整响应;等待执行环境不是网络结果未知")
        self.assertEqual(receipt["ai_requests_dispatched"], 1)
        self.assertEqual(len(adapter.calls), 1)
        # 收到响应后不被误挡(received ≠ unknown);但已有候选时不再被迫重新请求模型:
        # 先返回接续入口,明确 regenerate 才算新尝试。
        start2, _ = self._generate([impl_response()])
        self.assertEqual(start2["status"], "candidate_resumable")
        self.assertEqual(start2["run_id"], run_id)
        self.assertEqual(load_receipt(self.project, run_id)["ai_requests_dispatched"], 1,
                         "接续提示本身不得产生新请求")
        start3, _ = self._generate([impl_response()], {"mode": "regenerate"})
        self.assertEqual(start3["status"], "running")
        receipt3 = self._wait_done(start3["run_id"])
        self.assertEqual(receipt3["status"], "blocked_execution")
        # 同一逻辑任务累计额度记账:两次尝试各一请求
        state = generic_run.task_budget_state(self.project, generic_run.TASK_ID, self.sid)
        self.assertGreaterEqual(state["dispatched"], 2)
        self.assertEqual(state["ceiling"], 8)

    def _wait_done(self, run_id, limit=400):
        """轮询直到回执终态(上限约 20s)。

        放宽等待:整机高负载时 worker 线程调度抖动会让终态化超过原 2.5s 上限。
        等待上限不替代任何产品断言,超时仍判失败。
        """
        import time
        for _ in range(limit):
            doc = load_receipt(self.project, run_id)
            if doc and doc.get("status") != "running":
                return doc
            time.sleep(0.05)
        self.fail("运行未在限时内终态化")


class UnknownGateTests(FullChainBase):
    def test_transport_unknown_blocks_ack_then_lineage_inherited(self):
        """B:传输失败→效果未知门(同步返回旧 run)→确认→新尝试继承累计预算。"""
        self._confirm_eval(FakeAdapter([eval_response()]))
        self._adopt()
        start1, _ = self._generate([
            AIRequestError("read_idle_timeout", "读取空闲超时")])
        run1 = start1["run_id"]
        doc1 = self._wait_done(run1)
        self.assertEqual(doc1["status"], "failed_transport")
        self.assertEqual(doc1["current_request"]["effect"], "unknown")
        # 第二次 generate:路由同步返回 pending_verification(旧 run+原因),不造新 run
        start2, adapter2 = self._generate([impl_response()])
        self.assertEqual(start2["status"], "pending_verification")
        self.assertEqual(start2["run_id"], run1)
        self.assertEqual(start2["blocked_by"]["request_id"],
                         doc1["current_request"]["request_id"])
        self.assertEqual(adapter2.calls, [], "被门挡住时不得派发任何请求")
        # 磁盘上没有 start2 编号的新回执(没有假 running)
        self.assertIsNone(load_receipt(self.project, "gen-" + "0" * 12))
        # 确认(返回旧请求详情与累计预算)
        ack = self.bench.api("POST", f"/api/project/家庭借还登记/session/{self.sid}/ack-unknown",
                             {}, {"run_id": run1, "note": "测试确认"})
        self.assertEqual(ack["acknowledged"]["old_request_id"],
                         doc1["current_request"]["request_id"])
        self.assertEqual(ack["budget_lineage"]["inherited_ai_requests"], 1)
        # 第三次:新尝试真实派发,回执继承旧请求事实
        start3, _ = self._generate([impl_response()])
        self.assertEqual(start3["status"], "running")
        doc3 = self._wait_done(start3["run_id"])
        self.assertEqual(doc3["status"], "blocked_execution")
        lineage = doc3["budget_lineage"]
        self.assertEqual(lineage["inherited_ai_requests"], 1)
        self.assertEqual(lineage["inherited_runs"][0]["run_id"], run1)

    def test_worker_outcome_writes_truthful_receipt(self):
        """B-4/S09:worker 早期返回必须落权威状态,gen-status 不再永远 unknown。"""
        doc = write_worker_outcome(self.project, "gen-" + "a" * 12,
                                   {"status": "pending_verification", "session_id": self.sid,
                                    "blocked_by": {"run_id": "gen-" + "b" * 12},
                                    "note": "旧请求待核实"})
        self.assertEqual(doc["status"], "not_started")
        self.assertEqual(doc["blocked_by"]["run_id"], "gen-" + "b" * 12)
        again = load_receipt(self.project, doc["run_id"])
        self.assertEqual(again["run_id"], doc["run_id"])

    def _wait_done(self, run_id, limit=400):
        """轮询直到回执终态(上限约 20s)。

        放宽等待:整机高负载时 worker 线程调度抖动会让终态化超过原 2.5s 上限。
        等待上限不替代任何产品断言,超时仍判失败。
        """
        import time
        for _ in range(limit):
            doc = load_receipt(self.project, run_id)
            if doc and doc.get("status") != "running":
                return doc
            time.sleep(0.05)
        self.fail("运行未在限时内终态化")


class RootBoundaryTests(unittest.TestCase):
    """D:运行状态根在首次 I/O 前校验;拒绝时零派发、零控制文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.project = self.base / "p"
        self.project.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_state_root_as_file_rejected_before_any_io(self):
        # `.opencoding` 为普通文件(非目录):命中同一校验分支(exists && !is_dir)。
        # Windows 建目录符号链接需特权;链接分支在 Linux 审核环境已证(S02),
        # 本机另有 test_validate_runtime_roots_rejects_symlink_when_supported。
        (self.project / ".opencoding").write_text("not a dir", encoding="utf-8")
        fake_adapter = FakeAdapter([impl_response()])
        contract = _validate_contract(GOOD_CONTRACT)
        with self.assertRaises(GenericRunError) as ctx:
            run_generic_app(self.project, "目标", fake_adapter, contract=contract)
        self.assertEqual(ctx.exception.code, "runtime_root_unsafe")
        self.assertEqual(fake_adapter.calls, [], "根校验失败时不得派发")
        # 零控制文件:链接/占位文件未被改写,没有 generic_runs 产生
        self.assertEqual((self.project / ".opencoding").read_text(encoding="utf-8"), "not a dir")
        self.assertFalse((self.project / ".opencoding" / "generic_runs").exists())

    def test_validate_runtime_roots_rejects_symlink_when_supported(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.project / ".opencoding"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("当前环境无符号链接权限;Windows 用文件占位等价验证")
        with self.assertRaises(GenericRunError) as ctx:
            validate_runtime_roots(self.project)
        self.assertEqual(ctx.exception.code, "runtime_root_unsafe")
        self.assertEqual(list(outside.iterdir()), [], "链接目标零新增")


class ContractCoverageTests(unittest.TestCase):
    """C:restart 断言必须关联 main 建立的记录;空预期须显式声明。"""

    def test_unlinked_restart_assert_rejected(self):
        """保留 main 写入(存在记录标识),但 restart 断言不引用该记录 → 拒绝。"""
        bad = json.loads(json.dumps(GOOD_CONTRACT))
        bad["steps"] = [s for s in bad["steps"] if s.get("phase") != "restart"]
        bad["steps"].append({"op": "call", "phase": "restart", "function": "list_items", "args": []})
        bad["steps"].append({"op": "assert", "phase": "restart", "equals": []})
        with self.assertRaises(GenericRunError) as ctx:
            _validate_contract(bad)
        self.assertEqual(ctx.exception.code, "restart_assert_unlinked")

    def test_weak_scalar_not_accepted_as_record_identity(self):
        """CP5 §6/T06:数量 1 与版本号 1 同值不得冒充"业务记录仍在"。"""
        bad = json.loads(json.dumps(GOOD_CONTRACT))
        bad["steps"] = [
            {"op": "call", "phase": "main", "function": "create_item", "args": ["扳手", 1]},
            {"op": "call", "phase": "restart", "function": "read_state", "args": []},
            {"op": "assert", "phase": "restart", "equals": {"schema_version": 1}},
        ]
        with self.assertRaises(GenericRunError) as ctx:
            _validate_contract(bad)
        self.assertIn(ctx.exception.code, ("restart_assert_unlinked", "record_identity_missing"))
        # bad_reference 场景:磁盘只写 {} 的程序不得通过;加了真实标识断言才可能通过
        ok = json.loads(json.dumps(bad))
        ok["steps"][-1] = {"op": "assert", "phase": "restart", "contains": [{"name": "扳手"}]}
        normalized = _validate_contract(ok)
        self.assertEqual(normalized["business_identities"], ["扳手"])

    def test_numeric_only_contract_has_no_record_identity(self):
        """只有数值实参的契约无法标识业务记录,派发前必须重评。"""
        bad = json.loads(json.dumps(GOOD_CONTRACT))
        bad["steps"] = [
            {"op": "call", "phase": "main", "function": "create_item", "args": [1, 2]},
            {"op": "call", "phase": "restart", "function": "list_items", "args": []},
            {"op": "assert", "phase": "restart", "equals": {"count": 1}},
        ]
        with self.assertRaises(GenericRunError) as ctx:
            _validate_contract(bad)
        self.assertEqual(ctx.exception.code, "record_identity_missing")

    def test_empty_expectation_with_justification_accepted(self):
        ok = json.loads(json.dumps(GOOD_CONTRACT))
        ok["steps"] = [s for s in ok["steps"] if s.get("phase") != "restart"]
        ok["steps"].append({"op": "call", "phase": "restart", "function": "list_items", "args": []})
        ok["steps"].append({"op": "assert", "phase": "restart", "equals": []})
        ok["restart_empty_expectation"] = {
            "requirement_ref": "需求第 3 条:每次启动都从空清单开始",
            "justification": "需求明确:程序启动时清单为空,由用户逐步录入"}
        normalized = _validate_contract(ok)
        self.assertEqual(normalized["restart_empty_expectation"]["justification"][:4],
                         "需求明确")

    def test_empty_expectation_without_justification_rejected(self):
        ok = json.loads(json.dumps(GOOD_CONTRACT))
        ok["steps"] = [s for s in ok["steps"] if s.get("phase") == "restart"]
        ok["steps"].append({"op": "assert", "phase": "restart", "equals": []})
        ok["restart_empty_expectation"] = {"justification": ""}
        with self.assertRaises(GenericRunError):
            _validate_contract(ok)

    def test_linked_good_contract_passes(self):
        normalized = _validate_contract(GOOD_CONTRACT)
        self.assertEqual(normalized["runtime"], "python-stdlib")
        self.assertIsNone(normalized["restart_empty_expectation"])


if __name__ == "__main__":
    unittest.main()
