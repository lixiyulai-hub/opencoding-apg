import unittest

from opencoding.advisor import build_evaluation_messages


class EvaluationPromptContractTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
