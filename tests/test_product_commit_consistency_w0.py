# -*- coding: utf-8 -*-
"""W0(2026-09-30 集成批次 01):一致提交收口——正式撤销与确认最终提交的可协调次序。

对应审核剩余项:
- B1-final:最后一次回执写入之前发生的正式撤销,不得产生可用的新接续关系;
- B2-live-owner:活着的慢持锁者(真实等待超过原 60 秒阈值)不得仅因锁年龄被
  当作崩溃残留接管,不得出现"两次确认都成功却只剩一个事件"。

同时给出残留恢复、代际释放、撤销永不被提交阻塞三项明确保守语义的正反例。

替身边界:模型/能力/受限执行后端是控制流替身(不发网络、不运行真实候选);
授权、正式撤销、回执落盘、确认临界区锁、合成文件事务均为真实产品代码路径。
本文件不制造真实磁盘灾难、不杀进程、不改系统时钟(授权到期仅用合成移位时钟)。
"""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from opencoding import aiconfig, advisor, generic_run, grants, service
from opencoding.facts import add_fact

from tests.test_product_full_chain_v5 import FakeAdapter, eval_response
from tests.test_product_generic_run_v2 import GOOD_MAIN, GOOD_SELFTEST, _contract
from tests.test_product_service import _complete


def _good_payload():
    return {"real": True, "provider": "fake", "model": "fake-model",
            "request_id": "req-w0-" + os.urandom(4).hex(),
            "structured": {"summary": "实现", "files": [
                {"path": "app/main.py", "content": GOOD_MAIN},
                {"path": "app/selftest.py", "content": GOOD_SELFTEST}]}}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace.mkdir(parents=True)
        self.project = self.workspace / "家庭借还登记"
        self.project.mkdir()
        self.view = _complete(self.project)
        self.sid = self.view["session"]["id"]
        self.contract = generic_run._validate_contract(_contract())

    def tearDown(self):
        self._tmp.cleanup()

    def _no_capability(self):
        return {"available": False, "kind": "none", "command": [],
                "reason": "测试替身:显式声明后端不可用"}

    def _adopt_binding(self):
        payload = eval_response(choice="cli")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.workspace / "appdata")}):
            aiconfig.save_config({"provider": "openai_compatible", "base_url": "https://x/v1",
                                  "api_key": "k", "model": "m"})
            record = advisor.run_ai_evaluation(self.project, self.sid, FakeAdapter([payload]),
                                               run_id="t")
        revision = service.session_view(self.project, self.sid)["session"]["revision"]
        advisor.confirm_evaluation(self.project, self.sid, record["evaluation_id"],
                                   expected_revision=revision, accepted=True)
        adopted = advisor.adopt_confirmed_evaluation(self.project, self.sid)["adoption"]
        return {"session_id": self.sid,
                "evaluation_id": record["evaluation_id"],
                "plan_digest": adopted["plan_digest"],
                "contract_sha256": (adopted["ai_binding"] or {}).get("contract_sha256")}

    def _dispatch_blocked(self, run_id, binding):
        class Adapter:
            real = True
            provider = "fake"
            model = "fake-model"
            calls = 0

            def complete(self, messages, **kw):
                type(self).calls += 1
                return _good_payload()

        with mock.patch.object(generic_run, "_capability", return_value=self._no_capability()):
            receipt = generic_run.run_generic_app(
                self.project, "家庭借还登记", Adapter(), contract=self.contract,
                run_id=run_id, session_id=self.sid, adoption_binding=binding,
                max_repair_rounds=0, acknowledged_unknown=True)
        self.assertEqual(receipt["status"], "blocked_execution", receipt.get("failure"))
        return receipt

    def _bench(self):
        from opencoding.workbench import Workbench

        return Workbench(self.workspace), "/api/project/家庭借还登记/session/" + self.sid

    @staticmethod
    def _shifted_clock(expires):
        from datetime import datetime as _dt, timedelta as _td
        base = _dt.fromisoformat(expires.replace("Z", "+00:00"))
        future = base + _td(minutes=10)

        class _ShiftedClock:
            @staticmethod
            def now(tz=None):
                return future if tz is not None else future.replace(tzinfo=None)

            fromisoformat = staticmethod(_dt.fromisoformat)

        return _ShiftedClock

    def _grant_files(self):
        d = self.project / ".opencoding" / "grants"
        return sorted(p.name for p in d.glob("*.json")) if d.is_dir() else []


