# -*- coding: utf-8 -*-
"""CP5 §5.2/§5.3:同一逻辑任务累计额度真正限制派发;已收到候选可接续。

对照审核 T04/T05 的原始反例：
- T04：同 session、同目标、同采用，max_repair_rounds=0 → 上限 4；
  第五次必须被拒绝（不是继续派发也不是无限grant）；
- T05：已拿到候选只是缺后端，再点生成不得被迫重新调用模型。

替身边界：适配器与受限后端为控制流替身；不发网络、不运行候选、不冒充真实模型。
"""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from opencoding import aiconfig, advisor, generic_run, service
from opencoding.aiadapter import AIRequestError
from opencoding.facts import add_fact
from opencoding.workbench import Workbench, WorkbenchError

from tests.test_product_full_chain_v5 import FakeAdapter, GOOD_CONTRACT, eval_response, impl_response
from tests.test_product_generic_run_v2 import GOOD_MAIN, GOOD_SELFTEST, _contract
from tests.test_product_service import _complete


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace.mkdir(parents=True)
        self.project = self.workspace / "家庭借还登记"
        self.project.mkdir()
        self.view = _complete(self.project)
        self.sid = self.view["session"]["id"]
        self.bench = Workbench(self.workspace)
        self.contract = generic_run._validate_contract(_contract())
        self.base_limit = 4  # max_repair_rounds=0 → 4 + 2*0

    def tearDown(self):
        self._tmp.cleanup()

    def _adopt_binding(self):
        payload = eval_response(choice="cli")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.workspace / "appdata")}):
            aiconfig.save_config({"provider": "openai_compatible", "base_url": "https://x/v1",
                                  "api_key": "k", "model": "m"})
            record = advisor.run_ai_evaluation(self.project, self.sid, FakeAdapter([payload]),
                                               run_id="t")
        revision = service.session_view(self.project, self.sid)["session"]["revision"]
        advisor.confirm_evaluation(self.project, self.sid, record["evaluation_id"],
                                   expected_revision=revision, accepted=True)
        adopted = advisor.adopt_confirmed_evaluation(self.project, self.sid)["adoption"]
        return {"session_id": self.sid,
                "evaluation_id": record["evaluation_id"],
                "plan_digest": adopted["plan_digest"],
                "contract_sha256": (adopted["ai_binding"] or {}).get("contract_sha256")}

    def _timeout_adapter(self):
        class Timeout:
            real = False
            provider = "test"
            model = "x"
            calls = 0

            def complete(self, *a, **k):
                type(self).calls += 1
                raise AIRequestError("read_idle_timeout", "读取空闲超时")

        return Timeout()

    def _dispatch_once(self, adapter, run_id, binding):
        return generic_run.run_generic_app(self.project, "家庭借还登记", adapter,
                                           contract=self.contract, run_id=run_id,
                                           session_id=self.sid, adoption_binding=binding,
                                           max_repair_rounds=0, acknowledged_unknown=True)


class TaskBudgetTests(_Base):
    def test_cumulative_limit_blocks_fifth_dispatch(self):
        """T04 原反例:四次用尽后第五次必须拒绝,不再签发新额度。"""
        binding = self._adopt_binding()
        adapter = self._timeout_adapter()
        for index in range(self.base_limit):
            receipt = self._dispatch_once(adapter, "gen-000000000b%02d" % (index + 1), binding)
            self.assertEqual(receipt["status"], "failed_transport", receipt.get("failure"))
            self.assertEqual(receipt["ai_requests_dispatched"], 1)
        state = generic_run.task_budget_state(self.project, generic_run.TASK_ID, self.sid)
        self.assertEqual(state["ceiling"], self.base_limit)
        self.assertEqual(state["dispatched"], self.base_limit)
        self.assertEqual(state["remaining"], 0)
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            self._dispatch_once(adapter, "gen-000000000b99", binding)
        self.assertEqual(ctx.exception.code, "task_budget_exhausted")
        self.assertEqual(adapter.calls, self.base_limit, "被拒时不得发出新请求")
        # 备注:第五次不能留下假回执/假 running
        self.assertIsNone(generic_run.load_receipt(self.project, "gen-000000000b99"))

    def test_extension_is_explicit_authorization_not_ack(self):
        """追加额度是新的明确授权:要理由、要确认,并留痕。"""
        binding = self._adopt_binding()
        adapter = self._timeout_adapter()
        for index in range(self.base_limit):
            self._dispatch_once(adapter, "gen-000000000e%02d" % (index + 1), binding)
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            generic_run.extend_task_budget(self.project, generic_run.TASK_ID, self.sid,
                                           amount=2, reason="", issued_by="x")
        self.assertEqual(ctx.exception.code, "task_budget_reason_required")
        state = generic_run.extend_task_budget(self.project, generic_run.TASK_ID, self.sid,
                                               amount=2, reason="用户确认追加两次尝试",
                                               issued_by="曦哥")
        self.assertEqual(state["ceiling"], self.base_limit + 2)
        self.assertEqual(state["remaining"], 2)
        self.assertEqual(state["extensions"][-1]["amount"], 2)
        receipt = self._dispatch_once(adapter, "gen-000000000e99", binding)
        self.assertEqual(receipt["ai_requests_dispatched"], 1)
        self.assertEqual(receipt["task_budget"]["ceiling"], self.base_limit + 2)


