"""Independent foundation journeys; no downstream application or external service."""

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from opencoding.decisions import build_recommendation
from opencoding.documents import render_documents
from opencoding.intake import QUESTION_DEFINITIONS, answer_question, new_session
from opencoding.planning import build_task_plan, validate_task_plan
from opencoding.sessions import load_session, save_session
from opencoding.transactions import apply_changes, preview_changes, rollback_changes


def complete_session(goal="community lending register", platform="web", **overrides):
    session = new_session(goal)
    answers = {
        "audience": "residents and administrators",
        "outcome": "record a loan and confirm its return",
        "platform": platform,
        "data_persistence": "\u9700\u8981",
    }
    answers.update(overrides)
    for question in QUESTION_DEFINITIONS:
        session = answer_question(
            session, question["id"], answers.get(question["id"], "\u4e0d\u9700\u8981")
        )
    return session


def journey(session):
    recommendation = build_recommendation(session)
    plan = build_task_plan(recommendation)
    documents = render_documents(recommendation, plan)
    return recommendation, plan, documents


def inventory(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


class ProductFoundationIntegrationTests(unittest.TestCase):
    def test_unanswered_draft_can_preview_without_claiming_execution(self):
        recommendation, plan, documents = journey(new_session("local notebook"))
        self.assertEqual(recommendation["status"], "draft")
        self.assertTrue(recommendation["unresolved"])
        self.assertFalse(any(task["action"]["type"] == "implement_feature" for task in plan["tasks"]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = inventory(root)
            preview = preview_changes(root, documents)
            self.assertEqual(preview["status"], "preview")
            self.assertEqual(inventory(root), before)

    def test_chinese_goal_and_scenario_reach_documents_and_plan(self):
        goal = "\u793e\u533a\u5de5\u5177\u501f\u8fd8\u767b\u8bb0"
        outcome = "\u5c45\u6c11\u767b\u8bb0\u501f\u7528\u5de5\u5177\uff0c\u7ba1\u7406\u5458\u786e\u8ba4\u5f52\u8fd8"
        session = complete_session(
            goal=goal, platform="\u7f51\u9875",
            audience="\u793e\u533a\u5c45\u6c11\u548c\u7ba1\u7406\u5458", outcome=outcome,
        )
        recommendation, plan, documents = journey(session)
        self.assertEqual(recommendation["status"], "ready")
        self.assertEqual(recommendation["project"]["goal"], goal)
        self.assertEqual(recommendation["project"]["outcome"], outcome)
        self.assertIn(goal, documents["product.md"])
        self.assertIn(outcome, documents["product.md"])
        self.assertTrue(validate_task_plan(plan)["valid"])
        features = [task for task in plan["tasks"] if task["action"]["type"] == "implement_feature"]
        self.assertTrue(features)
        scenario_ids = {item["id"] for item in recommendation["project"]["scenarios"]}
        self.assertTrue(all(task["action"]["scenario_id"] in scenario_ids for task in features))

    def test_seven_platforms_have_specific_stack_and_matching_plan_outputs(self):
        for answer, platform, extension in (
            ("Windows", "windows", ".ts"),
            ("Mac", "macos", ".swift"),
            ("iPhone", "ios", ".swift"),
            ("\u5b89\u5353", "android", ".kt"),
            ("\u7f51\u9875", "web", ".ts"),
            ("\u5c0f\u7a0b\u5e8f", "mini_program", ".wxml"),
            ("\u547d\u4ee4\u884c", "cli", ".py"),
        ):
            with self.subTest(platform=platform):
                recommendation, plan, _documents = journey(complete_session(platform=answer))
                self.assertEqual(recommendation["status"], "ready")
                self.assertEqual(recommendation["platforms"]["primary"], platform)
                self.assertTrue(recommendation["stack"]["client"]["technology"])
                self.assertTrue(recommendation["stack"]["client"]["version_basis"])
                features = [task for task in plan["tasks"] if task["action"]["type"] == "implement_feature"]
                self.assertTrue(features)
                self.assertTrue(all(
                    output.endswith(extension) for task in features for output in task["outputs"]
                ))

    def test_core_documents_have_separate_roles(self):
        _recommendation, plan, documents = journey(complete_session(platform="\u547d\u4ee4\u884c"))
        core = [documents[path] for path in ("AGENTS.md", "memory.md", "PRG.md", "plan.md")]
        self.assertEqual(len(set(core)), 4)
        for stage in ("INSPECT", "PROGRESS", "PLAN", "DISPATCH", "VALIDATE", "REPORT", "REQUEUE", "FREEZE"):
            self.assertIn(stage, documents["PRG.md"])
        for task in plan["tasks"]:
            self.assertIn(task["id"], documents["plan.md"])

    def test_local_persistence_does_not_imply_remote_server_or_payment(self):
        recommendation, plan, documents = journey(complete_session(platform="\u547d\u4ee4\u884c"))
        needs = {item["id"]: item["need"] for item in recommendation["capabilities"]}
        self.assertEqual(needs["database"], "required")
        self.assertEqual(needs["server"], "not_needed")
        self.assertEqual(needs["payment"], "not_needed")
        self.assertEqual(needs["notifications"], "not_needed")
        self.assertNotIn("payment.md", documents)
        self.assertNotIn("notifications.md", documents)
        self.assertFalse(any(task["action"].get("capability") == "payment" for task in plan["tasks"]))

    def test_external_business_needs_plan_only_and_keep_activation_gate(self):
        recommendation, plan, documents = journey(complete_session(
            cross_device="\u9700\u8981", multi_user="\u9700\u8981",
            external_data="\u9700\u8981", account_access="\u9700\u8981",
            admin_access="\u9700\u8981", file_storage="\u9700\u8981",
            payments="\u9700\u8981", notifications="\u9700\u8981",
        ))
        needs = {item["id"]: item for item in recommendation["capabilities"]}
        self.assertEqual(len(needs), 8)
        for capability in ("server", "database", "api", "auth", "payment", "notifications", "admin", "storage"):
            self.assertEqual(needs[capability]["need"], "required")
            self.assertTrue(needs[capability]["activation_gate"])
        self.assertIn("payment.md", documents)
        self.assertIn("notifications.md", documents)
        integrations = [
            task for task in plan["tasks"]
            if task["action"]["type"] == "integration_design"
            and task["action"]["capability"] in {"payment", "notifications"}
        ]
        self.assertEqual(len(integrations), 2)
        self.assertTrue(all(not task["activation_gate"]["required"] for task in integrations))
        self.assertTrue(all("\u786e\u8ba4" in task["activation_gate"]["reason"] for task in integrations))

    def test_revision_change_invalidates_old_documents_plan_pair(self):
        session = complete_session(platform="Windows")
        old_recommendation, old_plan, _documents = journey(session)
        revised = answer_question(session, "platform", "Mac")
        revised = answer_question(revised, "platform", "Mac")
        new_recommendation = build_recommendation(revised)
        self.assertEqual(new_recommendation["status"], "ready")
        self.assertGreater(new_recommendation["revision"], old_recommendation["revision"])
        with self.assertRaises(ValueError):
            render_documents(new_recommendation, old_plan)

    def test_chinese_answers_persist_and_recover_before_recommendation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = new_session("\u793e\u533a\u5de5\u5177\u501f\u8fd8")
            save_session(root, session)
            answers = {"audience": "\u5c45\u6c11", "outcome": "\u767b\u8bb0\u501f\u7528\u548c\u5f52\u8fd8",
                       "platform": "\u7f51\u9875", "data_persistence": "\u9700\u8981"}
            for question in QUESTION_DEFINITIONS:
                session = answer_question(
                    load_session(root, session["id"]), question["id"],
                    answers.get(question["id"], "\u4e0d\u9700\u8981"),
                )
                save_session(root, session)
            loaded = load_session(root, session["id"])
            self.assertEqual(loaded, session)
            self.assertEqual(build_recommendation(loaded)["status"], "ready")

    def test_exact_preview_writes_real_documents_and_retains_evidence_after_rollback(self):
        _recommendation, _plan, documents = journey(complete_session(platform="\u547d\u4ee4\u884c"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preview = preview_changes(root, documents)
            rejected = apply_changes(root, preview, approved_digest="not-approved")
            self.assertEqual(rejected["status"], "blocked")
            self.assertEqual(inventory(root), {})
            applied = apply_changes(root, preview, approved_digest=preview["plan_digest"])
            self.assertEqual(applied["status"], "applied")
            for entry in preview["entries"]:
                self.assertEqual(
                    hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest(), entry["after_sha256"]
                )
                self.assertEqual((root / entry["path"]).read_text(encoding="utf-8"), documents[entry["path"]])
            rolled = rollback_changes(root, applied["transaction_id"])
            self.assertEqual(rolled["status"], "rolled_back")
            self.assertTrue(Path(applied["receipt_path"]).exists())
            self.assertTrue(all(not (root / path).exists() for path in documents))
            self.assertEqual(rollback_changes(root, applied["transaction_id"])["status"], "rolled_back")

    def test_preapply_user_drift_is_rejected_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "memory.md").write_text("before", encoding="utf-8")
            preview = preview_changes(root, {"memory.md": "planned"})
            (root / "memory.md").write_text("user edited", encoding="utf-8")
            before = inventory(root)
            result = apply_changes(root, preview, approved_digest=preview["plan_digest"])
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(inventory(root), before)

    def test_same_byte_user_replacement_is_not_deleted_on_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preview = preview_changes(root, {"memory.md": "planned"})
            result = apply_changes(root, preview, approved_digest=preview["plan_digest"])
            old_identity = (root / "memory.md").stat().st_ino
            replacement = root / "user-replacement"
            replacement.write_text("planned", encoding="utf-8")
            os.replace(replacement, root / "memory.md")
            new_identity = (root / "memory.md").stat().st_ino
            self.assertNotEqual(old_identity, new_identity)
            rolled = rollback_changes(root, result["transaction_id"])
            self.assertIn(rolled["status"], {"blocked", "partial_failure"})
            self.assertEqual((root / "memory.md").stat().st_ino, new_identity)

    def test_owned_nested_directories_are_removed_but_user_content_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preview = preview_changes(root, {"owned/deep/new.txt": "planned"})
            result = apply_changes(root, preview, approved_digest=preview["plan_digest"])
            (root / "owned" / "unmanaged.txt").write_text("keep", encoding="utf-8")
            rolled = rollback_changes(root, result["transaction_id"])
            self.assertEqual(rolled["status"], "partial_failure")
            self.assertEqual(rolled["rollback_residual_paths"], ["owned"])
            self.assertFalse((root / "owned" / "deep").exists())
            self.assertEqual((root / "owned" / "unmanaged.txt").read_text(encoding="utf-8"), "keep")

    def test_repeated_rollback_does_not_report_already_removed_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preview = preview_changes(root, {"owned/deep/new.txt": "planned"})
            result = apply_changes(root, preview, approved_digest=preview["plan_digest"])
            for _attempt in range(2):
                rolled = rollback_changes(root, result["transaction_id"])
                self.assertEqual(rolled["status"], "rolled_back")
                self.assertEqual(rolled.get("rollback_residual_paths", []), [])
            self.assertFalse((root / "owned").exists())

    def test_generated_task_graph_rejects_cycle_and_missing_dependency(self):
        _recommendation, plan, _documents = journey(complete_session())
        self.assertTrue(validate_task_plan(plan)["valid"])
        missing = deepcopy(plan)
        missing["tasks"][0]["depends_on"] = ["missing-task"]
        self.assertFalse(validate_task_plan(missing)["valid"])
        cyclic = deepcopy(plan)
        cyclic["tasks"][0]["depends_on"] = [cyclic["tasks"][0]["id"]]
        self.assertFalse(validate_task_plan(cyclic)["valid"])


if __name__ == "__main__":
    unittest.main()
