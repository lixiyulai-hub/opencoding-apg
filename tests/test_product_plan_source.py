# -*- coding: utf-8 -*-
"""CP5 §4.2 / REAL-09:AI 确认的决策必须**真正变成正式计划**,而不是在规则计划上贴绑定。

本组测试只回答一个问题:用户确认的 AI 首选变化时,落到正式执行目标里的
recommendation / task_plan / plan_digest / executor_mapping / 会话展示 / 漂移核对
是否同步变化且可复现;以及后来出现的硬约束事实能否让旧 AI 计划失效。

替身边界:AI 适配器为控制流替身(明确无外部副作用),不发网络、不执行候选;
不等于真实模型调用或真实隔离背书。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from opencoding import aiconfig, advisor, plansource, service
from opencoding.aiadapter import AIRequestError  # noqa: F401 - 保证替身通道可用
from opencoding.facts import add_fact
from opencoding.workbench import Workbench, WorkbenchError

from tests.test_product_full_chain_v5 import FakeAdapter, GOOD_CONTRACT, eval_response
from tests.test_product_service import _complete


def eval_payload(choice, contract=None):
    return {"real": True, "provider": "fake", "model": "fake-model",
            "request_id": "req-src-" + os.urandom(4).hex(),
            "structured": {"request_kind": "evaluate", "choice": choice,
                           "summary": "测试评估", "reasons": ["r1"],
                           "recommendation": {"stack": "python + 本地存储",
                                              "rejected": [], "assumptions": [],
                                              "unknowns": [], "revisit_when": []},
                           "implementation_contract": contract or GOOD_CONTRACT}}


class PlanSourceTests(unittest.TestCase):
    """AI 首选必须真的改掉正式计划。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace.mkdir(parents=True)
        self.project = self.workspace / "社区借还登记"
        self.project.mkdir()
        self.view = _complete(self.project)
        self.sid = self.view["session"]["id"]
        self.bench = Workbench(self.workspace)

    def tearDown(self):
        self._tmp.cleanup()

    def _evaluate_then_confirm(self, choice):
        adapter = FakeAdapter([eval_payload(choice)])
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.workspace / "appdata")}):
            aiconfig.save_config({"provider": "openai_compatible", "base_url": "https://x/v1",
                                  "api_key": "k", "model": "m"})
            record = advisor.run_ai_evaluation(self.project, self.sid, adapter, run_id="t")
        revision = service.session_view(self.project, self.sid)["session"]["revision"]
        advisor.confirm_evaluation(self.project, self.sid, record["evaluation_id"],
                                   expected_revision=revision, accepted=True)
        return record

    def test_ai_choice_produces_different_official_plan(self):
        """AI 首选(cli)与规则首选(网页)不同时,正式计划与文档必须真的不同。"""
        rule_evaluated = service.evaluate_session(self.project, self.sid)
        rule_primary = (rule_evaluated["recommendation"]["platforms"] or {}).get("primary")
        self.assertNotEqual(str(rule_primary).lower(), "cli",
                            "夹具规则首选应当不是 cli,否则本用例失去对照意义")
        self._evaluate_then_confirm("cli")
        adopted = advisor.adopt_confirmed_evaluation(self.project, self.sid)["adoption"]
        binding = adopted["ai_binding"]

        # 1) 正式计划本体跟随 AI 首选
        self.assertEqual((adopted["recommendation"]["platforms"] or {}).get("primary"), "cli")
        # 2) 计划摘要真的不同(不是只多了 ai_binding)
        self.assertNotEqual(binding["rule_plan_digest"], binding["ai_plan_digest"])
        self.assertTrue(binding["plan_changed_by_ai"])
        self.assertEqual(binding["plan_follows_ai"], True)
        # 3) 任务图与文档随之变化
        ai_evaluated = service.evaluate_session(self.project, self.sid)
        self.assertNotEqual(ai_evaluated["task_plan"], rule_evaluated["task_plan"])
        self.assertEqual(service.plan_digest(ai_evaluated["task_plan"]), binding["ai_plan_digest"])
        # 4) 会话展示层也必须是 AI 计划来源(展示与执行不能各说一套)
        view = service.session_view(self.project, self.sid)
        self.assertEqual((view["recommendation"]["platforms"] or {}).get("primary"), "cli")
        self.assertEqual(view["task_plan"], ai_evaluated["task_plan"])

    def test_same_ai_choice_is_reproducible(self):
        """同一份 AI 决策在任意时刻重算必须逐字节一致(可复现计划来源)。"""
        self._evaluate_then_confirm("cli")
        first = advisor.adopt_confirmed_evaluation(self.project, self.sid)["adoption"]
        second_digest = service.evaluate_session(self.project, self.sid)["adopted_plan_digest"]
        third_digest = service.evaluate_session(self.project, self.sid)["adopted_plan_digest"]
        self.assertEqual(first["plan_digest"], second_digest)
        self.assertEqual(second_digest, third_digest)
        status = service.adoption_input_status(self.project, self.sid)
        self.assertTrue(status["matches"], status.get("drift"))
        self.assertEqual((status.get("plan_source") or {}).get("kind"), "confirmed_ai_evaluation")

    def test_new_fact_after_adoption_marks_input_drift(self):
        """采用后又出现相关输入变化:必须报漂移,不允许静默沿用旧 AI 计划。"""
        self._evaluate_then_confirm("cli")
        advisor.adopt_confirmed_evaluation(self.project, self.sid)
        self.assertTrue(service.adoption_input_status(self.project, self.sid)["matches"])
        add_fact(self.project, self.sid, content="界面文案使用简体中文",
                 source_type="user", source_ref="authorized-test-fixture")
        status = service.adoption_input_status(self.project, self.sid)
        self.assertIn("facts_digest", status["drift"])
        self.assertFalse(status["matches"])

    def test_later_hard_constraint_invalidates_ai_plan(self):
        """采用后新增硬约束事实禁止该 AI 首选:计划来源本身失效,而非只算字段漂移。"""
        self._evaluate_then_confirm("cli")
        advisor.adopt_confirmed_evaluation(self.project, self.sid)
        add_fact(self.project, self.sid, content="本项目不得做成独立命令行应用",
                 source_type="repository", source_ref="authorized-test-fixture")
        status = service.adoption_input_status(self.project, self.sid)
        self.assertIn("plan_source_conflict", status["drift"])
        self.assertEqual((status.get("plan_source_conflict") or {}).get("constraint"),
                         "no_standalone_cli")
        self.assertFalse(status["matches"])
        # 生成入口必须在同一处被挡住
        with self.assertRaises(WorkbenchError) as ctx:
            self.bench.api("POST", f"/api/project/社区借还登记/session/{self.sid}/generate", {}, {})
        self.assertEqual(ctx.exception.code, "adoption_drifted")

    def test_adoption_rejects_choice_conflicting_with_registered_constraint(self):
        """已登记硬约束禁止 AI 首选时,采用必须明确拒绝(不静默回退规则计划)。"""
        add_fact(self.project, self.sid, content="只能在现有网站内增量修改",
                 source_type="repository", source_ref="authorized-test-fixture")
        self._evaluate_then_confirm("cli")
        with self.assertRaises(ValueError) as ctx:
            advisor.adopt_confirmed_evaluation(self.project, self.sid)
        self.assertIn("web_only_incremental", str(ctx.exception))
        self.assertFalse((self.project / ".opencoding" / "adoptions" / (self.sid + ".json")).exists())

    def test_plan_source_view_flagged_when_constraint_conflict(self):
        """current_plan_source 必须如实说明 AI 计划来源当前是否仍然可用。"""
        self._evaluate_then_confirm("cli")
        advisor.adopt_confirmed_evaluation(self.project, self.sid)
        source = service.current_plan_source(self.project, self.sid)
        self.assertEqual(source["kind"], "confirmed_ai_evaluation")
        self.assertTrue(source["usable"])
        add_fact(self.project, self.sid, content="本项目不得做成独立命令行应用",
                 source_type="repository", source_ref="authorized-test-fixture")
        source = service.current_plan_source(self.project, self.sid)
        self.assertFalse(source["usable"])
        self.assertEqual(source.get("conflicting_constraint"), "no_standalone_cli")


