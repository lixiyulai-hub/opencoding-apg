import unittest

from opencoding.decisions import CAPABILITY_IDS, SUPPORTED_PLATFORMS, build_recommendation
from opencoding.intake import answer_question, new_session


BASE_ANSWERS = {
    "audience": "家长和孩子",
    "outcome": "孩子完成任务，家长查看结果",
    "data_persistence": "需要",
    "cross_device": "不需要",
    "file_storage": "不需要",
    "external_data": "不需要",
    "admin_access": "不需要",
    "account_access": "不需要",
    "notifications": "不需要",
    "payments": "不需要",
    "multi_user": "不需要",
}


def complete_session(platform, **overrides):
    session = new_session("做一个家庭任务工具")
    answers = dict(BASE_ANSWERS)
    answers["platform"] = platform
    answers.update(overrides)
    for question_id, answer in answers.items():
        session = answer_question(session, question_id, answer)
    return session


class DecisionTests(unittest.TestCase):
    def test_supported_platforms_have_concrete_stack_metadata(self):
        self.assertEqual(set(SUPPORTED_PLATFORMS), {"windows", "macos", "ios", "android", "web", "mini_program", "cli"})
        for platform in SUPPORTED_PLATFORMS:
            recommendation = build_recommendation(complete_session(platform))
            self.assertEqual(recommendation["platforms"]["requested"], [platform])
            self.assertEqual(recommendation["platforms"]["confidence"], "high")
            for item in recommendation["stack"].values():
                self.assertTrue(item["technology"])
                self.assertTrue(item["reason"])
                self.assertIn("未", item["version_basis"])
                self.assertIn("费用", item["cost_note"])

    def test_recommendation_carries_real_business_context_and_acceptance(self):
        recommendation = build_recommendation(complete_session("Mac 桌面"))

        self.assertEqual(recommendation["schema_version"], "1.1")
        self.assertEqual(recommendation["project"]["goal"], "做一个家庭任务工具")
        self.assertEqual(recommendation["project"]["audience"], "家长和孩子")
        self.assertEqual(recommendation["project"]["outcome"], "孩子完成任务，家长查看结果")
        self.assertEqual(recommendation["project"]["scenarios"][0]["source"], "user.answers.outcome")
        self.assertTrue(any("孩子完成任务" in item for item in recommendation["acceptance"]))

    def test_mac_desktop_and_iphone_are_distinct_platform_recommendations(self):
        mac = build_recommendation(complete_session("Mac 桌面"))
        iphone = build_recommendation(complete_session("苹果手机"))

        self.assertEqual(mac["platforms"]["primary"], "macos")
        self.assertEqual(iphone["platforms"]["primary"], "ios")
        self.assertNotEqual(mac["platforms"]["primary"], iphone["platforms"]["primary"])

    def test_excluded_windows_is_not_requested_or_primary(self):
        recommendation = build_recommendation(complete_session("不要 Windows，只要 Mac"))

        self.assertEqual(recommendation["platforms"]["requested"], ["macos"])
        self.assertEqual(recommendation["platforms"]["primary"], "macos")

    def test_multi_platform_is_a_usable_low_confidence_draft(self):
        recommendation = build_recommendation(complete_session("Windows 和 Mac"))

        self.assertEqual(recommendation["platforms"]["requested"], ["windows", "macos"])
        self.assertEqual(recommendation["platforms"]["primary"], "windows")
        self.assertEqual(recommendation["platforms"]["confidence"], "low")
        self.assertEqual(recommendation["status"], "draft")
        self.assertTrue(any("multiple_targets" in item for item in recommendation["unresolved"]))

    def test_unknown_platform_is_low_confidence_and_not_silently_resolved(self):
        recommendation = build_recommendation(complete_session("不知道"))

        self.assertEqual(recommendation["platforms"]["requested"], [])
        self.assertEqual(recommendation["platforms"]["confidence"], "low")
        self.assertEqual(recommendation["status"], "draft")
        self.assertTrue(any("platform" in item for item in recommendation["unresolved"]))

    def test_local_persistence_does_not_force_server_and_four_new_needs_are_independent(self):
        local = build_recommendation(complete_session("命令行", data_persistence="需要"))
        local_by_id = {item["id"]: item for item in local["capabilities"]}
        self.assertEqual(local_by_id["server"]["need"], "not_needed")
        self.assertEqual(local_by_id["database"]["need"], "required")
        self.assertNotIn("需要服务边界", local["stack"]["backend"]["reason"])

        remote = build_recommendation(
            complete_session(
                "网页",
                cross_device="需要",
                file_storage="需要",
                external_data="需要",
                admin_access="需要",
            )
        )
        remote_by_id = {item["id"]: item for item in remote["capabilities"]}
        self.assertEqual(remote_by_id["server"]["need"], "required")
        self.assertEqual(remote_by_id["storage"]["need"], "required")
        self.assertEqual(remote_by_id["api"]["need"], "required")
        self.assertEqual(remote_by_id["admin"]["need"], "required")

    def test_unknown_storage_is_not_described_as_refusal(self):
        recommendation = build_recommendation(complete_session("网页", file_storage="不确定", data_persistence="不确定"))
        by_id = {item["id"]: item for item in recommendation["capabilities"]}

        self.assertEqual(by_id["database"]["need"], "unknown")
        self.assertEqual(by_id["storage"]["need"], "unknown")
        self.assertNotIn("明确不需要", recommendation["stack"]["database"]["reason"])
        self.assertIn("待确认", recommendation["stack"]["database"]["technology"])

    def test_all_capabilities_are_structured_and_payment_can_be_not_needed(self):
        recommendation = build_recommendation(complete_session("命令行"))
        by_id = {item["id"]: item for item in recommendation["capabilities"]}

        self.assertEqual(set(by_id), set(CAPABILITY_IDS))
        self.assertEqual(recommendation["status"], "ready")
        self.assertEqual(by_id["payment"]["need"], "not_needed")
        self.assertTrue(all(item["activation_gate"] for item in by_id.values()))
        self.assertNotIn("payment", recommendation["stack"]["backend"]["technology"].lower())

    def test_unanswered_session_produces_draft_with_explicit_unresolved_items(self):
        recommendation = build_recommendation(new_session("做一个工具"))

        self.assertEqual(recommendation["status"], "draft")
        self.assertTrue(any(item.endswith(":unanswered") for item in recommendation["unresolved"]))
        self.assertEqual(recommendation["platforms"]["confidence"], "low")


if __name__ == "__main__":
    unittest.main()
