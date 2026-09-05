import unittest

from opencoding.intake import answer_question, new_session, next_questions


ALL_ANSWERS = {
    "audience": "家长和孩子",
    "outcome": "家长能安排任务，孩子能完成并看到结果",
    "platform": "Mac 桌面",
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


def complete_session(overrides=None):
    session = new_session("做一个帮助家庭安排学习任务的工具")
    answers = dict(ALL_ANSWERS)
    answers.update(overrides or {})
    for question_id, answer in answers.items():
        session = answer_question(session, question_id, answer)
    return session


class IntakeTests(unittest.TestCase):
    def test_revision_zero_and_new_business_questions_are_in_schema(self):
        session = new_session("帮我做一个学习工具")

        self.assertEqual(session["schema_version"], "1.1")
        self.assertEqual(session["revision"], 0)
        self.assertEqual(session["state"], "clarifying")
        self.assertEqual(len(next_questions(session)), 12)
        self.assertEqual({item["id"] for item in session["questions"]}, set(ALL_ANSWERS))

    def test_answers_increment_revision_and_keep_history(self):
        session = new_session("做一个家庭任务工具")
        first = answer_question(session, "platform", "Mac 桌面")
        second = answer_question(first, "platform", "苹果手机")

        self.assertEqual(first["revision"], 1)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["answers"]["platform"], "苹果手机")
        self.assertEqual(len(second["answer_history"]), 2)
        self.assertTrue(second["answer_history"][1]["changed"])

    def test_unknown_conflict_and_explicit_recovery_stay_distinct(self):
        session = new_session("做一个家庭任务工具")
        unknown = answer_question(session, "payments", "不确定")
        affirmative = answer_question(unknown, "payments", "需要")
        conflict = answer_question(affirmative, "payments", "不需要")

        self.assertIsNone(unknown["requirements"]["payments"]["value"])
        self.assertEqual(unknown["requirements"]["payments"]["kind"], "unknown")
        self.assertTrue(conflict["requirements"]["payments"]["conflict"])
        self.assertIsNone(conflict["requirements"]["payments"]["value"])
        self.assertTrue(any(item["id"] == "payments" and item["status"] == "needs_confirmation" for item in next_questions(conflict)))

        recovered = answer_question(conflict, "payments", "不需要")
        self.assertFalse(recovered["requirements"]["payments"]["conflict"])
        self.assertEqual(recovered["requirements"]["payments"]["kind"], "negative")
        self.assertIn("answer_conflict_resolved", recovered["requirements"]["payments"]["reason_codes"])

    def test_long_negation_does_not_select_excluded_platform(self):
        for answer in ("不要 Windows，只要 Mac", "不要 Windows 只要 Mac"):
            session = answer_question(new_session("工具"), "platform", answer)
            requirement = session["requirements"]["platform"]

            self.assertEqual(requirement["platforms"], ["macos"])
            self.assertEqual(requirement["value"], "macos")
            self.assertNotIn("windows", requirement["platforms"])

    def test_multi_platform_and_complex_negative_answers_remain_self_consistent(self):
        multi = answer_question(new_session("工具"), "platform", "Windows 和 Mac")
        requirement = multi["requirements"]["platform"]
        self.assertEqual(requirement["kind"], "ambiguous")
        self.assertIsNone(requirement["value"])
        self.assertEqual(requirement["platforms"], ["windows", "macos"])
        next_questions(multi)

        for question_id, answer in (("payments", "不支持支付"), ("notifications", "不是")):
            parsed = answer_question(new_session("工具"), question_id, answer)["requirements"][question_id]
            self.assertEqual(parsed["kind"], "negative")
            self.assertFalse(parsed["value"])

        for answer in ("不可以发通知", "不能发通知", "不希望发通知", "可以不发通知"):
            parsed = answer_question(new_session("工具"), "notifications", answer)["requirements"]["notifications"]
            self.assertNotEqual(parsed["value"], True)

        double_negative = answer_question(new_session("工具"), "notifications", "不是不需要通知")["requirements"]["notifications"]
        self.assertEqual(double_negative["kind"], "ambiguous")

        explicit_need = answer_question(new_session("工具"), "notifications", "需要发通知")["requirements"]["notifications"]
        self.assertEqual(explicit_need["kind"], "affirmative")
        self.assertTrue(explicit_need["value"])

        excluded = answer_question(new_session("工具"), "platform", "不用 Windows 和 Android，只用 Mac")
        self.assertEqual(excluded["requirements"]["platform"]["platforms"], ["macos"])

    def test_all_business_questions_are_required_for_ready(self):
        partial = complete_session({"external_data": "不知道"})
        self.assertEqual(partial["state"], "clarifying")
        self.assertTrue(any(item["id"] == "external_data" and item["status"] == "needs_clarification" for item in next_questions(partial)))

        complete = complete_session()
        self.assertEqual(complete["state"], "recommendation_ready")
        self.assertEqual(next_questions(complete), [])

    def test_secret_like_goal_answers_and_multiline_key_are_redacted(self):
        session = new_session("做一个工具 token=sk-test-1234567890")
        session = answer_question(session, "outcome", "完成 token=sk-test-1234567890")
        session = answer_question(session, "audience", "-----BEGIN PRIVATE KEY-----\nsecret-material\n-----END PRIVATE KEY-----")

        self.assertNotIn("sk-test-1234567890", session["goal"])
        self.assertNotIn("sk-test-1234567890", session["answers"]["outcome"])
        self.assertNotIn("secret-material", session["answers"]["audience"])
        self.assertIn("[REDACTED]", session["answers"]["audience"])

    def test_nested_session_unknown_field_is_rejected(self):
        session = answer_question(new_session("做一个工具"), "platform", "网页")
        session["requirements"]["platform"]["unexpected"] = True
        with self.assertRaises(ValueError):
            next_questions(session)


if __name__ == "__main__":
    unittest.main()
