from __future__ import annotations

import json
import unittest

from opencoding.evidence_boundary import (
    EvidenceBoundaryError,
    build_read_only_audit_snapshot,
    classify_evidence,
    platform_compatibility_declaration,
)
from opencoding.safety import inspect_sensitive, sanitize_text
from opencoding.w5_acceptance import build_synthetic_acceptance_matrix


class EvidenceBoundaryTests(unittest.TestCase):
    def test_evidence_classes_are_explicit_and_fail_closed(self):
        self.assertEqual(
            classify_evidence({"evidence_class": "real", "real_user": True, "live_verified": True}, source="run-1")["class"],
            "real",
        )
        self.assertEqual(
            classify_evidence({"evidence_class": "synthetic", "synthetic": True}, source="fixture")["class"],
            "synthetic",
        )
        self.assertEqual(classify_evidence({"status": "pending"}, source="unknown")["class"], "unverified")
        with self.assertRaisesRegex(EvidenceBoundaryError, "real evidence"):
            classify_evidence({"evidence_class": "real", "real_user": True}, source="run-2")
        with self.assertRaises(EvidenceBoundaryError) as context:
            classify_evidence({"evidence_class": "synthetic", "synthetic": True, "live_verified": True}, source="fixture")
        self.assertEqual(context.exception.code, "evidence_conflict")

    def test_platform_declaration_does_not_promote_a_label_to_compatibility(self):
        declaration = platform_compatibility_declaration(
            "web", status="unverified", execution_observed=False,
            toolchain_status="observed", host_family="linux",
            reason="Node/npm observed; browser execution is not observed.",
        )
        self.assertEqual(declaration["claim"], "contract_only")
        self.assertEqual(declaration["evidence_class"], "unverified")
        self.assertFalse(declaration["execution_observed"])
        with self.assertRaisesRegex(EvidenceBoundaryError, "observed compatibility"):
            platform_compatibility_declaration("web", status="observed", execution_observed=False)

    def test_read_only_audit_omits_prompt_cookie_credentials_and_user_content(self):
        secret = "sk-" + "x" * 32
        prompt = "这是不应进入外部审计的用户 Prompt：家庭成员名单"
        cookie = "sessionid=private-cookie-value"
        payload = {
            "evidence_class": "unverified", "status": "blocked",
            "prompt": prompt, "cookie": cookie, "api_key": secret,
            "user_content": "张三的电话号码 13800000000",
        }
        audit = build_read_only_audit_snapshot(
            payload, source="acceptance-report",
            platform_declarations=[{"target": "web", "status": "unverified", "reason": "contract only"}],
            summary={"item_count": 1},
        )
        serialized = json.dumps(audit, ensure_ascii=False, sort_keys=True)
        self.assertTrue(audit["read_only"])
        self.assertEqual(audit["evidence"]["class"], "unverified")
        self.assertNotIn(secret, serialized)
        self.assertNotIn(cookie, serialized)
        self.assertNotIn(prompt, serialized)
        self.assertNotIn("张三的电话号码", serialized)
        self.assertFalse(audit["privacy"]["raw_values_included"])
        self.assertFalse(audit["privacy"]["user_content_included"])

    def test_w5_preview_exposes_safe_audit_and_classified_platforms(self):
        preview = build_synthetic_acceptance_matrix()
        audit = preview["external_audit"]
        self.assertEqual(audit["status"], "ready_for_review")
        self.assertTrue(audit["read_only"])
        self.assertEqual(audit["evidence"]["class"], "synthetic")
        self.assertTrue(all(item["evidence_class"] == "unverified" for item in audit["platform_compatibility"]))
        self.assertTrue(all(item["evidence_class"] == "synthetic" for item in preview["matrix"]["scenarios"]))
        self.assertFalse(audit["privacy"]["raw_values_included"])

    def test_safety_marks_cookie_and_prompt_before_any_audit_projection(self):
        text = "Cookie: sid=secret-cookie; Prompt: private instructions"
        result = inspect_sensitive(text)
        self.assertIn("cookie_header", result["categories"])
        self.assertIn("prompt_assignment", result["categories"])
        safe = sanitize_text(text)
        self.assertNotIn("secret-cookie", safe)
        self.assertNotIn("private instructions", safe)


if __name__ == "__main__":
    unittest.main()
