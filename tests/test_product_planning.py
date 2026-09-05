import copy
import unittest

from opencoding.documents import render_documents
from opencoding.planning import build_task_plan, task_waves, validate_task_plan

try:
    from .test_product_documents import recommendation
except ImportError:
    from test_product_documents import recommendation


class ProductPlanningTests(unittest.TestCase):
    def test_business_tasks_include_implementation_and_verification(self):
        plan = build_task_plan(recommendation(payment="required", notifications="not_needed"))
        self.assertEqual(plan["schema_version"], "1.1")
        self.assertEqual(plan["revision"], 1)
        self.assertTrue(validate_task_plan(plan)["valid"])
        self.assertEqual(task_waves(plan), plan["waves"])
        action_types = {task["action"]["type"] for task in plan["tasks"]}
        self.assertIn("implement_feature", action_types)
        self.assertIn("verify_feature", action_types)
        implementation = next(task for task in plan["tasks"] if task["action"]["type"] == "implement_feature")
        verification = next(task for task in plan["tasks"] if task["action"]["type"] == "verify_feature")
        self.assertEqual(implementation["outputs"], ["src/features/borrow-return.ts"])
        self.assertEqual(verification["outputs"], ["tests/features/test_borrow-return.test.ts"])
        self.assertIn("借还状态可查看", implementation["description"])

    def test_plan_document_is_rendered_from_same_graph(self):
        recommendation_value = recommendation()
        plan = build_task_plan(recommendation_value)
        docs = render_documents(recommendation_value, plan)
        for task in plan["tasks"]:
            self.assertIn(task["id"], docs["plan.md"])
        for wave in plan["waves"]:
            self.assertIn(", ".join(wave), docs["plan.md"])

    def test_empty_draft_has_clarification_plan_without_fabricated_feature(self):
        plan = build_task_plan(recommendation(revision=0, draft=True))
        self.assertFalse(any(task["action"]["type"] in {"implement_feature", "verify_feature"} for task in plan["tasks"]))
        self.assertEqual(plan["revision"], 0)

    def test_malformed_plans_return_invalid_without_crashing(self):
        cases = [None, {"tasks": None}, {"schema_version": "1.1", "session_id": "x", "revision": 0, "tasks": [{"id": "a"}], "waves": [], "unresolved": []}]
        for value in cases:
            result = validate_task_plan(value)
            self.assertFalse(result["valid"])
            self.assertEqual(result["status"], "invalid")
        plan = build_task_plan(recommendation())
        broken = copy.deepcopy(plan)
        broken["tasks"][0]["depends_on"] = None
        self.assertFalse(validate_task_plan(broken)["valid"])
        broken = copy.deepcopy(plan)
        broken["tasks"][0]["outputs"] = [["not-hashable"]]
        self.assertFalse(validate_task_plan(broken)["valid"])
        broken = copy.deepcopy(plan)
        broken["tasks"][0]["outputs"] = [None]
        self.assertFalse(validate_task_plan(broken)["valid"])
        broken = copy.deepcopy(plan)
        broken["tasks"][0]["action"] = {"type": None}
        self.assertFalse(validate_task_plan(broken)["valid"])

    def test_windows_paths_and_reserved_names_are_rejected(self):
        plan = build_task_plan(recommendation())
        for path in ("C:/outside.txt", "//server/share.txt", "file.txt:stream", ".git/config", "NUL.txt", "trailing.", "trailing ", "bad\x00name.txt", "bad*name.txt", "bad?name.txt", "bad<name.txt", "bad>name.txt", "bad|name.txt", 'bad"name.txt'):
            broken = copy.deepcopy(plan)
            broken["tasks"][0]["outputs"] = [path]
            result = validate_task_plan(broken)
            self.assertFalse(result["valid"], path)
        for path in ("sessions/record.json", "transactions/item.json"):
            broken = copy.deepcopy(plan)
            broken["tasks"][0]["outputs"] = [path]
            broken["tasks"][0]["action"] = {"type": "security_review"}
            self.assertTrue(validate_task_plan(broken)["valid"], path)

    def test_empty_or_malformed_waves_are_rejected(self):
        plan = build_task_plan(recommendation())
        broken = copy.deepcopy(plan)
        broken["tasks"] = []
        broken["waves"] = [["ghost"]]
        result = validate_task_plan(broken)
        self.assertFalse(result["valid"])
        self.assertTrue(any("waves" in error for error in result["errors"]))
        broken = copy.deepcopy(plan)
        broken["waves"] = ["not-a-wave"]
        self.assertFalse(validate_task_plan(broken)["valid"])

    def test_acceptance_must_contain_nonempty_business_text(self):
        plan = build_task_plan(recommendation())
        for acceptance in ([], [""], ["  "]):
            broken = copy.deepcopy(plan)
            broken["tasks"][0]["acceptance"] = acceptance
            result = validate_task_plan(broken)
            self.assertFalse(result["valid"], acceptance)

    def test_parent_child_outputs_are_rejected_within_and_across_tasks(self):
        base = {"schema_version": "1.1", "session_id": "paths", "revision": 0, "unresolved": [], "tasks": [], "waves": []}
        def task(task_id, depends_on, outputs):
            return {"id": task_id, "title": task_id, "description": "任务", "depends_on": depends_on, "inputs": [], "outputs": outputs, "action": {"type": "security_review"}, "acceptance": ["业务结果"], "rollback": "恢复", "retry": {"max_attempts": 1}, "activation_gate": {"required": False, "reason": "本地"}}
        same = copy.deepcopy(base)
        same["tasks"] = [task("single", [], ["src/item", "src/item/child.py"])]
        same["waves"] = [["single"]]
        self.assertFalse(validate_task_plan(same)["valid"])
        across = copy.deepcopy(base)
        across["tasks"] = [task("parent", [], ["src/item"]), task("child", ["parent"], ["src/item/child.py"])]
        across["waves"] = [["parent"], ["child"]]
        self.assertFalse(validate_task_plan(across)["valid"])

    def test_ordered_dependent_tasks_may_revise_same_output(self):
        base = {"schema_version": "1.1", "session_id": "ordered", "revision": 0, "unresolved": [], "tasks": [], "waves": []}
        def task(task_id, depends_on):
            return {"id": task_id, "title": task_id, "description": "任务", "depends_on": depends_on, "inputs": [], "outputs": ["src/main.py"], "action": {"type": "security_review"}, "acceptance": ["业务结果"], "rollback": "恢复", "retry": {"max_attempts": 1}, "activation_gate": {"required": False, "reason": "本地"}}
        base["tasks"] = [task("a", []), task("b", ["a"])]
        base["waves"] = [["a"], ["b"]]
        self.assertTrue(validate_task_plan(base)["valid"])
        base["tasks"][1]["depends_on"] = []
        base["waves"] = [["a", "b"]]
        result = validate_task_plan(base)
        self.assertFalse(result["valid"])
        self.assertTrue(any("duplicate_output_path" in error or "same_wave_write_conflict" in error for error in result["errors"]))

    def test_same_wave_parent_child_and_read_write_conflicts_are_rejected(self):
        base = {
            "schema_version": "1.1", "session_id": "conflict", "revision": 0, "unresolved": [],
            "tasks": [], "waves": [],
        }
        def task(task_id, inputs, outputs):
            return {"id": task_id, "title": task_id, "description": "任务", "depends_on": [], "inputs": inputs, "outputs": outputs, "action": {"type": "security_review"}, "acceptance": ["业务结果"], "rollback": "恢复", "retry": {"max_attempts": 1}, "activation_gate": {"required": False, "reason": "本地"}}
        parent = copy.deepcopy(base)
        parent["tasks"] = [task("a", [], ["src"]), task("b", [], ["src/child.py"])]
        parent["waves"] = [["a", "b"]]
        result = validate_task_plan(parent)
        self.assertFalse(result["valid"])
        self.assertTrue(any("same_wave_write_conflict" in error for error in result["errors"]))
        rw = copy.deepcopy(base)
        rw["tasks"] = [task("a", [], ["src/data.json"]), task("b", ["src/data.json"], ["out.txt"])]
        rw["waves"] = [["a", "b"]]
        result = validate_task_plan(rw)
        self.assertFalse(result["valid"])
        self.assertTrue(any("same_wave_read_write_conflict" in error for error in result["errors"]))

    def test_typed_action_fields_are_exact(self):
        plan = build_task_plan(recommendation())
        broken = copy.deepcopy(plan)
        task = next(item for item in broken["tasks"] if item["action"]["type"] == "implement_feature")
        task["action"]["command"] = "echo unsafe"
        result = validate_task_plan(broken)
        self.assertFalse(result["valid"])
        self.assertTrue(any("invalid_action_fields" in error for error in result["errors"]))

    def test_capability_action_combinations_are_strict(self):
        plan = build_task_plan(recommendation())
        broken = copy.deepcopy(plan)
        task = next(item for item in broken["tasks"] if item["action"]["type"] == "define_schema")
        task["action"]["capability"] = "api"
        result = validate_task_plan(broken)
        self.assertFalse(result["valid"])
        self.assertTrue(any("invalid_capability_for_action" in error for error in result["errors"]))

    def test_platform_client_artifacts_follow_selected_stack(self):
        ios_plan = build_task_plan(recommendation(platform="ios", client_technology="SwiftUI"))
        self.assertEqual(next(task for task in ios_plan["tasks"] if task["action"]["type"] == "implement_feature")["outputs"], ["src/features/borrow-return.swift"])
        android_plan = build_task_plan(recommendation(platform="android", client_technology="Kotlin/Compose"))
        self.assertEqual(next(task for task in android_plan["tasks"] if task["action"]["type"] == "implement_feature")["outputs"], ["src/features/borrow-return.kt"])
        unsupported = build_task_plan(recommendation(platform="ios", client_technology="TypeScript"))
        self.assertFalse(any(task["action"]["type"] == "implement_feature" for task in unsupported["tasks"]))
        self.assertTrue(any("待确认" in item for item in unsupported["unresolved"]))

    def test_all_supported_platform_defaults_have_matching_artifacts(self):
        expected = {
            "windows": ("Tauri 2 + TypeScript", ".ts"),
            "macos": ("SwiftUI + Xcode", ".swift"),
            "ios": ("SwiftUI + Xcode", ".swift"),
            "android": ("Kotlin + Compose", ".kt"),
            "web": ("TypeScript + Vite", ".ts"),
            "mini_program": ("WXML", ".wxml"),
            "cli": ("Python", ".py"),
        }
        for platform, (technology, suffix) in expected.items():
            plan = build_task_plan(recommendation(platform=platform, client_technology=technology))
            implementation = next(task for task in plan["tasks"] if task["action"]["type"] == "implement_feature")
            self.assertTrue(implementation["outputs"][0].endswith(suffix), platform)

    def test_unknown_client_technology_stays_unresolved(self):
        plan = build_task_plan(recommendation(platform="ios", client_technology="COBOL"))
        self.assertFalse(any(task["action"]["type"] == "implement_feature" for task in plan["tasks"]))
        self.assertTrue(any("COBOL" in item for item in plan["unresolved"]))

    def test_plan_document_lists_task_delivery_details(self):
        value = recommendation()
        plan = build_task_plan(value)
        docs = render_documents(value, plan)
        self.assertIn("产物：", docs["plan.md"])
        self.assertIn("激活边界：", docs["plan.md"])
        self.assertIn("回滚：", docs["plan.md"])


if __name__ == "__main__":
    unittest.main()
