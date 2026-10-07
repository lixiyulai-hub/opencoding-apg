# -*- coding: utf-8 -*-
"""W1(2026-09-30 集成批次 01):从空工作区到中文主路径的服务层可操作验收。

覆盖:
- 空工作区创建项目 → 立即可见 → 重启(新 Workbench 实例)后仍在;
- 列举接口严格只读:不为未初始化目录写任何身份文件;
- 已有目录需要显式确认才能导入,导入不移动/改名/删除用户文件;
- AI 配置"已保存"与"连通性已验证"分开,测试连接必须显式确认;
- 会话视图带出可接续候选与历史运行;产物位置可直接打开;
- 只读文件回传拒绝越界路径与符号链接。

替身边界:不发起真实网络请求(连通性测试只验证"必须显式确认"与状态分离);
AI 评估/生成若涉及外部调用均使用控制流替身。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from opencoding import aiconfig, generic_run, service
from opencoding.workbench import Workbench, WorkbenchError


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True)
        # AI 配置隔离到合成目录,绝不触碰用户真实配置
        self._env = mock.patch.dict(os.environ,
                                    {"LOCALAPPDATA": str(self.root / "appdata")})
        self._env.start()
        self.addCleanup(self._env.stop)

    def tearDown(self):
        self._tmp.cleanup()


class EmptyWorkspaceProjectTests(_Base):
    """空工作区 → 创建 → 立即可见 → 重启仍在。"""

    def test_create_then_list_then_reopen(self):
        bench = Workbench(self.workspace)
        boot0 = bench.api("GET", "/api/bootstrap", {}, {})
        self.assertEqual(boot0["projects"], [], "空工作区初始无项目")

        created = bench.api("POST", "/api/project", {}, {"name": "家庭借还登记"})
        self.assertTrue(created["ok"])
        self.assertTrue(created["created"])
        self.assertTrue((self.workspace / "家庭借还登记").is_dir())

        boot1 = bench.api("GET", "/api/bootstrap", {}, {})
        names = [p["name"] for p in boot1["projects"]]
        self.assertIn("家庭借还登记", names, "创建后必须立即可见、可选择")

        # 重启:新实例、新令牌,只依赖磁盘状态
        restarted = Workbench(self.workspace)
        boot2 = restarted.api("GET", "/api/bootstrap", {}, {})
        self.assertIn("家庭借还登记", [p["name"] for p in boot2["projects"]],
                      "重启后项目仍在")
        self.assertTrue((self.workspace / "家庭借还登记" / ".opencoding"
                         / "project.json").is_file(), "项目身份已落盘")

    def test_create_then_session_goal_flow(self):
        bench = Workbench(self.workspace)
        bench.api("POST", "/api/project", {}, {"name": "库存小工具"})
        opened = bench.api("GET", "/api/project/库存小工具", {}, {})
        self.assertEqual(opened["name"], "库存小工具")
        created = bench.api("POST", "/api/project/库存小工具/session", {},
                            {"goal": "我想做一个记录家里东西借出归还的小工具"})
        sid = created["session"]["id"]
        view = bench.api("GET", "/api/project/库存小工具/session/" + sid, {}, {})
        self.assertIn("借出归还", view["session"]["goal"])
        self.assertIn("resume", view, "会话视图必须带出接续状态")
        self.assertIn("runs", view, "会话视图必须带出历史运行")

    def test_invalid_names_rejected(self):
        bench = Workbench(self.workspace)
        for bad in ("", "  ", "a/b", "..", ".hidden"):
            with self.assertRaises(WorkbenchError):
                bench.api("POST", "/api/project", {}, {"name": bad})


class ReadOnlyListingTests(_Base):
    """A23:列举接口只读,不顺便初始化用户目录。"""

    def test_bootstrap_does_not_initialize_unlisted_dirs(self):
        plain = self.workspace / "我的杂文件夹"
        plain.mkdir()
        (plain / "note.txt").write_text("用户自己的文件", encoding="utf-8")
        bench = Workbench(self.workspace)
        boot = bench.api("GET", "/api/bootstrap", {}, {})
        self.assertIn("我的杂文件夹", boot.get("uninitialized") or [])
        self.assertFalse((plain / ".opencoding").exists(),
                         "列举不得写入任何项目身份文件")
        self.assertEqual((plain / "note.txt").read_text(encoding="utf-8"),
                         "用户自己的文件", "用户文件原样保留")

    def test_import_requires_confirm_and_preserves_files(self):
        plain = self.workspace / "已有资料"
        plain.mkdir()
        (plain / "keep.txt").write_text("不要动我", encoding="utf-8")
        bench = Workbench(self.workspace)
        with self.assertRaises(WorkbenchError) as ctx:
            bench.api("POST", "/api/project/import", {}, {"name": "已有资料"})
        self.assertEqual(ctx.exception.code, "import_unconfirmed")
        self.assertFalse((plain / ".opencoding").exists())

        done = bench.api("POST", "/api/project/import", {},
                         {"name": "已有资料", "confirm": True})
        self.assertTrue(done["imported"])
        self.assertEqual((plain / "keep.txt").read_text(encoding="utf-8"), "不要动我")
        self.assertIn("已有资料", [p["name"] for p in
                                  bench.api("GET", "/api/bootstrap", {}, {})["projects"]])

    def test_import_rejects_symlink(self):
        target = self.workspace / "真实目录"
        target.mkdir()
        link = self.workspace / "链接名"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("当前环境无法创建符号链接")
        bench = Workbench(self.workspace)
        try:
            with self.assertRaises(WorkbenchError) as ctx:
                bench.api("POST", "/api/project/import", {},
                          {"name": "链接名", "confirm": True})
            self.assertEqual(ctx.exception.code, "unsafe_project_path")
        finally:
            if link.is_symlink():
                link.unlink()


class AIStatusTests(_Base):
    """已保存 ≠ 已连通;测试连接必须显式确认。"""

    def _save_openai_like(self):
        bench = Workbench(self.workspace)
        return bench.api("POST", "/api/ai/config", {}, {
            "provider": "openai_compatible",
            "base_url": "https://example.invalid/v1",
            "api_key": "sk-synthetic-not-real",
            "model": "fake-model"})

    def test_save_is_not_tested_and_test_requires_confirm(self):
        saved = self._save_openai_like()
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["test"]["state"], "not_tested",
                         "保存配置不得宣称已连通")
        self.assertIn("未测试", saved["note"])
        bench = Workbench(self.workspace)
        with self.assertRaises(WorkbenchError) as ctx:
            bench.api("POST", "/api/ai/test", {}, {})
        self.assertEqual(ctx.exception.code, "ai_test_unconfirmed",
                         "测试连接必须显式确认,不得偷偷外连")

    def test_status_exposes_test_state(self):
        self._save_openai_like()
        bench = Workbench(self.workspace)
        status = bench.api("GET", "/api/ai/status", {}, {})
        self.assertTrue(status["ai"]["configured"])
        self.assertEqual(status["test"]["state"], "not_tested")
        self.assertNotIn("sk-synthetic", json.dumps(status, ensure_ascii=False),
                         "任何响应不得回显密钥")

    def test_unreachable_endpoint_reports_blocked(self):
        self._save_openai_like()
        bench = Workbench(self.workspace)
        result = bench.api("POST", "/api/ai/test", {}, {"confirm": True})
        self.assertIn(result["test"]["state"], ("unreachable", "blocked", "verified"))
        self.assertNotIn("sk-synthetic", json.dumps(result, ensure_ascii=False))


class SessionFlowTests(_Base):
    """会话视图带出接续与历史;产物位置可打开;只读回传拒绝越界。"""

    def test_resume_and_runs_surface_in_session_view(self):
        from opencoding import advisor
        from opencoding.aiadapter import AIRequestError
        from tests.test_product_full_chain_v5 import FakeAdapter, eval_response
        from tests.test_product_generic_run_v2 import GOOD_MAIN, GOOD_SELFTEST, _contract
        from tests.test_product_service import _complete

        bench = Workbench(self.workspace)
        bench.api("POST", "/api/project", {}, {"name": "家庭借还登记"})
        project = self.workspace / "家庭借还登记"
        view = _complete(project)
        sid = view["session"]["id"]
        payload = eval_response(choice="cli")
        aiconfig.save_config({"provider": "openai_compatible",
                              "base_url": "https://x/v1", "api_key": "k", "model": "m"})
        record = advisor.run_ai_evaluation(project, sid, FakeAdapter([payload]), run_id="t")
        revision = service.session_view(project, sid)["session"]["revision"]
        advisor.confirm_evaluation(project, sid, record["evaluation_id"],
                                   expected_revision=revision, accepted=True)
        advisor.adopt_confirmed_evaluation(project, sid)

        class Adapter:
            real = True
            provider = "fake"
            model = "fake-model"

            def complete(self, messages, **kw):
                return {"real": True, "provider": "fake", "model": "fake-model",
                        "request_id": "req-w1-" + os.urandom(4).hex(),
                        "structured": {"summary": "实现", "files": [
                            {"path": "app/main.py", "content": GOOD_MAIN},
                            {"path": "app/selftest.py", "content": GOOD_SELFTEST}]}}

        contract = generic_run._validate_contract(_contract())
        with mock.patch.object(generic_run, "_capability",
                               return_value={"available": False, "kind": "none",
                                             "command": [], "reason": "替身:后端不可用"}):
            receipt = generic_run.run_generic_app(
                project, "家庭借还登记", Adapter(), contract=contract,
                session_id=sid, adoption_binding=None, run_id="gen-0000c6b00001",
                max_repair_rounds=0, acknowledged_unknown=True)
        self.assertEqual(receipt["status"], "blocked_execution")

        seen = bench.api("GET", f"/api/project/家庭借还登记/session/{sid}", {}, {})
        self.assertTrue(seen["resume"]["available"], "可接续候选必须出现在页面上")
        self.assertEqual(seen["resume"]["run_id"], "gen-0000c6b00001")
        self.assertTrue(any(r["run_id"] == "gen-0000c6b00001" for r in seen["runs"]),
                        "历史运行必须可查")

        result = bench.api("GET", "/api/project/家庭借还登记/result",
                           {"run_id": "gen-0000c6b00001"}, {})
        self.assertEqual(result["run_id"], "gen-0000c6b00001")
        self.assertIn("家庭借还登记", result["project_root"])
        for item in result["files"]:
            self.assertTrue(item["raw_url"].startswith("/api/raw/"))

    def test_raw_route_rejects_escape(self):
        bench = Workbench(self.workspace)
        bench.api("POST", "/api/project", {}, {"name": "家庭借还登记"})
        sent = {}

        class FakeHandler:
            bench = None

            def send_response(self, code):
                sent["code"] = code

            def send_header(self, k, v):
                pass

            def end_headers(self):
                pass

            @property
            def wfile(self):
                class W:
                    def write(self, data):
                        sent["bytes"] = len(data)
                return W()

        from opencoding.workbench import _Handler

        handler = FakeHandler()
        handler.bench = bench

        with self.assertRaises(WorkbenchError) as ctx:
            _Handler._serve_raw(handler, "家庭借还登记/../../secret.txt")
        self.assertEqual(ctx.exception.code, "path_out_of_project")
        with self.assertRaises(WorkbenchError) as ctx2:
            _Handler._serve_raw(handler, "家庭借还登记/app/main.py")
        self.assertEqual(ctx2.exception.code, "file_not_found")
        # 正常文件可回传(写一份真实产物再读)
        (self.workspace / "家庭借还登记" / "app").mkdir(exist_ok=True)
        (self.workspace / "家庭借还登记" / "app" / "main.py").write_text(
            "print('hi')", encoding="utf-8")
        _Handler._serve_raw(handler, "家庭借还登记/app/main.py")
        self.assertEqual(sent.get("code"), 200, "项目内正常文件必须可读回")


if __name__ == "__main__":
    unittest.main()
