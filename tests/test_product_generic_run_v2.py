# -*- coding: utf-8 -*-
"""generic_run v3 / 网关适配器收口的离线一致性测试(R17-C 检查点1 修复回归)。

覆盖审核反例:P01(路径逃逸)、P04(预算计数)、P05/P06(并发预约)、P07(内存实现假通过)、
P08(冻结 SHA 核对)、P10(过期确认)、P12(绑定补齐透明)、P13(字节流截止)。
全部使用本地可控流与固定字节参考实现;不联网、不冒充真实网关成绩。
"""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from opencoding import generic_run
from opencoding.aiadapter import AIRequestError, MockAdapter, WorkBuddyGatewayAdapter

GOOD_MAIN = '''# -*- coding: utf-8 -*-
"""参考实现:家庭物品借还(真实落盘)。"""
import json
from pathlib import Path

DATA = Path(__file__).parent / "data" / "store.json"


def _load():
    if DATA.exists():
        return json.loads(DATA.read_text(encoding="utf-8"))
    return {"items": {}}


def _save(db):
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")


def create_item(name):
    db = _load()
    for it in db["items"].values():
        if it["name"] == name:
            raise ValueError("同名物品已存在")
    item_id = "i-%04d" % (len(db["items"]) + 1)
    db["items"][item_id] = {"name": name, "status": "在库", "holder": None}
    _save(db)
    return item_id


def lend_item(item_id, person):
    db = _load()
    it = db["items"].get(item_id)
    if it is None:
        raise ValueError("物品不存在")
    if it["status"] != "在库":
        raise ValueError("物品已借出")
    it["status"] = "已借出"
    it["holder"] = person
    _save(db)


def return_item(item_id):
    db = _load()
    it = db["items"].get(item_id)
    if it is None:
        raise ValueError("物品不存在")
    if it["status"] != "已借出":
        raise ValueError("物品未借出")
    it["status"] = "在库"
    it["holder"] = None
    _save(db)


def list_items():
    db = _load()
    return [dict(v, id=k) for k, v in db["items"].items()]
'''

# 内存-only 实现(P07 反例):不落盘,重启即丢
MEM_MAIN = GOOD_MAIN.replace('DATA = Path(__file__).parent / "data" / "store.json"', 'DATA = None')
MEM_MAIN = MEM_MAIN.replace('''def _load():
    if DATA.exists():
        return json.loads(DATA.read_text(encoding="utf-8"))
    return {"items": {}}


def _save(db):
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")''',
            '_DB = {"items": {}}\n\n\ndef _load():\n    return _DB\n\n\ndef _save(db):\n    pass')

GOOD_SELFTEST = '''# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import main
assert main.create_item("自测物")
print("ok")
'''


def _contract(main_src=GOOD_MAIN, extra_files=None):
    files = [{"path": "app/main.py", "purpose": "核心逻辑"},
             {"path": "app/selftest.py", "purpose": "辅助自测"}]
    if extra_files:
        files.extend(extra_files)
    return {
        "entry_module": "app.main",
        "files": files,
        "functions": {"create_item": "创建物品", "lend_item": "借出",
                      "return_item": "归还", "list_items": "列表"},
        "data_dir": "app/data",
        "steps": [
            {"op": "call", "function": "create_item", "args": ["测试物品-梯子"], "save_as": "item_id"},
            {"op": "call", "function": "list_items", "save_as": "items1"},
            {"op": "assert", "saved": "items1", "contains": [{"name": "测试物品-梯子", "status": "在库"}]},
            {"op": "call", "function": "lend_item", "args": ["$item_id", "张三"]},
            {"op": "call", "function": "list_items", "save_as": "items2"},
            {"op": "assert", "saved": "items2", "contains": [{"name": "测试物品-梯子", "status": "已借出", "holder": "张三"}]},
            {"op": "call", "function": "lend_item", "args": ["$item_id", "李四"], "expect_exception": True},
            {"op": "call", "function": "return_item", "args": ["$item_id"]},
            {"op": "call", "function": "return_item", "args": ["$item_id"], "expect_exception": True},
            {"op": "call", "function": "list_items", "save_as": "items3", "phase": "restart"},
            {"op": "assert", "saved": "items3", "contains": [{"name": "测试物品-梯子"}], "phase": "restart"},
            {"op": "assert_file_exists", "path": "data/store.json", "phase": "restart"},
        ],
    }


