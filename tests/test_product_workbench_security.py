"""工作台授权、会话隔离、文件和 HTTP 边界的反例回归；不访问真实 Provider。"""

import http.client
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from opencoding import advisor, aiconfig, aiadapter, generic_run, service
from opencoding.workbench import Workbench, WorkbenchError, _Handler
from http.server import ThreadingHTTPServer


class WorkbenchSecurityBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.base / "config")})
        self.env.start()
        self.workspace = self.base / "workspace"
        self.bench = Workbench(self.workspace)
        self.bench.api("POST", "/api/project", {}, {"name": "project"})
        self.project = self.workspace / "project"
        self.sid = self.bench.api("POST", "/api/project/project/session", {},
                                  {"goal": "记录家里的物品"})["session"]["id"]
        self.prefix = f"/api/project/project/session/{self.sid}/"

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def assert_rejected(self, method, path, body, code):
        with self.assertRaises(WorkbenchError) as caught:
            self.bench.api(method, path, {}, body)
        self.assertEqual(caught.exception.code, code)

    def save_run(self, owner=None):
        run_id = "gen-aaaaaaaaaaaa"
        folder = self.project / ".opencoding" / "generic_runs"
        folder.mkdir(exist_ok=True)
        receipt = {"run_id": run_id, "session_id": owner or self.sid, "status": "blocked_execution"}
        (folder / (run_id + ".json")).write_text(json.dumps(receipt), encoding="utf-8")
        return run_id, receipt


class StrictConfirmationTests(WorkbenchSecurityBase):
    def test_false_like_confirmations_do_not_import_probe_or_extend(self):
        plain = self.workspace / "plain"
        plain.mkdir()
        aiconfig.save_config({"provider": "openai_compatible", "base_url": "https://example.invalid",
                              "api_key": "synthetic", "model": "fake"})
        with mock.patch.object(aiadapter, "probe_connection") as probe, \
             mock.patch.object(generic_run, "extend_task_budget") as extend:
            for value in (None, False, "false", "true", 0, 1, [], [True], {}, {"yes": True}):
                with self.subTest(value=value):
                    self.assert_rejected("POST", "/api/project/import",
                                         {"name": "plain", "confirm": value}, "import_unconfirmed")
                    self.assert_rejected("POST", "/api/ai/test", {"confirm": value}, "ai_test_unconfirmed")
                    self.assert_rejected("POST", self.prefix + "extend-budget",
                                         {"confirm": value, "reason": "测试扩充额度"},
                                         "budget_extension_unconfirmed")
            probe.assert_not_called()
            extend.assert_not_called()
        self.assertFalse((plain / ".opencoding").exists())

    def test_valid_confirm_calls_once_and_budget_amount_is_not_coerced(self):
        aiconfig.save_config({"provider": "openai_compatible", "base_url": "https://example.invalid",
                              "api_key": "synthetic", "model": "fake"})
        with mock.patch.object(aiadapter, "probe_connection", return_value={"state": "blocked"}) as probe:
            self.bench.api("POST", "/api/ai/test", {}, {"confirm": True})
            probe.assert_called_once()
        with mock.patch.object(generic_run, "extend_task_budget", return_value={}) as extend:
            for amount in (True, False, "2", 2.5, None, []):
                self.assert_rejected("POST", self.prefix + "extend-budget",
                                     {"confirm": True, "amount": amount, "reason": "测试扩充额度"},
                                     "budget_amount_invalid")
            extend.assert_not_called()
            self.bench.api("POST", self.prefix + "extend-budget", {},
                           {"confirm": True, "amount": 2, "reason": "测试扩充额度"})
            self.assertEqual(extend.call_args.kwargs["amount"], 2)

    def test_evaluation_confirmation_and_revision_preserve_types(self):
        with mock.patch.object(advisor, "confirm_evaluation", return_value={"status": "confirmed"}) as confirm:
            for value in (None, "false", "true", 0, 1, [], [True], {}, {"yes": True}):
                self.assert_rejected("POST", self.prefix + "confirm-evaluation",
                                     {"accepted": value, "revision": 0}, "evaluation_confirmation_invalid")
            for revision in (True, False, "0", 0.1, -1, None):
                self.assert_rejected("POST", self.prefix + "confirm-evaluation",
                                     {"accepted": True, "revision": revision}, "revision_invalid")
            confirm.assert_not_called()
            for value in (True, False):
                self.bench.api("POST", self.prefix + "confirm-evaluation", {},
                               {"accepted": value, "revision": 0, "evaluation_id": "test"})
                self.assertIs(confirm.call_args.kwargs["accepted"], value)

    def test_regenerate_string_false_rejected_before_provider_access(self):
        with mock.patch.object(aiconfig, "load_config") as load:
            for value in ("false", "true", 1, [], {}):
                self.assert_rejected("POST", self.prefix + "generate",
                                     {"regenerate": value}, "regenerate_invalid")
            load.assert_not_called()


