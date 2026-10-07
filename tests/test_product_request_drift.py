# -*- coding: utf-8 -*-
"""CP5:请求进行中事实发生变化时,旧候选必须不能提交。

覆盖三道边界:派发前(直接拒绝)、收到响应后(不入验证)、隔离执行前/提交前。
替身边界:适配器为控制流替身(不发网络、不执行候选);不等于真实模型或真实隔离背书。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from opencoding import aiconfig, advisor, generic_run, service
from opencoding.facts import add_fact

from tests.test_product_full_chain_v5 import FakeAdapter, GOOD_CONTRACT, eval_response
from tests.test_product_generic_run_v2 import GOOD_MAIN, GOOD_SELFTEST, _contract
from tests.test_product_service import _complete


def _impl_payload():
    return {"real": True, "provider": "fake", "model": "fake-model",
            "request_id": "req-impl-" + os.urandom(4).hex(),
            "structured": {"summary": "实现", "files": [
                {"path": "app/main.py", "content": GOOD_MAIN},
                {"path": "app/selftest.py", "content": GOOD_SELFTEST}]}}


class MidRequestFactTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace.mkdir(parents=True)
        self.project = self.workspace / "家庭借还登记"
        self.project.mkdir()
        self.view = _complete(self.project)
        self.sid = self.view["session"]["id"]

    def tearDown(self):
        self._tmp.cleanup()

    def _adopt_binding(self, choice="cli"):
        payload = eval_response(choice=choice)
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

    def _run(self, binding, mutate=None, run_id="gen-0000d1f10001"):
        def respond(messages, **kw):
            if mutate is not None:
                mutate()
            return _impl_payload()

        class Adapter:
            real = True
            provider = "fake"
            model = "fake-model"

            def complete(self, messages, **kw):
                return respond(messages, **kw)

        contract = generic_run._validate_contract(_contract())
        return generic_run.run_generic_app(self.project, "家庭借还登记", Adapter(),
                                           contract=contract, run_id=run_id,
                                           session_id=self.sid, adoption_binding=binding,
                                           max_repair_rounds=0)

    def test_fact_change_during_response_blocks_commit(self):
        """请求期间新增事实:收到响应后即止步,不验证、不提交,产物不落盘。"""
        binding = self._adopt_binding()
        receipt = self._run(binding, mutate=lambda: add_fact(
            self.project, self.sid, content="单设备离线使用", source_type="user",
            source_ref="authorized-test-fixture"))
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("相关有效输入已变化", receipt["failure"])
        self.assertTrue(receipt["attempts"][0].get("input_drift"))
        self.assertFalse((self.project / "app" / "main.py").exists())
        self.assertIsNone(receipt.get("transaction_id"))

    def test_fact_change_before_dispatch_rejected(self):
        """派发前就已漂移:直接拒绝,零回执、零写面。"""
        binding = self._adopt_binding()
        add_fact(self.project, self.sid, content="界面文案使用简体中文",
                 source_type="user", source_ref="authorized-test-fixture")
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            self._run(binding, run_id="gen-0000d1f10002")
        self.assertEqual(ctx.exception.code, "adoption_input_drift")
        self.assertFalse((self.project / ".opencoding" / "runs").exists()
                         and list((self.project / ".opencoding" / "runs").glob("gen-0000d1f10002*")))

    def test_no_fact_change_does_not_false_block(self):
        """无漂移时不得误挡:候选照常保留,状态如实为受限后端不可用。"""
        binding = self._adopt_binding()
        receipt = self._run(binding, run_id="gen-0000d1f10003")
        self.assertNotIn("相关有效输入已变化", str(receipt.get("failure")))
        self.assertFalse(receipt["attempts"][0].get("input_drift"))
        self.assertIn(receipt["status"], ("blocked_execution", "delivered", "exhausted", "failed"))

    def test_missing_adoption_after_start_treated_as_drift(self):
        """运行期间采用记录被删:视为输入已变,拒绝以旧候选收尾。"""
        binding = self._adopt_binding()
        target = self.project / ".opencoding" / "adoptions" / (self.sid + ".json")

        def delete_adoption():
            target.unlink()

        receipt = self._run(binding, mutate=delete_adoption, run_id="gen-0000d1f10004")
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("采用记录已不存在", receipt["failure"])

    def test_lineage_recorded_when_drift(self):
        """漂移拒单必须落可核查回执(有失败原因,不给 running 假状态)。"""
        binding = self._adopt_binding()
        receipt = self._run(binding, mutate=lambda: add_fact(
            self.project, self.sid, content="单设备离线使用", source_type="user",
            source_ref="authorized-test-fixture"), run_id="gen-0000d1f10005")
        saved = generic_run.load_receipt(self.project, "gen-0000d1f10005")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(json.loads(json.dumps(saved, ensure_ascii=False))["failure"],
                         receipt["failure"])


if __name__ == "__main__":
    unittest.main()