class CommitOrderTests(_Base):
    """B1-final:最后写入前的正式撤销必须让确认提交失败并回退。"""

    def test_final_write_official_revoke_refuses_and_reverts(self):
        """在真实回执落盘入口之前调用正式撤销 → 拒绝、回退、不可接续。

        撤销以追加型撤销流水为线性化点,落盘函数内部复核该计数;因此撤销
        插入点位于"最后一次原授权检查之后、回执写入之前"也一定被发现。
        """
        from opencoding.workbench import WorkbenchError

        binding = self._adopt_binding()
        run_id = "gen-0000c6a00001"
        self._dispatch_blocked(run_id, binding)
        original = grants.load_grant(self.project, generic_run.load_receipt(
            self.project, run_id)["grant_id"])
        bench, base = self._bench()
        real_write = generic_run._write_receipt
        trace = {}
        armed = {"on": True}

        def before_receipt_write(project, doc, **kwargs):
            if armed["on"] and doc.get("resume_confirmations"):
                armed["on"] = False
                trace["epoch_before_revoke"] = grants.revocation_epoch(project)
                trace["passed_final_check"] = True
                revoked = grants.revoke_batch_grant(
                    project, original["grant_id"],
                    reason="合成 W0:最终回执写入前的正式撤销")
                trace["revoke_returned"] = bool(revoked["revoked"])
                trace["epoch_after_revoke"] = grants.revocation_epoch(project)
            return real_write(project, doc, **kwargs)

        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])), \
             mock.patch.object(generic_run, "_write_receipt", side_effect=before_receipt_write):
            with self.assertRaises(WorkbenchError) as ctx:
                bench.api("POST", base + "/confirm-new-batch", {},
                          {"run_id": run_id, "confirm": True})

        self.assertTrue(trace.get("passed_final_check"), "探针确实到达最后写入点")
        self.assertTrue(trace.get("revoke_returned"), "正式撤销已返回并写盘")
        self.assertEqual(trace["epoch_after_revoke"], trace["epoch_before_revoke"] + 1,
                         "撤销计数单调推进")
        self.assertEqual(ctx.exception.code, "grant_revoked",
                         "最后写入前的正式撤销必须让确认提交失败")
        stored = generic_run.load_receipt(self.project, run_id)
        self.assertNotIn("grant_id_active", stored, "不得留下可用的新活跃授权")
        self.assertNotIn("resume_confirmations", stored, "不得留下确认事件")
        self.assertTrue(grants.load_grant(self.project, original["grant_id"])["revoked"],
                        "原撤销记录保留")
        for name in self._grant_files():
            doc = grants.load_grant(self.project, name[:-len(".json")])
            if doc["grant_id"] == original["grant_id"]:
                continue
            self.assertTrue(doc["revoked"], "本次签发必须回退撤销:" + doc["grant_id"])
        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])):
            denied = generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(denied["status"], "failed", "撤销后不得产生可用接续")

    def test_revoke_is_never_blocked_by_confirm_section(self):
        """撤销是主权动作:确认临界区被持有时,正式撤销仍必须立即生效。"""

        binding = self._adopt_binding()
        run_id = "gen-0000c6a00002"
        self._dispatch_blocked(run_id, binding)
        original = grants.load_grant(self.project, generic_run.load_receipt(
            self.project, run_id)["grant_id"])
        self.assertTrue(generic_run._confirm_reserve(self.project), "预置占用确认临界区")
        try:
            before = grants.revocation_epoch(self.project)
            revoked = grants.revoke_batch_grant(self.project, original["grant_id"],
                                                reason="合成 W0:临界区持有时撤销")
            after = grants.revocation_epoch(self.project)
        finally:
            generic_run._confirm_release(self.project)
        self.assertTrue(revoked["revoked"], "撤销不得被提交临界区阻塞")
        self.assertEqual(after, before + 1)
        self.assertEqual(grants.grant_valid(grants.load_grant(
            self.project, original["grant_id"])), (False, "grant_revoked"))

    def test_revocation_journal_fails_closed_before_grant_file(self):
        """撤销流水已生效而授权文件改写未完成时,加载仍判定为已撤销(失败关闭)。"""

        binding = self._adopt_binding()
        run_id = "gen-0000c6a00003"
        self._dispatch_blocked(run_id, binding)
        original_id = generic_run.load_receipt(self.project, run_id)["grant_id"]
        grants._append_revocation(self.project, original_id, reason="合成 W0:仅流水生效")
        doc = grants.load_grant(self.project, original_id)
        self.assertTrue(doc["revoked"], "流水已生效即按已撤销判定")
        self.assertEqual(grants.grant_valid(doc), (False, "grant_revoked"))