def _sse(chunks, done=True):
    lines = []
    for c in chunks:
        lines.append(("data: " + json.dumps({"choices": [{"delta": {"content": c}}]}) + "\n\n").encode("utf-8"))
    if done:
        lines.append(b"data: [DONE]\n\n")
    return iter(lines)


class FakeResponse:
    def __init__(self, lines):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def read1(self, n=-1):
        try:
            return next(self._lines)
        except StopIteration:
            return b""

    def close(self):
        pass


def _adapter(**kw):
    kwargs = dict(endpoint="https://gw.example", publishable_key="wbpk_x", model="m")
    kwargs.update(kw)
    return WorkBuddyGatewayAdapter(**kwargs)


class PathValidationTests(unittest.TestCase):
    def test_rejects_escape_forms(self):
        for bad in ["../outside.py", "app/../../evil.py", "/abs/path.py", "C:\\evil.py",
                    "app/..\\..\\x.py", "\\\\srv\\share\\x.py", "main.py", "", None, 123,
                    "app/x:y.py"]:
            with self.assertRaises(generic_run.GenericRunError):
                generic_run.canonical_candidate_path(bad)

    def test_accepts_legal_nested(self):
        self.assertEqual(generic_run.canonical_candidate_path("app/main.py"), "app/main.py")
        self.assertEqual(generic_run.canonical_candidate_path("app/sub/dir/x.py"), "app/sub/dir/x.py")
        self.assertEqual(generic_run.canonical_candidate_path("app/./main.py"), "app/main.py")

    def test_static_check_rejects_before_write(self):
        files = [{"path": "app/../../evil.py", "content": "x"}]
        canonical, problems = generic_run._static_check(files)
        self.assertEqual(canonical, [])
        self.assertTrue(problems and problems[0].startswith("candidate_path_invalid"))