class ReceivedMaterialTests(_Base):
    def _good_adapter(self):
        class Adapter:
            real = True
            provider = "fake"
            model = "fake-model"
            calls = 0

            def complete(self, messages, **kw):
                type(self).calls += 1
                return {"real": True, "provider": "fake", "model": "fake-model",
                        "request_id": "req-impl-" + os.urandom(4).hex(),
                        "structured": {"summary": "实现", "files": [
                            {"path": "app/main.py", "content": GOOD_MAIN},
                            {"path": "app/selftest.py", "content": GOOD_SELFTEST}]}}

        return Adapter()

    def test_received_material_exists_whenever_flagged_received(self):
        """状态received不得出现在本体之前:凡是标received,磁盘上必须有完整本体。"""
        binding = self._adopt_binding()
        adapter = self._good_adapter()
        receipt = self._dispatch_once(adapter, "gen-0000000ce001", binding)
        current = receipt.get("current_request") or {}
        self.assertEqual(current.get("effect"), "received")
        material = generic_run.load_received_material(self.project, "gen-0000000ce001", 1)
        self.assertIsNotNone(material, "标记received却没有落盘本体")
        self.assertTrue(material["structured"]["files"])
        self.assertEqual(material["request_id"], current.get("request_id"))
        attempt = receipt["attempts"][0]
        self.assertEqual(attempt["received_material"]["sha256"],
                         current["received_material"]["sha256"])

    def test_resume_does_not_call_model_again(self):
        """T05 原反例:候选已在,环境好了继续不应重新请求模型。"""
        binding = self._adopt_binding()
        adapter = self._good_adapter()
        receipt = self._dispatch_once(adapter, "gen-00000beef001", binding)
        self.assertIn(receipt["status"], ("blocked_execution", "delivered", "exhausted", "failed"))
        pending = generic_run.resumable_candidate(self.project, self.sid)
        self.assertIsNotNone(pending, "应存在可接续候选")
        self.assertTrue(pending["resumable"])
        self.assertEqual(pending["run_id"], "gen-00000beef001")
        before = adapter.calls
        resumed = generic_run.resume_saved_candidate(self.project, "gen-00000beef001")
        self.assertEqual(adapter.calls, before, "接续不得发起新 AI 请求")
        self.assertIn(resumed["status"], ("blocked_execution", "delivered", "failed"))
        self.assertTrue(resumed["attempts"][-1].get("resumed_from_attempt"))
        self.assertEqual(resumed["attempts"][-1]["ai_request_dispatched"], False)

    def test_workbench_prefers_resume_then_accepts_regenerate(self):
        """工作台再次生成:默认回接续入口;明确 regenerate 才走新尝试。

        附带修复(审计 C6FIX3):regenerate 派发的是**后台 worker 线程**——
        测试必须在 fixture 拆除前有界等待该 worker 到达真实终态并核对其
        收尾,否则拆除竞态会把后台异常(采用记录缺失/写回执 FileExistsError)
        漏成测试噪声。不吞异常、不全局杀线程、不把"路由同步返回"当完成。
        """
        import time as _time

        binding = self._adopt_binding()
        adapter = self._good_adapter()
        self._dispatch_once(adapter, "gen-00000beef002", binding)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.workspace / "appdata")}):
            aiconfig.save_config({"provider": "openai_compatible", "base_url": "https://x/v1",
                                  "api_key": "k", "model": "m"})
            with mock.patch("opencoding.aiadapter.adapter_from_config", return_value=adapter):
                first = self.bench.api(
                    "POST", f"/api/project/家庭借还登记/session/{self.sid}/generate", {}, {})
                self.assertEqual(first["status"], "candidate_resumable")
                before = adapter.calls
                second = self.bench.api(
                    "POST", f"/api/project/家庭借还登记/session/{self.sid}/generate", {},
                    {"mode": "regenerate"})
        self.assertEqual(adapter.calls, before, "路由同步返回,不当场派发")
        self.assertIn(second.get("status"), ("running", "already_running", "pending_verification"))
        if second.get("status") == "running":
            # 有界等待本测试派发的 worker 到达终态(超时=失败,如实报告)
            run_id = str(second["run_id"])
            deadline = _time.monotonic() + 30.0
            receipt = None
            while _time.monotonic() < deadline:
                receipt = generic_run.load_receipt(self.project, run_id)
                if receipt is not None and receipt.get("status") != "running":
                    break
                _time.sleep(0.05)
            self.assertIsNotNone(receipt, "有界等待内 worker 未写出运行回执")
            self.assertIn(receipt.get("status"),
                          ("blocked_execution", "delivered", "failed", "failed_transport",
                           "exhausted", "cancelled"),
                          "worker 必须到达真实终态:" + str((receipt or {}).get("failure")))
        else:
            # already_running/pending_verification:没有新 worker,无需等待
            self.assertIn(second.get("status"),
                          ("already_running", "pending_verification"))

    def test_resume_refuses_when_input_drifted(self):
        """事实已变的旧候选不许接续。"""
        binding = self._adopt_binding()
        adapter = self._good_adapter()
        self._dispatch_once(adapter, "gen-00000beef003", binding)
        add_fact(self.project, self.sid, content="单设备离线使用", source_type="user",
                 source_ref="authorized-test-fixture")
        pending = generic_run.resumable_candidate(self.project, self.sid)
        self.assertIsNotNone(pending)
        self.assertFalse(pending["resumable"])
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            generic_run.resume_saved_candidate(self.project, "gen-00000beef003")
        self.assertEqual(ctx.exception.code, "adoption_input_drift")

    def test_resume_route_errors_are_visible(self):
        binding = self._adopt_binding()
        self.assertIsNotNone(binding)
        with self.assertRaises(WorkbenchError) as ctx:
            self.bench.api("POST", f"/api/project/家庭借还登记/session/{self.sid}/resume",
                           {}, {"run_id": "gen-0000deadbeef"})
        self.assertEqual(ctx.exception.code, "run_not_found")