class PlansourceUnitTests(unittest.TestCase):
    """纯函数层:同一 AI 决策必须逐字节重算一致;冲突检测不得漏判。"""

    def _rule(self):
        return {
            "schema_version": "recommendation-1.1",
            "project": {"kind": "web"},
            "platforms": {"requested": ["web"], "primary": "web",
                          "confidence": "high", "reason": "规则草案",
                          "unresolved": ["platform:needs_user_confirmation"]},
            "capabilities": [{"id": "server", "need": "unknown"},
                             {"id": "database", "need": "required"},
                             {"id": "api", "need": "unknown"}],
            "stack": {"client": "x", "backend": "y", "database": "z", "runtime": "r"},
            "assumptions": ["规则假设"],
            "unresolved": ["platform:needs_user_confirmation"],
        }

    def test_plan_input_is_pure_and_pins_choice(self):
        first = plansource.plan_input_for_choice(self._rule(), "cli", {})
        second = plansource.plan_input_for_choice(self._rule(), "cli", {})
        self.assertEqual(json.dumps(first, ensure_ascii=False, sort_keys=True),
                         json.dumps(second, ensure_ascii=False, sort_keys=True))
        self.assertEqual(first["platforms"]["primary"], "cli")
        self.assertNotIn("platform:needs_user_confirmation", first["unresolved"])
        self.assertNotIn("platform:needs_user_confirmation", first["platforms"]["unresolved"])
        self.assertNotEqual(first["stack"], self._rule()["stack"])
        self.assertTrue(any("用户已确认的 AI 评估" in str(a) for a in first["assumptions"]))

    def test_rule_reference_stays_rule_when_no_choice(self):
        self.assertEqual(plansource.plan_input_for_choice(self._rule(), "", {})["platforms"]["primary"], "web")

    def test_hard_constraint_conflict_detection(self):
        constraints = [{"kind": "no_standalone_cli"}, {"kind": "web_only_incremental"}]
        self.assertEqual(plansource.hard_constraint_conflict(constraints, "cli")[0], "no_standalone_cli")
        self.assertEqual(plansource.hard_constraint_conflict(constraints, "desktop")[0], "web_only_incremental")
        self.assertIsNone(plansource.hard_constraint_conflict(constraints, "web"))
        self.assertIsNone(plansource.hard_constraint_conflict([{"kind": None}], "cli"))


if __name__ == "__main__":
    unittest.main()