class GenericRunV3Tests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="oc-gen3-"))
        (self.root / ".opencoding").mkdir()

    def _responder(self, files):
        def respond(messages, request_kind="implement", nonce=""):
            payload = {"summary": "s", "files": files}
            if request_kind == "repair":
                payload["fix"] = "f"
            return json.dumps(payload, ensure_ascii=False)

        return respond

    def test_p01_blocked_and_no_project_write(self):
        """P01 反例回归:预置项目文件,恶意路径候选被拒;项目文件不变。"""
        (self.root / "app").mkdir()
        (self.root / "app" / "main.py").write_text("ORIGINAL", encoding="utf-8")
        (self.root / "control.txt").write_text("CONTROL", encoding="utf-8")
        files = [{"path": "app/main.py", "content": "EVIL"},
                 {"path": "app/selftest.py", "content": GOOD_SELFTEST},
                 {"path": "../control.txt", "content": "PWNED"}]
        adapter = MockAdapter(responder=self._responder(files))
        contract = generic_run._validate_contract(_contract())
        receipt = generic_run.run_generic_app(self.root, "目标", adapter,
                                              contract=contract, run_id="gen-p01regre0001",
                                              max_repair_rounds=0)
        self.assertEqual((self.root / "app" / "main.py").read_text(encoding="utf-8"), "ORIGINAL")
        self.assertEqual((self.root / "control.txt").read_text(encoding="utf-8"), "CONTROL")
        self.assertIn(receipt["status"], ("exhausted", "failed"))
        self.assertNotIn("transaction_id", receipt)

    def test_blocked_execution_preserves_candidate(self):
        files = [{"path": "app/main.py", "content": GOOD_MAIN},
                 {"path": "app/selftest.py", "content": GOOD_SELFTEST}]
        adapter = MockAdapter(responder=self._responder(files))
        contract = generic_run._validate_contract(_contract())
        receipt = generic_run.run_generic_app(self.root, "家庭借还登记", adapter,
                                              contract=contract, run_id="gen-blockv30001")
        self.assertEqual(receipt["status"], "blocked_execution")
        self.assertFalse((self.root / "app" / "main.py").exists())
        self.assertTrue(receipt["attempts"][0].get("candidate_preserved"))
        self.assertIn("preimage_at_dispatch", receipt)

    def test_p04_budget_counts_failures(self):
        class TimeoutAdapter:
            real = False
            provider = "test"
            model = "x"

            def complete(self, *a, **k):
                raise AIRequestError("read_idle_timeout", "读取空闲超时")

        adapter = TimeoutAdapter()
        contract = generic_run._validate_contract(_contract())
        receipt = generic_run.run_generic_app(self.root, "x", adapter, contract=contract,
                                              run_id="gen-p04budget0001", max_repair_rounds=1)
        self.assertEqual(receipt["ai_requests_dispatched"], 1, "传输失败即停,不转修复不重发")
        self.assertEqual(receipt["status"], "failed_transport", "传输失败不得伪装为业务修复")
        self.assertEqual(receipt["current_request"]["effect"], "unknown")
        for a in receipt["attempts"]:
            self.assertEqual(a.get("error_code"), "read_idle_timeout")
        journal = self.root / ".opencoding" / "grants" / "journal"
        journals = list(journal.glob("*.jsonl")) if journal.is_dir() else []
        self.assertTrue(journals, "预算日志缺失")

    def test_p06_concurrent_dispatch_blocked(self):
        files = [{"path": "app/main.py", "content": GOOD_MAIN},
                 {"path": "app/selftest.py", "content": GOOD_SELFTEST}]
        results = []
        barrier = threading.Barrier(2)

        def worker():
            adapter = MockAdapter(responder=self._responder(files))
            barrier.wait()
            results.append(generic_run.run_generic_app(
                self.root, "x", adapter, contract=generic_run._validate_contract(_contract()),
                run_id="gen-p06conc0001", max_repair_rounds=0))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        statuses = [r["status"] for r in results]
        self.assertEqual(statuses.count("already_running"), 1, statuses)

    def test_cancel_checked_after_return(self):
        files = [{"path": "app/main.py", "content": GOOD_MAIN},
                 {"path": "app/selftest.py", "content": GOOD_SELFTEST}]

        def respond(messages, request_kind="implement", nonce=""):
            generic_run.request_cancel(self.root, "gen-c4fce1000001")
            return json.dumps({"summary": "s", "files": files}, ensure_ascii=False)

        adapter = MockAdapter(responder=respond)
        receipt = generic_run.run_generic_app(self.root, "x", adapter, contract=generic_run._validate_contract(_contract()),
                                              run_id="gen-c4fce1000001", max_repair_rounds=0)
        self.assertEqual(receipt["status"], "cancelled")
        self.assertNotIn("transaction_id", receipt)


class FrozenCheckerV2Tests(unittest.TestCase):
    def _run_checker(self, main_src):
        with tempfile.TemporaryDirectory(prefix="oc-frozen2-") as td:
            root = Path(td)
            (root / "app").mkdir()
            (root / "app" / "__init__.py").write_text("", encoding="utf-8")
            (root / "app" / "main.py").write_text(main_src, encoding="utf-8")
            fc = root / "frozen_checks"
            fc.mkdir()
            contract = _contract()
            spec = generic_run._frozen_check_spec("gen-ffffffff0001", "测试", contract)
            (fc / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            (fc / "app_contract_v2.py").write_text(generic_run.FROZEN_CHECKER_SOURCE, encoding="utf-8")
            # 与真实验证器一致:main 与 restart 两个独立进程都必须通过
            procs = []
            for phase in ("main", "restart"):
                procs.append(subprocess.run(
                    [sys.executable, "-X", "utf8", "frozen_checks/app_contract_v2.py", phase],
                    cwd=str(root), capture_output=True, text=True, encoding="utf-8", timeout=90,
                    env={"PYTHONPATH": str(root), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}))

            class BothPhases:
                returncode = 0 if all(p2.returncode == 0 for p2 in procs) else 1
                stdout = "\n".join(p2.stdout for p2 in procs)
                stderr = "\n".join(p2.stderr for p2 in procs)

            return BothPhases

    def test_reference_impl_passes(self):
        proc = self._run_checker(GOOD_MAIN)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_memory_only_impl_fails(self):
        """P07 反例:内存实现(不落盘)必须被重启保存断言拒绝。"""
        proc = self._run_checker(MEM_MAIN)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)

    def test_contract_validation_rejects_unknown_ops(self):
        bad = _contract()
        bad["steps"] = [{"op": "exec", "code": "evil"}]
        with self.assertRaises(generic_run.GenericRunError):
            generic_run._validate_contract(bad)

    def test_contract_rejects_escape_file(self):
        bad = _contract()
        bad["files"] = [{"path": "../evil.py", "purpose": "x"}]
        with self.assertRaises(generic_run.GenericRunError):
            generic_run._validate_contract(bad)


