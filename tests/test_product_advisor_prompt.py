import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from opencoding.advisor import build_evaluation_messages
from opencoding import generic_run


class EvaluationPromptContractTests(unittest.TestCase):
    def _example_contract(self):
        messages = build_evaluation_messages({"goal": "合成项目"}, [], {}, [], {})
        payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
        return payload["response_spec"]["输出字段"]["implementation_contract"]

    def test_contract_prompt_requires_app_only_candidate_files(self):
        messages = build_evaluation_messages(
            {"goal": "合成项目", "answers": {}},
            [],
            {"platforms": {}, "stack": "python", "capabilities": [], "unresolved": []},
            [],
            {"toolchains": {}},
        )
        prompt = messages[-1]["content"]
        self.assertIn("所有 files.path 都必须位于 app/ 目录下", prompt)
        self.assertIn("不要把 tests/、README.md 或其他项目根文件", prompt)
        self.assertIn("restart 阶段的 assert", prompt)
        self.assertIn("不能只断言数量、版本号或布尔值", prompt)

    def test_prompt_example_runs_across_processes_and_rejects_memory_only_storage(self):
        contract = generic_run._validate_contract(self._example_contract())
        self.assertIn("持久化记录-A", contract["business_identities"])
        source = '''
import json
from pathlib import Path
STORE = Path(__file__).resolve().parent / "data" / "xx.json"
MEMORY = []
def create_record(name):
    if not name.strip():
        raise ValueError("blank")
    result = {"name": name}
    MEMORY.append(result)
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(MEMORY), encoding="utf-8")
    return result
def list_records():
    return json.loads(STORE.read_text(encoding="utf-8"))
'''
        # Both implementations write the file: file existence alone must not pass restart.
        for memory_only in (False, True):
            with self.subTest(memory_only=memory_only), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "app").mkdir()
                (root / "app/__init__.py").write_text("", encoding="utf-8")
                code = source.replace('return json.loads(STORE.read_text(encoding="utf-8"))',
                                      'return MEMORY') if memory_only else source
                (root / "app/main.py").write_text(code, encoding="utf-8")
                checks = root / "frozen_checks"
                checks.mkdir()
                (checks / "spec.json").write_text(json.dumps(
                    generic_run._frozen_check_spec("gen-prompt", "合成", contract)),
                    encoding="utf-8")
                (checks / "checker.py").write_text(generic_run.FROZEN_CHECKER_SOURCE, encoding="utf-8")
                results = [subprocess.run(
                    [sys.executable, "-B", "-X", "utf8", str(checks / "checker.py"), phase],
                    cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=15,
                ) for phase in ("main", "restart")]
                self.assertEqual(results[0].returncode, 0, results[0].stdout + results[0].stderr)
                self.assertEqual(results[1].returncode, 1 if memory_only else 0,
                                 results[1].stdout + results[1].stderr)

    def test_identity_matching_stays_exact_when_business_rule_is_not_declared(self):
        raw = self._example_contract()
        raw["steps"][0]["args"] = ["  持久化记录-A  "]
        with self.assertRaises(generic_run.GenericRunError) as caught:
            generic_run._validate_contract(raw)
        self.assertEqual(caught.exception.code, "restart_assert_unlinked")


if __name__ == "__main__":
    unittest.main()