class TaskBudgetLedgerShapeTests(_Base):
    """FIX-03(原 C6-03 V03):合法 JSON 但**结构损坏**的账本不得被规范化为
    "未消费"再放行派发;展示/派发/扩额共用同一校验入口;拒绝时原字节保持。"""

    def _ledger_file(self) -> Path:
        files = sorted((self.project / ".opencoding" / "task-budgets").glob("*.json"))
        self.assertEqual(len(files), 1, "同任务账本唯一")
        return files[0]

    def _burn_four(self, adapter, binding):
        for index in range(self.base_limit):
            receipt = self._dispatch_once(adapter, "gen-0000000c6f%02d" % (index + 1), binding)
            self.assertEqual(receipt["status"], "failed_transport", receipt.get("failure"))
            self.assertEqual(receipt["ai_requests_dispatched"], 1)
        state = generic_run.task_budget_state(self.project, generic_run.TASK_ID, self.sid)
        self.assertEqual(state["remaining"], 0)

    def _corrupt(self, mutate):
        """派发 4 次后按 mutate 破坏账本结构;返回(损坏后字节 SHA, 适配器)。"""
        adapter = self._timeout_adapter()
        binding = self._adopt_binding()
        self._burn_four(adapter, binding)
        path = self._ledger_file()
        doc = json.loads(path.read_text(encoding="utf-8"))
        mutate(doc)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        return hashlib.sha256(path.read_bytes()).hexdigest(), adapter, binding

    def _assert_blocked(self, sha_corrupt, adapter, binding):
        path = self._ledger_file()
        # 只读视图:如实标注不可读,remaining=0,不显示可用额度
        state = generic_run.task_budget_state(self.project, generic_run.TASK_ID, self.sid)
        self.assertEqual(state["remaining"], 0)
        self.assertIn("ledger_unreadable", state)
        self.assertIn("ledger_shape_invalid", state["ledger_unreadable"])
        # 派发:拒绝且零额外调用
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            self._dispatch_once(adapter, "gen-0000000c6fff", binding)
        self.assertEqual(ctx.exception.code, "task_budget_ledger_unreadable")
        self.assertEqual(adapter.calls, self.base_limit, "损坏账本不得触发新请求")
        # 扩额:同样拒绝
        with self.assertRaises(generic_run.GenericRunError) as ctx2:
            generic_run.extend_task_budget(self.project, generic_run.TASK_ID, self.sid,
                                           amount=2, reason="测试损坏账本扩额拒绝",
                                           issued_by="test")
        self.assertEqual(ctx2.exception.code, "task_budget_ledger_unreadable")
        # 原字节保持:不被规范化覆盖
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), sha_corrupt)
        self.assertFalse((self.project / "app" / "main.py").exists())

    def test_runs_list_is_rejected_and_bytes_preserved(self):
        sha, adapter, binding = self._corrupt(
            lambda doc: doc.__setitem__("runs", [{"gen-x": {"dispatched": 1}}]))
        self._assert_blocked(sha, adapter, binding)

    def test_dispatched_string_is_rejected(self):
        def _break(doc):
            for entry in doc["runs"].values():
                entry["dispatched"] = "1"
        sha, adapter, binding = self._corrupt(_break)
        self._assert_blocked(sha, adapter, binding)

    def test_dispatched_missing_is_rejected(self):
        def _break(doc):
            for entry in doc["runs"].values():
                entry.pop("dispatched")
        sha, adapter, binding = self._corrupt(_break)
        self._assert_blocked(sha, adapter, binding)

    def _ceiling_case(self, bad):
        import hashlib
        adapter = self._timeout_adapter()
        binding = self._adopt_binding()
        self._burn_four(adapter, binding)
        path = self._ledger_file()
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["ceiling"] = bad
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        self._assert_blocked(sha, adapter, binding)

    def test_ceiling_bool_rejected(self):
        self._ceiling_case(True)

    def test_ceiling_string_rejected(self):
        self._ceiling_case("4")

    def test_ceiling_none_rejected(self):
        self._ceiling_case(None)

    def test_dispatched_bool_rejected(self):
        def _break(doc):
            for entry in doc["runs"].values():
                entry["dispatched"] = True
        sha, adapter, binding = self._corrupt(_break)
        self._assert_blocked(sha, adapter, binding)

    def test_identity_mismatch_rejected(self):
        """结构合法但 task/session 身份与请求不符 → 不得当成本任务的空账本。"""
        adapter = self._timeout_adapter()
        binding = self._adopt_binding()
        self._burn_four(adapter, binding)
        path = self._ledger_file()
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["task_id"] = "other-task"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        state = generic_run.task_budget_state(self.project, generic_run.TASK_ID, self.sid)
        self.assertEqual(state["remaining"], 0)
        self.assertIn("identity_mismatch", state.get("ledger_unreadable") or "")
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            self._dispatch_once(adapter, "gen-0000000c6ffe", binding)
        self.assertEqual(ctx.exception.code, "task_budget_ledger_unreadable")
        self.assertEqual(adapter.calls, self.base_limit)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), sha)

    def test_schema_version_invalid_rejected(self):
        sha, adapter, binding = self._corrupt(
            lambda doc: doc.__setitem__("schema_version", "9.9"))
        self._assert_blocked(sha, adapter, binding)

    def test_valid_ledger_still_works_after_checks(self):
        """正对照:结构合法的已消费账本继续正常计账、合法扩额可用。"""
        adapter = self._timeout_adapter()
        binding = self._adopt_binding()
        self._burn_four(adapter, binding)
        state = generic_run.extend_task_budget(self.project, generic_run.TASK_ID, self.sid,
                                               amount=1, reason="合法追加一次尝试",
                                               issued_by="test-valid")
        self.assertEqual(state["remaining"], 1)
        receipt = self._dispatch_once(adapter, "gen-0000000c6faa", binding)
        self.assertEqual(receipt["ai_requests_dispatched"], 1)
        self.assertEqual(adapter.calls, self.base_limit + 1)
        state2 = generic_run.task_budget_state(self.project, generic_run.TASK_ID, self.sid)
        self.assertEqual(state2["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
