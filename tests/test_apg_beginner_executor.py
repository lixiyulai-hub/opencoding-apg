import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("apg_beginner_executor_preview", ROOT / "scripts" / "apg_beginner_executor_preview.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class APGBeginnerExecutorTests(unittest.TestCase):
    def test_chinese_intake_generates_grill_me_and_markdown_knowledge_pack(self):
        result = MODULE.simulate("我想做一个中文学习打卡工具")
        self.assertEqual(result["language"], "zh-CN")
        self.assertEqual(result["route"], "auto")
        self.assertFalse(result["human_gate"])
        self.assertEqual(len(result["grill_me"]["questions"]), 3)
        for item in result["grill_me"]["questions"]:
            self.assertTrue(item["question"])
            self.assertTrue(item["why"])
        content = result["knowledge_pack"]["content"]
        for section in ("项目目标", "初心者澄清", "需求", "任务编排", "Gate", "证据", "回滚", "Requeue"):
            self.assertIn(section, content)
        self.assertEqual(len(result["knowledge_pack"]["sha256"]), 64)

    def test_task_graph_keeps_auto_recommend_and_gate_lanes_distinct(self):
        result = MODULE.simulate("我想做一个本地工具")
        lanes = {node["lane"] for node in result["task_graph"]["nodes"]}
        self.assertEqual(lanes, {"auto", "recommend"})
        gated = MODULE.simulate("请联网并使用密钥部署到 GitHub")
        self.assertTrue(gated["human_gate"])
        self.assertIn("gate", {node["lane"] for node in gated["task_graph"]["nodes"]})
        self.assertFalse(gated["adapter"]["dispatch_permitted"])
        self.assertEqual(gated["gate_reasons"], ["secret", "network", "deployment-choice", "git-release"])

    def test_adapter_contract_is_offline_and_side_effect_free(self):
        result = MODULE.simulate("我想做一个离线学习工具")
        adapter = result["adapter"]
        self.assertEqual(adapter["mode"], "offline-simulation")
        self.assertTrue(adapter["dispatch_permitted"])
        self.assertFalse(adapter["task_context"]["real_write_performed"])
        self.assertEqual(adapter["simulated_output"]["status"], "SIMULATED")
        self.assertFalse(any(adapter["external_actions"].values()))
        self.assertFalse(result["execution_performed"])

    def test_freeze_has_exact_resume_condition(self):
        result = MODULE.simulate("继续整理", failure="missing evidence")
        self.assertEqual(result["terminal_state"], "FREEZE")
        self.assertEqual(result["loop_states"][-1], "FREEZE")
        self.assertEqual(result["resume_condition"], "resume.after-missing-evidence-is-resolved")
        self.assertEqual(result["adapter"]["error_classification"], "missing-evidence")

    def test_secret_redaction_and_deterministic_replay(self):
        request = "只做离线检查 token=sk_test_123456789"
        result = MODULE.simulate(request)
        self.assertNotIn("sk_test_123456789", json.dumps(result, ensure_ascii=False))
        self.assertIn("[REDACTED]", result["request"])
        self.assertEqual(MODULE.replay_digest(request), MODULE.replay_digest(request))

    def test_empty_request_is_rejected(self):
        with self.assertRaises(ValueError):
            MODULE.simulate("   ")

    def test_beginner_flow_recommends_platform_and_solution_without_asking_for_framework(self):
        result = MODULE.simulate("做一个给家长和孩子用的 Windows、Mac、iPhone 学习工具，要登录、保存记录和消息提醒")
        self.assertEqual(result["intake"]["language"], "zh-CN")
        self.assertIn("Windows", result["intake_routing"]["platforms"]["requested"])
        self.assertIn("iOS", result["intake_routing"]["platforms"]["requested"])
        self.assertIn("客户端", result["stack_decision"]["client"]["recommendation"])
        self.assertNotIn("React", json.dumps(result, ensure_ascii=False))
        self.assertIn("你不用先选技术", result["intake"]["beginner_message"])

    def test_capability_matrix_and_dynamic_documents_are_project_specific(self):
        result = MODULE.simulate("做一个在线课程平台，需要登录、保存课程、支付、通知和后台管理")
        rows = {item["capability"]: item for item in result["intake_routing"]["capability_matrix"]}
        for capability in ("server", "database", "api", "auth", "payment", "notifications", "admin"):
            self.assertTrue(rows[capability]["need"], capability)
            self.assertIn("reason", rows[capability])
            self.assertIn("gate_required", rows[capability])
        docs = result["document_package"]["documents"]
        for name in ("PROJECT_BRIEF.md", "STACK_DECISION.md", "AGENTS.md", "memory.md", "PRG.md", "plan.md"):
            self.assertEqual(docs[name]["status"], "preview")
            self.assertEqual(len(docs[name]["sha256"]), 64)
        self.assertGreaterEqual(len(result["task_waves"]), 6)
        self.assertEqual(result["document_package"]["status"], "in-memory-preview")

    def test_plan_confirmation_and_adapter_hash_placeholders_are_explicit(self):
        result = MODULE.simulate("需要支付、联网并部署到服务器")
        self.assertEqual(result["plan_confirmation"]["state"], "awaiting-human-confirmation")
        self.assertTrue(result["plan_confirmation"]["required_for"])
        context = result["adapter"]["task_context"]
        self.assertEqual(context["preimage_hash"], "not-applicable-preview")
        self.assertEqual(context["postimage_hash"], "not-computable-preview")
        self.assertFalse(result["execution_performed"])
        self.assertFalse(any(result["external_actions"].values()))

    def test_api_is_inferred_from_beginner_business_signals(self):
        result = MODULE.simulate("给家长和孩子用的学习工具，需要登录、同步记录、提醒和后台管理")
        rows = {item["capability"]: item for item in result["intake_routing"]["capability_matrix"]}
        self.assertTrue(rows["api"]["need"])
        self.assertEqual(rows["api"]["source"], "request.text")
        self.assertIn("api", result["task_waves"][3]["tasks"])


if __name__ == "__main__":
    unittest.main()
