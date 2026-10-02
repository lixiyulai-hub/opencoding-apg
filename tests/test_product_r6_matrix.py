"""r6 有限行为矩阵持久回归（W01–W10）。

每条测试对应 r5 审核窗口给出的一个场景：W01–W03 属于 F08 可信业务观察与故障
归因，W04–W07 属于 F02 事实前置与有效输入，W08 为不支持边界，W09–W10 属于
F10 真实事务关联与回滚中断。全部使用临时目录与固定合成夹具，不触网、不触真实
项目、不调用真实模型。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from opencoding import autorun as autorun_module
from opencoding import grants, service
from opencoding import transactions as transactions_module
from opencoding.aiadapter import MockAdapter
from opencoding.autorun import LENDREG_SCENARIO, AutorunError
from opencoding.facts import add_fact
from opencoding.intake import QUESTION_DEFINITIONS

from tests import REVIEWED_FIXTURE
from tests.test_product_autorun import (
    APP,
    INIT,
    MAIN,
    MESSAGES,
    MODELS,
    MockResponder,
    STORAGE,
    TASK_FILES,
    TEST_GUARD,
    TEST_RESTART,
)


ANSWERS = {
    "audience": "社区居民和管理员",
    "platform": "命令行",
    "outcome": "登记借用并确认归还",
    "data_persistence": "需要",
    "cross_device": "不需要",
    "multi_user": "不需要",
}


def make_grant(root: Path) -> dict:
    return grants.issue_batch_grant(
        root,
        goal="r6 有限行为矩阵批次",
        allowed_paths=["lendreg", "tests", "scripts", "data", "RECOVERY.md"],
        action_kinds=["local_write", "local_run", "ai_request"],
        issued_by="r6 合成测试替身",
        budget={"max_ai_requests": 40, "max_repair_rounds": 12},
    )


def run_batch(*args, **kwargs):
    """N01-a：受信任合成测试显式绑定已审查夹具身份。"""

    kwargs.setdefault("trusted_fixture", REVIEWED_FIXTURE)
    return autorun_module.run_batch(*args, **kwargs)


def load_ledger(root: Path, run_id: str) -> dict:
    return json.loads(
        (root / ".opencoding" / "autoruns" / (run_id + ".json")).read_text(encoding="utf-8")
    )


def build_session(root: Path, platform: str = "命令行") -> str:
    view = service.create_session(root, "社区借还登记")
    answers = dict(ANSWERS, platform=platform)
    for question in QUESTION_DEFINITIONS:
        view = service.submit_answer(
            root, view["session"]["id"], view["session"]["revision"],
            question["id"], answers.get(question["id"], "不需要"),
        )
    return view["session"]["id"]


def inventory(root: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root)).replace("\\", "/")] = path.stat().st_size
    return result


def write_reference_sources(root: Path) -> None:
    """写入一份正常参考实现（lendreg 包 + 两个交付测试）。"""

    for task_id in ("d01-models", "d02-storage", "d03-add", "d04-lend", "d08-chinese-entry"):
        for entry in TASK_FILES[task_id]:
            path = root / entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(entry["content"], encoding="utf-8")
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_borrow_guard.py").write_text(TEST_GUARD, encoding="utf-8")
    (root / "tests" / "test_restart_persistence.py").write_text(TEST_RESTART, encoding="utf-8")


def verify_test_artifact(root: Path, module_name: str):
    autorun_module._set_trusted_fixture(root, REVIEWED_FIXTURE)
    try:
        return autorun_module._verify_python_test_artifact(
            root, "tests/" + module_name + ".py", module_name
        )
    finally:
        autorun_module._TRUSTED_FIXTURES.pop(str(root), None)


class F08TrustedObservationTests(unittest.TestCase):
    """W01–W03：完成信号必须来自可信侧观察到的业务行为。"""

    def test_w01_non_object_business_result_freezes_without_commit(self):
        class NonObjectResponder:
            def __call__(self, messages, *, request_kind, nonce):
                if request_kind != "implement":
                    return json.dumps({"nonce": nonce, "summary": "s", "files": []})
                return json.dumps({
                    "nonce": nonce,
                    "summary": "非对象业务结果",
                    "files": [
                        {"path": "lendreg/__init__.py", "content": INIT},
                        {"path": "lendreg/models.py", "content": (
                            "def new_item(item_id, name):\n"
                            "    return []\n"
                            "def new_loan(loan_id, item_id, borrower):\n"
                            "    return None\n"
                        )},
                    ],
                }, ensure_ascii=False)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            summary = run_batch(
                root,
                {"name": "W01", "goal": LENDREG_SCENARIO["goal"],
                 "tasks": [task for task in LENDREG_SCENARIO["tasks"] if task["task_id"] == "d01-models"],
                 "verifiers": LENDREG_SCENARIO["verifiers"]},
                grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=NonObjectResponder()),
                run_id="run-w01",
            )
            self.assertEqual(summary["states"]["d01-models"], "frozen")
            self.assertFalse(summary["deliverable"])
            self.assertFalse((root / "lendreg" / "models.py").exists())
            self.assertGreater(
                grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"], 0
            )

    def test_w02_dead_branch_declaration_with_forged_output_is_not_deliverable(self):
        forged = (
            "import json\n"
            "import sys\n"
            "if False:\n"
            "    def new_item(item_id, name):\n"
            "        return {'id': item_id, 'name': name, 'status': 'available'}\n"
            "    def new_loan(loan_id, item_id, borrower):\n"
            "        return {'id': loan_id, 'item_id': item_id, 'borrower': borrower, 'closed': False}\n"
            "print('CONTROLLER_D01=' + json.dumps({\n"
            "    'item': {'id': 'n-1', 'name': '测试物品', 'status': 'available'},\n"
            "    'loan': {'id': 'l-1', 'item_id': 'n-1', 'borrower': '借用人甲', 'closed': False},\n"
            "    'invalid_empty_id_rejected': True,\n"
            "    'invalid_long_id_rejected': True,\n"
            "    'empty_name_rejected': True,\n"
            "}, ensure_ascii=False, sort_keys=True))\n"
            "raise SystemExit(0)\n"
        )

        def responder(messages, *, request_kind, nonce):
            if request_kind != "implement":
                return json.dumps({"nonce": nonce, "summary": "s", "files": []})
            return json.dumps({
                "nonce": nonce,
                "summary": "死分支声明 + 伪造输出",
                "files": [
                    {"path": "lendreg/__init__.py", "content": INIT},
                    {"path": "lendreg/models.py", "content": forged},
                ],
            }, ensure_ascii=False)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            summary = run_batch(
                root,
                {"name": "W02", "goal": LENDREG_SCENARIO["goal"],
                 "tasks": [task for task in LENDREG_SCENARIO["tasks"] if task["task_id"] == "d01-models"],
                 "verifiers": LENDREG_SCENARIO["verifiers"]},
                grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=responder),
                run_id="run-w02",
            )
            self.assertEqual(summary["states"]["d01-models"], "frozen")
            self.assertEqual(summary["succeeded"], 0)
            self.assertFalse(summary["deliverable"])
            self.assertFalse((root / "lendreg" / "models.py").exists())

    def test_x01_argv_challenges_and_printed_marker_are_not_completion_signals(self):
        """r7/X01→r8/Y01 分层：argv 挑战与 stdout 标记路径在材料身份层被切断。

        候选声明了可调用的业务函数（通过静态检查），模块导入时从自身 argv 找
        旧挑战值、打印 `__TRUSTED_D01__` 伪记录并退出自身子进程。r8 材料身份
        绑定后：候选字节与已审查固定参考不一致，在材料身份层即被拒绝（探针
        不再执行），结论必须是 verification unavailable 且不可交付。
        """
        forged = (
            "import json\n"
            "import sys\n"
            "def new_item(item_id, name):\n"
            "    return {'id': item_id, 'name': name, 'status': 'available'}\n"
            "def new_loan(loan_id, item_id, borrower):\n"
            "    return {'id': loan_id, 'item_id': item_id, 'borrower': borrower, 'closed': False}\n"
            "argv_challenges = [a for a in sys.argv[1:] if a.startswith('d01c')]\n"
            "print('__TRUSTED_D01__ ' + json.dumps({\n"
            "    'item': {'id': (argv_challenges[0] if argv_challenges else 'd01c-a'),\n"
            "             'name': '测试物品', 'status': 'available'},\n"
            "    'loan': {'id': 'l-x', 'item_id': 'd01c-a', 'borrower': '借用人甲', 'closed': False},\n"
            "    'invalid_empty_id_rejected': True,\n"
            "    'invalid_long_id_rejected': True,\n"
            "    'empty_name_rejected': True,\n"
            "}, ensure_ascii=False, sort_keys=True))\n"
            "raise SystemExit(0)\n"
        )

        def responder(messages, *, request_kind, nonce):
            if request_kind != "implement":
                return json.dumps({"nonce": nonce, "summary": "s", "files": []})
            return json.dumps({
                "nonce": nonce,
                "summary": "argv 挑战 + 伪标记 + 自行退出",
                "files": [
                    {"path": "lendreg/__init__.py", "content": INIT},
                    {"path": "lendreg/models.py", "content": forged},
                ],
            }, ensure_ascii=False)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            summary = run_batch(
                root,
                {"name": "X01", "goal": LENDREG_SCENARIO["goal"],
                 "tasks": [task for task in LENDREG_SCENARIO["tasks"] if task["task_id"] == "d01-models"],
                 "verifiers": LENDREG_SCENARIO["verifiers"]},
                grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=responder),
                run_id="run-x01",
            )
            self.assertEqual(summary["states"]["d01-models"], "frozen")
            self.assertEqual(summary["succeeded"], 0)
            self.assertFalse(summary["deliverable"])
            self.assertFalse((root / "lendreg" / "models.py").exists())
            task_json = json.dumps(
                load_ledger(root, "run-x01")["tasks"]["d01-models"], ensure_ascii=False
            )
            self.assertIn("verification unavailable", task_json)

    def test_x01_printed_compliant_record_conflicts_with_sealed_observation(self):
        """r7/X01→r8/Y01 分层：任意非参考字节在材料身份层即被拒绝。

        候选业务函数返回固定假数据（契约不符），但导入时向 stdout 打印一份
        内容完全合规的记录加旧标记。r8 材料身份绑定后，非参考字节在材料身份
        层即被拒绝（探针不再执行，stdout 伪记录根本没有机会产生）；结论仍是
        冻结、不可交付。
        """
        forged = (
            "import json\n"
            "import sys\n"
            "def new_item(item_id, name):\n"
            "    return {'id': 'n-1', 'name': name, 'status': 'available'}\n"
            "def new_loan(loan_id, item_id, borrower):\n"
            "    return {'id': 'l-1', 'item_id': 'n-1', 'borrower': borrower, 'closed': False}\n"
            "print('__TRUSTED_D01__ ' + json.dumps({\n"
            "    'item': {'id': 'd01c-a', 'name': '测试物品', 'status': 'available'},\n"
            "    'loan': {'id': 'd01c-a-loan', 'item_id': 'd01c-a', 'borrower': '借用人甲', 'closed': False},\n"
            "    'invalid_empty_id_rejected': True,\n"
            "    'invalid_long_id_rejected': True,\n"
            "    'empty_name_rejected': True,\n"
            "}, ensure_ascii=False, sort_keys=True))\n"
        )

        def responder(messages, *, request_kind, nonce):
            if request_kind != "implement":
                return json.dumps({"nonce": nonce, "summary": "s", "files": []})
            return json.dumps({
                "nonce": nonce,
                "summary": "stdout 合规伪记录 + 假实现",
                "files": [
                    {"path": "lendreg/__init__.py", "content": INIT},
                    {"path": "lendreg/models.py", "content": forged},
                ],
            }, ensure_ascii=False)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            summary = run_batch(
                root,
                {"name": "X01", "goal": LENDREG_SCENARIO["goal"],
                 "tasks": [task for task in LENDREG_SCENARIO["tasks"] if task["task_id"] == "d01-models"],
                 "verifiers": LENDREG_SCENARIO["verifiers"]},
                grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=responder),
                run_id="run-x01-conflict",
            )
            self.assertEqual(summary["states"]["d01-models"], "frozen")
            self.assertEqual(summary["succeeded"], 0)
            self.assertFalse(summary["deliverable"])
            self.assertFalse((root / "lendreg" / "models.py").exists())
            task_json = json.dumps(
                load_ledger(root, "run-x01-conflict")["tasks"]["d01-models"], ensure_ascii=False
            )
            self.assertIn("D01 材料身份未登记", task_json)
            self.assertIn("verification unavailable", task_json)
            # stdout 伪记录可以进入证据文本，但不得成为验收通过的依据。
            self.assertNotEqual(summary["states"]["d01-models"], "succeeded")

    def test_w03_directory_label_test_is_not_business_detection(self):
        fake = (
            "import subprocess\n"
            "import unittest\n"
            "from pathlib import Path\n"
            "\n"
            "\n"
            "class BorrowGuardTests(unittest.TestCase):\n"
            "    def test_repeat_lend_fails_and_data_unchanged(self):\n"
            "        project = Path(__file__).resolve().parents[1]\n"
            "        if 1 == 2:\n"
            "            subprocess.run([__import__('sys').executable, '-m', 'lendreg', 'add', 't-1', '测试物'], cwd=str(project), check=True)\n"
            "            subprocess.run([__import__('sys').executable, '-m', 'lendreg', 'lend', 't-1', '借用人'], cwd=str(project), check=True)\n"
            "            store = project / 'data' / 'store.json'\n"
            "            before = store.read_bytes()\n"
            "            result = subprocess.run([__import__('sys').executable, '-m', 'lendreg', 'lend', 't-1', '另一个人'], cwd=str(project), capture_output=True)\n"
            "            self.assertNotEqual(result.returncode, 0)\n"
            "            self.assertEqual(store.read_bytes(), before)\n"
            "        self.assertNotIn('.probe-mutant-', str(project))\n"
            "\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            write_reference_sources(root)
            (root / "tests" / "test_borrow_guard.py").write_text(fake, encoding="utf-8")
            ok, evidence = verify_test_artifact(root, "test_borrow_guard")
            self.assertFalse(ok)
            self.assertIn("受控错误实现：未被检测", " ".join(evidence))

    def test_w03_positive_control_real_business_test_detects_fault(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            write_reference_sources(root)
            ok, evidence = verify_test_artifact(root, "test_borrow_guard")
            self.assertTrue(ok, " ".join(evidence))
            ok_restart, evidence_restart = verify_test_artifact(root, "test_restart_persistence")
            self.assertTrue(ok_restart, " ".join(evidence_restart))
            # r7/X02：顺序对照必须真实运行并留痕。
            self.assertIn(
                "顺序对照（normal→normal）：重复正常参考运行一致",
                " ".join(evidence) + " " + " ".join(evidence_restart),
            )

    def test_x02_state_leaking_marker_test_cannot_fake_detection(self):
        """r7/X02：跨运行状态残留的伪检出通道被快照重建关闭。

        候选测试第一次运行写状态标记、第二次运行只看标记就宣布"检出"（业务
        断言全部写死为永真）。旧实现只清 data/，标记残留到错误参考运行，会
        被误判为检出能力；新实现每次参考运行前从初始快照完整重建候选可写区，
        标记永不残留，该候选没有任何真实检出能力，不可交付。
        """
        fake = (
            "import subprocess\n"
            "import sys\n"
            "import unittest\n"
            "from pathlib import Path\n"
            "\n"
            "\n"
            "class BorrowGuardTests(unittest.TestCase):\n"
            "    def test_repeat_lend_fails_and_data_unchanged(self):\n"
            "        project = Path(__file__).resolve().parents[1]\n"
            "        marker = project / '.x02-state.marker'\n"
            "        if marker.exists():\n"
            "            self.fail('第二次运行：凭自建标记宣布伪检出')\n"
            "        subprocess.run([sys.executable, '-m', 'lendreg', 'add', 't-1', '测试物'], cwd=str(project), check=True)\n"
            "        subprocess.run([sys.executable, '-m', 'lendreg', 'lend', 't-1', '借用人'], cwd=str(project), capture_output=True)\n"
            "        store = project / 'data' / 'store.json'\n"
            "        before = store.read_bytes()\n"
            "        subprocess.run([sys.executable, '-m', 'lendreg', 'lend', 't-1', '另一个人'], cwd=str(project), capture_output=True)\n"
            "        after = store.read_bytes()\n"
            "        self.assertNotEqual(-1, -2)\n"
            "        self.assertEqual(len(before) >= 0, len(after) >= 0)\n"
            "        marker.write_text('seen', encoding='utf-8')\n"
            "\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            write_reference_sources(root)
            (root / "tests" / "test_borrow_guard.py").write_text(fake, encoding="utf-8")
            ok, evidence = verify_test_artifact(root, "test_borrow_guard")
            self.assertFalse(ok, " ".join(evidence))
            joined = " ".join(evidence)
            # 标记在等价起点上永不残留：顺序对照一致，且伪测试没有真实检出能力。
            self.assertIn("顺序对照（normal→normal）：重复正常参考运行一致", joined)
            self.assertIn("受控错误实现：未被检测", joined)

    def test_x02_marker_only_restart_test_cannot_fake_detection(self):
        """r7/X02：另一份只看自建标记的伪测试（restart 模块）同样不可交付。

        业务断言拼接保底永真；检出完全依赖第二次运行读到的自建标记。快照
        重建后标记不可能残留，验收不得给出"受控错误实现被测试拒绝"。
        """
        fake = (
            "import subprocess\n"
            "import sys\n"
            "import unittest\n"
            "from pathlib import Path\n"
            "\n"
            "\n"
            "class RestartPersistenceTests(unittest.TestCase):\n"
            "    def test_data_survives_restart(self):\n"
            "        project = Path(__file__).resolve().parents[1]\n"
            "        marker = project / '.x02-state.marker'\n"
            "        if marker.exists():\n"
            "            self.fail('第二次运行：凭自建标记宣布伪检出')\n"
            "        subprocess.run([sys.executable, '-m', 'lendreg', 'add', 'rp-1', '电磁炉'], cwd=str(project), check=True)\n"
            "        listed = subprocess.run([sys.executable, '-m', 'lendreg', 'list'], cwd=str(project), capture_output=True, text=True)\n"
            "        subprocess.run([sys.executable, '--version'], capture_output=True)\n"
            "        self.assertIn('电磁炉', listed.stdout + '电磁炉')\n"
            "        self.assertIn('借用人庚', listed.stdout + '借用人庚')\n"
            "        self.assertIn('stdout', 'stdout')\n"
            "        marker.write_text('seen', encoding='utf-8')\n"
            "\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            write_reference_sources(root)
            (root / "tests" / "test_restart_persistence.py").write_text(fake, encoding="utf-8")
            ok, evidence = verify_test_artifact(root, "test_restart_persistence")
            self.assertFalse(ok, " ".join(evidence))
            joined = " ".join(evidence)
            self.assertIn("顺序对照（normal→normal）：重复正常参考运行一致", joined)
            self.assertNotIn("受控错误实现：被测试拒绝", joined)


class F02FactBeforeJudgementTests(unittest.TestCase):
    """W04–W07：事实前置、采用输入快照与漂移停止。"""

    def test_w04_formal_adoption_chain_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_id = build_session(root)
            adopted = service.adopt_evaluation_plan(root, session_id)
            digest = adopted["adoption"]["plan_digest"]
            grant = make_grant(root)
            summary = run_batch(
                root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder(broken={"d03-add": 1})),
                run_id="run-w04", adopted_plan_digest=digest,
            )
            self.assertEqual(summary["succeeded"], 10)
            self.assertTrue(summary["deliverable"])
            used = grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"]
            self.assertEqual(used, 11)
            resumed = run_batch(
                root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()),
                run_id="run-w04", adopted_plan_digest=digest,
            )
            self.assertTrue(resumed["deliverable"])
            self.assertEqual(
                grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"], used,
            )

    def test_w05_new_hard_fact_stops_old_plan_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_id = build_session(root)
            digest = service.adopt_evaluation_plan(root, session_id)["adoption"]["plan_digest"]
            add_fact(
                root, session_id,
                content="必须在已有网站内增量修改，不得生成或运行独立命令行借还应用",
                source_type="user",
            )
            status = service.adoption_input_status(root, session_id)
            self.assertFalse(status["matches"])
            self.assertIn("facts_digest", status["drift"])
            grant = make_grant(root)
            with self.assertRaises(AutorunError) as caught:
                run_batch(
                    root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                    adapter=MockAdapter(responder=MockResponder()),
                    run_id="run-w05", adopted_plan_digest=digest,
                )
            self.assertEqual(caught.exception.code, "adopted_plan_input_drift")
            self.assertEqual(
                grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"], 0
            )

    def test_w06_platform_change_stops_old_plan_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_id = build_session(root)
            digest = service.adopt_evaluation_plan(root, session_id)["adoption"]["plan_digest"]
            revision = service.session_view(root, session_id)["session"]["revision"]
            service.submit_answer(root, session_id, revision, "platform", "网页")
            status = service.adoption_input_status(root, session_id)
            self.assertFalse(status["matches"])
            grant = make_grant(root)
            with self.assertRaises(AutorunError) as caught:
                run_batch(
                    root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                    adapter=MockAdapter(responder=MockResponder()),
                    run_id="run-w06", adopted_plan_digest=digest,
                )
            self.assertEqual(caught.exception.code, "adopted_plan_input_drift")
            self.assertEqual(
                grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"], 0
            )

    def test_x03_fact_change_after_task_completes_stops_remaining_dispatch(self):
        """r7/X03：任务完成后经外部输入改变事实簿，剩余任务的派发必须全部停止。

        D01 验收提交后通过进度回调注入新硬事实；D02 起每次派发前的中途核对
        应发现 facts_digest 漂移并结构化冻结，不再发出任何 AI 请求（旧实现只
        在批次入口核对一次，D01 之后的 9 个请求仍会全部发出）。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_id = build_session(root)
            digest = service.adopt_evaluation_plan(root, session_id)["adoption"]["plan_digest"]
            grant = make_grant(root)
            injected = {"done": False}

            def progress(text: str) -> None:
                if not injected["done"] and "已验收通过" in text:
                    injected["done"] = True
                    add_fact(
                        root, session_id,
                        content="必须在已有网站内增量修改，不得生成或运行独立命令行借还应用",
                        source_type="user",
                    )

            summary = run_batch(
                root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()),
                run_id="run-x03-dispatch", adopted_plan_digest=digest,
                progress=progress,
            )
            self.assertEqual(summary["states"]["d01-models"], "succeeded")
            for task_id in (
                "d02-storage", "d03-add", "d04-lend", "d05-borrow-guard",
                "d06-return", "d07-list", "d08-chinese-entry",
                "d09-restart", "d10-full-flow",
            ):
                self.assertEqual(summary["states"][task_id], "frozen", task_id)
            self.assertFalse(summary["deliverable"])
            self.assertEqual(
                grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"], 1
            )
            ledger = load_ledger(root, "run-x03-dispatch")
            self.assertEqual(
                ledger["tasks"]["d02-storage"]["reason"], "adopted_plan_input_drift"
            )
            self.assertFalse((root / "lendreg" / "storage.py").exists())

    def test_x03_fact_change_after_result_received_blocks_commit(self):
        """r7/X03：收结果后、提交前发现漂移，候选不落地、本任务结构化冻结。

        D02 的实现响应已生成并落盘（result_received），提交前的中途核对发现
        事实簿漂移：候选不提交（storage.py 不存在）、D02 冻结且标记未提交，
        后继任务在各自派发前同样核对并冻结。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_id = build_session(root)
            digest = service.adopt_evaluation_plan(root, session_id)["adoption"]["plan_digest"]
            grant = make_grant(root)

            class DriftAfterResultResponder:
                def __init__(self):
                    self.inner = MockResponder()
                    self.injected = False

                def __call__(self, messages, *, request_kind, nonce):
                    payload = self.inner(messages, request_kind=request_kind, nonce=nonce)
                    if (
                        not self.injected
                        and request_kind == "implement"
                        and "（d02-storage）" in messages[-1]["content"]
                    ):
                        self.injected = True
                        add_fact(
                            root, session_id,
                            content="必须在已有网站内增量修改，不得生成或运行独立命令行借还应用",
                            source_type="user",
                        )
                    return payload

            summary = run_batch(
                root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=DriftAfterResultResponder()),
                run_id="run-x03-commit", adopted_plan_digest=digest,
            )
            self.assertEqual(summary["states"]["d01-models"], "succeeded")
            self.assertEqual(summary["states"]["d02-storage"], "frozen")
            for task_id in (
                "d03-add", "d04-lend", "d05-borrow-guard", "d06-return",
                "d07-list", "d08-chinese-entry", "d09-restart", "d10-full-flow",
            ):
                self.assertEqual(summary["states"][task_id], "frozen", task_id)
            self.assertFalse(summary["deliverable"])
            self.assertFalse((root / "lendreg" / "storage.py").exists())
            ledger = load_ledger(root, "run-x03-commit")
            d02 = ledger["tasks"]["d02-storage"]
            self.assertEqual(d02["reason"], "adopted_plan_input_drift")
            self.assertFalse(d02["attempts"][-1]["committed"])
            self.assertEqual(d02["attempts"][-1]["error_code"], "adopted_plan_input_drift")
            # D01 与 D02 各发出一个请求；D02 之后没有任何新请求。
            self.assertEqual(
                grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"], 2
            )

    def test_w07_relevant_repository_fact_participates_before_judgement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_id = build_session(root)
            add_fact(
                root, session_id,
                content="仓库已有网站页面，必须在已有网站内增量修改",
                source_type="repository", source_ref="authorized-test-fixture",
            )
            evaluated = service.evaluate_session(root, session_id)
            consideration = evaluated["fact_consideration"]
            self.assertTrue(consideration)
            self.assertTrue(any(item["applied"] for item in consideration))
            self.assertEqual(evaluated["recommendation"]["platforms"]["primary"], "web")
            mapping = service._executor_mapping(evaluated)
            self.assertFalse(mapping["supported"])

    def test_w07_unrelated_fact_is_recorded_and_does_not_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_id = build_session(root)
            digest = service.adopt_evaluation_plan(root, session_id)["adoption"]["plan_digest"]
            add_fact(root, session_id, content="界面文案使用简体中文", source_type="user")
            evaluated = service.evaluate_session(root, session_id)
            rows = [item for item in evaluated["fact_consideration"] if item["applied"]]
            self.assertEqual(rows, [])
            self.assertEqual(evaluated["recommendation"]["platforms"]["primary"], "cli")
            status = service.adoption_input_status(root, session_id)
            self.assertFalse(status["matches"])
            self.assertIn("facts_digest", status["drift"])
            # 事实簿变化进入漂移核对；重新采用同一计划后漂移消失，旧计划可继续。
            readopted = service.adopt_evaluation_plan(root, session_id)
            self.assertEqual(readopted["adoption"]["plan_digest"], digest)
            self.assertTrue(service.adoption_input_status(root, session_id)["matches"])
            autorun_module._check_adoption_input_drift(root, digest, LENDREG_SCENARIO)

    def test_w08_web_and_cross_device_stay_unsupported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_id = build_session(root, platform="网页")
            service.submit_answer(
                root, session_id,
                service.session_view(root, session_id)["session"]["revision"],
                "cross_device", "需要",
            )
            evaluated = service.evaluate_session(root, session_id)
            mapping = service._executor_mapping(evaluated)
            self.assertFalse(mapping["supported"])
            self.assertIsNone(mapping["executor_id"])
            grant = make_grant(root)
            with self.assertRaises(AutorunError) as caught:
                run_batch(
                    root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                    adapter=MockAdapter(responder=MockResponder()),
                    run_id="run-w08",
                    adopted_plan_digest=service.adopt_evaluation_plan(root, session_id)["adoption"]["plan_digest"],
                )
            self.assertEqual(caught.exception.code, "adopted_plan_missing")
            self.assertEqual(
                grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"], 0
            )


class F10TransactionAssociationTests(unittest.TestCase):
    """W09–W10：结构合法不等于事务关联真实；回滚中断后状态必须不可交付。"""

    def _committed_run(self, root: Path) -> tuple[str, str, Path]:
        grant = make_grant(root)
        summary = run_batch(
            root,
            {"name": "W09", "goal": LENDREG_SCENARIO["goal"],
             "tasks": [task for task in LENDREG_SCENARIO["tasks"] if task["task_id"] == "d01-models"],
             "verifiers": LENDREG_SCENARIO["verifiers"]},
            grant_id=grant["grant_id"],
            adapter=MockAdapter(responder=MockResponder()),
            run_id="run-w09",
        )
        self.assertTrue(summary["deliverable"])
        grant_id = grant["grant_id"]
        recovery_dir = autorun_module._recovery_dir(root, "run-w09")
        records = sorted(recovery_dir.glob("*.json"))
        self.assertTrue(records)
        return "run-w09", grant_id, records[0]

    def test_w09_structurally_valid_but_unassociated_index_is_not_deliverable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run_id, _grant_id, record_path = self._committed_run(root)
            original = json.loads(record_path.read_text(encoding="utf-8"))

            tampered = dict(original)
            tampered["transaction_id"] = "tx-20990101T000000000000Z-" + "0" * 12
            record_path.write_text(json.dumps(tampered, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaises(AutorunError) as caught:
                autorun_module.read_status_snapshot(root)
            self.assertEqual(caught.exception.code, "autorun_status_store_invalid")

            tampered = dict(original)
            tampered["files"] = [dict(item, path="lendreg/other.py") for item in original["files"]]
            record_path.write_text(json.dumps(tampered, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaises(AutorunError) as caught:
                autorun_module.read_status_snapshot(root)
            self.assertEqual(caught.exception.code, "autorun_status_store_invalid")

            tampered = dict(original)
            tampered["files"] = [dict(item, after_sha256="0" * 64) for item in original["files"]]
            record_path.write_text(json.dumps(tampered, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaises(AutorunError) as caught:
                autorun_module.read_status_snapshot(root)
            self.assertEqual(caught.exception.code, "autorun_status_store_invalid")

            shutil.rmtree(root / ".opencoding" / "transactions" / original["transaction_id"])
            with self.assertRaises(AutorunError) as caught:
                autorun_module.read_status_snapshot(root)
            self.assertEqual(caught.exception.code, "autorun_status_store_invalid")

    def test_x04_recovery_record_field_tampering_is_not_verifiable(self):
        """r7/X04：恢复记录的集合/前像/操作语义/receipt_path/attempt 篡改均不可核验。

        逐项篡改恢复记录并核对状态读取入口：完整唯一文件集合双向核对拦下漏报
        与越界多报；前像摘要、操作语义、规范 receipt_path 与账本 attempt 绑定
        各自独立拦下对应伪造。原样记录必须仍然可核验（正向对照）。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run_id, _grant_id, record_path = self._committed_run(root)
            original_bytes = record_path.read_bytes()
            original = json.loads(original_bytes.decode("utf-8"))

            def expect_invalid(mutate, label):
                tampered = json.loads(json.dumps(original, ensure_ascii=False))
                mutate(tampered)
                record_path.write_text(
                    json.dumps(tampered, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(AutorunError, msg=label) as caught:
                    autorun_module.read_status_snapshot(root)
                self.assertEqual(caught.exception.code, "autorun_status_store_invalid", label)

            try:
                # 正向对照：原样记录可核验，交付状态不受影响。
                snapshot = autorun_module.read_status_snapshot(root)
                run_rows = [row for row in snapshot["runs"] if row["run_id"] == run_id]
                self.assertTrue(run_rows)
                self.assertTrue(run_rows[0]["current_deliverable"])

                expect_invalid(lambda r: r["files"].pop(), "漏报文件条目")
                expect_invalid(
                    lambda r: r["files"].append({
                        "path": "lendreg/extra.py", "operation": "create",
                        "before_sha256": None, "after_sha256": "a" * 64,
                    }),
                    "越界多报文件条目",
                )
                expect_invalid(lambda r: r["files"].append(dict(r["files"][0])), "重复路径")
                expect_invalid(
                    lambda r: [item.update(before_sha256="0" * 64) for item in r["files"]],
                    "前像摘要与事务清单不一致",
                )
                expect_invalid(
                    lambda r: [item.update(operation="update") for item in r["files"]],
                    "操作语义与回执 before_exists 推导不一致",
                )
                expect_invalid(
                    lambda r: r.update(
                        receipt_path=str(root / ".opencoding" / "elsewhere" / "receipt.json")
                    ),
                    "receipt_path 偏离规范事务路径",
                )
                expect_invalid(lambda r: r.update(attempt=99), "尝试号不在账本尝试历史")
            finally:
                record_path.write_bytes(original_bytes)
            # 恢复原样后可核验性恢复（篡改动作本身不留下持久影响）。
            snapshot = autorun_module.read_status_snapshot(root)
            run_rows = [row for row in snapshot["runs"] if row["run_id"] == run_id]
            self.assertTrue(run_rows[0]["current_deliverable"])

    def test_w10_real_rollback_then_index_write_interruption_blocks_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run_id, _grant_id, record_path = self._committed_run(root)
            original = json.loads(record_path.read_text(encoding="utf-8"))
            real_write = autorun_module._write_json_atomic

            def interrupted_write(path, record):
                if Path(path).parent.name == "recovery":
                    raise RuntimeError("synthetic interruption before recovery index update")
                return real_write(path, record)

            autorun_module._write_json_atomic = interrupted_write
            try:
                with self.assertRaises(RuntimeError):
                    autorun_module.rollback_task_files(root, run_id)
            finally:
                autorun_module._write_json_atomic = real_write

            for item in original["files"]:
                self.assertFalse((root / item["path"]).exists(), item["path"])
            receipt = transactions_module.read_receipt_state(root, original["transaction_id"])
            self.assertEqual(receipt["rollback_status"], "rolled_back")
            persisted = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["state"], "committed")

            before = inventory(root)
            snapshot = autorun_module.read_status_snapshot(root)
            self.assertEqual(inventory(root), before)
            run_rows = [row for row in snapshot["runs"] if row["run_id"] == run_id]
            self.assertTrue(run_rows)
            self.assertNotEqual(run_rows[0]["status"], "succeeded")
            self.assertFalse(run_rows[0]["current_deliverable"])
            queried = service.execution_status(root)
            self.assertFalse(any(row.get("current_deliverable") for row in queried["runs"]))


class R8AuditFixTests(unittest.TestCase):
    """r8 审核三缺口：Y01 材料身份绑定、Y03 精确提交绑定、Y04 修复计数顺序。

    Y01：密封观察的密语与结果路径都在候选自身解释器的 env 中，HMAC 验签不能
    区分驱动写出与候选写出——只有与已审查固定参考逐字节一致的候选才允许合成
    控制流验收，其余一律 verification unavailable（安全拒绝）。
    Y03：恢复索引必须命中产生本次提交的 committed 绑定（事务+尝试+文件集合），
    账本历史中存在该尝试号不等于属于这一次提交。
    Y04：修复轮数计数与预算预留之前先核对采用输入，未派发的修复不得计入额度。
    """

    def _single_task_scenario(self) -> dict:
        return {"name": "r8", "goal": LENDREG_SCENARIO["goal"],
                "tasks": [task for task in LENDREG_SCENARIO["tasks"] if task["task_id"] == "d01-models"],
                "verifiers": LENDREG_SCENARIO["verifiers"]}

    def test_y01_reviewed_material_registration_matches_fixture_bytes(self):
        """材料身份登记值必须与已审查夹具的实际字节绑定（不是任意字符串标签）。"""
        expected = {
            entry["path"]: hashlib.sha256(entry["content"].encode("utf-8")).hexdigest()
            for entry in TASK_FILES["d01-models"]
        }
        self.assertEqual(autorun_module._D01_REVIEWED_MATERIAL, expected)
        self.assertEqual(set(autorun_module._D01_REVIEWED_MATERIAL), set(autorun_module._D01_MATERIAL_SURFACE))

    def test_y01_env_sealed_hmac_forgery_is_not_verifiable(self):
        """Y01 原样反例：无运行时业务函数，仅读自身 env 写合法 HMAC 即退出。

        r7 实现下该候选实际提交（trusted/deliverable）；r8 材料身份层在探针
        执行前拒绝——密封观察不再运行，结论冻结、不可交付，请求与预算保留。
        """
        forged = (
            "import hashlib, hmac, json, os\n"
            "if False:\n"
            "    def new_item(*args): pass\n"
            "    def new_loan(*args): pass\n"
            "rows = []\n"
            "for value in ('d01c-a', 'd01c-b'):\n"
            "    rows.append({'item': {'id': value, 'name': '测试物品', 'status': 'available'},\n"
            "        'loan': {'id': value + '-loan', 'item_id': value, 'closed': False},\n"
            "        'invalid_empty_id_rejected': True, 'invalid_long_id_rejected': True,\n"
            "        'empty_name_rejected': True})\n"
            "body = json.dumps(rows, ensure_ascii=False, sort_keys=True)\n"
            "material = os.environ['OPENCODING_D01_SECRET']\n"
            "signature = hmac.new(hashlib.sha256(material.encode('utf-8')).digest(),"
            " body.encode('utf-8'), hashlib.sha256).hexdigest()\n"
            "with open(os.environ['OPENCODING_D01_RESULT'], 'w', encoding='utf-8') as handle:\n"
            "    handle.write(signature + '\\n' + body)\n"
            "os._exit(0)\n"
        )

        def responder(messages, *, request_kind, nonce):
            if request_kind != "implement":
                return json.dumps({"nonce": nonce, "summary": "s", "files": []})
            return json.dumps({
                "nonce": nonce,
                "summary": "Y01 env 密封伪造",
                "files": [
                    {"path": "lendreg/__init__.py", "content": ""},
                    {"path": "lendreg/models.py", "content": forged},
                ],
            }, ensure_ascii=False)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            summary = run_batch(
                root, self._single_task_scenario(), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=responder), run_id="run-y01",
            )
            self.assertEqual(summary["states"]["d01-models"], "frozen")
            self.assertFalse(summary["deliverable"])
            self.assertFalse((root / "lendreg" / "models.py").exists())
            task_json = json.dumps(
                load_ledger(root, "run-y01")["tasks"]["d01-models"], ensure_ascii=False
            )
            # 分层显示：样本到达材料身份拒绝层（verification unavailable）。
            self.assertIn("材料身份", task_json)
            self.assertIn("verification unavailable", task_json)
            # 失败可重试（同签名 no_progress 停止）：3 次请求全部消耗在重试上，
            # 候选从未提交；预算如实保留消耗记录，不伪造未请求状态。
            ledger = load_ledger(root, "run-y01")["tasks"]["d01-models"]
            self.assertEqual([item["attempt"] for item in ledger["attempts"]], [1, 2, 3])
            self.assertTrue(all(item.get("verification_ok") is False for item in ledger["attempts"]))
            self.assertEqual(
                grants.load_grant(root, grant["grant_id"])["budget_used"]["ai_requests"], 3
            )

    def test_y01_modified_reviewed_bytes_are_rejected_and_reference_bytes_accepted(self):
        """注释级修改也不通过；逐字节参考材料保持合成控制流验收通过。"""
        modified = MODELS + "\n# innocent comment\n"

        def modified_responder(messages, *, request_kind, nonce):
            if request_kind != "implement":
                return json.dumps({"nonce": nonce, "summary": "s", "files": []})
            return json.dumps({
                "nonce": nonce,
                "summary": "参考材料注释级修改",
                "files": [
                    {"path": "lendreg/__init__.py", "content": INIT},
                    {"path": "lendreg/models.py", "content": modified},
                ],
            }, ensure_ascii=False)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            summary = run_batch(
                root, self._single_task_scenario(), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=modified_responder), run_id="run-y01-modified",
            )
            self.assertEqual(summary["states"]["d01-models"], "frozen")
            self.assertFalse(summary["deliverable"])
            task_json = json.dumps(
                load_ledger(root, "run-y01-modified")["tasks"]["d01-models"], ensure_ascii=False
            )
            self.assertIn("D01 材料身份未登记", task_json)

            accepted = run_batch(
                root, self._single_task_scenario(), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=MockResponder()), run_id="run-y01-reference",
            )
            self.assertEqual(accepted["states"]["d01-models"], "succeeded")
            self.assertTrue(accepted["deliverable"])

    def test_y03_recovery_index_must_reference_the_actually_committed_attempt(self):
        """失败1→提交2：恢复索引改指历史失败尝试1必须阻断，原样2与99对照。"""

        class RetryResponder:
            def __init__(self):
                self.calls = 0

            def __call__(self, messages, *, request_kind, nonce):
                if request_kind != "implement":
                    return json.dumps({"nonce": nonce, "summary": "s", "files": []})
                self.calls += 1
                models = MODELS if self.calls >= 2 else MODELS.replace("available", "wrong")
                return json.dumps({
                    "nonce": nonce,
                    "summary": "Y03 先失败后提交",
                    "files": [
                        {"path": "lendreg/__init__.py", "content": INIT},
                        {"path": "lendreg/models.py", "content": models},
                    ],
                }, ensure_ascii=False)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            responder = RetryResponder()
            summary = run_batch(
                root, self._single_task_scenario(), grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=responder), run_id="run-y03",
            )
            self.assertEqual(summary["states"]["d01-models"], "succeeded")
            ledger = load_ledger(root, "run-y03")
            attempts = ledger["tasks"]["d01-models"]["attempts"]
            self.assertEqual([item["attempt"] for item in attempts], [1, 2])
            self.assertEqual([item.get("verification_ok") for item in attempts], [False, True])
            recovery_dir = autorun_module._recovery_dir(root, "run-y03")
            record_path = sorted(recovery_dir.glob("d01-models-*.json"))[0]
            original_bytes = record_path.read_bytes()
            original = json.loads(original_bytes.decode("utf-8"))
            self.assertEqual(original["attempt"], 2)

            snapshot = autorun_module.read_status_snapshot(root)
            rows = [row for row in snapshot["runs"] if row["run_id"] == "run-y03"]
            self.assertTrue(rows[0]["current_deliverable"])

            def expect_invalid(mutate, label):
                tampered = json.loads(json.dumps(original, ensure_ascii=False))
                mutate(tampered)
                record_path.write_text(
                    json.dumps(tampered, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(AutorunError, msg=label) as caught:
                    autorun_module.read_status_snapshot(root)
                self.assertEqual(caught.exception.code, "autorun_status_store_invalid", label)

            try:
                expect_invalid(lambda r: r.update(attempt=1), "错绑历史失败尝试")
                expect_invalid(lambda r: r.update(attempt=99), "不存在的历史尝试")
            finally:
                record_path.write_bytes(original_bytes)
            snapshot = autorun_module.read_status_snapshot(root)
            rows = [row for row in snapshot["runs"] if row["run_id"] == "run-y03"]
            self.assertTrue(rows[0]["current_deliverable"])

    def test_y04_repair_budget_not_consumed_when_input_drifts_before_repair(self):
        """失败返回后、修复准备前注入事实变化：repair 预算 0、无新派发。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_id = build_session(root)
            digest = service.adopt_evaluation_plan(root, session_id)["adoption"]["plan_digest"]
            grant = make_grant(root)
            injected = {"done": False}

            real_stage = autorun_module._stage_and_verify

            def injecting_stage(*args, **kwargs):
                outcome = real_stage(*args, **kwargs)
                if not outcome[0] and not injected["done"]:
                    injected["done"] = True
                    add_fact(
                        root, session_id,
                        content="必须在已有网站内增量修改，不得生成或运行独立命令行借还应用",
                        source_type="user",
                    )
                return outcome

            broken_calls = []

            def responder(messages, *, request_kind, nonce):
                match = re.search(r"（(d\d{2}-[a-z-]+)）", messages[-1]["content"])
                task_id = match.group(1) if match else "unknown"
                if request_kind == "implement":
                    broken_calls.append(task_id)
                    models = MODELS.replace("available", "wrong")
                    return json.dumps({
                        "nonce": nonce,
                        "summary": "Y04 真实失败候选",
                        "files": [
                            {"path": "lendreg/__init__.py", "content": INIT},
                            {"path": "lendreg/models.py", "content": models},
                        ],
                    }, ensure_ascii=False)
                return json.dumps({"nonce": nonce, "summary": "s", "files": []})

            autorun_module._stage_and_verify = injecting_stage
            try:
                summary = run_batch(
                    root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                    adapter=MockAdapter(responder=responder), run_id="run-y04",
                    adopted_plan_digest=digest,
                )
            finally:
                autorun_module._stage_and_verify = real_stage

            self.assertEqual(summary["states"]["d01-models"], "frozen")
            self.assertFalse(summary["deliverable"])
            budget = grants.load_grant(root, grant["grant_id"])["budget_used"]
            self.assertEqual(budget["ai_requests"], 1, "漂移后不得再发出任何请求")
            self.assertEqual(budget["repair_rounds"], 0, "未派发的修复不得计入修复额度")
            ledger = load_ledger(root, "run-y04")
            task_record = ledger["tasks"]["d01-models"]
            self.assertEqual(task_record["repairs"], 0)
            self.assertEqual(task_record["reason"], "adopted_plan_input_drift")
            self.assertEqual(broken_calls, ["d01-models"], "只允许最初一次实现请求")
            self.assertFalse((root / "lendreg" / "storage.py").exists())


if __name__ == "__main__":
    unittest.main()
