# -*- coding: utf-8 -*-
"""C6-02:docker 探针与生命周期判定修复的回归测试(不要求引擎运行)。

审计对应(01_AUDIT_REPORT_CN.md §5 U06):
- U06-5.1:探针夹具不得删除自己要测的 spec 目标;spec_exists=false 一律 fail-closed;
- classify:EROFS/EACCES/EPERM 按 errno 语义归为 denied(策略拒绝),
  一般 OSError 仍是 failed:*,不再把"只读"误报为一般失败;
- U06-5.2:容器不继承宿主环境,A/B 网络对照目标必须经 ``docker run -e``
  显式传入且两侧同源;对照目标只允许本机回环或宿主映射别名
  host.docker.internal(FIX-02:默认路径由宿主自建一次性监听,
  不再把两个网络空间各自的 127.0.0.1 当作同一可达端点);
- U06-5.3:``completion_consistent`` 汇总 create/stop/remove/exit 一致性,
  sandbox 层对不一致结果拒绝交付(docker_lifecycle_unverified)。

C6FIX3 轮新增(R02a/R02b):
- R02a:回环配置(127.0.0.1/localhost)归一化为宿主别名寻址——默认路径与
  配置路径表达**同一个受控本地服务**;::1 明确拒绝并说明;外部地址维持拒绝;
- R02b:监听收尾在函数返回边界上逐项核实(套接字已关/线程已停/端口拒绝
  新连接),未确认如实上报并阻断资格签发,不做任意 sleep、不删断言。

全部用可控替身(fake runner + 明确就绪替身),不启动引擎、不拉镜像、不连外部地址;
替身边界:_ControlReachable 的"connected"是固定替身输出,不等于真实容器内连接。
"""
import errno
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from opencoding import docker_provider, sandbox


# ---------------------------------------------------------------- 替身

def _ok_probe(*, skip_network=False):
    return {
        "ro_root": {"status": "denied", "errno": 13},
        "ro_root_parent_exists": True,
        "data_writable": {"status": "writable"},
        "data_writable_parent_exists": True,
        "spec_readonly": {"status": "denied", "errno": 30},
        "spec_readonly_parent_exists": True,
        "network": {"status": "skipped:no_control_target"} if skip_network
                   else {"status": "failed:oserror:113"},
    }