class SessionOwnershipTests(WorkbenchSecurityBase):
    def test_foreign_receipt_cannot_be_read_or_mutated(self):
        other = self.bench.api("POST", "/api/project/project/session", {},
                               {"goal": "另一个会话"})["session"]["id"]
        run_id, receipt = self.save_run(other)
        receipt_path = self.project / ".opencoding/generic_runs" / (run_id + ".json")
        before = receipt_path.read_bytes()
        with mock.patch.object(generic_run, "resume_saved_candidate") as resume, \
             mock.patch.object(generic_run, "request_cancel") as cancel, \
             mock.patch.object(generic_run, "acknowledge_unknown") as ack:
            for method, action, body in (
                ("GET", "gen-status/" + run_id, {}),
                ("POST", "gen-status/" + run_id, {}),
                ("POST", "resume", {"run_id": run_id}),
                ("POST", "cancel-generation/" + run_id, {}),
                ("POST", "ack-unknown", {"run_id": run_id}),
            ):
                self.assert_rejected(method, self.prefix + action, body, "run_session_mismatch")
            resume.assert_not_called()
            cancel.assert_not_called()
            ack.assert_not_called()
        self.assertEqual(receipt_path.read_bytes(), before)

    def test_own_receipt_can_be_polled_resumed_cancelled_and_acknowledged(self):
        run_id, receipt = self.save_run()
        self.assertEqual(self.bench.api("GET", self.prefix + "gen-status/" + run_id, {}, {}), receipt)
        with mock.patch.object(generic_run, "resume_saved_candidate", return_value=receipt) as resume, \
             mock.patch.object(generic_run, "request_cancel", return_value={}) as cancel, \
             mock.patch.object(generic_run, "acknowledge_unknown", return_value={}) as ack:
            self.bench.api("POST", self.prefix + "resume", {}, {"run_id": run_id})
            self.bench.api("POST", self.prefix + "cancel-generation/" + run_id, {}, {})
            self.bench.api("POST", self.prefix + "ack-unknown", {}, {"run_id": run_id})
            resume.assert_called_once()
            cancel.assert_called_once()
            ack.assert_called_once()

    def test_preview_and_approval_must_match_url_root_and_session(self):
        with mock.patch.object(service, "approve_preview", return_value={}) as approve, \
             mock.patch.object(service, "apply_approved", return_value={}) as apply:
            for root, sid in ((str(self.project.resolve()), "other-session"),
                              (str(self.workspace.resolve()), self.sid)):
                self.assert_rejected("POST", self.prefix + "approve",
                                     {"preview": {"root": root, "session": {"id": sid}}},
                                     "document_session_mismatch")
                self.assert_rejected("POST", self.prefix + "apply",
                                     {"approval": {"root": root, "session_id": sid}},
                                     "document_session_mismatch")
            approve.assert_not_called()
            apply.assert_not_called()
            self.bench.api("POST", self.prefix + "approve", {},
                           {"preview": {"root": str(self.project.resolve()), "session": {"id": self.sid}}})
            self.bench.api("POST", self.prefix + "apply", {},
                           {"approval": {"root": str(self.project.resolve()), "session_id": self.sid}})
            approve.assert_called_once()
            apply.assert_called_once()

    def test_get_cannot_trigger_write_actions(self):
        run_id, _ = self.save_run()
        with mock.patch.object(generic_run, "request_cancel") as cancel, \
             mock.patch.object(service, "approve_preview") as approve, \
             mock.patch.object(service, "apply_approved") as apply:
            for action in ("cancel-generation/" + run_id, "approve", "apply", "resume", "ack-unknown"):
                self.assert_rejected("GET", self.prefix + action, {}, "method_not_allowed")
            cancel.assert_not_called()
            approve.assert_not_called()
            apply.assert_not_called()


