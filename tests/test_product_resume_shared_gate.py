# -*- coding: utf-8 -*-
"""C6-01/C6-03:接续与初次生成共用同一验证/提交控制;预算状态目录接入统一根保护。

对照 C6 审核 U02/U03/U04/U08 原边界:
- U03:已撤销/不可核对的 grant 不得由接续自动换发;接续进行中 request_cancel
  必须能登记取消(先持久化 running 阶段);
- U02:接续检查期间事实变化(matches=false)时不得提交旧候选——与普通生成
  共用同一控制闸的四道边界;
- U04:暂存候选必须与原响应落盘本体逐字节一致;被改动的字节不得当作
  "继续原候选"(拒绝或转新版本,不静默沿用旧 request 绑定);
- U08:预置符号链接在 task-budgets → 零账本写出、零派发;损坏账本保守
  停止,不静默重建空账本。

替身边界:适配器与受限后端为控制流替身(不发网络;仅控制链测试,
不等于真实隔离背书);授权/取消/账本均为真实产品代码路径。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from opencoding import aiconfig, advisor, generic_run, grants, service
from opencoding.aiadapter import AIRequestError
from opencoding.facts import add_fact

from tests.test_product_full_chain_v5 import FakeAdapter, eval_response
from tests.test_product_generic_run_v2 import GOOD_MAIN, GOOD_SELFTEST, _contract
from tests.test_product_service import _complete


def _good_payload():
    return {"real": True, "provider": "fake", "model": "fake-model",
            "request_id": "req-impl-" + os.urandom(4).hex(),
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

    def _yes_capability(self):
        return {"available": True, "kind": "test-stub", "command": ["true"],
                "reason": "测试替身:仅用于控制链,不运行真实候选"}

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
        """正常生成一次:后端不可用 → 候选已保存、状态 blocked_execution。"""
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
        self.assertTrue(receipt["attempts"][0].get("candidate_preserved"))
        return receipt

    def _grant_files(self):
        d = self.project / ".opencoding" / "grants"
        return sorted(p.name for p in d.glob("*.json")) if d.is_dir() else []

    @staticmethod
    def _shifted_clock(expires):
        """构造移位时钟(仅合成测试;不修改任何真实授权文件)。"""
        from datetime import datetime as _dt, timedelta as _td
        base = _dt.fromisoformat(expires.replace("Z", "+00:00"))
        future = base + _td(minutes=10)

        class _ShiftedClock:
            @staticmethod
            def now(tz=None):
                return future if tz is not None else future.replace(tzinfo=None)

            fromisoformat = staticmethod(_dt.fromisoformat)

        return _ShiftedClock


class ResumeGrantTests(_Base):
    """U03/FIX-01:接续核对当前活跃授权及父链——撤销/篡改/不可读/到期一律
    不换发;到期需用户正常入口的新批次确认,不得由过期祖先续签顶替。"""

    def test_revoked_grant_rejects_resume_without_renewal(self):
        binding = self._adopt_binding()
        run_id = "gen-0000c6010001"
        receipt = self._dispatch_blocked(run_id, binding)
        original_grant = receipt["grant_id"]
        grants.revoke_batch_grant(self.project, original_grant, reason="审核U03原边界:显式撤销")
        before = self._grant_files()
        resumed = generic_run.resume_saved_candidate(self.project, run_id)
        # 拒绝且留痕:不自动换发新批次
        self.assertEqual(resumed["status"], "failed")
        self.assertIn("grant_revoked", resumed.get("failure") or "")
        attempt = resumed["attempts"][-1]
        self.assertEqual(attempt["kind"], "resume")
        self.assertEqual(attempt["grant_rejected"]["reason"], "grant_revoked")
        self.assertNotIn("grant_used", attempt)
        self.assertEqual(self._grant_files(), before, "撤销后接续不得自动签发新授权")
        self.assertNotIn("grant_id_active", resumed)
        # 候选准确保留:零执行、零提交
        self.assertIsNone(resumed.get("transaction_id"))
        self.assertFalse((self.project / "app" / "main.py").exists())
        self.assertEqual(resumed["grant_id"], original_grant, "原 receipt 授权记录不得改写")

    def test_unreadable_grant_rejects_resume(self):
        binding = self._adopt_binding()
        run_id = "gen-0000c6010002"
        receipt = self._dispatch_blocked(run_id, binding)
        target = self.project / ".opencoding" / "grants" / (receipt["grant_id"] + ".json")
        target.write_text("{corrupted", encoding="utf-8")
        resumed = generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(resumed["status"], "failed")
        attempt = resumed["attempts"][-1]
        self.assertTrue(str(attempt["grant_rejected"]["reason"]).startswith("grant_unverifiable"))
        self.assertFalse((self.project / "app" / "main.py").exists())

    def test_expired_batch_requires_fresh_confirmation(self):
        """FIX-01:批次到期 → 接续**不自动签发新批次**(旧行为废除);
        接续按钮不构成新的批次确认,需用户正常授权入口。"""
        binding = self._adopt_binding()
        run_id = "gen-0000c6010003"
        receipt = self._dispatch_blocked(run_id, binding)
        original = grants.load_grant(self.project, receipt["grant_id"])
        with mock.patch.object(grants, "datetime",
                               self._shifted_clock(original["expires_at"])):
            resumed = generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(resumed["status"], "failed")
        self.assertIn("不自动签发新批次", resumed.get("failure") or "")
        attempt = resumed["attempts"][-1]
        self.assertEqual(attempt["grant_rejected"]["reason"], "grant_expired")
        self.assertEqual(attempt["grant_rejected"]["note"],
                         "batch_expired_needs_fresh_confirmation")
        self.assertNotIn("grant_used", attempt)
        self.assertEqual(self._grant_files(), [receipt["grant_id"] + ".json"],
                         "到期后不得产生任何新授权文件")
        self.assertNotIn("grant_id_active", resumed)
        self.assertIsNone(resumed.get("transaction_id"))
        self.assertFalse((self.project / "app" / "main.py").exists())
        # 候选仍可接续:通过正式新批次确认入口(R01)签发并绑定新批次后走正常接续;
        # 不再手改回执文件(旧测试尾巴已由正式入口取代)
        with mock.patch.object(grants, "datetime",
                               self._shifted_clock(original["expires_at"])):
            confirmed = generic_run.confirm_new_batch_for_resume(
                self.project, run_id, session_id=self.sid,
                issued_by="test-fresh-confirmation", note="批次到期后的正式新批次确认")
        new_grant_id = confirmed["new_grant_id"]
        self.assertNotEqual(new_grant_id, receipt["grant_id"], "新批次是新签发的授权")
        self.assertEqual(confirmed["previous_grant"]["state"], "grant_expired")
        self.assertEqual(confirmed["budget"]["max_ai_requests"], 0, "新批次零 AI 请求")
        self.assertEqual(self._grant_files(),
                         sorted([receipt["grant_id"] + ".json", new_grant_id + ".json"]),
                         "最初授权保留 + 新批次,无多余授权文件")
        stored = generic_run.load_receipt(self.project, run_id)
        self.assertEqual(stored["grant_id"], receipt["grant_id"], "最初授权记录永不改写")
        self.assertEqual(stored["grant_id_active"], new_grant_id)
        event = stored["resume_confirmations"][-1]
        self.assertEqual(event["original_grant_id"], receipt["grant_id"])
        self.assertEqual(event["new_grant_id"], new_grant_id)
        self.assertTrue(event["zero_ai_requests"])
        resumed2 = generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(resumed2["status"], "blocked_execution",
                         "新批次确认后接续恢复(后端不可用 → 如实 blocked,候选保留)")
        self.assertEqual(resumed2["attempts"][-1]["grant_used"], new_grant_id)
        self.assertEqual(resumed2["attempts"][-1]["grant_source"], "reused_active")

    def test_revoked_active_grant_cannot_be_superseded_while_ancestor_valid(self):
        """FIX-01/V02 反例:回执活跃授权 G2 被正式撤销后,即使最初授权 G1
        仍有效,接续也**不得**回退复用 G1 顶替被撤销的 G2。"""
        binding = self._adopt_binding()
        run_id = "gen-0000c6010005"
        receipt = self._dispatch_blocked(run_id, binding)
        g1 = receipt["grant_id"]
        g2 = grants.issue_batch_grant(
            self.project, goal="历史合法续批(测试预置,随后被正式撤销)",
            allowed_paths=["app"], action_kinds=["local_write", "local_run"],
            issued_by="test-history-renewal", budget={"max_ai_requests": 0})
        grants.revoke_batch_grant(self.project, g2["grant_id"], reason="审核V02:正式撤销活跃授权")
        stored = generic_run.load_receipt(self.project, run_id)
        stored["grant_id_active"] = g2["grant_id"]
        generic_run._write_receipt(self.project, stored)
        before = self._grant_files()
        resumed = generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(resumed["status"], "failed")
        self.assertIn("grant_revoked", resumed.get("failure") or "")
        attempt = resumed["attempts"][-1]
        self.assertEqual(attempt["grant_rejected"]["reason"], "grant_revoked")
        self.assertEqual(attempt["grant_chain"]["evaluated"], g2["grant_id"],
                         "判定对象必须是活跃授权,不是最初授权")
        self.assertNotIn("grant_used", attempt)
        self.assertEqual(self._grant_files(), before, "不得从 G1 或任何祖先续签新授权")
        self.assertIsNone(resumed.get("transaction_id"))

    def test_revoked_active_grant_not_superseded_after_ancestor_expired(self):
        """FIX-01/V02 完整时序:G1 到期 + 活跃 G2 被撤销 → 接续拒绝,
        不得由"从过期 G1 续出的 G3"顶替(原 V02 修前会 delivered)。"""
        binding = self._adopt_binding()
        run_id = "gen-0000c6010006"
        receipt = self._dispatch_blocked(run_id, binding)
        g1 = receipt["grant_id"]
        # 预置历史:G1 到期前曾合法续批 G2,回执活跃位=G2;随后 G2 被正式撤销
        g2 = grants.issue_batch_grant(
            self.project, goal="历史合法续批(测试预置,随后被正式撤销)",
            allowed_paths=["app"], action_kinds=["local_write", "local_run"],
            issued_by="test-history-renewal", budget={"max_ai_requests": 0})
        grants.revoke_batch_grant(self.project, g2["grant_id"], reason="审核V02:正式撤销活跃授权")
        stored = generic_run.load_receipt(self.project, run_id)
        stored["grant_id_active"] = g2["grant_id"]
        generic_run._write_receipt(self.project, stored)
        original = grants.load_grant(self.project, g1)
        before = self._grant_files()
        with mock.patch.object(grants, "datetime",
                               self._shifted_clock(original["expires_at"])):
            resumed = generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(resumed["status"], "failed",
                         "V02 修前 delivered 的路径必须被拒绝")
        self.assertIn("grant_revoked", resumed.get("failure") or "")
        attempt = resumed["attempts"][-1]
        self.assertEqual(attempt["grant_rejected"]["reason"], "grant_revoked")
        self.assertEqual(self._grant_files(), before,
                         "G1 已过期也不得续出 G3:授权文件数量不变")
        self.assertFalse((self.project / "app" / "main.py").exists())

    def test_reuse_valid_grant_records_actual_authorization(self):
        binding = self._adopt_binding()
        run_id = "gen-0000c6010004"
        receipt = self._dispatch_blocked(run_id, binding)
        before = self._grant_files()
        resumed = generic_run.resume_saved_candidate(self.project, run_id)
        attempt = resumed["attempts"][-1]
        self.assertEqual(attempt["grant_source"], "reused_original")
        self.assertEqual(attempt["grant_used"], receipt["grant_id"])
        self.assertEqual(resumed["grant_id_active"], receipt["grant_id"])
        self.assertEqual(self._grant_files(), before, "原授权有效时不得签发新批次")


class ResumeReauthorizationTests(_Base):
    """R01:批次到期 → 正式新批次确认 → 原候选接续(零新 AI)闭环与负例。

    正向:候选保存 → 原批次到期 → 普通接续被拒(不自动签发)→ 用户显式
    确认(正式服务入口)→ 同一候选接续 → 零新 AI 请求 → 一次真实事务 →
    再次提交被拒。负例:工作台未确认、跨会话身份不符、当前活跃授权被撤销、
    确认后候选/事实变化仍由原共享闸拒绝。V02(撤销不得顶替)保持。
    """

    def _expire_and_confirm(self, run_id):
        """公共时序:候选已存 → 移位时钟下原批次到期 → 正式确认新批次。"""
        binding = self._adopt_binding()
        receipt = self._dispatch_blocked(run_id, binding)
        original = grants.load_grant(self.project, receipt["grant_id"])
        with mock.patch.object(grants, "datetime",
                               self._shifted_clock(original["expires_at"])):
            confirmed = generic_run.confirm_new_batch_for_resume(
                self.project, run_id, session_id=self.sid,
                issued_by="test-reauth", note="审计R01正向时序")
        return receipt, confirmed

    def test_confirm_then_resume_original_candidate_zero_ai(self):
        """正向全时序:到期拒绝 → 显式确认 → 同一候选接续交付,零新 AI、一次真实事务。"""
        run_id = "gen-0000c6016001"
        receipt, confirmed = self._expire_and_confirm(run_id)
        new_grant_id = confirmed["new_grant_id"]
        self.assertNotEqual(new_grant_id, receipt["grant_id"])
        self.assertEqual(confirmed["previous_grant"]["state"], "grant_expired")
        self.assertEqual(confirmed["budget"]["max_ai_requests"], 0)
        # 授权文件:最初授权保留 + 新批次;确认事件绑定 run/会话/任务/候选/旧授权
        self.assertEqual(self._grant_files(),
                         sorted([receipt["grant_id"] + ".json", new_grant_id + ".json"]))
        stored = generic_run.load_receipt(self.project, run_id)
        self.assertEqual(stored["grant_id"], receipt["grant_id"], "最初授权记录永不改写")
        self.assertEqual(stored["grant_id_active"], new_grant_id)
        event = stored["resume_confirmations"][-1]
        self.assertEqual(event["original_grant_id"], receipt["grant_id"])
        self.assertEqual(event["new_grant_id"], new_grant_id)
        self.assertEqual(event["session_id"], self.sid)
        self.assertEqual(event["task_id"], generic_run.TASK_ID)
        self.assertEqual(event["candidate_attempt"], 1)
        self.assertTrue(event["zero_ai_requests"])
        self.assertEqual(event["candidate_files"], ["app/main.py", "app/selftest.py"])
        # 同一候选接续:检查替身,提交走真实产品链
        captured = {}

        def fake_stage(project, grant, task, files, *args, **kwargs):
            captured["grant_id"] = grant["grant_id"]
            captured["files"] = [item["path"] for item in files]
            return True, [], {}

        with mock.patch.object(generic_run, "_capability",
                               return_value=self._yes_capability()), \
             mock.patch.object(generic_run, "_stage_and_verify", side_effect=fake_stage):
            resumed = generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(resumed["status"], "delivered", resumed.get("failure"))
        self.assertIsNotNone(resumed.get("transaction_id"), "产生一次真实提交事务")
        self.assertEqual(captured["grant_id"], new_grant_id, "接续实际使用新批次")
        self.assertEqual(captured["files"], ["app/main.py", "app/selftest.py"],
                         "接续复用既有候选,不重新请求模型")
        attempt = resumed["attempts"][-1]
        self.assertFalse(attempt.get("ai_request_dispatched"))
        self.assertEqual(attempt["grant_source"], "reused_active")
        # 零新 AI:新批次的 AI 预算消费仍为 0
        self.assertEqual(grants.load_grant(
            self.project, new_grant_id)["budget_used"]["ai_requests"], 0)
        # 已提交后再次接续被拒绝
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(ctx.exception.code, "candidate_already_committed")

    def test_confirm_refuses_while_batch_still_active(self):
        """批次仍有效 → 拒绝冗余新批次确认(零新授权、零状态写入)。"""
        binding = self._adopt_binding()
        run_id = "gen-0000c6016002"
        self._dispatch_blocked(run_id, binding)
        before = self._grant_files()
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            generic_run.confirm_new_batch_for_resume(
                self.project, run_id, session_id=self.sid, issued_by="test")
        self.assertEqual(ctx.exception.code, "batch_still_active")
        self.assertEqual(self._grant_files(), before)
        stored = generic_run.load_receipt(self.project, run_id)
        self.assertNotIn("grant_id_active", stored)
        self.assertNotIn("resume_confirmations", stored)

    def test_confirm_refuses_cross_session_identity(self):
        """身份不符(运行不属于所传会话)→ 拒绝确认,零状态写入。"""
        binding = self._adopt_binding()
        run_id = "gen-0000c6016003"
        self._dispatch_blocked(run_id, binding)
        original = grants.load_grant(self.project, generic_run.load_receipt(
            self.project, run_id)["grant_id"])
        before = self._grant_files()
        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])):
            with self.assertRaises(generic_run.GenericRunError) as ctx:
                generic_run.confirm_new_batch_for_resume(
                    self.project, run_id, session_id="session-其他会话", issued_by="test")
        self.assertEqual(ctx.exception.code, "confirmation_identity_mismatch")
        self.assertEqual(self._grant_files(), before)
        stored = generic_run.load_receipt(self.project, run_id)
        self.assertNotIn("resume_confirmations", stored)

    def test_confirm_refuses_revoked_active_grant(self):
        """当前活跃授权被正式撤销(历史合法续批 G2)→ 新批次确认拒绝;
        V02 边界保持:撤销不得被任何后续动作顶替,普通接续同样拒绝。"""
        binding = self._adopt_binding()
        run_id = "gen-0000c6016004"
        self._dispatch_blocked(run_id, binding)
        g2 = grants.issue_batch_grant(
            self.project, goal="历史合法续批(测试预置,随后被正式撤销)",
            allowed_paths=["app"], action_kinds=["local_write", "local_run"],
            issued_by="test-history-renewal", budget={"max_ai_requests": 0})
        grants.revoke_batch_grant(self.project, g2["grant_id"],
                                  reason="R01负例:活跃授权正式撤销")
        stored = generic_run.load_receipt(self.project, run_id)
        stored["grant_id_active"] = g2["grant_id"]
        generic_run._write_receipt(self.project, stored)
        before = self._grant_files()
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            generic_run.confirm_new_batch_for_resume(
                self.project, run_id, session_id=self.sid, issued_by="test")
        self.assertEqual(ctx.exception.code, "grant_revoked")
        self.assertEqual(self._grant_files(), before, "撤销后不得产生新授权")
        saved = generic_run.load_receipt(self.project, run_id)
        self.assertEqual(saved["grant_id_active"], g2["grant_id"])
        self.assertNotIn("resume_confirmations", saved)
        resumed = generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(resumed["status"], "failed")
        self.assertIn("grant_revoked", resumed.get("failure") or "")

    def test_candidate_change_after_confirmation_still_rejected(self):
        """确认后候选字节被改 → 原共享闸(候选身份)仍拒绝,不因新批次放行。"""
        run_id = "gen-0000c6016005"
        receipt, _confirmed = self._expire_and_confirm(run_id)
        cand_dir = Path(receipt["attempts"][0]["candidate_preserved"])
        (cand_dir / "app" / "main.py").write_text("VALUE = 999\n", encoding="utf-8")
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(ctx.exception.code, "candidate_identity_mismatch")
        self.assertFalse((self.project / "app" / "main.py").exists())

    def test_fact_change_after_confirmation_still_blocks_commit(self):
        """确认后事实变化 → 提交前控制闸仍拒绝(新批次不豁免共享边界)。"""
        run_id = "gen-0000c6016006"
        self._expire_and_confirm(run_id)

        def fake_stage(*args, **kwargs):
            add_fact(self.project, self.sid, content="确认新批次后新增约束:仅离线",
                     source_type="user", source_ref="authorized-test-fixture")
            return True, [], {}

        with mock.patch.object(generic_run, "_capability",
                               return_value=self._yes_capability()), \
             mock.patch.object(generic_run, "_stage_and_verify", side_effect=fake_stage):
            resumed = generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(resumed["status"], "failed")
        self.assertIn("相关有效输入已变化", resumed["failure"])
        self.assertIsNone(resumed.get("transaction_id"))
        self.assertFalse((self.project / "app" / "main.py").exists())

    def test_workbench_confirm_new_batch_entry(self):
        """工作台正式入口:GET 预览只读;POST 未确认拒绝;显式确认后服务签发并绑定。"""
        from opencoding.workbench import Workbench, WorkbenchError

        binding = self._adopt_binding()
        run_id = "gen-0000c6016007"
        self._dispatch_blocked(run_id, binding)
        bench = Workbench(self.workspace)
        base = "/api/project/家庭借还登记/session/" + self.sid
        # GET 预览:批次仍有效 → confirmable=False,零状态写入
        preview = bench.api("GET", base + "/confirm-new-batch", {"run_id": run_id}, {})
        self.assertEqual(preview["preview"]["run_id"], run_id)
        self.assertFalse(preview["preview"]["confirmable"])
        stored = generic_run.load_receipt(self.project, run_id)
        self.assertNotIn("resume_confirmations", stored)
        # POST 未确认 → 显式拒绝
        with self.assertRaises(WorkbenchError) as ctx:
            bench.api("POST", base + "/confirm-new-batch", {}, {"run_id": run_id})
        self.assertEqual(ctx.exception.code, "new_batch_unconfirmed")
        # 到期后显式确认 → 服务签发并绑定回执
        original = grants.load_grant(self.project, generic_run.load_receipt(
            self.project, run_id)["grant_id"])
        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])):
            result = bench.api("POST", base + "/confirm-new-batch", {},
                               {"run_id": run_id, "confirm": True,
                                "issued_by": "工作台用户(测试)", "note": "审计R01"})
        self.assertEqual(result["status"], "confirmed")
        stored = generic_run.load_receipt(self.project, run_id)
        self.assertEqual(stored["grant_id_active"], result["new_grant_id"])
        self.assertEqual(stored["resume_confirmations"][-1]["issued_by"], "工作台用户(测试)")

    # ------------- R01-A/R01-B/R01-C(20260929 网页审核缺口修复) -------------

    def _receipt_bytes(self, run_id):
        return (generic_run._runs_dir(self.project) / (run_id + ".json")).read_bytes()

    def _bench(self):
        from opencoding.workbench import Workbench

        return Workbench(self.workspace), "/api/project/家庭借还登记/session/" + self.sid

    def test_confirm_requires_json_boolean_true(self):
        """R01-A:confirm 仅接受 JSON 布尔 true;缺失/null/false/字符串/数字/
        容器一律 new_batch_unconfirmed,拒绝路径零签发、回执字节不变、预算不动;
        显式布尔 true 仍正常确认(合同类型唯一放行)。"""
        from opencoding.workbench import WorkbenchError

        binding = self._adopt_binding()
        run_id = "gen-0000c6016008"
        self._dispatch_blocked(run_id, binding)
        original = grants.load_grant(self.project, generic_run.load_receipt(
            self.project, run_id)["grant_id"])
        bench, base = self._bench()
        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])):
            for value in (False, "false", "true", 1, 0, None, [], {}, [True], {"a": 1}):
                before_grants = self._grant_files()
                before_receipt = self._receipt_bytes(run_id)
                with self.assertRaises(WorkbenchError) as ctx:
                    bench.api("POST", base + "/confirm-new-batch", {},
                              {"run_id": run_id, "confirm": value})
                self.assertEqual(ctx.exception.code, "new_batch_unconfirmed",
                                 "输入 " + repr(value) + " 必须被拒绝")
                self.assertEqual(self._grant_files(), before_grants,
                                 "拒绝不得签发授权:" + repr(value))
                self.assertEqual(self._receipt_bytes(run_id), before_receipt,
                                 "拒绝不得改写回执:" + repr(value))
            budget = generic_run.task_budget_state(self.project, generic_run.TASK_ID, self.sid)
            self.assertEqual(budget["dispatched"], 1, "异形输入不得改变既有预算台账")
            result = bench.api("POST", base + "/confirm-new-batch", {},
                               {"run_id": run_id, "confirm": True})
        self.assertEqual(result["status"], "confirmed")

    def test_revoke_between_preview_and_issue_refuses(self):
        """R01-B(P04 镜像):预览返回后、签发前原授权被正式撤销 → 拒绝;
        零新授权、回执无确认事件与活跃位;原撤销记录保留;接续仍被拒(不自动签发)。"""
        from opencoding.workbench import WorkbenchError

        binding = self._adopt_binding()
        run_id = "gen-0000c6016009"
        self._dispatch_blocked(run_id, binding)
        original = grants.load_grant(self.project, generic_run.load_receipt(
            self.project, run_id)["grant_id"])
        bench, base = self._bench()
        real_preview = generic_run.describe_resume_confirmation
        trace = []

        def preview_then_revoke(*args, **kwargs):
            pre = real_preview(*args, **kwargs)
            trace.append(pre["previous_grant"]["state"])
            grants.revoke_batch_grant(self.project, pre["previous_grant"]["grant_id"],
                                      reason="R01-B负例:预览后正式撤销(测试注入)")
            return pre

        before = self._grant_files()
        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])), \
             mock.patch.object(generic_run, "describe_resume_confirmation",
                               side_effect=preview_then_revoke):
            with self.assertRaises(WorkbenchError) as ctx:
                bench.api("POST", base + "/confirm-new-batch", {},
                          {"run_id": run_id, "confirm": True})
        self.assertEqual(ctx.exception.code, "grant_revoked")
        self.assertEqual(trace, ["grant_expired"], "预览确实执行过且当时仍为到期")
        self.assertEqual(self._grant_files(), before, "拒绝路径不得签发任何新授权")
        stored = generic_run.load_receipt(self.project, run_id)
        self.assertNotIn("grant_id_active", stored)
        self.assertNotIn("resume_confirmations", stored)
        self.assertTrue(grants.load_grant(self.project, original["grant_id"])["revoked"],
                        "原授权撤销记录保留")
        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])):
            denied = generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(denied["status"], "failed")

    def test_tamper_between_preview_and_issue_refuses(self):
        """R01-B:预览后原授权文件被篡改 → 签发边界复验拒绝(指纹失配不签发),
        回执零写入。"""
        from opencoding.workbench import WorkbenchError

        binding = self._adopt_binding()
        run_id = "gen-0000c6016010"
        self._dispatch_blocked(run_id, binding)
        original = grants.load_grant(self.project, generic_run.load_receipt(
            self.project, run_id)["grant_id"])
        bench, base = self._bench()
        real_preview = generic_run.describe_resume_confirmation

        def preview_then_tamper(*args, **kwargs):
            pre = real_preview(*args, **kwargs)
            path = (self.project / ".opencoding" / "grants"
                    / (pre["previous_grant"]["grant_id"] + ".json"))
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["allowed_paths"] = [".."]
            path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            return pre

        before = self._grant_files()
        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])), \
             mock.patch.object(generic_run, "describe_resume_confirmation",
                               side_effect=preview_then_tamper):
            with self.assertRaises(WorkbenchError) as ctx:
                bench.api("POST", base + "/confirm-new-batch", {},
                          {"run_id": run_id, "confirm": True})
        self.assertEqual(ctx.exception.code, "grant_tampered")
        self.assertEqual(self._grant_files(), before, "结构不可信不得签发新授权")
        stored = generic_run.load_receipt(self.project, run_id)
        self.assertNotIn("grant_id_active", stored)
        self.assertNotIn("resume_confirmations", stored)

    def test_grant_restored_between_preview_and_issue_refuses(self):
        """R01-B:预览后原授权恢复有效(合成时钟序列)→ 复验拒绝并提示直接接续;
        零新授权、零回执写入。"""
        from opencoding.workbench import WorkbenchError

        binding = self._adopt_binding()
        run_id = "gen-0000c6016011"
        self._dispatch_blocked(run_id, binding)
        original = grants.load_grant(self.project, generic_run.load_receipt(
            self.project, run_id)["grant_id"])
        bench, base = self._bench()
        real_valid = grants.grant_valid
        calls = {"n": 0}

        def valid_restored_after_preview(grant):
            ok, why = real_valid(grant)
            calls["n"] += 1
            if why == "grant_expired" and calls["n"] >= 2:
                return True, "grant_active"  # 合成:确认过程中授权恢复有效
            return ok, why

        before = self._grant_files()
        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])), \
             mock.patch.object(grants, "grant_valid", side_effect=valid_restored_after_preview):
            with self.assertRaises(WorkbenchError) as ctx:
                bench.api("POST", base + "/confirm-new-batch", {},
                          {"run_id": run_id, "confirm": True})
        self.assertEqual(ctx.exception.code, "batch_still_active")
        self.assertEqual(calls["n"], 2, "预览与复验各核对一次")
        self.assertEqual(self._grant_files(), before)
        stored = generic_run.load_receipt(self.project, run_id)
        self.assertNotIn("grant_id_active", stored)
        self.assertNotIn("resume_confirmations", stored)

    def test_active_binding_interleaved_issue_reverts_orphan(self):
        """R01-B:签发窗口内另一确认更新了回执活跃位 → 本次刚签发的授权立即
        撤销(不留可用孤儿)、绑定被拒、不覆盖更新的活跃关系。"""
        from opencoding.workbench import WorkbenchError

        binding = self._adopt_binding()
        run_id = "gen-0000c6016012"
        self._dispatch_blocked(run_id, binding)
        original = grants.load_grant(self.project, generic_run.load_receipt(
            self.project, run_id)["grant_id"])
        bench, base = self._bench()
        real_issue = grants.issue_batch_grant
        seen = {}

        def issue_then_interleave(*args, **kwargs):
            new_grant = real_issue(*args, **kwargs)
            seen["orphan"] = new_grant["grant_id"]
            # 合成:签发窗口内另一确认抢先占据回执活跃位
            stored = generic_run.load_receipt(self.project, run_id)
            stored["grant_id_active"] = "grant-interleaved000"
            generic_run._write_receipt(self.project, stored)
            return new_grant

        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])), \
             mock.patch.object(grants, "issue_batch_grant", side_effect=issue_then_interleave):
            with self.assertRaises(WorkbenchError) as ctx:
                bench.api("POST", base + "/confirm-new-batch", {},
                          {"run_id": run_id, "confirm": True})
        self.assertEqual(ctx.exception.code, "confirmation_state_changed")
        self.assertTrue(grants.load_grant(self.project, seen["orphan"])["revoked"],
                        "刚签发的授权必须立即撤销,不留可用孤儿")
        stored = generic_run.load_receipt(self.project, run_id)
        self.assertEqual(stored["grant_id_active"], "grant-interleaved000",
                         "旧状态不得覆盖更新的活跃关系")
        self.assertNotIn("resume_confirmations", stored)

    def test_preview_scope_same_source_as_issuance_and_budget(self):
        """R01-C:到期授权预览=实际签发(四字段同源,非默认动作集);GET 零写入;
        既有累计预算如实展示且确认前后一致(不清零/不续期/不建第二台账)。"""
        from opencoding.workbench import Workbench

        binding = self._adopt_binding()
        run_id = "gen-0000c6016013"
        self._dispatch_blocked(run_id, binding)
        receipt = generic_run.load_receipt(self.project, run_id)
        original = grants.load_grant(self.project, receipt["grant_id"])
        budget_before = generic_run.task_budget_state(
            self.project, generic_run.TASK_ID, self.sid)
        self.assertEqual(budget_before["dispatched"], 1)
        bench, base = self._bench()

        def snap():
            return {str(p.relative_to(self.project)): p.read_bytes()
                    for p in self.project.rglob("*") if p.is_file()}

        keys = ["allowed_paths", "excluded_paths", "action_kinds", "data_scope"]
        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])):
            before = snap()
            preview = bench.api("GET", base + "/confirm-new-batch",
                                {"run_id": run_id}, {})["preview"]
            self.assertEqual(snap(), before, "GET 预览零写入")
            self.assertEqual({k: preview["new_batch_scope"][k] for k in keys},
                             {k: original[k] for k in keys},
                             "预览展示原授权真实范围(不再回退默认)")
            self.assertIn("ai_request", preview["new_batch_scope"]["action_kinds"],
                          "非默认动作集:原授权动作集与预览旧默认值可区分")
            self.assertEqual(preview["existing_task_budget"]["ceiling"],
                             budget_before["ceiling"])
            self.assertEqual(preview["existing_task_budget"]["dispatched"],
                             budget_before["dispatched"])
            self.assertEqual(preview["existing_task_budget"]["remaining"],
                             budget_before["remaining"])
            result = bench.api("POST", base + "/confirm-new-batch", {},
                               {"run_id": run_id, "confirm": True})
        self.assertEqual(result["status"], "confirmed")
        actual = grants.load_grant(self.project, result["new_grant_id"])
        self.assertEqual({k: preview["new_batch_scope"][k] for k in keys},
                         {k: actual[k] for k in keys}, "预览=实际签发(同源)")
        self.assertEqual({k: actual[k] for k in keys},
                         {k: original[k] for k in keys}, "签发逐项承接原授权,不扩大")
        budget_after = generic_run.task_budget_state(
            self.project, generic_run.TASK_ID, self.sid)
        self.assertEqual(
            (budget_after["ceiling"], budget_after["dispatched"], budget_after["remaining"]),
            (budget_before["ceiling"], budget_before["dispatched"], budget_before["remaining"]),
            "确认不动既有累计预算台账")

    def test_preview_ledger_unreadable_stays_conservative(self):
        """R01-C:账本损坏时预览如实标注(remaining=0 保守展示),不改写账本字节;
        接续派发仍按原规则保守拒绝(既有负例保持)。"""
        binding = self._adopt_binding()
        run_id = "gen-0000c6016014"
        self._dispatch_blocked(run_id, binding)
        original = grants.load_grant(self.project, generic_run.load_receipt(
            self.project, run_id)["grant_id"])
        ledger = generic_run._ledger_path(self.project, generic_run.TASK_ID, self.sid)
        ledger.write_bytes(b"{not-valid-json")
        bench, base = self._bench()
        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])):
            preview = bench.api("GET", base + "/confirm-new-batch",
                                {"run_id": run_id}, {})["preview"]
        self.assertTrue(preview["existing_task_budget"].get("unreadable"))
        self.assertEqual(preview["existing_task_budget"]["remaining"], 0)
        self.assertEqual(ledger.read_bytes(), b"{not-valid-json", "预览不改写账本字节")
        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])):
            denied = generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(denied["status"], "failed")

    # ------------- R01-B 最终一致提交边界(B1/B2/B3,20260930 审核) -------------

    def test_late_official_revoke_inside_issue_refuses_and_reverts(self):
        """R01-B/B1(q05 镜像):正式撤销发生在签发调用内部/紧前(预检全过后)→
        绑定前复查捕获;本次签发回退撤销,拒绝;原撤销记录保留;接续仍被拒。"""
        from opencoding.workbench import WorkbenchError

        binding = self._adopt_binding()
        run_id = "gen-0000c6016015"
        self._dispatch_blocked(run_id, binding)
        original = grants.load_grant(self.project, generic_run.load_receipt(
            self.project, run_id)["grant_id"])
        bench, base = self._bench()
        real_issue = grants.issue_batch_grant
        seen = {}

        def issue_after_late_revoke(*args, **kwargs):
            seen["old_state_at_issue_entry"] = grants.grant_valid(
                grants.load_grant(self.project, original["grant_id"]))
            grants.revoke_batch_grant(self.project, original["grant_id"],
                                      reason="B1负例:签发调用内部的较晚正式撤销")
            new_grant = real_issue(*args, **kwargs)
            seen["new_grant_id"] = new_grant["grant_id"]
            return new_grant

        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])), \
             mock.patch.object(grants, "issue_batch_grant", side_effect=issue_after_late_revoke):
            with self.assertRaises(WorkbenchError) as ctx:
                bench.api("POST", base + "/confirm-new-batch", {},
                          {"run_id": run_id, "confirm": True})
        self.assertEqual(ctx.exception.code, "grant_revoked")
        self.assertEqual(seen["old_state_at_issue_entry"], (False, "grant_expired"),
                         "预检确实全部通过(签发入口处原授权仍为到期)")
        self.assertIn("已回退撤销", str(ctx.exception.args[-1]),
                      "撤销完成时如实陈述回退结果,不用文案掩盖")
        self.assertTrue(grants.load_grant(self.project, seen["new_grant_id"])["revoked"],
                        "本次签发的新授权必须回退撤销,不得可用")
        self.assertTrue(grants.load_grant(self.project, original["grant_id"])["revoked"],
                        "原撤销记录保留")
        stored = generic_run.load_receipt(self.project, run_id)
        self.assertNotIn("grant_id_active", stored)
        self.assertNotIn("resume_confirmations", stored)
        self.assertEqual(len(self._grant_files()), 2,
                         "最初授权 + 仅一份新签发(已回退撤销)")
        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])):
            denied = generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(denied["status"], "failed")

    def test_two_concurrent_confirmations_serialize_to_single_winner(self):
        """R01-B/B2:两个真实线程经工作台并发确认同一运行——串行化后恰好一份
        成功;后到者在签发前被拒(无第二份签发、无事件丢失),最终活跃位唯一、
        确认事件恰好一份、不存在有效但无事件的新授权;线程全部结束。"""
        import threading
        from opencoding.workbench import Workbench, WorkbenchError

        binding = self._adopt_binding()
        run_id = "gen-0000c6016016"
        self._dispatch_blocked(run_id, binding)
        original = grants.load_grant(self.project, generic_run.load_receipt(
            self.project, run_id)["grant_id"])
        bench, base = self._bench()
        results = {}

        def confirm(note):
            try:
                res = bench.api("POST", base + "/confirm-new-batch", {},
                                {"run_id": run_id, "confirm": True,
                                 "issued_by": "并发测试", "note": note})
                results[note] = {"status": res["status"],
                                 "grant": res["new_grant_id"]}
            except WorkbenchError as exc:
                results[note] = {"code": exc.code}

        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])):
            barrier = threading.Barrier(2, timeout=30)
            threads = []
            for name in ("thread A", "thread B"):
                def _run(n=name):
                    barrier.wait()
                    confirm(n)
                threads.append(threading.Thread(target=_run))
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        self.assertTrue(all(not t.is_alive() for t in threads), "线程全部结束")
        self.assertEqual(len(results), 2)
        ok = [v for v in results.values() if v.get("status") == "confirmed"]
        refused = [v for v in results.values() if "code" in v]
        self.assertEqual(len(ok), 1, "重叠确认只允许一份成功:" + repr(results))
        self.assertEqual(len(refused), 1)
        self.assertIn(refused[0]["code"],
                      ("confirmation_state_changed", "confirmation_busy"))
        stored = generic_run.load_receipt(self.project, run_id)
        self.assertEqual(stored["grant_id_active"], ok[0]["grant"],
                         "最终活跃位唯一且与成功者一致")
        self.assertEqual(len(stored.get("resume_confirmations") or []), 1,
                         "确认事件恰好一份,不因覆盖而丢失")
        self.assertEqual(stored["resume_confirmations"][-1]["new_grant_id"],
                         ok[0]["grant"])
        for name in self._grant_files():
            doc = grants.load_grant(self.project, name[:-len(".json")])
            if doc["grant_id"] == original["grant_id"]:
                continue
            self.assertTrue(doc["grant_id"] == stored["grant_id_active"] or doc["revoked"],
                            "每个新授权要么已绑定要么已撤销:" + doc["grant_id"])

    def test_receipt_write_failure_reverts_issued_grant(self):
        """R01-B/B3(q07 镜像):确认回执落盘失败(注入合成 OSError)→ 本次签发
        立即回退撤销;回执无活跃位/事件;错误如实说明,不留有效未绑定授权。"""
        from opencoding.workbench import WorkbenchError

        binding = self._adopt_binding()
        run_id = "gen-0000c6016017"
        self._dispatch_blocked(run_id, binding)
        original = grants.load_grant(self.project, generic_run.load_receipt(
            self.project, run_id)["grant_id"])
        bench, base = self._bench()
        seen = {}

        real_write = generic_run._write_receipt

        def failing_write(root, receipt, **kwargs):
            # W0:产品现在向落盘函数传入提交线性化复核参数(expect_revocation_epoch);
            # 夹具继续在同一位置注入合成 OSError,不改变注入点语义。
            seen["write_kwargs"] = sorted(kwargs)
            if receipt.get("grant_id_active"):
                seen["attempted_active"] = receipt["grant_id_active"]
                raise OSError(28, "synthetic receipt write error")
            return real_write(root, receipt, **kwargs)

        with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])), \
             mock.patch.object(generic_run, "_write_receipt", side_effect=failing_write):
            with self.assertRaises(WorkbenchError) as ctx:
                bench.api("POST", base + "/confirm-new-batch", {},
                          {"run_id": run_id, "confirm": True})
        self.assertEqual(ctx.exception.code, "confirmation_commit_failed")
        self.assertIn("回执写入失败", str(ctx.exception.args[-1]),
                      "错误原因可解释")
        self.assertIn("expect_revocation_epoch", seen.get("write_kwargs") or [],
                      "落盘调用必须携带撤销线性化复核参数")
        doc = grants.load_grant(self.project, seen["attempted_active"])
        self.assertTrue(doc["revoked"], "未完成绑定的授权必须回退撤销")
        self.assertEqual(grants.grant_valid(doc), (False, "grant_revoked"))
        stored = generic_run.load_receipt(self.project, run_id)
        self.assertNotIn("grant_id_active", stored)
        self.assertNotIn("resume_confirmations", stored)

    def test_confirmation_busy_when_lock_held(self):
        """R01-B:确认临界区被占时有界等待后按 confirmation_busy 明确拒绝,
        不签发、不伪装业务结论。"""
        from opencoding.workbench import WorkbenchError

        binding = self._adopt_binding()
        run_id = "gen-0000c6016018"
        self._dispatch_blocked(run_id, binding)
        original = grants.load_grant(self.project, generic_run.load_receipt(
            self.project, run_id)["grant_id"])
        bench, base = self._bench()
        self.assertTrue(generic_run._confirm_reserve(self.project),
                        "预置占用确认串行化临界区")
        saved = generic_run._CONFIRM_LOCK_WAIT_TIMEOUT
        try:
            generic_run._CONFIRM_LOCK_WAIT_TIMEOUT = 0.3
            with mock.patch.object(grants, "datetime", self._shifted_clock(original["expires_at"])):
                with self.assertRaises(WorkbenchError) as ctx:
                    bench.api("POST", base + "/confirm-new-batch", {},
                              {"run_id": run_id, "confirm": True})
        finally:
            generic_run._CONFIRM_LOCK_WAIT_TIMEOUT = saved
            generic_run._confirm_release(self.project)
        self.assertEqual(ctx.exception.code, "confirmation_busy")
        stored = generic_run.load_receipt(self.project, run_id)
        self.assertNotIn("grant_id_active", stored)
        self.assertNotIn("resume_confirmations", stored)


class ResumeCancelTests(_Base):
    """U03:接续先持久化 running 阶段,request_cancel 可登记并中断后继提交。"""

    def test_cancel_during_resume_blocks_commit(self):
        binding = self._adopt_binding()
        run_id = "gen-0000c6011001"
        self._dispatch_blocked(run_id, binding)
        observed = {}

        def fake_stage(*args, **kwargs):
            # 接续检查进行中:正式取消入口必须可登记(此时状态应为 running)
            observed["cancel"] = generic_run.request_cancel(self.project, run_id)
            return True, [], {}

        with mock.patch.object(generic_run, "_capability",
                               return_value=self._yes_capability()), \
             mock.patch.object(generic_run, "_stage_and_verify", side_effect=fake_stage):
            resumed = generic_run.resume_saved_candidate(self.project, run_id)
        self.assertTrue(observed["cancel"]["cancel_requested"],
                        "接续进行中取消入口必须可登记(不能仍显示旧终态)")
        self.assertEqual(resumed["status"], "cancelled")
        self.assertTrue(resumed["cancel_requested"])
        self.assertEqual(resumed["current_request"]["effect"], "cancelled")
        self.assertIsNone(resumed.get("transaction_id"))
        self.assertFalse((self.project / "app" / "main.py").exists(), "取消后不得提交")
        # 候选保持可接续
        pending = generic_run.resumable_candidate(self.project, self.sid)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["run_id"], run_id)

    def test_running_stage_persisted_at_resume_start(self):
        binding = self._adopt_binding()
        run_id = "gen-0000c6011002"
        self._dispatch_blocked(run_id, binding)
        observed = {}

        def fake_stage(*args, **kwargs):
            doc = generic_run.load_receipt(self.project, run_id)
            observed["status"] = doc.get("status")
            observed["kind"] = (doc.get("current_request") or {}).get("kind")
            return False, ["verifier-stub-failure"], {}

        with mock.patch.object(generic_run, "_capability",
                               return_value=self._yes_capability()), \
             mock.patch.object(generic_run, "_stage_and_verify", side_effect=fake_stage):
            resumed = generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(observed["status"], "running",
                         "接续开始必须形成可查询的真实运行阶段")
        self.assertEqual(observed["kind"], "resume")
        self.assertEqual(resumed["status"], "failed")
        self.assertIn("隔离验收", resumed["failure"])


class ResumeDriftTests(_Base):
    """U02:接续检查期间事实变化 → 与普通生成同一控制闸拒绝提交。"""

    def test_fact_change_during_resume_blocks_commit(self):
        binding = self._adopt_binding()
        run_id = "gen-0000c6012001"
        self._dispatch_blocked(run_id, binding)

        def fake_stage(*args, **kwargs):
            add_fact(self.project, self.sid, content="接续期间新增约束:仅离线",
                     source_type="user", source_ref="authorized-test-fixture")
            return True, [], {}

        with mock.patch.object(generic_run, "_capability",
                               return_value=self._yes_capability()), \
             mock.patch.object(generic_run, "_stage_and_verify", side_effect=fake_stage):
            resumed = generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(resumed["status"], "failed")
        self.assertIn("相关有效输入已变化", resumed["failure"])
        self.assertTrue(resumed["attempts"][-1].get("gate_reject"))
        self.assertIsNone(resumed.get("transaction_id"))
        self.assertFalse((self.project / "app" / "main.py").exists())

    def test_adopted_plan_removal_during_resume_blocks_commit(self):
        binding = self._adopt_binding()
        run_id = "gen-0000c6012002"
        self._dispatch_blocked(run_id, binding)

        def fake_stage(*args, **kwargs):
            (self.project / ".opencoding" / "adoptions" / (self.sid + ".json")).unlink()
            return True, [], {}

        with mock.patch.object(generic_run, "_capability",
                               return_value=self._yes_capability()), \
             mock.patch.object(generic_run, "_stage_and_verify", side_effect=fake_stage):
            resumed = generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(resumed["status"], "failed")
        self.assertIn("采用记录已不存在", resumed["failure"])
        self.assertIsNone(resumed.get("transaction_id"))


class ResumeIdentityTests(_Base):
    """U04:暂存候选必须与原响应本体逐字节一致才算"同一候选"。"""

    def test_tampered_candidate_bytes_rejected(self):
        binding = self._adopt_binding()
        run_id = "gen-0000c6013001"
        receipt = self._dispatch_blocked(run_id, binding)
        cand_dir = Path(receipt["attempts"][0]["candidate_preserved"])
        target = cand_dir / "app" / "main.py"
        before = target.read_text(encoding="utf-8")
        target.write_text(before.replace("1", "9"), encoding="utf-8")
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(ctx.exception.code, "candidate_identity_mismatch")
        saved = generic_run.load_receipt(self.project, run_id)
        self.assertEqual(saved["status"], "blocked_execution", "入口拒绝不得改写运行状态")
        self.assertIsNone(saved.get("transaction_id"))
        self.assertFalse((self.project / "app" / "main.py").exists())
        self.assertIsNone(generic_run.resumable_candidate(self.project, self.sid),
                          "身份不符的候选不进接续预览")

    def test_extra_staged_file_rejected(self):
        binding = self._adopt_binding()
        run_id = "gen-0000c6013002"
        receipt = self._dispatch_blocked(run_id, binding)
        cand_dir = Path(receipt["attempts"][0]["candidate_preserved"])
        (cand_dir / "app" / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(ctx.exception.code, "candidate_identity_mismatch")

    def test_missing_received_material_rejected(self):
        binding = self._adopt_binding()
        run_id = "gen-0000c6013003"
        receipt = self._dispatch_blocked(run_id, binding)
        material = self.project / ".opencoding" / "generic_runs" / \
            (run_id + "-received-attempt1.json")
        self.assertTrue(material.is_file())
        material.unlink()
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            generic_run.resume_saved_candidate(self.project, run_id)
        self.assertEqual(ctx.exception.code, "received_material_missing")

    def test_intact_candidate_resume_uses_original_bytes(self):
        binding = self._adopt_binding()
        run_id = "gen-0000c6013004"
        receipt = self._dispatch_blocked(run_id, binding)
        captured = {}

        def fake_stage(project, grant, task, files, *args, **kwargs):
            captured["files"] = list(files)
            return True, [], {}

        with mock.patch.object(generic_run, "_capability",
                               return_value=self._yes_capability()), \
             mock.patch.object(generic_run, "_stage_and_verify", side_effect=fake_stage):
            generic_run.resume_saved_candidate(self.project, run_id)
        sent = {item["path"]: item["content"] for item in captured["files"]}
        self.assertEqual(sent, {"app/main.py": GOOD_MAIN, "app/selftest.py": GOOD_SELFTEST},
                         "接续必须使用与原响应逐字节一致的候选")


class NormalPathGrantGateTests(_Base):
    """普通生成路径同一控制闸:请求期间授权被撤销 → 响应后边界拒绝。"""

    def test_grant_revoked_during_request_blocks_commit(self):
        binding = self._adopt_binding()
        receipt_holder = {}
        project = self.project

        class Adapter:
            real = True
            provider = "fake"
            model = "fake-model"

            def complete(self, messages, **kw):
                grants.revoke_batch_grant(project, receipt_holder["grant_id"],
                                          reason="测试:请求期间撤销")
                return _good_payload()

        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.workspace / "appdata")}):
            aiconfig.save_config({"provider": "openai_compatible", "base_url": "https://x/v1",
                                  "api_key": "k", "model": "m"})
            payload = eval_response(choice="cli")
            record = advisor.run_ai_evaluation(self.project, self.sid,
                                               FakeAdapter([payload]), run_id="t")
        revision = service.session_view(self.project, self.sid)["session"]["revision"]
        advisor.confirm_evaluation(self.project, self.sid, record["evaluation_id"],
                                   expected_revision=revision, accepted=True)
        adopted = advisor.adopt_confirmed_evaluation(self.project, self.sid)["adoption"]
        binding2 = {"session_id": self.sid, "evaluation_id": record["evaluation_id"],
                    "plan_digest": adopted["plan_digest"],
                    "contract_sha256": (adopted["ai_binding"] or {}).get("contract_sha256")}

        class GrantCapture:
            """先拿到 grant_id 再派发:用 issue 后的回调捕获。"""

        # 派发后回执即含 grant_id;为在 complete 内撤销,先取绑定再派发,
        # 借助 mock 捕获 issue_batch_grant 的返回
        real_issue = grants.issue_batch_grant

        def spy_issue(*args, **kwargs):
            g = real_issue(*args, **kwargs)
            receipt_holder["grant_id"] = g["grant_id"]
            return g

        with mock.patch.object(grants, "issue_batch_grant", side_effect=spy_issue):
            receipt = generic_run.run_generic_app(
                self.project, "家庭借还登记", Adapter(), contract=self.contract,
                run_id="gen-0000c6014001", session_id=self.sid, adoption_binding=binding2,
                max_repair_rounds=0, acknowledged_unknown=True)
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("批次授权已失效", receipt["failure"])
        self.assertIsNone(receipt.get("transaction_id"))
        self.assertFalse((self.project / "app" / "main.py").exists())


class TaskBudgetRootTests(_Base):
    """C6-03(U08):task-budgets 纳入统一根/祖先保护;损坏账本保守停止。"""

    def setUp(self):
        super().setUp()
        self._symlink_ok = True
        probe = self.workspace / "_probe_link"
        try:
            probe.symlink_to(self.workspace)
            probe.unlink()
        except (OSError, NotImplementedError):
            self._symlink_ok = False

    def test_budget_dir_access_rejects_symlink(self):
        """预置符号链接在任何账本读写前被拒(写入时统一出口)。"""
        if not self._symlink_ok:
            self.skipTest("当前环境不支持创建符号链接(Windows 需开发者模式/管理员)")
        budgets = self.project / ".opencoding" / "task-budgets"
        budgets.parent.mkdir(parents=True, exist_ok=True)
        outside = self.workspace / "outside-audit-dir"
        outside.mkdir(exist_ok=True)
        budgets.symlink_to(outside)
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            generic_run.open_task_ledger(self.project, generic_run.TASK_ID, self.sid,
                                         ceiling=4, run_id="gen-0000c6015001")
        self.assertEqual(ctx.exception.code, "runtime_root_unsafe")
        self.assertEqual(list(outside.iterdir()), [], "链接目标零新增文件")

    def test_run_entry_validation_covers_budget_dir(self):
        """运行入口的统一校验覆盖 task-budgets:预置链接 → 零派发、零账本写出。"""
        if not self._symlink_ok:
            self.skipTest("当前环境不支持创建符号链接(Windows 需开发者模式/管理员)")
        binding = self._adopt_binding()
        budgets = self.project / ".opencoding" / "task-budgets"
        budgets.parent.mkdir(parents=True, exist_ok=True)
        outside = self.workspace / "outside-audit-dir"
        outside.mkdir(exist_ok=True)
        budgets.symlink_to(outside)

        class Adapter:
            real = True
            provider = "fake"
            model = "fake-model"
            calls = 0

            def complete(self, messages, **kw):
                type(self).calls += 1
                return _good_payload()

        adapter = Adapter()
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            generic_run.run_generic_app(
                self.project, "家庭借还登记", adapter, contract=self.contract,
                run_id="gen-0000c6015002", session_id=self.sid, adoption_binding=binding,
                max_repair_rounds=0, acknowledged_unknown=True)
        self.assertEqual(ctx.exception.code, "runtime_root_unsafe")
        self.assertEqual(adapter.calls, 0, "零派发")
        self.assertEqual(list(outside.iterdir()), [], "链接目标零账本写出")

    def test_corrupted_ledger_stops_dispatch_conservatively(self):
        """损坏/不可读账本:保守停止——不重建空账本、不派发、不扩额。"""
        binding = self._adopt_binding()

        class Timeout:
            real = False
            provider = "test"
            model = "x"
            calls = 0

            def complete(self, *a, **k):
                type(self).calls += 1
                raise AIRequestError("read_idle_timeout", "读取空闲超时")

        adapter = Timeout()
        receipt = generic_run.run_generic_app(
            self.project, "家庭借还登记", adapter, contract=self.contract,
            run_id="gen-0000c6015003", session_id=self.sid, adoption_binding=binding,
            max_repair_rounds=0, acknowledged_unknown=True)
        self.assertEqual(receipt["status"], "failed_transport")
        ledger_path = self.project / ".opencoding" / "task-budgets"
        files = list(ledger_path.glob("*.json"))
        self.assertEqual(len(files), 1)
        real_bytes = files[0].read_bytes()
        files[0].write_text("{damaged-ledger", encoding="utf-8")
        # 再次派发:保守拒绝,不派发、不重建
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            generic_run.run_generic_app(
                self.project, "家庭借还登记", adapter, contract=self.contract,
                run_id="gen-0000c6015004", session_id=self.sid, adoption_binding=binding,
                max_repair_rounds=0, acknowledged_unknown=True)
        self.assertEqual(ctx.exception.code, "task_budget_ledger_unreadable")
        self.assertEqual(adapter.calls, 1, "账本不可读时不得派发新请求")
        self.assertEqual(files[0].read_bytes(), b"{damaged-ledger", "不得静默重建空账本")
        # 扩额同样拒绝(不知道真实已消费量就不允许累加)
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            generic_run.extend_task_budget(self.project, generic_run.TASK_ID, self.sid,
                                           amount=5, reason="测试损坏账本扩额拒绝",
                                           issued_by="test")
        self.assertEqual(ctx.exception.code, "task_budget_ledger_unreadable")
        state = generic_run.task_budget_state(self.project, generic_run.TASK_ID, self.sid)
        self.assertEqual(state["remaining"], 0, "不可读账本按保守口径不允许继续消费")
        self.assertTrue(state.get("ledger_unreadable"))
        # 原始字节未变(证据保留)
        self.assertEqual(files[0].read_bytes(), b"{damaged-ledger")


if __name__ == "__main__":
    unittest.main()