class _FakeDocker:
    """受控替身:记录命令、模拟容器生命周期。不触碰真实引擎。"""

    def __init__(self, probe=None, *, inspect_state="false", rm_succeeds=True):
        self.calls = []
        self.probe = probe if probe is not None else _ok_probe()
        self.inspect_state = inspect_state
        self.rm_succeeds = rm_succeeds
        self._inspect_calls = 0
        self._probe_env = {}

    def __call__(self, cmd, **_kwargs):
        self.calls.append(list(cmd))
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "run":
            pairs = [cmd[i + 1] for i, value in enumerate(cmd[:-1]) if value == "-e"]
            self._probe_env = {
                item.split("=", 1)[0]: item.split("=", 1)[1]
                for item in pairs if "=" in item
            }
            return subprocess.CompletedProcess(cmd, 0, "opencoding-cid-c6feed\n", "")
        if sub == "wait":
            return subprocess.CompletedProcess(cmd, 0, "0\n", "")
        if sub == "inspect":
            self._inspect_calls += 1
            if self._inspect_calls == 1:
                return subprocess.CompletedProcess(cmd, 0, self.inspect_state + "\n", "")
            return subprocess.CompletedProcess(cmd, 1 if self.rm_succeeds else 0, "", "")
        if sub == "logs":
            probe = dict(self.probe)
            if self._probe_env.get("OPENCODING_DOCKER_PROBE_HOST"):
                probe["network_target"] = {
                    "host": self._probe_env["OPENCODING_DOCKER_PROBE_HOST"],
                    "port": int(self._probe_env.get("OPENCODING_DOCKER_PROBE_PORT", "0")),
                    "mode": "direct_target",
                }
            return subprocess.CompletedProcess(cmd, 0, "PROBE:" + json.dumps(probe), "")
        if sub == "rm":
            return subprocess.CompletedProcess(cmd, 0 if self.rm_succeeds else 1, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


class _ControlReachable(_FakeDocker):
    """对照容器(默认桥接)可达,用于让网络 A/B 全绿。"""

    def __call__(self, cmd, **kwargs):
        if len(cmd) > 1 and cmd[1] == "run" and "--network" in cmd \
                and cmd[cmd.index("--network") + 1] == "bridge":
            return subprocess.CompletedProcess(
                cmd, 0, "CONTROL:" + json.dumps({"status": "connected", "peer_ip": "192.168.65.254"}), "")
        return super().__call__(cmd, **kwargs)


class _Ready:
    """明确替身:CLI/引擎/本地镜像/镜像身份齐备(不代表边界已验证)。"""

    def __init__(self, image_id="sha256:c6feedbeef"):
        self.image_id = image_id
        self.saved = None

    def __enter__(self):
        m = docker_provider
        self.saved = (m.docker_cli_available, m.daemon_reachable,
                      m.image_present_locally, m.image_id)
        m.docker_cli_available = lambda: True
        m.daemon_reachable = lambda timeout=10: (True, "替身引擎可用")
        m.image_present_locally = lambda image=None: (True, "替身镜像存在")
        m.image_id = lambda image=None: (self.image_id, "替身镜像身份")
        return self

    def __exit__(self, *exc):
        (docker_provider.docker_cli_available, docker_provider.daemon_reachable,
         docker_provider.image_present_locally, docker_provider.image_id) = self.saved
        return False


def _classify_from_probe_source():
    """从真实 PROBE_SOURCE 提取 classify(不做手工 JSON 拼装,测的是真源码)。"""
    marker = "result = {}"
    index = docker_provider.PROBE_SOURCE.index(marker)
    prefix = docker_provider.PROBE_SOURCE[:index]
    namespace: dict = {"__name__": "probe_source_extract"}
    exec(compile(prefix, "<PROBE_SOURCE-head>", "exec"), namespace)  # noqa: S102
    return namespace["classify"]


class _EnvBase(unittest.TestCase):
    """公共夹具:独立 scratch + 资格目录 + 探针对照目标环境变量。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oc-docker-c6-")
        self.root = Path(self.tmp.name) / "scratch"
        (self.root / "app" / "data").mkdir(parents=True, exist_ok=True)
        (self.root / "frozen_checks").mkdir(parents=True, exist_ok=True)
        self.prev = {k: os.environ.get(k) for k in
                     ("OPENCODING_DOCKER_ENTITLEMENT_DIR",
                      "OPENCODING_DOCKER_PROBE_HOST", "OPENCODING_DOCKER_PROBE_PORT")}
        os.environ["OPENCODING_DOCKER_ENTITLEMENT_DIR"] = str(Path(self.tmp.name) / "ent")
        os.environ["OPENCODING_DOCKER_PROBE_HOST"] = "127.0.0.1"
        os.environ["OPENCODING_DOCKER_PROBE_PORT"] = "9"

    def tearDown(self):
        for key, value in self.prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()


# ---------------------------------------------------------------- U06-5.1 夹具保留

class ProbeFixturePreservationTests(_EnvBase):
    """探针夹具必须保留被测对象;目标缺失一律 fail-closed。"""

    def test_fixtures_keep_spec_target_intact(self):
        control = docker_provider._ensure_probe_fixtures(self.root, "app/data")
        self.assertEqual(control["host_spec_writable"], "writable")
        self.assertEqual(control["host_data_writable"], "writable")
        self.assertTrue(control["spec_exists"], "正向对照后 spec 目标必须还在")
        self.assertTrue(control["data_dir_exists"])
        spec = self.root / "frozen_checks" / "spec.json"
        self.assertTrue(spec.is_file())
        content = spec.read_text(encoding="utf-8")
        self.assertIn("frozen-spec-probe", content)
        self.assertNotIn("host-control", content, "对照写入不得覆盖 spec 原内容")

    def test_fixtures_idempotent_and_spec_survives_repeat(self):
        first = docker_provider._ensure_probe_fixtures(self.root, "app/data")
        second = docker_provider._ensure_probe_fixtures(self.root, "app/data")
        self.assertTrue(first["spec_exists"])
        self.assertTrue(second["spec_exists"])
        spec = self.root / "frozen_checks" / "spec.json"
        before = spec.read_text(encoding="utf-8")
        self.assertEqual(before, spec.read_text(encoding="utf-8"))
        self.assertNotIn("host-control", before)

    def test_probe_boundaries_refuses_when_spec_missing(self):
        """U06-5.1 反例:spec 目标缺失时不得把结果当只读保护成立。"""
        broken = {"host_spec_writable": "writable", "host_data_writable": "writable",
                  "spec_exists": False, "spec_bytes": 0, "data_dir_exists": True}
        with mock.patch.object(docker_provider, "_ensure_probe_fixtures",
                               return_value=broken):
            with _Ready():
                result = docker_provider.probe_boundaries(
                    self.root, runner=_ControlReachable())
        self.assertFalse(result["granted"])
        self.assertIn("spec 目标缺失", result["reason"])

    def test_probe_happy_path_keeps_spec_and_grants_v6(self):
        probe = _ok_probe()
        probe["network"] = {"status": "failed:oserror:113"}
        with _Ready() as ready:
            result = docker_provider.probe_boundaries(self.root, runner=_ControlReachable(probe))
        self.assertTrue(result["granted"], result["reason"])
        self.assertTrue(result["network_diagnostic"]["same_target"])
        self.assertEqual(result["network_diagnostic"]["mode"], "bridge_resolved_ip")
        self.assertEqual(result["control"]["network"]["restricted_target"]["host"],
                         result["control"]["network"]["resolved_target"]["host"])
        self.assertTrue(result["control"]["spec_exists"])
        spec = self.root / "frozen_checks" / "spec.json"
        self.assertTrue(spec.is_file())
        self.assertNotIn("host-control", spec.read_text(encoding="utf-8"))
        digest = docker_provider.policy_digest(docker_provider._image(), ready.image_id)
        doc = docker_provider.load_entitlement(digest)
        self.assertIsNotNone(doc)
        self.assertEqual(doc["facts"]["probe_version"], "docker-probe-v6")
        self.assertEqual(docker_provider.PROBE_VERSION, "docker-probe-v6")


# ---------------------------------------------------------------- classify errno 语义

class ClassifyErrnoSemanticsTests(unittest.TestCase):
    """EROFS/EACCES/EPERM → denied;一般 OSError → failed:*;成功 → writable。"""

    @classmethod
    def setUpClass(cls):
        # staticmethod 包装:避免函数存为类属性后被实例访问绑定成方法
        cls.classify = staticmethod(_classify_from_probe_source())

    def _oserror(self, code):
        def action():
            raise OSError(code, os.strerror(code) if code else "boom")
        return action

    def test_erofs_is_denied(self):
        result = self.classify(self._oserror(errno.EROFS))
        self.assertEqual(result["status"], "denied")
        self.assertEqual(result["errno"], errno.EROFS)
        self.assertEqual(result.get("semantic"), "readonly_or_denied")

    def test_eacces_and_eperm_are_denied(self):
        for code in (errno.EACCES, errno.EPERM):
            result = self.classify(self._oserror(code))
            self.assertEqual(result["status"], "denied", result)
            self.assertEqual(result["errno"], code)

    def test_generic_oserror_is_failed_not_denied(self):
        result = self.classify(self._oserror(5))
        self.assertEqual(result["status"], "failed:oserror:5")
        self.assertNotEqual(result["status"], "denied")

    def test_missing_and_success_shapes(self):
        def missing():
            raise FileNotFoundError("no such file")
        self.assertEqual(self.classify(missing)["status"], "failed:path_missing")

        def fine():
            return None
        self.assertEqual(self.classify(fine), {"status": "writable"})


# ---------------------------------------------------------------- env 显式传递与对照目标

class RunCommandEnvTests(_EnvBase):
    """容器不继承宿主环境;A/B 两侧同一目标且经 -e 显式传入。"""

    @unittest.skipUnless(os.name == "nt", "路径映射断言针对 Windows 宿主")
    def test_env_passed_as_explicit_flags(self):
        cmd = docker_provider.build_run_command(
            self.root, ["python", "frozen_checks/docker_probe.py"],
            env={"OPENCODING_DOCKER_PROBE_HOST": "127.0.0.1",
                 "OPENCODING_DOCKER_PROBE_PORT": "18099"})
        pairs = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-e"]
        self.assertEqual(pairs, ["OPENCODING_DOCKER_PROBE_HOST=127.0.0.1",
                                 "OPENCODING_DOCKER_PROBE_PORT=18099"])
        image_index = cmd.index("python:3.11-slim")
        for pair in pairs:
            self.assertLess(cmd.index("-e", 0, image_index), image_index)

    def test_env_invalid_entries_rejected(self):
        bad = [
            {"1BAD": "x"},            # 数字开头
            {"HAS SPACE": "x"},       # 非法字符
            {"OK": 'qu"ote'},         # 值含双引号
            {"OK": "new\nline"},      # 值含换行
            {"OK": "sin'gle"},        # 值含单引号
            {"": "x"},                # 空键
        ]
        for env in bad:
            with self.assertRaises(ValueError, msg=env):
                docker_provider.build_run_command(self.root, ["python"], env=env)

    def test_probe_control_target_validation(self):
        with mock.patch.dict(os.environ):
            for key in ("OPENCODING_DOCKER_PROBE_HOST", "OPENCODING_DOCKER_PROBE_PORT"):
                os.environ.pop(key, None)
            result = docker_provider._probe_control_target()
            self.assertFalse(result["configured"])

            cases = [
                ({"OPENCODING_DOCKER_PROBE_HOST": "8.8.8.8",
                  "OPENCODING_DOCKER_PROBE_PORT": "53"}, "refused", "回环"),
                ({"OPENCODING_DOCKER_PROBE_HOST": "127.0.0.1",
                  "OPENCODING_DOCKER_PROBE_PORT": "0"}, "refused", "1-65535"),
                ({"OPENCODING_DOCKER_PROBE_HOST": "127.0.0.1",
                  "OPENCODING_DOCKER_PROBE_PORT": "70000"}, "refused", "1-65535"),
                ({"OPENCODING_DOCKER_PROBE_HOST": "127.0.0.1",
                  "OPENCODING_DOCKER_PROBE_PORT": "abc"}, "refused", "整数"),
            ]
            for env, field, needle in cases:
                os.environ.clear()
                os.environ.update(env)
                result = docker_provider._probe_control_target()
                self.assertIn(field, result, env)
                self.assertIn(needle, str(result[field]))

            os.environ["OPENCODING_DOCKER_PROBE_HOST"] = "127.0.0.1"
            os.environ["OPENCODING_DOCKER_PROBE_PORT"] = "18099"
            result = docker_provider._probe_control_target()
            self.assertTrue(result["configured"])
            # R02a:回环配置归一化为宿主别名寻址(容器内 127.0.0.1 指容器自身)
            self.assertEqual(result["host"], "host.docker.internal")
            self.assertEqual(result["port"], 18099, "端口语义保持用户配置")
            self.assertEqual(result["source"], "configured_loopback_normalized")
            self.assertEqual(result["requested_host"], "127.0.0.1")

            os.environ["OPENCODING_DOCKER_PROBE_HOST"] = "localhost"
            result = docker_provider._probe_control_target()
            self.assertTrue(result["configured"])
            self.assertEqual(result["host"], "host.docker.internal")
            self.assertEqual(result["source"], "configured_loopback_normalized")

            # R02a:IPv6 回环经宿主别名映射无可靠可达性保证 → 明确拒绝并说明
            os.environ["OPENCODING_DOCKER_PROBE_HOST"] = "::1"
            result = docker_provider._probe_control_target()
            self.assertIn("refused", result)
            self.assertIn("::1", str(result["refused"]))

            # FIX-02:host.docker.internal 是宿主映射别名(指向本机),允许且原样;
            # 其余外部地址仍然拒绝
            os.environ["OPENCODING_DOCKER_PROBE_HOST"] = "host.docker.internal"
            result = docker_provider._probe_control_target()
            self.assertTrue(result["configured"])
            self.assertEqual(result["host"], "host.docker.internal")
            self.assertEqual(result["source"], "configured")

            os.environ["OPENCODING_DOCKER_PROBE_HOST"] = "example.com"
            result = docker_provider._probe_control_target()
            self.assertIn("refused", result)

    def test_probe_passes_same_target_to_container_and_control(self):
        """A(受限容器)/B(对照容器)收到的必须是同一目标;回环配置归一化后
        与默认路径表达同一个受控本地服务(R02a)。"""
        seen_run_cmds = []

        class _Recorder(_ControlReachable):
            def __call__(self, cmd, **kwargs):
                if len(cmd) > 1 and cmd[1] == "run":
                    seen_run_cmds.append(list(cmd))
                return super().__call__(cmd, **kwargs)

        probe = _ok_probe()
        probe["network"] = {"status": "failed:oserror:113"}
        with _Ready():
            result = docker_provider.probe_boundaries(self.root, runner=_Recorder(probe))
        self.assertTrue(result["granted"], result["reason"])
        # 归一化已记录:请求的是 127.0.0.1,实际寻址是宿主别名
        network = result["control"]["network"]
        self.assertEqual(network["target"]["host"], "host.docker.internal")
        probe_runs = [c for c in seen_run_cmds
                      if "--network" in c and c[c.index("--network") + 1] == "none"]
        control_runs = [c for c in seen_run_cmds
                        if "--network" in c and c[c.index("--network") + 1] == "bridge"]
        self.assertEqual(len(probe_runs), 1)
        self.assertEqual(len(control_runs), 1)
        # A 侧:目标经 -e 显式进入受限容器——归一化后的宿主别名,不是回环字符串
        probe_cmd = probe_runs[0]
        pairs = [probe_cmd[i + 1] for i, v in enumerate(probe_cmd) if v == "-e"]
        self.assertIn("OPENCODING_DOCKER_PROBE_HOST=192.168.65.254", pairs)
        self.assertIn("OPENCODING_DOCKER_PROBE_PORT=9", pairs)
        # B 侧:对照容器同一目标(命令尾部显式携带 host/port)
        control_cmd = control_runs[0]
        self.assertEqual(control_cmd[-2:], ["host.docker.internal", "9"])


# ---------------------------------------------------------------- FIX-02:一次性监听默认路径

class EphemeralListenerTests(_EnvBase):
    """FIX-02(U06-5.2):默认路径自建本批专用一次性监听,A/B 同一真实可达端点。

    两个不同网络空间各自的 127.0.0.1 各指其自身回环,仅传同一字符串不构成
    "同一可达端点";现在默认由宿主自建监听,容器侧统一 host.docker.internal
    寻址,对照(bridge)可达证明端点真实存在,受限(none)失败才是策略效果。
    """

    @staticmethod
    def _pairs(cmd):
        return [cmd[i + 1] for i, v in enumerate(cmd) if v == "-e"]

    def test_default_path_self_provisions_reachable_listener(self):
        """未配置环境变量 → 自建监听;A/B 两侧同一 host.docker.internal 端点;
        探测结束监听必须关闭(端口不再可达)。"""
        import socket
        for key in ("OPENCODING_DOCKER_PROBE_HOST", "OPENCODING_DOCKER_PROBE_PORT"):
            os.environ.pop(key, None)
        seen_run_cmds = []

        class _Recorder(_ControlReachable):
            def __call__(self, cmd, **kwargs):
                if len(cmd) > 1 and cmd[1] == "run":
                    seen_run_cmds.append(list(cmd))
                return super().__call__(cmd, **kwargs)

        probe = _ok_probe()
        probe["network"] = {"status": "failed:oserror:113"}
        with _Ready():
            result = docker_provider.probe_boundaries(self.root, runner=_Recorder(probe))
        self.assertTrue(result["granted"], result["reason"])
        network = result["control"]["network"]
        self.assertTrue(network["performed"])
        self.assertEqual(network["target"]["host"], "host.docker.internal")
        probe_runs = [c for c in seen_run_cmds
                      if "--network" in c and c[c.index("--network") + 1] == "none"]
        control_runs = [c for c in seen_run_cmds
                        if "--network" in c and c[c.index("--network") + 1] == "bridge"]
        self.assertEqual(len(probe_runs), 1)
        self.assertEqual(len(control_runs), 1)
        pairs = self._pairs(probe_runs[0])
        host_pair = next(p for p in pairs if p.startswith("OPENCODING_DOCKER_PROBE_HOST="))
        port_pair = next(p for p in pairs if p.startswith("OPENCODING_DOCKER_PROBE_PORT="))
        self.assertEqual(host_pair, "OPENCODING_DOCKER_PROBE_HOST=192.168.65.254")
        port = int(port_pair.split("=", 1)[1])
        self.assertTrue(1 <= port <= 65535)
        # B 侧:对照容器同一目标(命令尾部显式携带 host/port)
        self.assertEqual(control_runs[0][-2:], ["host.docker.internal", str(port)])
        # R02b:返回边界上的有界收尾结论逐项确认
        cleanup = result.get("listener_cleanup")
        self.assertIsInstance(cleanup, dict)
        self.assertTrue(cleanup["confirmed"], cleanup)
        self.assertTrue(cleanup["socket_closed"])
        self.assertTrue(cleanup["thread_stopped"])
        self.assertTrue(cleanup["port_free"])
        # 探测结束后一次性监听必须已关闭:同一端口不再可达
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()

    def test_listener_failure_is_fail_closed(self):
        """监听建立失败 → 不发起任何 docker 调用,整体拒绝签发。"""
        for key in ("OPENCODING_DOCKER_PROBE_HOST", "OPENCODING_DOCKER_PROBE_PORT"):
            os.environ.pop(key, None)
        runner = _ControlReachable()
        with _Ready():
            with mock.patch.object(docker_provider, "_start_probe_listener",
                                   return_value={"ok": False, "reason": "端口被占(替身)"}):
                result = docker_provider.probe_boundaries(self.root, runner=runner)
        self.assertFalse(result["granted"])
        self.assertIn("fail-closed", result["reason"])
        self.assertIn("端口被占", result["reason"])
        self.assertEqual(runner.calls, [], "监听失败不得发起任何 docker 调用")

    def test_configured_path_does_not_start_listener(self):
        """显式配置目标时沿用既有路径,不自建监听。"""
        with _Ready():
            with mock.patch.object(docker_provider, "_start_probe_listener",
                                   side_effect=AssertionError("配置了目标就不该自建监听")):
                result = docker_provider.probe_boundaries(
                    self.root, runner=_ControlReachable())
        self.assertTrue(result["granted"], result["reason"])


# ---------------------------------------------------------------- R02b:监听有界收尾

class ListenerBoundedStopTests(_EnvBase):
    """R02b:收尾必须在函数返回边界上逐项核实,未确认不得当作已停止。

    旧实现只置标志 + close 就返回,Linux P04 实测线程仍存活、端口短暂
    仍可连;现在返回结论 dict(socket_closed/thread_stopped/port_free/
    confirmed),不做任意 sleep、不删断言、不用成功替身替代真实观测。
    """

    def test_stop_confirmed_at_return_boundary(self):
        """正常结束:返回时线程已停、端口新连接被拒(真实 socket 观测)。"""
        import socket
        listener = docker_provider._start_probe_listener()
        self.assertTrue(listener["ok"])
        # 监听窗口内:端口真实可达(正向对照)
        socket.create_connection(("127.0.0.1", listener["port"]), timeout=2).close()
        result = docker_provider._stop_probe_listener(listener)
        self.assertIsInstance(result, dict)
        self.assertTrue(result["confirmed"], result)
        self.assertTrue(result["socket_closed"])
        self.assertTrue(result["thread_stopped"])
        self.assertTrue(result["port_free"])
        self.assertFalse(listener["thread"].is_alive())
        # 函数返回边界之后:端口拒绝新连接
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", listener["port"]), timeout=2).close()

    def test_thread_stuck_within_bound_reported_unconfirmed(self):
        """线程未在有界超时内结束 → confirmed=False,如实上报,不记'已结束'。"""
        import socket
        import threading

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = int(srv.getsockname()[1])
        release = threading.Event()

        def _hold():
            # 拒绝停止的线程替身:有界 join 超时内不结束
            release.wait(5.0)
            srv.close()

        thread = threading.Thread(target=_hold, daemon=True)
        thread.start()
        listener = {"ok": True, "socket": srv, "port": port,
                    "thread": thread, "serving": {"open": True}}
        saved = docker_provider._PROBE_LISTENER_JOIN_TIMEOUT
        try:
            docker_provider._PROBE_LISTENER_JOIN_TIMEOUT = 0.2
            result = docker_provider._stop_probe_listener(listener)
        finally:
            docker_provider._PROBE_LISTENER_JOIN_TIMEOUT = saved
            release.set()
            thread.join(5.0)
        self.assertFalse(result["confirmed"], result)
        self.assertTrue(result["socket_closed"])
        self.assertFalse(result["thread_stopped"], "有界超时内未结束必须如实记录")
        self.assertTrue(result["port_free"], "套接字已关则端口释放;未确认来自线程未停")

    @unittest.skipUnless(os.name == "nt", "双监听占用依赖 Windows SO_REUSEADDR 语义")
    def test_port_still_connectable_after_stop_reported(self):
        """返回边界上端口仍可连(P04 现象的真实 socket 观测)→ port_free=False、
        confirmed=False,绝不记录"已结束"。"""
        import socket
        import threading

        listener = docker_provider._start_probe_listener()
        self.assertTrue(listener["ok"])
        port = int(listener["port"])
        # Windows SO_REUSEADDR 允许第二个监听占用同一端口,模拟"停止后仍可连"
        squat = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        squat.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squat.bind(("127.0.0.1", port))
        squat.listen(4)
        try:
            result = docker_provider._stop_probe_listener(listener)
        finally:
            squat.close()
        self.assertFalse(result["confirmed"], result)
        self.assertFalse(result["port_free"], "仍能连上必须如实记录,不得当作已停止")
        self.assertTrue(result["thread_stopped"])

    def test_observation_refused_still_confirmed_for_real_stop(self):
        """R02b:明确拒绝(ConnectionRefusedError)仍按契约判 port_free=True——
        分类不改变明确信号结论(注入式单测,平台无关)。"""
        import socket

        listener = docker_provider._start_probe_listener()
        self.assertTrue(listener["ok"])
        socket.create_connection(("127.0.0.1", listener["port"]), timeout=2).close()
        with mock.patch.object(socket, "create_connection", side_effect=ConnectionRefusedError(
                errno.ECONNREFUSED, "synthetic refusal")):
            result = docker_provider._stop_probe_listener(listener)
        self.assertTrue(result["confirmed"], result)
        self.assertIs(result["port_free"], True)
        self.assertIn("明确拒绝", result["port_free_reason"] or "")

    @unittest.skipIf(os.name == "nt", "POSIX 分支:connect 明确拒绝是主信号,无兜底"
                                        "(与网页审计 Linux 重放 P06 同一路径)")
    def test_observation_emfile_reported_unknown_posix(self):
        """R02b(P06 镜像,POSIX):真实监听/线程正常停止,仅端口观测注入 EMFILE →
        "unknown" 而非 True;错误类别/errno 记入 reason;confirmed=False。"""
        import socket

        listener = docker_provider._start_probe_listener()
        self.assertTrue(listener["ok"])
        socket.create_connection(("127.0.0.1", listener["port"]), timeout=2).close()
        with mock.patch.object(socket, "create_connection", side_effect=OSError(
                errno.EMFILE, "synthetic observation resource error")):
            result = docker_provider._stop_probe_listener(listener)
        self.assertTrue(result["socket_closed"])
        self.assertTrue(result["thread_stopped"], "真实线程已停;未知的只是端口观测")
        self.assertEqual(result["port_free"], "unknown",
                         "观测资源异常不得当作'端口已释放'的肯定证据")
        self.assertFalse(result["confirmed"])
        # 诊断合同:错误类别(OSError)与错误号(errno=24=EMFILE)如实留痕,
        # 不依赖异常文案含符号名"EMFILE"。
        self.assertIn("observation_oserror:", result["port_free_reason"] or "")
        self.assertIn("OSError", result["port_free_reason"] or "")
        self.assertIn("errno=24", result["port_free_reason"] or "")

    @unittest.skipIf(os.name == "nt", "POSIX 分支:观测超时不做兜底二次猜测")
    def test_observation_timeout_reported_unknown_posix(self):
        """R02b(P06 镜像,POSIX):端口观测注入 TimeoutError → "unknown" 而非 True。"""
        import socket

        listener = docker_provider._start_probe_listener()
        self.assertTrue(listener["ok"])
        socket.create_connection(("127.0.0.1", listener["port"]), timeout=2).close()
        with mock.patch.object(socket, "create_connection",
                               side_effect=TimeoutError("synthetic observation timeout")):
            result = docker_provider._stop_probe_listener(listener)
        self.assertTrue(result["socket_closed"])
        self.assertTrue(result["thread_stopped"])
        self.assertEqual(result["port_free"], "unknown")
        self.assertFalse(result["confirmed"])
        self.assertIn("observation_oserror", result["port_free_reason"] or "")

    @unittest.skipUnless(os.name == "nt", "Windows 实测:回环栈对已关闭监听端口静默丢"
                                          "SYN,connect 稳定超时;bind 探测是平台确定性信号")
    def test_observation_timeout_windows_bind_probe_gives_explicit_verdict(self):
        """R02b(Windows 平台路径):connect 观测超时 → bind 探测给出内核明确结论
        (非把超时当证据);结论来源记录在 port_free_reason 供核查。"""
        import socket

        listener = docker_provider._start_probe_listener()
        self.assertTrue(listener["ok"])
        socket.create_connection(("127.0.0.1", listener["port"]), timeout=2).close()
        with mock.patch.object(socket, "create_connection",
                               side_effect=TimeoutError("synthetic observation timeout")):
            result = docker_provider._stop_probe_listener(listener)
        self.assertTrue(result["socket_closed"])
        self.assertTrue(result["thread_stopped"])
        self.assertIs(result["port_free"], True,
                      "结论来自 bind 探测的内核明确信号,而非超时本身")
        self.assertTrue(result["confirmed"], result)
        self.assertIn("bind_probe", result["port_free_reason"] or "")

    @unittest.skipUnless(os.name == "nt", "Windows 版:资源异常须同时打掉两个观测面"
                                          "(connect 与 bind 探测)才如实 unknown")
    def test_observation_emfile_all_probes_down_reported_unknown_windows(self):
        """R02b(Windows 负例):真实 EMFILE 会耗尽一切新 socket 创建——两个观测面
        同时失败 → "unknown";confirmed=False,不当作已释放。"""
        import socket

        listener = docker_provider._start_probe_listener()
        self.assertTrue(listener["ok"])
        socket.create_connection(("127.0.0.1", listener["port"]), timeout=2).close()
        emfile = OSError(errno.EMFILE, "synthetic host resource exhaustion")
        with mock.patch.object(socket, "create_connection", side_effect=emfile), \
             mock.patch.object(socket, "socket", side_effect=emfile):
            result = docker_provider._stop_probe_listener(listener)
        self.assertTrue(result["socket_closed"])
        self.assertTrue(result["thread_stopped"])
        self.assertEqual(result["port_free"], "unknown")
        self.assertFalse(result["confirmed"])
        self.assertIn("errno=24", result["port_free_reason"] or "",
                      "两个观测面的失败类别与 errno 都要留痕(EMFILE=24)")

    def test_observation_refused_windows_real_stop_confirmed(self):
        """Windows 真实正样本(P05 等价):真实监听/线程停止、connect 超时 →
        bind 兜底给出明确结论 → confirmed=True(平台实测记录)。"""
        import socket

        listener = docker_provider._start_probe_listener()
        self.assertTrue(listener["ok"])
        socket.create_connection(("127.0.0.1", listener["port"]), timeout=2).close()
        result = docker_provider._stop_probe_listener(listener)
        self.assertTrue(result["confirmed"], result)
        self.assertIs(result["port_free"], True)


class ListenerCleanupFailClosedTests(_EnvBase):
    """R02b 接线:收尾未确认 → probe_boundaries 记 problem、不签发资格。"""

    def test_unconfirmed_cleanup_blocks_entitlement(self):
        for key in ("OPENCODING_DOCKER_PROBE_HOST", "OPENCODING_DOCKER_PROBE_PORT"):
            os.environ.pop(key, None)
        fake_listener = {"ok": True, "socket": None, "port": 59999,
                         "host": "host.docker.internal", "thread": None,
                         "serving": {"open": True}}
        unconfirmed = {"socket_closed": True, "thread_stopped": False,
                       "port_free": False, "confirmed": False}
        with _Ready():
            with mock.patch.object(docker_provider, "_start_probe_listener",
                                   return_value=fake_listener), \
                 mock.patch.object(docker_provider, "_stop_probe_listener",
                                   return_value=unconfirmed):
                result = docker_provider.probe_boundaries(
                    self.root, runner=_ControlReachable())
        self.assertFalse(result["granted"])
        self.assertIn("listener_cleanup_unconfirmed", result["reason"])
        self.assertEqual(result["listener_cleanup"], unconfirmed)

    def test_confirmed_cleanup_recorded_in_result(self):
        """收尾确认 → 结论随结果返回(证据可核),资格照常判定。"""
        for key in ("OPENCODING_DOCKER_PROBE_HOST", "OPENCODING_DOCKER_PROBE_PORT"):
            os.environ.pop(key, None)
        with _Ready():
            result = docker_provider.probe_boundaries(
                self.root, runner=_ControlReachable())
        self.assertTrue(result["granted"], result["reason"])
        self.assertTrue(result["listener_cleanup"]["confirmed"])

    @unittest.skipIf(os.name == "nt", "POSIX 分支:观测异常无兜底,单点注入即 unknown"
                                        "(与网页审计 Linux 重放 P07 同一路径)")
    def test_unknown_observation_blocks_entitlement_posix(self):
        """R02b(P07 镜像,POSIX):真实监听 + 端口观测注入 EMFILE → 分类产出
        "unknown"/confirmed=False → probe_boundaries 记 listener_cleanup_unconfirmed、
        不签发资格(不整体替换收尾函数,只注入观测异常)。"""
        import socket

        for key in ("OPENCODING_DOCKER_PROBE_HOST", "OPENCODING_DOCKER_PROBE_PORT"):
            os.environ.pop(key, None)
        with _Ready():
            with mock.patch.object(socket, "create_connection", side_effect=OSError(
                    errno.EMFILE, "synthetic cleanup observation failure")):
                result = docker_provider.probe_boundaries(
                    self.root, runner=_ControlReachable())
        self.assertFalse(result["granted"], result["reason"])
        self.assertIsNone(result["entitlement_id"])
        self.assertEqual(result["listener_cleanup"]["port_free"], "unknown")
        self.assertFalse(result["listener_cleanup"]["confirmed"])
        self.assertIn("listener_cleanup_unconfirmed", result["reason"])

    @unittest.skipUnless(os.name == "nt", "Windows 版:真实资源异常会同时打掉 connect"
                                          "与 bind 两个观测面")
    def test_unknown_observation_blocks_entitlement_windows(self):
        """R02b(P07 镜像,Windows):双观测面同时失败(EMFILE)→ unknown/
        confirmed=False → probe_boundaries 不签发资格。监听自身建立放行一次
        (首次 socket() 真实),其后的观测面全部 EMFILE。"""
        import socket

        for key in ("OPENCODING_DOCKER_PROBE_HOST", "OPENCODING_DOCKER_PROBE_PORT"):
            os.environ.pop(key, None)
        real_socket = socket.socket
        state = {"n": 0}

        def flaky_socket(*args, **kwargs):
            state["n"] += 1
            if state["n"] <= 1:  # 首次:允许本批一次性监听建立
                return real_socket(*args, **kwargs)
            raise OSError(errno.EMFILE, "synthetic host resource exhaustion")

        with _Ready():
            with mock.patch.object(socket, "create_connection", side_effect=OSError(
                    errno.EMFILE, "synthetic host resource exhaustion")), \
                 mock.patch.object(socket, "socket", side_effect=flaky_socket):
                result = docker_provider.probe_boundaries(
                    self.root, runner=_ControlReachable())
        self.assertGreaterEqual(state["n"], 2, "监听建立后观测面确实被注入")
        self.assertFalse(result["granted"], result["reason"])
        self.assertIsNone(result["entitlement_id"])
        self.assertEqual(result["listener_cleanup"]["port_free"], "unknown")
        self.assertFalse(result["listener_cleanup"]["confirmed"])
        self.assertIn("listener_cleanup_unconfirmed", result["reason"])


# ---------------------------------------------------------------- 生命周期一致性

class LifecycleConsistencyTests(_EnvBase):
    """completion_consistent 必须如实汇总四环;sandbox 对不一致拒绝交付。"""

    def test_consistent_completion_is_true(self):
        outcome = docker_provider.run_container(
            self.root, ["python", "app/selftest.py"],
            entitlement_required=False, runner=_FakeDocker())
        life = outcome.container
        self.assertTrue(life["completion_consistent"], life)
        self.assertTrue(life["confirmed_stopped"])
        self.assertTrue(life["confirmed_removed"])
        self.assertEqual(life["exit_code"], 0)

    def test_stop_unconfirmed_makes_completion_inconsistent(self):
        outcome = docker_provider.run_container(
            self.root, ["python", "app/selftest.py"],
            entitlement_required=False,
            runner=_FakeDocker(inspect_state="true"))
        life = outcome.container
        self.assertFalse(life["confirmed_stopped"])
        self.assertFalse(life["completion_consistent"])

    def test_remove_unconfirmed_makes_completion_inconsistent(self):
        outcome = docker_provider.run_container(
            self.root, ["python", "app/selftest.py"],
            entitlement_required=False,
            runner=_FakeDocker(rm_succeeds=False))
        life = outcome.container
        self.assertFalse(life["confirmed_removed"])
        self.assertFalse(life["completion_consistent"])

    def _sandbox_result(self, consistent: bool):
        life = {
            "container_name": "opencoding-cand-fake",
            "create_returncode": 0,
            "confirmed_stopped": True,
            "confirmed_removed": True if consistent else False,
            "exit_code": 0,
            "completion_consistent": consistent,
        }
        result = docker_provider.ContainerRunResult(
            ["docker", "run"], 0 if consistent else -1, "out", "", container=life)
        capability = {"kind": "docker", "available": True, "verified_boundaries": True,
                      "provider": "docker", "command": [], "reason": "替身资格"}
        return result, capability

    def test_sandbox_refuses_inconsistent_container(self):
        """U06-5.3:生命周期未核实一致 → 不得当作可供交付的成功。"""
        result, capability = self._sandbox_result(consistent=False)
        with mock.patch.object(sandbox, "execution_capability",
                               return_value=capability), \
             mock.patch.object(docker_provider, "run_container", return_value=result):
            with self.assertRaises(sandbox.SandboxError) as ctx:
                sandbox.run(["python", "candidate.py"], cwd=self.root,
                            guard_dir=self.root / "guard")
        self.assertEqual(ctx.exception.code, "docker_lifecycle_unverified")
        self.assertIn("生命周期未完整核实", ctx.exception.args[0])

    def test_sandbox_accepts_consistent_container(self):
        result, capability = self._sandbox_result(consistent=True)
        with mock.patch.object(sandbox, "execution_capability",
                               return_value=capability), \
             mock.patch.object(docker_provider, "run_container", return_value=result):
            outcome = sandbox.run(["python", "candidate.py"], cwd=self.root,
                                  guard_dir=self.root / "guard")
        self.assertIs(outcome, result)


# ---------------------------------------------------------------- 就绪措辞

class ReadinessWordingTests(unittest.TestCase):
    def test_engine_unreachable_means_image_not_checked(self):
        with _Ready():
            docker_provider.daemon_reachable = lambda timeout=10: (False, "引擎不可达")
            detail = docker_provider._readiness_detail()
        self.assertFalse(detail["engine"])
        self.assertFalse(detail["image_present"])
        self.assertIn("未核对镜像", detail["image_message"])
        self.assertFalse(detail["ready"])


if __name__ == "__main__":
    unittest.main()
