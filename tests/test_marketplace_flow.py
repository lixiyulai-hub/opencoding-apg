import unittest

from opencoding.decisions import build_recommendation
from opencoding.documents import render_documents, validate_recommendation
from opencoding.intake import MARKETPLACE_QUESTION_DEFINITIONS, answer_question, new_session, next_questions
from opencoding.planning import build_task_plan


BASE_ANSWERS = {
    "audience": "买家、第三方卖家和平台运营",
    "outcome": "买家下单，卖家履约，平台按规则结算",
    "platform": "网页",
    "data_persistence": "需要",
    "cross_device": "需要",
    "file_storage": "需要",
    "external_data": "不需要",
    "admin_access": "需要",
    "account_access": "需要",
    "notifications": "需要",
    "payments": "需要",
    "multi_user": "需要",
    "seller_onboarding": "允许第三方卖家入驻，提交主体和商品资质，由平台审核；平台不自行收购或囤货。",
    "identity_verification": "平台负责基础实名，卖家提交主体资料，具体核验服务待确认。",
    "product_listing": "卖家填写品牌、成色、价格并上传照片，平台审核后可修改或下架。",
    "authentication_responsibility": "第三方鉴定机构负责真伪鉴定，平台展示结果，责任边界待法务确认。",
    "orders_commissions_settlement": "买家付款后平台暂存，成交收取佣金，鉴定和售后期结束后结算给卖家。",
    "logistics": "卖家发货，买家收货，平台展示物流追踪，是否保价待确认。",
    "after_sales_disputes": "平台受理退货退款和鉴定争议，按规则裁决，时限和责任待确认。",
    "risk_governance": "后台处理违规商品、欺诈、封禁和申诉并保留审计记录，规则待确认。",
}


def complete_marketplace_session(overrides=None):
    session = new_session("二手奢侈品交易独立站")
    answers = dict(BASE_ANSWERS)
    answers.update(overrides or {})
    for question in session["questions"]:
        session = answer_question(session, question["id"], answers[question["id"]])
    return session


class MarketplaceFlowTests(unittest.TestCase):
    def test_domain_questions_are_contextual_and_unknown_stays_unresolved(self):
        session = new_session("二手奢侈品交易独立站")
        self.assertEqual(
            [item["id"] for item in session["questions"]][-len(MARKETPLACE_QUESTION_DEFINITIONS):],
            [item["id"] for item in MARKETPLACE_QUESTION_DEFINITIONS],
        )
        session = answer_question(session, "seller_onboarding", "不确定")
        self.assertEqual(session["requirements"]["seller_onboarding"]["kind"], "unknown")
        self.assertEqual(session["requirements"]["seller_onboarding"]["source"], "user")
        seller_question = next(item for item in next_questions(session) if item["id"] == "seller_onboarding")
        self.assertEqual(seller_question["status"], "needs_clarification")
        recommendation = build_recommendation(session)
        self.assertEqual(recommendation["status"], "draft")
        self.assertTrue(any(item.startswith("agent.assumption:") for item in recommendation["assumptions"]))
        self.assertTrue(any(item.startswith("seller_onboarding:") for item in recommendation["unresolved"]))

    def test_real_marketplace_answers_reach_ready_plan_with_source_boundaries(self):
        session = complete_marketplace_session()
        self.assertEqual(session["state"], "recommendation_ready")
        self.assertTrue(all(item["source"] == "user" for item in session["answer_history"]))
        recommendation = build_recommendation(session)
        validate_recommendation(recommendation)
        self.assertEqual(recommendation["status"], "ready")
        self.assertEqual(len(recommendation["project"]["scenarios"]), 9)
        self.assertTrue(all(item["source"].startswith("user.answers.") or item["source"] == "answers.outcome" for item in recommendation["project"]["scenarios"]))
        capabilities = {item["id"]: item for item in recommendation["capabilities"]}
        for capability_id in ("server", "database", "api", "auth", "payment", "admin", "storage"):
            self.assertEqual(capabilities[capability_id]["need"], "required", capability_id)
        plan = build_task_plan(recommendation)
        feature_ids = {task["action"].get("scenario_id") for task in plan["tasks"] if task["action"]["type"] == "implement_feature"}
        self.assertEqual(feature_ids, {item["id"] for item in recommendation["project"]["scenarios"]})
        docs = render_documents(recommendation, plan)
        self.assertIn("卖家入驻", docs["product.md"])
        self.assertIn("user.answers.seller_onboarding", docs["product.md"])
        self.assertIn("Agent 临时假设", docs["memory.md"])
        self.assertIn("不自行收购或囤货", docs["memory.md"])


if __name__ == "__main__":
    unittest.main()
