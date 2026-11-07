from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from opencoding import service
from opencoding.business_scenarios import confirm_scenarios
from opencoding.stage_report import stage_report, summarize_gate_receipt
from tests.test_product_service import _complete, _inventory


def journeys():
    rows = [
        ("search", "知识搜索", "搜索收不到验证码", "出现关联指南", "空结果明确提示"),
        ("questions", "提问与审核", "提交问题并审核答复", "审核后公开答复", "联系方式不进入公开响应"),
        ("quotes-import", "报价导入", "预览并提交报价批次", "可查来源币种单位时效", "错误批次不部分提交"),
        ("merchant-link", "报价导购", "跳转已核验商家链接", "跳转可归因", "过期报价不可购买，跳转不算成交"),
        ("consultation", "咨询与后台", "提交咨询并管理状态", "申请状态持久化", "未认证管理操作被拒绝"),
    ]
    return [{"id": i, "title": t, "actor": "用户与管理员", "action": a, "result": r,
             "source": "confirmed-project-requirements", "input": "合成测试数据", "failure": f,
             "acceptance": [r, f]} for i, t, a, r, f in rows]


class JourneyReportTests(unittest.TestCase):
    def test_five_journeys_fact_binding_and_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            view = _complete(root)
            session = view["session"]["id"]
            fact = confirm_scenarios(root, session, journeys())
            self.assertEqual(confirm_scenarios(root, session, journeys()), fact)
            evaluated = service.evaluate_session(root, session)
            implementations = [t for t in evaluated["task_plan"]["tasks"] if t["action"]["type"] == "implement_feature"]
            self.assertEqual({t["action"]["scenario_id"] for t in implementations}, {s["id"] for s in journeys()})
            self.assertTrue(any(t["applied"] and t["kind"] == "business_scenarios" for t in evaluated["fact_consideration"]))
            for task in implementations:
                expected = next(s for s in journeys() if s["id"] == task["action"]["scenario_id"])
                self.assertTrue(set(expected["acceptance"]).issubset(task["acceptance"]))
            service.adopt_evaluation_plan(root, session)
            changed = deepcopy(journeys())
            changed[0]["result"] = "按语言筛选关联指南"
            confirm_scenarios(root, session, changed)
            self.assertNotEqual(service.evaluate_session(root, session)["input_digest"], evaluated["input_digest"])
            self.assertFalse(service.adoption_input_status(root, session)["matches"])
            service.submit_answer(root, session, view["session"]["revision"], "outcome", "新增其他业务结果")
            self.assertIn("business_scenarios:requirements_changed", service.evaluate_session(root, session)["recommendation"]["unresolved"])

    def test_gate_and_stage_status_do_not_promote_adoption_or_command_success(self):
        receipt = {"classification": "scope-violation", "outputs": {"exit_code": 4, "changed_paths": [".cache"],
                   "gate_execution_evidence": {"entries": [{"kind": "command", "gate_id": "test", "process_exit_code": 0, "status": "pass"}]}}}
        summary = summarize_gate_receipt(receipt)
        self.assertEqual(summary["overall"], "failed")
        self.assertEqual(summary["scope"], "failed")
        self.assertEqual(summary["commands"][0]["process_exit_code"], 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session = _complete(root)["session"]["id"]
            service.adopt_evaluation_plan(root, session)
            receipts = root / ".governance/receipts"
            receipts.mkdir(parents=True)
            (receipts / "failed-attempt.json").write_text(json.dumps(receipt), encoding="utf-8")
            before = _inventory(root)
            report = stage_report(root, session)
            self.assertEqual(report["execution"]["status"], "not_initialized")
            self.assertEqual(report["user_acceptance"], "not_recorded")
            self.assertEqual(report["governance"][0]["overall"], "failed")
            self.assertEqual(before, _inventory(root))