class ConfirmLockOwnershipTests(_Base):
    """B2-live-owner 与锁的代际/残留语义。"""

    def test_live_slow_owner_not_preempted_single_event(self):
        """真实等待超过原 60 秒阈值的**存活**持锁者不得被接管。

        A 持确认锁停在真实回执 I/O 前并保持存活;B 在锁真实年龄超过阈值后
        进入:必须被明确拒绝,不得删除 A 的锁;B 不产生第二份有效授权与事件。
        不改变阈值、不改 mtime、不对锁相关时钟做替身;授权到期仅用合成时钟。
        """
        from opencoding.workbench import WorkbenchError

        binding = self._adopt_binding()
        run_id = "gen-0000c6a00011"
        self._dispatch_blocked(run_id, binding)
        original = grants.load_grant(self.project, generic_run.load_receipt(
            self.project, run_id)["grant_id"])
        bench, base = self._bench()
        real_write = generic_run._write_receipt
        at_write = threading.Event()
        release = threading.Event()
        results = {}
        trace = {}
        threads = []

        def intercept(project, doc, **kwargs):
            if threading.current_thread().name == "w0-confirm-A" \
                    and doc.get("resume_confirmations"):
                trace["A_at_write"] = True
                at_write.set()
                if not release.wait(180):
                    raise TimeoutError("合成调度超时")
            return real_write(project, doc, **kwargs)

        def confirm(note):
            try:
                res = bench.api("POST", base + "/confirm-new-batch", {},
                                {"run_id": run_id, "confirm": True, "note": note})
                results[note] = {"status": res["status"], "grant": res["new_grant_id"]}
            except BaseException as exc:
                results[note] = {"code": getattr(exc, "code", None),
                                 "exception": type(exc).__name__}

        lock_path = generic_run._confirm_lock_path(self.project)
        started = time.time()
        try:
            with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])), \
                 mock.patch.object(generic_run, "_write_receipt", side_effect=intercept):
                ta = threading.Thread(target=confirm, args=("A",), name="w0-confirm-A")
                threads.append(ta)
                ta.start()
                self.assertTrue(at_write.wait(30), "A 必须到达受保护的回执 I/O")
                threshold = generic_run._CONFIRM_LOCK_STALE_SECONDS
                self.assertEqual(threshold, 60.0, "不降低原阈值")
                deadline = time.time() + threshold + 1.5
                while time.time() < deadline:
                    time.sleep(min(0.5, deadline - time.time()))
                info = json.loads(lock_path.read_text(encoding="utf-8"))
                from datetime import datetime as _dt, timezone as _tz
                acquired = _dt.fromisoformat(info["acquired_at"].replace("Z", "+00:00"))
                trace["real_lock_age"] = (_dt.now(_tz.utc) - acquired).total_seconds()
                trace["heartbeat_fresh_seconds"] = time.time() - float(info["heartbeat_at"])
                trace["A_alive"] = ta.is_alive()
                self.assertGreater(trace["real_lock_age"], threshold,
                                   "锁的真实持有年龄必须超过原阈值")
                self.assertLess(trace["heartbeat_fresh_seconds"], 5.0,
                                "存活持有者的心跳必须持续推进")
                self.assertTrue(trace["A_alive"], "A 仍存活(活慢线程,不是崩溃)")
                tb = threading.Thread(target=confirm, args=("B",), name="w0-confirm-B")
                threads.append(tb)
                tb.start()
                tb.join(60)
                trace["B_result"] = results.get("B")
                self.assertFalse(tb.is_alive(), "B 必须有界结束")
                self.assertEqual(results["B"].get("code"), "confirmation_busy",
                                 "存活持锁者不得被年龄阈值抢占:" + repr(results["B"]))
                self.assertNotIn("grant_id_active",
                                 generic_run.load_receipt(self.project, run_id),
                                 "B 被拒时不得写入活跃位")
                release.set()
                ta.join(60)
            elapsed = time.time() - started
        finally:
            release.set()
            for t in threads:
                t.join(60)

        self.assertFalse(any(t.is_alive() for t in threads), "所有线程结束")
        self.assertGreaterEqual(elapsed, 61.0, "真实等待,未压缩时间")
        self.assertEqual(results["A"].get("status"), "confirmed", repr(results))
        stored = generic_run.load_receipt(self.project, run_id)
        self.assertEqual(len(stored.get("resume_confirmations") or []), 1,
                         "确认事件恰好一份,不丢事件")
        self.assertEqual(stored["grant_id_active"], results["A"]["grant"])
        self.assertFalse(lock_path.exists(), "锁随持有者释放而清理")
        for name in self._grant_files():
            doc = grants.load_grant(self.project, name[:-len(".json")])
            if doc["grant_id"] == original["grant_id"]:
                continue
            self.assertTrue(doc["grant_id"] == stored["grant_id_active"] or doc["revoked"],
                            "不存在第二份有效无事件的授权")

    def test_dead_owner_lock_is_recovered(self):
        """持有者进程已确认退出 → 残留锁可被接管(有明确的残留恢复路径)。"""

        dead_pid = self._reaped_pid()
        lock_path = generic_run._confirm_lock_path(self.project)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"kind": "confirmation", "pid": dead_pid,
                   "process_token": "dead-process-token",
                   "thread_id": 1, "generation": "dead-generation",
                   "acquired_at": "2026-09-30T00:00:00Z", "heartbeat_at": 0.0}
        lock_path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(generic_run._pid_alive(dead_pid), False, "持有者进程确认已退出")
        acquired = generic_run._confirm_reserve(self.project)
        try:
            self.assertTrue(acquired, "已退出持有者的残留锁必须可恢复")
            info = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertNotEqual(info.get("generation"), "dead-generation", "新代际接管")
        finally:
            generic_run._confirm_release(self.project)

    @staticmethod
    def _reaped_pid():
        import subprocess
        import sys

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_release_never_deletes_successor_lock(self):
        """旧持有者释放不得删除继任者的锁(代际隔离)。"""

        lock_path = generic_run._confirm_lock_path(self.project)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue(generic_run._confirm_reserve(self.project))
        first = json.loads(lock_path.read_text(encoding="utf-8"))["generation"]
        # 模拟继任者:锁被另一代际取代
        lock_path.write_text(json.dumps({
            "kind": "confirmation", "pid": os.getpid(),
            "process_token": generic_run._PROCESS_TOKEN,
            "thread_id": threading.get_native_id() + 1,
            "generation": "successor-generation",
            "acquired_at": generic_run._now(), "heartbeat_at": time.time()}),
            encoding="utf-8")
        self.assertFalse(generic_run._confirm_release(self.project, first),
                         "旧代际不得删除继任者的锁")
        self.assertTrue(lock_path.exists(), "继任者锁仍然在位")

    def test_owner_alive_silent_is_not_preempted(self):
        """持有者存活但心跳停止 → 保守拒绝,不按年龄抢占。"""

        lock_path = generic_run._confirm_lock_path(self.project)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps({
            "kind": "confirmation", "pid": os.getpid(),
            "process_token": generic_run._PROCESS_TOKEN,
            "thread_id": threading.get_native_id() + 7,
            "generation": "silent-generation",
            "acquired_at": generic_run._now(),
            "heartbeat_at": time.time() - 3600}), encoding="utf-8")
        self.assertFalse(generic_run._confirm_reserve(self.project), "存活持有者不被接管")
        self.assertEqual(generic_run._CONFIRM_LAST_REASON, "owner_alive_silent")
        self.assertTrue(lock_path.exists(), "保守处理:不动他人的锁")


if __name__ == "__main__":
    unittest.main()
