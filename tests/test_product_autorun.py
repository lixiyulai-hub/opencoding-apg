"""自主执行循环契约测试：十任务连续推进、有限修复、冻结与断点接续。

本模块的 MockResponder 是测试替身：把借还示例源码以剧本方式返回，
用于验证控制层机制（凭据、写入、验收、修复、预算、账本）。它不代表
真实 AI 产出；真实链路见 .opencoding-dev-runs 下的真实运行证据。
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from opencoding import grants, uxtext
from opencoding import autorun as autorun_module
from opencoding.aiadapter import MockAdapter
from opencoding.autorun import LENDREG_SCENARIO, AutorunError, validate_scenario
from tests import REVIEWED_FIXTURE


def run_batch(*args, **kwargs):
    """N01-a：受信任合成测试显式绑定已审查夹具身份（不再使用环境变量豁免）。"""

    kwargs.setdefault("trusted_fixture", REVIEWED_FIXTURE)
    from opencoding.autorun import run_batch as _run_batch_impl

    return _run_batch_impl(*args, **kwargs)


MODELS = '''"""借还登记数据模型（合成示例，无真实个人信息）。"""

from datetime import datetime, timezone


def _utcnow():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _check_id(value, label):
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError(label + " 必须是非空字符串且不超过 64 字符")


def new_item(item_id, name):
    _check_id(item_id, "物品编号")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("物品名称不能为空")
    return {"id": item_id, "name": name.strip(), "status": "available", "created_at": _utcnow()}


def new_loan(loan_id, item_id, borrower):
    _check_id(loan_id, "记录编号")
    _check_id(item_id, "物品编号")
    if not isinstance(borrower, str) or not borrower.strip():
        raise ValueError("借用人不能为空")
    return {
        "id": loan_id,
        "item_id": item_id,
        "borrower": borrower.strip(),
        "borrowed_at": _utcnow(),
        "returned_at": None,
        "closed": False,
    }
'''

STORAGE = '''"""本地 JSON 保存层：原子写、缺失返空表、损坏不破坏原文件。"""

import json
import os
from pathlib import Path
import tempfile


def save_data(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".store-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\\n") as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_data(path):
    target = Path(path)
    if not target.exists():
        return {"items": {}, "loans": []}
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("数据文件损坏，未做修改") from exc
    if not isinstance(data, dict) or "items" not in data or "loans" not in data:
        raise ValueError("数据文件结构无效")
    return data
'''

MESSAGES = '''"""中文提示集中定义。"""

MSG = {
    "usage_add": "用法：python -m lendreg add <物品编号> <名称>",
    "usage_lend": "用法：python -m lendreg lend <物品编号> <借用人>",
    "usage_return": "用法：python -m lendreg return <物品编号>",
    "item_exists": "错误：该物品编号已存在，未做修改。",
    "item_missing": "错误：物品不存在。",
    "item_borrowed": "错误：该物品已借出，暂不能重复借出。",
    "item_available": "错误：该物品当前未借出，无需归还。",
    "unknown_command": "错误：不支持的命令。",
    "add_ok": "已新增物品：{name}",
    "lend_ok": "借出成功：{item} → {borrower}",
    "return_ok": "归还完成：{item}",
    "list_available": "== 可用物品 ==",
    "list_borrowed": "== 借出中 ==",
    "list_history": "== 历史记录 ==",
    "list_empty": "（暂无）",
}
'''

APP = '''"""中文命令行借还登记主流程。"""

import sys
import uuid

try:
    from . import messages
except ImportError:
    messages = None

from . import models, storage

_FALLBACK = {
    "usage_add": "用法：python -m lendreg add <物品编号> <名称>",
    "usage_lend": "用法：python -m lendreg lend <物品编号> <借用人>",
    "usage_return": "用法：python -m lendreg return <物品编号>",
    "item_exists": "错误：该物品编号已存在，未做修改。",
    "item_missing": "错误：物品不存在。",
    "item_borrowed": "错误：该物品已借出，暂不能重复借出。",
    "item_available": "错误：该物品当前未借出，无需归还。",
    "unknown_command": "错误：不支持的命令。",
    "add_ok": "已新增物品：{name}",
    "lend_ok": "借出成功：{item} → {borrower}",
    "return_ok": "归还完成：{item}",
    "list_available": "== 可用物品 ==",
    "list_borrowed": "== 借出中 ==",
    "list_history": "== 历史记录 ==",
    "list_empty": "（暂无）",
}


def _msg(key):
    if messages is not None:
        return messages.MSG[key]
    return _FALLBACK[key]

STORE_PATH = "data/store.json"
USAGE = "用法：python -m lendreg <add|lend|return|list> ..."


def _load():
    return storage.load_data(STORE_PATH)


def _save(data):
    storage.save_data(STORE_PATH, data)


def cmd_add(args):
    if len(args) != 2:
        print(_msg("usage_add"), file=sys.stderr)
        return 2
    item_id, name = args
    data = _load()
    if item_id in data["items"]:
        print(_msg("item_exists"), file=sys.stderr)
        return 1
    data["items"][item_id] = models.new_item(item_id, name)
    _save(data)
    print(_msg("add_ok").format(name=name))
    return 0


def cmd_lend(args):
    if len(args) != 2:
        print(_msg("usage_lend"), file=sys.stderr)
        return 2
    item_id, borrower = args
    data = _load()
    item = data["items"].get(item_id)
    if item is None:
        print(_msg("item_missing"), file=sys.stderr)
        return 1
    if item["status"] != "available":
        print(_msg("item_borrowed"), file=sys.stderr)
        return 1
    item["status"] = "borrowed"
    data["loans"].append(models.new_loan(uuid.uuid4().hex[:8], item_id, borrower))
    _save(data)
    print(_msg("lend_ok").format(item=item["name"], borrower=borrower))
    return 0


def cmd_return(args):
    if len(args) != 1:
        print(_msg("usage_return"), file=sys.stderr)
        return 2
    item_id = args[0]
    data = _load()
    item = data["items"].get(item_id)
    if item is None:
        print(_msg("item_missing"), file=sys.stderr)
        return 1
    open_loan = None
    for loan in reversed(data["loans"]):
        if loan["item_id"] == item_id and not loan["closed"]:
            open_loan = loan
            break
    if open_loan is None:
        print(_msg("item_available"), file=sys.stderr)
        return 1
    open_loan["closed"] = True
    open_loan["returned_at"] = models._utcnow()
    item["status"] = "available"
    _save(data)
    print(_msg("return_ok").format(item=item["name"]))
    return 0


def cmd_list(args):
    data = _load()
    lines = [_msg("list_available")]
    available = [item for item in data["items"].values() if item["status"] == "available"]
    lines.extend("{id} {name}".format(**item) for item in available)
    if not available:
        lines.append(_msg("list_empty"))
    lines.append(_msg("list_borrowed"))
    borrowed = [item for item in data["items"].values() if item["status"] == "borrowed"]
    borrowers = {}
    for loan in data["loans"]:
        if not loan["closed"]:
            borrowers[loan["item_id"]] = loan["borrower"]
    lines.extend("{id} {name} → {who}".format(**item, who=borrowers.get(item["id"], "?")) for item in borrowed)
    if not borrowed:
        lines.append(_msg("list_empty"))
    lines.append(_msg("list_history"))
    lines.extend(
        "{id} {item_id} {borrower} 借于 {borrowed_at}".format(
            id=loan["id"], item_id=loan["item_id"], borrower=loan["borrower"], borrowed_at=loan["borrowed_at"]
        ) + ("（已归还）" if loan["closed"] else "")
        for loan in data["loans"]
    )
    if not data["loans"]:
        lines.append(_msg("list_empty"))
    print("\\n".join(lines))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    command, args = argv[0], argv[1:]
    handlers = {"add": cmd_add, "lend": cmd_lend, "return": cmd_return, "list": cmd_list}
    handler = handlers.get(command)
    if handler is None:
        print(_msg("unknown_command"), file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    try:
        return handler(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
'''

INIT = '''"""lendreg 包：中文单机借还登记（合成示例）。"""

from .models import new_item, new_loan

__all__ = ["new_item", "new_loan"]
'''

MAIN = '''import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main())
'''

TEST_GUARD = '''"""黑盒测试：重复借出被拒绝且数据不被破坏。"""

import json
import subprocess
import sys
import unittest
from pathlib import Path


class BorrowGuardTests(unittest.TestCase):
    def test_repeat_lend_fails_and_data_unchanged(self):
        project = Path(__file__).resolve().parents[1]
        subprocess.run([sys.executable, "-m", "lendreg", "add", "t-1", "测试物"], cwd=project, check=True)
        subprocess.run([sys.executable, "-m", "lendreg", "lend", "t-1", "借用人"], cwd=project, check=True)
        store = project / "data" / "store.json"
        before = store.read_bytes()
        result = subprocess.run([sys.executable, "-m", "lendreg", "lend", "t-1", "另一个人"], cwd=project, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(store.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
'''

TEST_RESTART = '''"""黑盒测试：重启进程后数据仍可读取。"""

import subprocess
import sys
import unittest
from pathlib import Path


class RestartPersistenceTests(unittest.TestCase):
    def test_data_survives_process_restart(self):
        project = Path(__file__).resolve().parents[1]
        add_result = subprocess.run([sys.executable, "-m", "lendreg", "add", "r-1", "电磁炉"], cwd=project, capture_output=True, text=True)
        self.assertEqual(add_result.returncode, 0, add_result.stderr)
        lend_result = subprocess.run([sys.executable, "-m", "lendreg", "lend", "r-1", "借用人庚"], cwd=project, capture_output=True, text=True)
        self.assertEqual(lend_result.returncode, 0, lend_result.stderr)
        result = subprocess.run([sys.executable, "-m", "lendreg", "list"], cwd=project, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("电磁炉", result.stdout)
        self.assertIn("借用人庚", result.stdout)


if __name__ == "__main__":
    unittest.main()
'''

FULL_FLOW = '''"""完整流程串联验证。"""

import subprocess
import sys

STEPS = [
    ["add", "ff-1", "投影仪"],
    ["add", "ff-2", "插线板"],
    ["lend", "ff-1", "借用人辛"],
    ["lend", "ff-1", "借用人壬"],
    ["return", "ff-1"],
    ["list"],
]


def main():
    for index, step in enumerate(STEPS):
        result = subprocess.run([sys.executable, "-m", "lendreg", *step], capture_output=True, text=True)
        repeat_guard = index == 3
        failed = result.returncode != 0
        if repeat_guard and not failed:
            print("完整流程失败：重复借出未被阻止。", file=sys.stderr)
            return 1
        if not repeat_guard and failed:
            print("完整流程失败：步骤 " + " ".join(step) + "：" + result.stderr, file=sys.stderr)
            return 1
        if step[0] == "list":
            if "投影仪" not in result.stdout or "插线板" not in result.stdout:
                print("完整流程失败：list 缺少物品。", file=sys.stderr)
                return 1
    print("完整流程通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

RECOVERY = '''# 受控恢复说明

本批代码文件清单：
- `lendreg/__init__.py`
- `lendreg/models.py`
- `lendreg/storage.py`
- `lendreg/app.py`
- `lendreg/__main__.py`
- `lendreg/messages.py`
- `tests/test_borrow_guard.py`
- `tests/test_restart_persistence.py`
- `scripts/full_flow.py`
- `RECOVERY.md`

回滚 = 删除或还原上述代码文件；`data/` 目录是业务数据，不属于代码
回滚范围，删除业务数据不是代码回滚。恢复时保留 `data/` 与 `RECOVERY.md`。
'''


def _files(mapping):
    return [{"path": path, "content": content} for path, content in mapping.items()]


TASK_FILES = {
    "d01-models": _files({"lendreg/__init__.py": INIT, "lendreg/models.py": MODELS}),
    "d02-storage": _files({"lendreg/storage.py": STORAGE}),
    "d03-add": _files({"lendreg/app.py": APP, "lendreg/__main__.py": MAIN}),
    "d04-lend": _files({"lendreg/app.py": APP}),
    "d05-borrow-guard": _files({"tests/test_borrow_guard.py": TEST_GUARD}),
    "d06-return": _files({"lendreg/app.py": APP}),
    "d07-list": _files({"lendreg/app.py": APP}),
    "d08-chinese-entry": _files({"lendreg/messages.py": MESSAGES, "lendreg/app.py": APP}),
    "d09-restart": _files({"tests/test_restart_persistence.py": TEST_RESTART}),
    "d10-full-flow": _files({"scripts/full_flow.py": FULL_FLOW, "RECOVERY.md": RECOVERY}),
}


class MockResponder:
    """测试替身：按任务编号返回剧本文件；可选任务注入坏实现。"""

    def __init__(self, broken=None):
        self._broken = dict(broken or {})
        self.calls = []

    def __call__(self, messages, *, request_kind, nonce):
        if request_kind != "implement":
            return json.dumps({"nonce": nonce, "summary": "s", "files": []})
        text = messages[-1]["content"]
        match = re.search(r"（(d\d{2}-[a-z-]+)）", text)
        task_id = match.group(1) if match else "unknown"
        self.calls.append(task_id)
        broken_round = self._broken.get(task_id)
        if broken_round is not None and self.calls.count(task_id) <= broken_round:
            return json.dumps({
                "nonce": nonce,
                "summary": "坏实现",
                "files": [{"path": "lendreg/app.py", "content": "print('未完成')\n"}],
            })
        return json.dumps({"nonce": nonce, "summary": "剧本实现", "files": TASK_FILES[task_id]}, ensure_ascii=False)


def make_grant(root: Path):
    return grants.issue_batch_grant(
        root,
        goal="合成单机借还登记批次（测试）",
        allowed_paths=["lendreg", "tests", "scripts", "data", "RECOVERY.md"],
        action_kinds=["local_write", "local_run", "ai_request"],
        issued_by="用户在自主模式确认本合成批次（测试替身）",
        budget={"max_ai_requests": 40, "max_repair_rounds": 12},
    )


class AutorunBatchTests(unittest.TestCase):
    def test_scenario_is_valid(self):
        validate_scenario(LENDREG_SCENARIO)

    def test_mock_batch_all_ten_succeed_without_extra_questions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            responder = MockResponder()
            outputs: list[str] = []
            summary = run_batch(
                root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=responder), run_id="run-mock-1",
                progress=outputs.append,
            )
            self.assertEqual(summary["succeeded"], 10, json.dumps(summary, ensure_ascii=False))
            self.assertEqual(summary["frozen"], 0)
            self.assertTrue(any("当前判断" in line for line in outputs))
            stats = uxtext.interruption_stats(root)
            self.assertEqual(stats["by_category"]["repeat_question"], 0)
            self.assertEqual(stats["by_category"]["new_authorization"], 0)
            ledger = json.loads((root / ".opencoding" / "autoruns" / "run-mock-1.json").read_text(encoding="utf-8"))
            for task in LENDREG_SCENARIO["tasks"]:
                record = ledger["tasks"][task["task_id"]]
                self.assertEqual(record["state"], "succeeded", task["task_id"])
                self.assertTrue(record["attempts"][-1]["verification_ok"])
                self.assertTrue(record["attempts"][-1]["ai_credential_id"].startswith("cred-"))

    def test_resume_does_not_repeat_verified_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            first = MockResponder()
            run_batch(root, LENDREG_SCENARIO, grant_id=grant["grant_id"], adapter=MockAdapter(responder=first), run_id="run-resume-1")
            second = MockResponder()
            summary = run_batch(root, LENDREG_SCENARIO, grant_id=grant["grant_id"], adapter=MockAdapter(responder=second), run_id="run-resume-1")
            self.assertEqual(summary["succeeded"], 10)
            self.assertEqual(second.calls, [], "已验收任务不得重复请求 AI")

    def test_repair_loop_recovers_and_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            responder = MockResponder(broken={"d03-add": 1})
            summary = run_batch(
                root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=responder), run_id="run-repair-1",
            )
            self.assertEqual(summary["succeeded"], 10)
            ledger = json.loads((root / ".opencoding" / "autoruns" / "run-repair-1.json").read_text(encoding="utf-8"))
            attempts = ledger["tasks"]["d03-add"]["attempts"]
            self.assertFalse(attempts[0]["verification_ok"], "初次失败必须保留在账本")
            self.assertTrue(attempts[1]["verification_ok"])
            self.assertEqual(attempts[1]["kind"], "repair")
            grant_after = grants.load_grant(root, grant["grant_id"])
            self.assertEqual(grant_after["budget_used"]["repair_rounds"], 1)

    def test_persistent_failure_freezes_task_and_dependents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            responder = MockResponder(broken={"d04-lend": 99})
            summary = run_batch(
                root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=responder), run_id="run-freeze-1",
            )
            self.assertEqual(summary["succeeded"], 3)
            self.assertEqual(summary["states"]["d04-lend"], "frozen")
            self.assertEqual(summary["states"]["d05-borrow-guard"], "frozen")
            self.assertEqual(summary["states"]["d10-full-flow"], "frozen")
            ledger = json.loads((root / ".opencoding" / "autoruns" / "run-freeze-1.json").read_text(encoding="utf-8"))
            record = ledger["tasks"]["d04-lend"]
            self.assertEqual(record["state"], "frozen")
            self.assertFalse(record["attempts"][0]["verification_ok"], "原失败证据必须保留")

    def test_candidate_outside_task_outputs_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)

            def rogue(messages, *, request_kind, nonce):
                return json.dumps({
                    "nonce": nonce,
                    "summary": "越界",
                    "files": [{"path": "lendreg/models.py", "content": "print('rogue')\n"}, {"path": "rogue.txt", "content": "x"}],
                })

            summary = run_batch(
                root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=rogue), run_id="run-rogue-1",
            )
            self.assertEqual(summary["states"]["d01-models"], "frozen")
            ledger = json.loads((root / ".opencoding" / "autoruns" / "run-rogue-1.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["tasks"]["d01-models"]["error_code"], "candidate_path_not_allowed")

    def test_expired_grant_blocks_batch_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            grants.revoke_batch_grant(root, grant["grant_id"], reason="测试撤销。")
            with self.assertRaises(AutorunError) as caught:
                run_batch(root, LENDREG_SCENARIO, grant_id=grant["grant_id"], adapter=MockAdapter(), run_id="run-x-1")
            self.assertEqual(caught.exception.code, "grant_revoked")


class AutorunCancelAndRecoveryTests(unittest.TestCase):
    def test_cancelled_run_never_auto_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            responder = MockResponder()
            autorun_module.cancel_run(root, "run-cancel-1", reason="用户改变计划。")
            summary = run_batch(
                root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=responder), run_id="run-cancel-1",
            )
            self.assertEqual(responder.calls, [], "已取消运行不得派发任何 AI 请求")
            self.assertEqual(summary["succeeded"], 0)
            self.assertEqual(summary["cancelled"], 10)
            self.assertTrue(all(state == "cancelled" for state in summary["states"].values()))

    def test_user_modification_of_verified_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(root, LENDREG_SCENARIO, grant_id=grant["grant_id"], adapter=MockAdapter(responder=MockResponder()), run_id="run-usermod-1")
            models_file = root / "lendreg" / "models.py"
            original = models_file.read_text(encoding="utf-8")
            models_file.write_text(original + "\n# 用户手工补充的说明，不应被覆盖\n", encoding="utf-8")
            responder = MockResponder()
            summary = run_batch(
                root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=responder), run_id="run-usermod-1",
            )
            self.assertEqual(responder.calls, [], "已验收任务不得因用户改动而自动重写")
            self.assertEqual(summary["states"]["d01-models"], "frozen")
            self.assertIn("用户手工补充的说明", models_file.read_text(encoding="utf-8"))
            ledger = json.loads((root / ".opencoding" / "autoruns" / "run-usermod-1.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["tasks"]["d01-models"]["reason"], "user_modification_detected")

    def test_mid_batch_adapter_failure_freezes_task_and_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            base = MockResponder()
            calls = {"n": 0}

            def flaky(messages, *, request_kind, nonce):
                calls["n"] += 1
                if calls["n"] == 3:
                    return json.dumps({"error": "模拟网络故障"})
                return base(messages, request_kind=request_kind, nonce=nonce)

            summary = run_batch(
                root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=flaky), run_id="run-flaky-1",
            )
            self.assertEqual(summary["succeeded"], 2)
            self.assertEqual(summary["states"]["d03-add"], "frozen")
            ledger = json.loads((root / ".opencoding" / "autoruns" / "run-flaky-1.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["tasks"]["d03-add"]["error_code"], "model_declined")

    def test_ledger_loss_before_receipt_reruns_task_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            run_batch(root, LENDREG_SCENARIO, grant_id=grant["grant_id"], adapter=MockAdapter(responder=MockResponder()), run_id="run-ledgerloss-1")
            ledger_path = root / ".opencoding" / "autoruns" / "run-ledgerloss-1.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            del ledger["tasks"]["d05-borrow-guard"]
            ledger["summary"]["states"]["d05-borrow-guard"] = "pending"
            ledger_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
            responder = MockResponder()
            summary = run_batch(
                root, LENDREG_SCENARIO, grant_id=grant["grant_id"],
                adapter=MockAdapter(responder=responder), run_id="run-ledgerloss-1",
            )
            self.assertEqual(summary["succeeded"], 10)
            self.assertEqual(responder.calls.count("d05-borrow-guard"), 1, "缺收据的任务重新核实一次")
            self.assertNotIn("d01-models", responder.calls, "有收据的任务不得重复执行")


class AutorunCliTests(unittest.TestCase):
    def test_cli_autorun_mock_mode_shows_honest_freeze_demo(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        import opencoding.cli as cli_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli_module.main([
                    "--root", str(root), "--autorun", "lendreg",
                    "--mock-ai", "--run-id", "run-cli-1",
                ])
            self.assertEqual(code, 1, "空候选模拟必须诚实冻结，不能假成功")
            text = stdout.getvalue()
            self.assertIn("模拟", text)
            self.assertIn("当前判断", text)
            self.assertIn("批次结果：成功 0/10", text)
            self.assertIn("冻结 10", text)
            self.assertIn("中断统计", text)
            self.assertIn("不必要重复询问 0", text)
            ledger = json.loads((root / ".opencoding" / "autoruns" / "run-cli-1.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["tasks"]["d01-models"]["error_code"], "candidate_empty")
            stats = uxtext.interruption_stats(root)
            self.assertEqual(stats["by_category"]["new_authorization"], 0)

    def test_cli_autorun_rejects_unknown_scenario_and_orphan_flags(self):
        import io
        from contextlib import redirect_stderr

        import opencoding.cli as cli_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cli_module.main(["--root", str(root), "--mock-ai"])
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = cli_module.main(["--root", str(root), "--autorun", "nope", "--mock-ai"])
            self.assertEqual(code, 2)
            self.assertIn("不支持的自主场景", stderr.getvalue())

    def test_cli_autorun_real_mode_reports_missing_config(self):
        import io
        from contextlib import redirect_stderr
        from unittest import mock

        import opencoding.cli as cli_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            stderr = io.StringIO()
            with mock.patch.dict("os.environ", {}, clear=True):
                with redirect_stderr(stderr):
                    code = cli_module.main(["--root", str(root), "--autorun", "lendreg"])
            self.assertEqual(code, 2)
            self.assertIn("OPENCODING_AI_API_KEY", stderr.getvalue())
            self.assertIn("当前暂停原因", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