class ProjectPathTests(WorkbenchSecurityBase):
    def test_metadata_links_rejected_before_create_or_read(self):
        outside = self.base / "outside"
        outside.mkdir()
        for rel in (".opencoding", ".opencoding/sessions", ".opencoding/project.json"):
            with self.subTest(rel=rel):
                root = self.workspace / "unsafe"
                root.mkdir(exist_ok=True)
                link = root / rel
                link.parent.mkdir(parents=True, exist_ok=True)
                try:
                    link.symlink_to(outside, target_is_directory=True)
                except (OSError, NotImplementedError):
                    self.skipTest("当前环境不支持符号链接")
                try:
                    self.assert_rejected("POST", "/api/project", {"name": "unsafe"}, "unsafe_project_path")
                    self.assert_rejected("GET", "/api/project/unsafe", {}, "unsafe_project_path")
                    self.assertEqual(list(outside.iterdir()), [])
                    boot = self.bench.api("GET", "/api/bootstrap", {}, {})
                    self.assertNotIn("unsafe", [p["name"] for p in boot["projects"]])
                finally:
                    link.unlink()

    def test_metadata_regular_file_is_not_created_through(self):
        root = self.workspace / "plain"
        root.mkdir()
        marker = root / ".opencoding"
        marker.write_text("keep", encoding="utf-8")
        self.assert_rejected("POST", "/api/project", {"name": "plain"}, "unsafe_project_path")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_windows_junction_rejected_without_writing_target(self):
        outside = self.base / "outside"
        outside.mkdir()
        root = self.workspace / "junction-project"
        root.mkdir()
        link = root / ".opencoding"
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if made.returncode:
            self.skipTest("当前环境不能创建 junction")
        try:
            self.assert_rejected("POST", "/api/project", {"name": root.name}, "unsafe_project_path")
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            # 只删除测试创建的 junction 本身，不递归目标目录。
            link.rmdir()

    def test_invalid_windows_names_rejected(self):
        for name in ("CON", "nul.txt", "trailing.", "a\x00b", "a\nb"):
            self.assert_rejected("POST", "/api/project", {"name": name}, "invalid_project_name")


class HTTPBoundaryTests(WorkbenchSecurityBase):
    def setUp(self):
        super().setUp()
        handler = type("TestHandler", (_Handler,), {"bench": self.bench})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.origin = "http://" + self.host

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        merged = {"Cookie": "oc_token=" + self.bench.token, "Content-Type": "application/json"}
        merged.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=merged)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_host_origin_and_content_type_reject_without_write(self):
        for headers, status in (
            ({"Host": "evil.invalid:" + str(self.server.server_port)}, 403),
            ({"Host": "127.0.0.1:1"}, 403),
            ({"Origin": "https://evil.invalid"}, 403),
            ({"Origin": "null"}, 403),
            ({"Origin": self.origin, "Sec-Fetch-Site": "same-site"}, 403),
            ({"Sec-Fetch-Site": "same-origin"}, 403),
            ({"Content-Type": "text/plain"}, 415),
        ):
            with self.subTest(headers=headers):
                code, _, _ = self.request("POST", "/api/project", {"name": "forbidden"}, headers)
                self.assertEqual(code, status)
                self.assertFalse((self.workspace / "forbidden").exists())

    def test_same_origin_browser_and_authenticated_json_client_work(self):
        for name, headers in (("browser", {"Origin": self.origin, "Sec-Fetch-Site": "same-origin"}),
                              ("client", {})):
            code, _, data = self.request("POST", "/api/project", {"name": name}, headers)
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(data)["created"])

    def test_raw_active_content_is_inert_and_links_rejected(self):
        app = self.project / "app"
        app.mkdir()
        payload = b"<script>fetch('/api/project',{method:'POST'})</script>"
        for name in ("demo.html", "demo.svg", "demo.js"):
            (app / name).write_bytes(payload)
            code, headers, data = self.request("GET", "/api/raw/project/app/" + name)
            self.assertEqual(code, 200)
            self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("sandbox", headers["Content-Security-Policy"])
            self.assertEqual(data, payload)
        linked = app / "alias.html"
        try:
            linked.symlink_to(app / "demo.html")
        except (OSError, NotImplementedError):
            self.skipTest("当前环境不支持符号链接")
        try:
            code, _, _ = self.request("GET", "/api/raw/project/app/alias.html")
            self.assertEqual(code, 400)
        finally:
            linked.unlink()

    def test_raw_paths_are_decoded_once(self):
        app = self.project / "app"
        app.mkdir()
        (app / "%2e%2e.txt").write_text("literal name", encoding="utf-8")
        code, _, data = self.request("GET", "/api/raw/project/app/%252e%252e.txt")
        self.assertEqual(code, 200)
        self.assertEqual(data, b"literal name")

    def test_get_cancel_rejected_without_mutation(self):
        run_id, _ = self.save_run()
        with mock.patch.object(generic_run, "request_cancel") as cancel:
            code, _, _ = self.request("GET", self.prefix + "cancel-generation/" + run_id)
            self.assertEqual(code, 405)
            cancel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
