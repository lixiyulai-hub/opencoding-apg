import copy
import unittest

from opencoding.documents import render_documents, validate_recommendation
from opencoding.planning import build_task_plan


def recommendation(*, revision=1, status="ready", payment="not_needed", notifications="not_needed", draft=False, platform="web", client_technology="TypeScript"):
    capabilities = []
    needs = {
        "server": "not_needed",
        "database": "required",
        "api": "required",
        "auth": "required",
        "payment": payment,
        "notifications": notifications,
        "admin": "optional",
        "storage": "not_needed",
    }
    for capability_id, need in needs.items():
        capabilities.append({
            "id": capability_id,
            "need": need,
            "reason": f"用于社区工具借还的{capability_id}边界",
            "source": "answers.business_need",
            "activation_gate": "仅记录方案；激活外部服务需另行确认",
        })
    project = {
        "goal": "做一个社区工具借还登记系统",
        "audience": None if draft else "社区居民和管理员",
        "outcome": None if draft else "居民登记借用工具，管理员确认归还",
        "scenarios": [] if draft else [{
            "id": "borrow-return",
            "title": "工具借用与归还",
            "actor": "社区居民和管理员",
            "action": "登记借用并确认归还",
            "result": "借还状态可查看",
            "source": "user.answers.outcome",
        }],
    }
    return {
        "schema_version": "1.1",
        "session_id": "session-docs",
        "revision": revision,
        "status": "draft" if draft else status,
        "project": project,
        "platforms": {
            "requested": [] if draft else [platform],
            "primary": None if draft else platform,
            "reason": "社区居民需要通过浏览器登记借还",
            "confidence": "low" if draft else "medium",
            "unresolved": ["确认首发平台"] if draft else [],
        },
        "stack": {
            key: {
                "technology": client_technology if key == "client" else f"{key}-technology",
                "reason": f"适配{key}职责",
                "alternatives": ["本地替代方案"],
                "version_basis": "离线未核实具体版本",
                "maintenance": "由项目维护者定期更新",
                "cost_note": "当前仅做本地规划，费用未核实",
            }
            for key in ("client", "backend", "database", "runtime")
        },
        "capabilities": capabilities,
        "assumptions": ["社区管理员负责确认归还"],
        "unresolved": ["确认内容管理员"] if draft else [],
        "acceptance": ["居民登记借用工具，管理员确认归还"] if not draft else ["做一个社区工具借还登记系统的业务目标已记录"],
    }


class ProductDocumentsTests(unittest.TestCase):
    def test_core_documents_are_chinese_and_carry_business_context(self):
        docs = render_documents(recommendation())
        for name in ("AGENTS.md", "memory.md", "PRG.md", "plan.md"):
            self.assertIn(name, docs)
            self.assertTrue(docs[name].startswith("# "))
        self.assertIn("项目工作规则", docs["AGENTS.md"])
        self.assertIn("社区工具借还登记系统", docs["product.md"])
        self.assertIn("社区居民和管理员", docs["product.md"])
        self.assertIn("居民登记借用工具，管理员确认归还", docs["product.md"])
        self.assertIn("工具借用与归还", docs["ui.md"])
        self.assertIn("INSPECT", docs["PRG.md"])
        self.assertIn("FREEZE", docs["PRG.md"])
        self.assertNotIn("Product Requirements Guide", docs["PRG.md"])

    def test_conditional_documents_follow_capability_need(self):
        docs = render_documents(recommendation(payment="required", notifications="optional"))
        self.assertIn("payment.md", docs)
        self.assertIn("notifications.md", docs)
        self.assertIn("不连接支付服务", docs["payment.md"])
        self.assertIn("不联系通知服务", docs["notifications.md"])
        docs = render_documents(recommendation())
        self.assertNotIn("payment.md", docs)
        self.assertNotIn("notifications.md", docs)

    def test_revision_zero_draft_is_valid_but_not_ready(self):
        draft = recommendation(revision=0, draft=True)
        validate_recommendation(draft)
        docs = render_documents(draft)
        self.assertIn("待澄清", docs["memory.md"])
        self.assertIn("draft", docs["PRG.md"])

    def test_strict_nested_schema_rejects_incomplete_values(self):
        invalid = recommendation()
        invalid.pop("project")
        with self.assertRaises(ValueError):
            validate_recommendation(invalid)

    def test_high_confidence_requires_confirmed_requested_platform(self):
        invalid = recommendation()
        invalid["platforms"] = dict(invalid["platforms"], requested=[], confidence="high")
        with self.assertRaises(ValueError):
            validate_recommendation(invalid)

    def test_low_confidence_draft_can_keep_candidate_primary_without_requested_platform(self):
        candidate = recommendation(revision=0, draft=True)
        candidate["platforms"] = dict(candidate["platforms"], requested=[], primary="web", confidence="low")
        validate_recommendation(candidate)
        docs = render_documents(candidate)
        self.assertIn("web", docs["PRG.md"])
        self.assertIn("确认首发平台", docs["memory.md"])
        invalid = recommendation()
        invalid["platforms"] = dict(invalid["platforms"], requested=["web"], primary="ios", confidence="medium")
        with self.assertRaises(ValueError):
            validate_recommendation(invalid)

    def test_render_rejects_foreign_task_plan_source(self):
        value = recommendation()
        plan = build_task_plan(value)
        plan["session_id"] = "different-session"
        with self.assertRaises(ValueError):
            render_documents(value, plan)

    def test_render_rejects_same_revision_stale_business_graph(self):
        value = recommendation()
        plan = build_task_plan(value)
        changed = recommendation()
        changed["project"]["goal"] = "做一个社区日记登记系统"
        changed["project"]["outcome"] = "居民记录日记，管理员查看内容"
        changed["project"]["scenarios"][0]["title"] = "日记记录"
        changed["project"]["scenarios"][0]["action"] = "记录日记并查看"
        changed["project"]["scenarios"][0]["result"] = "日记内容可查看"
        changed["acceptance"] = ["居民记录日记，管理员查看内容"]
        with self.assertRaises(ValueError):
            render_documents(changed, plan)

    def test_render_cannot_hide_recommendation_unresolved(self):
        candidate = recommendation(revision=0, draft=True)
        plan = build_task_plan(candidate)
        plan["unresolved"] = []
        docs = render_documents(candidate, plan)
        self.assertIn("确认内容管理员", docs["memory.md"])
        self.assertIn("确认首发平台", docs["memory.md"])
        invalid = recommendation()
        invalid["stack"].pop("runtime")
        with self.assertRaises(ValueError):
            validate_recommendation(invalid)
        invalid = recommendation()
        invalid["capabilities"] = invalid["capabilities"][:-1]
        with self.assertRaises(ValueError):
            validate_recommendation(invalid)
        invalid = recommendation()
        invalid["capabilities"][0]["activation_gate"] = {"required": True}
        with self.assertRaises(ValueError):
            validate_recommendation(invalid)
        invalid = copy.deepcopy(recommendation())
        invalid["status"] = "recommendation_ready"
        with self.assertRaises(ValueError):
            validate_recommendation(invalid)


if __name__ == "__main__":
    unittest.main()
