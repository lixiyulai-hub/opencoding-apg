from __future__ import annotations

import json
import ast
from pathlib import Path
import re
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AgentNativeDocsContractTests(unittest.TestCase):
    def test_offline_installation_uses_generated_wheel_name_and_lists_local_api(self):
        document = (ROOT / "docs/product/OFFLINE_INSTALLATION.md").read_text(encoding="utf-8")
        package_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        self.assertIn(f"opencoding_local_entry-{package_version}-py3-none-any.whl", document)
        self.assertNotIn(r"opencoding_local_entry-*.whl", document)
        for name in (
            "create_session(root, goal)",
            "session_view(root, session_id, *, include_preview=False)",
            "preview_session(root, session_id)",
            "approve_preview(preview, *, expires_in_seconds=300)",
            "apply_approved(root, approval)",
            "rollback(root, transaction_id)",
            "opencoding.scheduler.read_snapshot(root, task_id=None)",
        ):
            self.assertIn(name, document)

    def test_published_session_example_is_valid_python_without_import_side_effects(self):
        document = (ROOT / "docs/product/OFFLINE_INSTALLATION.md").read_text(encoding="utf-8")
        match = re.search(r"## 可调用的会话/文档示例.*?```python\n(.*?)\n```", document, re.DOTALL)
        self.assertIsNotNone(match)
        ast.parse(match.group(1), filename="OFFLINE_INSTALLATION.md:session-example")

    def test_agent_native_guide_keeps_conflict_and_zero_write_boundaries(self):
        document = (ROOT / "docs/product/AGENT_NATIVE_USE.md").read_text(encoding="utf-8")
        self.assertIn("`busy` and `stale` return to the caller before approval or apply.", document)
        self.assertIn("A `busy` or `stale` answer result has no session payload", document)
        self.assertIn("Only `read_snapshot` is a zero-write scheduler view", document)
        self.assertIn("A stale or busy result is a stop-and-reconcile condition", document)
        self.assertIn("installed wheel and sdist include the validated package skill resource", document)

    def test_w5_matrix_report_and_plan_keep_synthetic_boundaries_explicit(self):
        report = (ROOT / "docs/product/DELIVERY_REPORT_AGENT_NATIVE_W5_MATRIX_CN.md").read_text(encoding="utf-8")
        self.assertIn("clarify → plan → preview → execute → verify → rollback → review", report)
        self.assertIn("real_user=false", report)
        self.assertIn("receipt_covered_files_only", report)
        self.assertIn("external_audit", report)
        self.assertIn("prompt_included=false", report)
        plan = json.loads((ROOT / "docs/product/DELIVERY_PLAN_AGENT_NATIVE_V2.json").read_text(encoding="utf-8"))
        transaction = plan["current_w5_transaction"]
        self.assertEqual(transaction["id"], "agent-native-w5-synthetic-matrix")
        self.assertTrue(transaction["synthetic_only"])
        self.assertFalse(transaction["provider_used"])
        self.assertIn("opencoding/w5_acceptance.py", transaction["paths"])

    def test_w5_evidence_boundary_report_keeps_read_only_privacy_contract(self):
        report = (ROOT / "docs/product/DELIVERY_REPORT_AGENT_NATIVE_W5_EVIDENCE_BOUNDARY_CN.md").read_text(encoding="utf-8")
        for marker in ("real", "synthetic", "unverified", "read_only=true", "prompt_included=false", "api_key_included=false"):
            self.assertIn(marker, report)
        plan = json.loads((ROOT / "docs/product/DELIVERY_PLAN_AGENT_NATIVE_V2.json").read_text(encoding="utf-8"))
        transaction = plan["current_w5_evidence_boundary_transaction"]
        self.assertEqual(transaction["id"], "agent-native-w5-evidence-boundary")
        self.assertTrue(transaction["read_only_audit"])
        self.assertFalse(transaction["provider_used"])
        self.assertIn("opencoding/evidence_boundary.py", transaction["paths"])

    def test_w4_report_and_plan_keep_external_gate_explicit(self):
        report = (ROOT / "docs/product/DELIVERY_REPORT_AGENT_NATIVE_W4_CONTRACT_CN.md").read_text(encoding="utf-8")
        self.assertIn("blocked_human_gate", report)
        self.assertIn("live_verified=false", report)
        self.assertIn("真实 Host、Provider、网络、凭据或外部服务", report)
        plan = json.loads((ROOT / "docs/product/DELIVERY_PLAN_AGENT_NATIVE_V2.json").read_text(encoding="utf-8"))
        transaction = plan["current_w4_transaction"]
        self.assertEqual(transaction["id"], "agent-native-w4-offline-contract")
        self.assertFalse(transaction["real_provider"])
        self.assertFalse(transaction["real_host"])
        self.assertEqual(transaction["network"], False)
        self.assertIn("opencoding/host_connector.py", transaction["paths"])

    def test_delivery_plan_marks_retained_evidence_as_archive_only(self):
        plan = json.loads((ROOT / "docs/product/DELIVERY_PLAN_AGENT_NATIVE_V2.json").read_text(encoding="utf-8"))
        self.assertEqual(
            plan["immediate_transaction"]["paths"],
            [
                "docs/product/OFFLINE_INSTALLATION.md",
                "docs/product/AGENT_NATIVE_USE.md",
                "docs/product/DELIVERY_PLAN_AGENT_NATIVE_V2.json",
            ],
        )
        self.assertEqual(plan["current_transaction"]["id"], "agent-native-package-r3")
        self.assertIn("opencoding/resources/skill/SKILL.md", plan["current_transaction"]["paths"])
        self.assertEqual(plan["package_resource_boundary"]["partial_resource"], "fail_closed")
        self.assertEqual(plan["current_checkout_verification"]["full_unittest"]["total"], 688)
        self.assertEqual(plan["current_checkout_verification"]["full_unittest"]["passed"], 679)
        boundary = plan["retained_evidence_boundary"]
        self.assertFalse(boundary["checkout_contains"])
        self.assertFalse(boundary["source_bound"])
        self.assertEqual(boundary["availability"], "absent-from-checkout-and-fetched-history")
        self.assertTrue(all(item["status"] == "historical_claim_unverified_in_checkout" for item in plan["retained_delivery_evidence"][:4]))


if __name__ == "__main__":
    unittest.main()