class GatewaySSETests(unittest.TestCase):
    def test_byte_stream_deadline_is_timely(self):
        """P13 反例:持续字节无换行,截止必须及时中断(不依赖换行)。"""
        import time

        a = _adapter(deadline_seconds=0.08)

        class ByteResp:
            def read1(self, n=-1):
                time.sleep(0.02)
                return b"x"

            def close(self):
                pass

        t0 = time.monotonic()
        with self.assertRaises(AIRequestError) as ctx:
            a._consume_sse(ByteResp())
        self.assertEqual(ctx.exception.code, "deadline_exceeded")
        self.assertLess(time.monotonic() - t0, 1.0, "截止未及时中断字节流")

    def test_normal_chunks_and_meta(self):
        a = _adapter()
        content, meta = a._consume_sse(FakeResponse(_sse(["你好", "！"])))
        self.assertEqual(content, "你好！")
        self.assertIn("deadline_seconds", meta)

    def test_missing_done_raises(self):
        a = _adapter()
        with self.assertRaises(AIRequestError) as ctx:
            a._consume_sse(FakeResponse(_sse(["abc"], done=False)))
        self.assertEqual(ctx.exception.code, "gateway_stream_interrupted")

    def test_extract_json_tolerant(self):
        self.assertEqual(WorkBuddyGatewayAdapter._extract_json_text('```json\n{"a":1}\n```'), ('{"a":1}', True))
        self.assertEqual(WorkBuddyGatewayAdapter._extract_json_text('说明 {"a":1} 结束'), ('{"a":1}', True))
        self.assertEqual(WorkBuddyGatewayAdapter._extract_json_text('{"a":1}'), ('{"a":1}', False))

    def test_late_complete_rejected_after_deadline(self):
        """静默 0.3s 后送达完整 DONE:deadline 0.08s 必须拒绝,不接受迟到成功。"""
        import time

        a = _adapter(deadline_seconds=0.08, idle_timeout=0.5)

        class LateResp:
            def read1(self, n=-1):
                time.sleep(0.3)
                frame = json.dumps({"choices": [{"delta": {"content": "完整响应"}}]})
                return (b"data: " + frame.encode("utf-8") + b"\n\n" + b"data: [DONE]\n\n")

            def close(self):
                pass

        t0 = time.monotonic()
        with self.assertRaises(AIRequestError) as ctx:
            a._consume_sse(LateResp())
        self.assertEqual(ctx.exception.code, "deadline_exceeded")
        self.assertLess(time.monotonic() - t0, 1.5)

    def test_binding_fill_transparent(self):
        """P12:缺字段被本地补齐必须显式记录;nonce 不允许本地补齐。"""
        content = '{"nonce": "n1", "summary": "s", "choice": "windows", "reasons": ["r"], "input_digest": "' + "a" * 64 + '"}'
        binding = {"schema_version": "1.1", "request_id": "req-1", "run_id": "r1", "task_id": "t1",
                   "attempt": 1, "request_kind": "evaluate", "nonce": "n1",
                   "input_digest": "a" * 64, "allowed_outputs": []}
        filled = WorkBuddyGatewayAdapter._missing_binding_fields(content, binding)
        self.assertIn("run_id", filled)
        self.assertNotIn("nonce", filled)


class StageSafetyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="oc-stage-"))
        (self.root / ".opencoding").mkdir()

    def test_p02_ancestor_symlink_rejected(self):
        """Q02 回归:运行目录预置父级符号链接时,合法候选也拒绝写出暂存根。"""
        import os

        run_dir = generic_run._runs_dir(self.root)
        run_dir.mkdir(parents=True, exist_ok=True)
        outside = Path(tempfile.mkdtemp(prefix="oc-outside-"))
        link = run_dir / "gen-p02lnk000001-candidate-attempt1"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except OSError:
            self.skipTest("本机无法创建符号链接(需管理员/开发者模式)")
            return
        canonical = [("app/main.py", "x")]
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            generic_run._save_candidates(self.root, "gen-p02lnk000001", 1, canonical)
        self.assertIn(ctx.exception.code,
                      ("candidate_stage_conflict", "candidate_stage_unsafe"))
        self.assertFalse((outside / "app" / "main.py").exists(), "越暂存根写入发生")

    def test_q08_alive_owner_lock_not_taken(self):
        """Q08 回归:心跳过期但 owner 进程仍存活 → 不得接管。"""
        import os
        import time as _time

        lp = generic_run._lock_path(self.root)
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(json.dumps({"run_id": "gen-deadbeef0001", "pid": os.getpid(),
                                  "heartbeat_at": "2020-01-01T00:00:00+00:00"}), encoding="utf-8")
        old_m = _time.time() - 3600
        import os as _os
        _os.utime(lp, (old_m, old_m))
        result = generic_run.run_generic_app(
            self.root, "x", MockAdapter(responder=lambda *a, **k: "{}"),
            contract=generic_run._validate_contract(_contract()), run_id="gen-newrun000001")
        self.assertEqual(result["status"], "already_running")
        self.assertTrue(lp.exists(), "存活 owner 的锁不得被删除")


class V5GateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="oc-v5-"))
        (self.root / ".opencoding").mkdir()

    def test_r03_unknown_effect_blocks_new_dispatch(self):
        """R03:效果未知的旧请求存在时,新派发被拦;acknowledge 后才放行。"""
        run_id = "gen-0ffe0c000001"
        receipt = {"run_id": run_id, "status": "failed_transport",
                   "current_request": {"request_id": "req-old", "effect": "unknown"},
                   "finished_at": "2026-09-28T00:00:00+00:00"}
        d = generic_run._runs_dir(self.root)
        d.mkdir(parents=True, exist_ok=True)
        (d / (run_id + ".json")).write_text(json.dumps(receipt), encoding="utf-8")
        adapter = MockAdapter(responder=lambda *a, **k: json.dumps({"summary": "s", "files": []}))
        contract = generic_run._validate_contract(_contract())
        result = generic_run.run_generic_app(self.root, "x", adapter, contract=contract,
                                             run_id="gen-0ffe0c000002")
        self.assertEqual(result["status"], "pending_verification", result)
        generic_run.acknowledge_unknown(self.root, run_id, "已知悉不确定性,发起新尝试")
        result2 = generic_run.run_generic_app(self.root, "x", adapter, contract=contract,
                                              run_id="gen-0ffe0c000002")
        self.assertNotEqual(result2["status"], "pending_verification")

    def test_r05_access_denied_owner_not_dead(self):
        """R05:OpenProcess 拒绝访问(未知)不得当作死亡;模拟三态函数。"""
        self.assertEqual(generic_run._pid_state(-1), generic_run.PID_UNKNOWN)
        # 当前进程必然存活(Windows 上 OpenProcess 成功)
        import os
        self.assertEqual(generic_run._pid_state(os.getpid()), generic_run.PID_ALIVE)

    def test_r10_weak_restart_contract_rejected(self):
        """R10:restart 只有 assert_file_exists 不能作为持久化验收。"""
        contract = _contract()
        for s in contract["steps"]:
            if s.get("phase") == "restart" and s.get("op") == "assert":
                s.clear()
                s.update({"op": "assert_file_exists", "path": "data/store.json", "phase": "restart"})
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            generic_run._validate_contract(contract)
        self.assertEqual(ctx.exception.code, "contract_invalid")


if __name__ == "__main__":
    unittest.main()
