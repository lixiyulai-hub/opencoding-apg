# -*- coding: utf-8 -*-
"""docker_provider 适配层测试(命令构建/映射/执行准入;不要求引擎运行)。

CP5 新增场景对应 01 审核 §3.2/§3.3/§3.4 与本批次004differences:
- 环境就绪(CLI/引擎/镜像齐备)不得单独获得派发权限;
- 业务写区不得覆盖冻结规范/检查器/控制区(T07 反例 data_dir=frozen_checks);
- 探针必须区分"策略拒绝"与"路径缺失/本来不可达";
- 容器超时/取消后必须核实已停止并已移除,不能只依赖 --rm。
全部用可控替身(fake runner + 明确的就绪替身),不启动引擎、不拉镜像、不连外部地址。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from opencoding import docker_provider, sandbox


WINDOWS_ONLY = unittest.skipUnless(os.name == "nt", "路径映射断言针对 Windows 宿主;Linux 语义不同")


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

    def __call__(self, cmd, **_kwargs):
        self.calls.append(list(cmd))
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "run":
            return subprocess.CompletedProcess(cmd, 0, "opencoding-cid-abcdef\n", "")
        if sub == "wait":
            return subprocess.CompletedProcess(cmd, 0, "0\n", "")
        if sub == "inspect":
            self._inspect_calls += 1
            if self._inspect_calls == 1:
                return subprocess.CompletedProcess(cmd, 0, self.inspect_state + "\n", "")
            # rm 之后的 inspect:容器应已不存在(returncode != 0)
            return subprocess.CompletedProcess(cmd, 1 if self.rm_succeeds else 0, "", "")
        if sub == "logs":
            return subprocess.CompletedProcess(cmd, 0, "PROBE:" + json.dumps(self.probe), "")
        if sub == "rm":
            return subprocess.CompletedProcess(cmd, 0 if self.rm_succeeds else 1, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


class _Ready:
    """明确替身:CLI/引擎/本地镜像/镜像身份齐备(不代表边界已验证)。"""

    def __init__(self, module, image_id="sha256:cafebabe1234"):
        self.module = module
        self.image_id = image_id
        self.saved = None

    def __enter__(self):
        m = self.module
        self.saved = (m.docker_cli_available, m.daemon_reachable,
                      m.image_present_locally, m.image_id)
        m.docker_cli_available = lambda: True
        m.daemon_reachable = lambda timeout=10: (True, "替身引擎可用")
        m.image_present_locally = lambda image=None: (True, "替身镜像存在")
        m.image_id = lambda image=None: (self.image_id, "替身镜像身份")
        return self

    def __exit__(self, *exc):
        (self.module.docker_cli_available, self.module.daemon_reachable,
         self.module.image_present_locally, self.module.image_id) = self.saved
        return False


class DockerProviderBuildTests(unittest.TestCase):
    @WINDOWS_ONLY
    def test_build_run_command_boundaries(self):
        cmd = docker_provider.build_run_command(
            r"D:\OpenCoding-dev\workspace\p\.opencoding\generic_runs\gen-x-candidate-attempt1",
            ["python", "frozen_checks/app_contract_v2.py", "main"])
        self.assertEqual(cmd[0], "docker")
        self.assertIn("--network", cmd)
        self.assertEqual(cmd[cmd.index("--network") + 1], "none")
        self.assertIn("--read-only", cmd)
        self.assertIn("--pull", cmd)
        self.assertEqual(cmd[cmd.index("--pull") + 1], "never")
        # CP5 §3.3:写区显式标注 rw,其余明确 ro(不依赖"没有 ro 就是可写"的隐含语义)
        mounts = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-v"]
        self.assertTrue(mounts[0].endswith(":/work:ro"), mounts)
        self.assertTrue(any(m.endswith(":/work/app/data:rw") for m in mounts), mounts)
        self.assertEqual(cmd[-4:], ["python:3.11-slim", "python",
                                    "frozen_checks/app_contract_v2.py", "main"])

    @WINDOWS_ONLY
    def test_build_run_command_custom_data_dir(self):
        cmd = docker_provider.build_run_command(
            r"D:\tmp\scratch", ["python", "x.py"], data_dir="app/store")
        mounts = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-v"]
        self.assertTrue(any(m.endswith(":/work/app/store:rw") for m in mounts), mounts)

    def test_diagnose_readonly_shape(self):
        d = docker_provider.diagnose()
        self.assertIn("cli_available", d)
        self.assertIn("environment_ready", d)
        self.assertEqual(d["verified_boundaries"], d["available"])

    @WINDOWS_ONLY
    def test_unc_rejected(self):
        with self.assertRaises(ValueError):
            docker_provider.build_run_command("\\\\srv\\share\\x", ["python"])

    @WINDOWS_ONLY
    def test_container_argv_maps_interpreter(self):
        out = docker_provider.container_argv([sys.executable, "-X", "utf8", "app/selftest.py"])
        self.assertEqual(out[0], "python")
        self.assertEqual(out[-1], "app/selftest.py")

    def test_execution_capability_shape(self):
        cap = docker_provider.execution_capability()
        self.assertIn(cap["kind"], ("docker", "docker_unverified", "docker_unavailable"))
        self.assertIsInstance(cap["available"], bool)
        # 关键合同:available 与 verified_boundaries 必须一致,不得出现"可用但未验证"
        self.assertEqual(bool(cap["available"]), bool(cap.get("verified_boundaries")))
        self.assertIn("environment_ready", cap)


class DockerWriteAreaTests(unittest.TestCase):
    """CP5 §3.3/T07:业务写区不得覆盖检查区、控制区或 scratch 整体。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oc-docker-wa-")
        self.root = Path(self.tmp.name) / "scratch"
        (self.root / "app" / "data").mkdir(parents=True, exist_ok=True)
        (self.root / "frozen_checks").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_normal_business_data_dir_allowed(self):
        rel, refusal = docker_provider.validate_data_dir(self.root, "app/data")
        self.assertIsNone(refusal)
        self.assertEqual(rel, "app/data")

    def test_frozen_checks_rejected(self):
        _, refusal = docker_provider.validate_data_dir(self.root, "frozen_checks")
        self.assertIsNotNone(refusal)
        with self.assertRaises(ValueError):
            docker_provider.build_run_command(self.root, ["python", "x.py"],
                                              data_dir="frozen_checks")

    def test_control_dir_rejected(self):
        _, refusal = docker_provider.validate_data_dir(self.root, ".opencoding")
        self.assertIsNotNone(refusal)

    def test_whole_scratch_rejected(self):
        for bad in (".", "", "..", "/"):
            _, refusal = docker_provider.validate_data_dir(self.root, bad)
            self.assertIsNotNone(refusal, bad)

    def test_code_root_rejected(self):
        _, refusal = docker_provider.validate_data_dir(self.root, "app")
        self.assertIsNotNone(refusal)

    def test_escape_rejected(self):
        _, refusal = docker_provider.validate_data_dir(self.root, "../outside/data")
        self.assertIsNotNone(refusal)

    def test_protected_file_rejected(self):
        _, refusal = docker_provider.validate_data_dir(self.root, "spec.json")
        self.assertIsNotNone(refusal)

    def test_unverified_env_is_not_dispatchable(self):
        with _Ready(docker_provider):
            cap = docker_provider.execution_capability()
        self.assertTrue(cap["environment_ready"])
        self.assertFalse(cap["available"], "仅环境就绪不得具备未知候选执行资格")
        self.assertFalse(cap["verified_boundaries"])
        self.assertEqual(cap["kind"], "docker_unverified")

    def test_run_container_refused_without_entitlement(self):
        fake = _FakeDocker()
        with _Ready(docker_provider):
            with self.assertRaises(RuntimeError) as ctx:
                docker_provider.run_container(self.root, ["python", "app/selftest.py"],
                                              data_dir="app/data", runner=fake)
        self.assertIn("execution_entitlement_missing", str(ctx.exception))
        self.assertEqual(fake.calls, [], "无资格不得接触任何 docker 调用")


class DockerProbeEntitlementTests(unittest.TestCase):
    """CP5 §3.4:探针必须接进准入,并区分策略拒绝与其他故障。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oc-docker-ent-")
        self.root = Path(self.tmp.name) / "scratch"
        (self.root / "app" / "data").mkdir(parents=True, exist_ok=True)
        (self.root / "frozen_checks").mkdir(parents=True, exist_ok=True)
        self.prev_ent_dir = os.environ.get("OPENCODING_DOCKER_ENTITLEMENT_DIR")
        self.prev_ctl = {k: os.environ.get(k) for k in ("OPENCODING_DOCKER_PROBE_HOST",
                                                        "OPENCODING_DOCKER_PROBE_PORT")}
        os.environ["OPENCODING_DOCKER_ENTITLEMENT_DIR"] = str(Path(self.tmp.name) / "ent")
        os.environ.pop("OPENCODING_DOCKER_PROBE_HOST", None)
        os.environ.pop("OPENCODING_DOCKER_PROBE_PORT", None)

    def tearDown(self):
        for key, value in self.prev_ctl.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self.prev_ent_dir is None:
            os.environ.pop("OPENCODING_DOCKER_ENTITLEMENT_DIR", None)
        else:
            os.environ["OPENCODING_DOCKER_ENTITLEMENT_DIR"] = self.prev_ent_dir
        self.tmp.cleanup()

    def test_default_listener_path_and_probe_skip_still_fail_closed(self):
        """FIX-02:未配置目标时默认自建一次性监听做 A/B;探针跳过网络项仍 fail-closed。"""
        # (a) 默认路径:监听就位 + 对照可达 + 受限组失败 → 允许签发
        probe = _ok_probe()
        probe["network"] = {"status": "failed:oserror:113"}

        class _Ctl(_FakeDocker):
            def __call__(self, cmd, **kwargs):
                if len(cmd) > 1 and cmd[1] == "run" and "--network" in cmd \
                        and cmd[cmd.index("--network") + 1] == "bridge":
                    return subprocess.CompletedProcess(
                        cmd, 0, "CONTROL:" + json.dumps({"status": "connected", "peer_ip": "192.168.65.254"}), "")
                return super().__call__(cmd, **kwargs)

        with _Ready(docker_provider):
            result = docker_provider.probe_boundaries(self.root, runner=_Ctl(probe))
        self.assertTrue(result["granted"], result["reason"])
        self.assertEqual(result["control"]["network"]["target"]["host"],
                         "host.docker.internal")

        # (b) 探针没有执行网络项(报告 skipped)而对照已执行 → fail-closed
        fake = _Ctl(_ok_probe(skip_network=True))
        with _Ready(docker_provider):
            result = docker_provider.probe_boundaries(self.root, runner=fake)
        self.assertFalse(result["granted"])
        self.assertIn("network=skipped", result["reason"])

    def test_path_missing_is_not_strategy_denial(self):
        probe = _ok_probe()
        probe["spec_readonly"] = {"status": "failed:path_missing"}
        fake = _FakeDocker(probe)
        with _Ready(docker_provider):
            result = docker_provider.probe_boundaries(self.root, runner=fake)
        self.assertFalse(result["granted"])
        self.assertIn("spec_readonly=failed:path_missing", result["reason"])

    def test_network_probe_runs_when_control_target_given(self):
        os.environ["OPENCODING_DOCKER_PROBE_HOST"] = "127.0.0.1"
        os.environ["OPENCODING_DOCKER_PROBE_PORT"] = "9"
        probe = _ok_probe()
        probe["network"] = {"status": "failed:oserror:113"}
        fake = _FakeDocker(probe)

        class _ControlReachable(_FakeDocker):
            def __call__(self, cmd, **kwargs):
                if len(cmd) > 1 and cmd[1] == "run" and "--network" in cmd:
                    index = cmd.index("--network")
                    if cmd[index + 1] == "bridge":
                        return subprocess.CompletedProcess(
                            cmd, 0, "CONTROL:" + json.dumps({"status": "connected", "peer_ip": "192.168.65.254"}), "")
                return super().__call__(cmd, **kwargs)

        with _Ready(docker_provider):
            result = docker_provider.probe_boundaries(self.root, runner=_ControlReachable(probe))
            self.assertTrue(result["granted"], result["reason"])
            self.assertTrue(result["control"]["network"]["control_reachable"])
            self.assertTrue(result["entitlement_id"])
            # 资格签发后能力才为真,且绑定镜像身份与策略
            cap = docker_provider.execution_capability()
        self.assertTrue(cap["available"])
        self.assertTrue(cap["verified_boundaries"])
        self.assertEqual(cap["entitlement_id"], result["entitlement_id"])

    def test_network_dns_failure_is_a_valid_none_network_denial(self):
        """DNS failure under --network none is a valid isolation refusal."""
        probe = _ok_probe()
        probe["network"] = {"status": "failed:oserror:-3"}
        fake = _FakeDocker(probe)

        class _ControlReachable(_FakeDocker):
            def __call__(self, cmd, **kwargs):
                if len(cmd) > 1 and cmd[1] == "run" and "--network" in cmd:
                    index = cmd.index("--network")
                    if cmd[index + 1] == "bridge":
                        return subprocess.CompletedProcess(
                            cmd, 0, "CONTROL:" + json.dumps({"status": "connected", "peer_ip": "192.168.65.254"}), "")
                return super().__call__(cmd, **kwargs)

        with _Ready(docker_provider):
            result = docker_provider.probe_boundaries(
                self.root, runner=_ControlReachable(probe))
        self.assertTrue(result["granted"], result["reason"])

    def test_entitlement_expires_and_cannot_be_faked(self):
        with _Ready(docker_provider, image_id="sha256:aaaa"):
            digest = docker_provider.policy_digest(docker_provider._image(), "sha256:aaaa")
            docker_provider.grant_entitlement(digest, {"image_id": "sha256:aaaa"})
            self.assertIsNotNone(docker_provider.load_entitlement(digest))
            # 镜像身份变化 → 旧资格立即失效
            other = docker_provider.policy_digest(docker_provider._image(), "sha256:bbbb")
            self.assertIsNone(docker_provider.load_entitlement(other))
        with _Ready(docker_provider, image_id="sha256:cccc"):
            # 未通过探针不得产生资格
            self.assertIsNone(docker_provider.current_entitlement())
            self.assertFalse(docker_provider.execution_capability()["available"])

    def test_run_container_uses_entitlement_and_confirms_lifecycle(self):
        with _Ready(docker_provider, image_id="sha256:ddddeee") as ready:
            self.assertFalse(docker_provider.execution_capability()["available"])
            # 越权取得资格(模拟配置写入而非实测)必须被 reject:这里走正规探针
            os.environ["OPENCODING_DOCKER_PROBE_HOST"] = "127.0.0.1"
            os.environ["OPENCODING_DOCKER_PROBE_PORT"] = "9"

            class _Ctl(_FakeDocker):
                def __call__(self, cmd, **kwargs):
                    if len(cmd) > 1 and cmd[1] == "run" and cmd[cmd.index("--network") + 1] == "bridge":
                        return subprocess.CompletedProcess(
                            cmd, 0, "CONTROL:" + json.dumps({"status": "connected", "peer_ip": "192.168.65.254"}), "")
                    return super().__call__(cmd, **kwargs)

            probe_result = docker_provider.probe_boundaries(self.root, runner=_Ctl())
            self.assertTrue(probe_result["granted"], probe_result["reason"])
            fake = _FakeDocker()
            outcome = docker_provider.run_container(
                self.root, ["python", "app/selftest.py"], data_dir="app/data", runner=fake)
            lifecycle = outcome.container
            self.assertTrue(lifecycle["confirmed_stopped"], lifecycle)
            self.assertTrue(lifecycle["confirmed_removed"], lifecycle)
            self.assertTrue(any(c[1] == "rm" for c in [tuple(x[:2]) for x in fake.calls] + []),
                            fake.calls)
            names = [x for x in fake.calls if len(x) > 2 and x[1] == "run" and "--name" in x]
            self.assertEqual(len(names), 1)
            self.assertTrue(names[0][names[0].index("--name") + 1].startswith("opencoding-"))
            self.assertIn("-d", names[0])
            # 写区非法时即使有资格也必须拒绝
            with self.assertRaises(ValueError):
                docker_provider.run_container(self.root, ["python", "x.py"],
                                              data_dir="frozen_checks", runner=_FakeDocker())
            self.assertEqual(ready.image_id, "sha256:ddddeee")

    def test_stop_not_confirmed_blocks_grant(self):
        """容器在超时后仍未核到停止 → 不得签发资格。"""
        probe = _ok_probe()
        os.environ["OPENCODING_DOCKER_PROBE_HOST"] = "127.0.0.1"
        os.environ["OPENCODING_DOCKER_PROBE_PORT"] = "9"

        class _RunningForever(_FakeDocker):
            def __init__(self, probe):
                super().__init__(probe, inspect_state="true")

            def __call__(self, cmd, **kwargs):
                if len(cmd) > 1 and cmd[1] == "run" and cmd[cmd.index("--network") + 1] == "bridge":
                    return subprocess.CompletedProcess(
                        cmd, 0, "CONTROL:" + json.dumps({"status": "connected", "peer_ip": "192.168.65.254"}), "")
                return super().__call__(cmd, **kwargs)

        with _Ready(docker_provider, image_id="sha256:running"):
            result = docker_provider.probe_boundaries(self.root, runner=_RunningForever(probe))
        self.assertFalse(result["granted"])
        self.assertIn("container_stop_not_confirmed", result["reason"])


class RemoveStateThreeStateTests(unittest.TestCase):
    """FIX-02(U06-5.4)/审计 V08:移除结论按可核对证据三分,不许混淆。

    removed       = rm 明确成功且随后查询证实容器不存在;
    still_present = 查询成功且容器仍在;
    unverified    = rm 失败且查询也失败——效果未知,禁止当成已移除。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oc-docker-rm3-")
        self.root = Path(self.tmp.name) / "scratch"
        (self.root / "app" / "data").mkdir(parents=True, exist_ok=True)
        (self.root / "frozen_checks").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _lifecycle(self, fake):
        outcome = docker_provider.run_container(
            self.root, ["python", "app/selftest.py"],
            entitlement_required=False, runner=fake)
        return outcome.container

    def test_normal_path_records_removed(self):
        life = self._lifecycle(_FakeDocker())
        self.assertEqual(life["remove_state"], "removed")
        self.assertTrue(life["confirmed_removed"])
        self.assertTrue(life["completion_consistent"], life)

    def test_rm_failure_with_inspect_ok_is_still_present(self):
        life = self._lifecycle(_FakeDocker(rm_succeeds=False))
        self.assertEqual(life["remove_state"], "still_present")
        self.assertFalse(life["confirmed_removed"])
        self.assertFalse(life["completion_consistent"])

    def test_rm_and_inspect_both_fail_is_unverified_not_removed(self):
        """V08 反例:rm 失败 + 引擎/查询也失败 → unverified,禁止当已移除。"""

        class _EngineDown(_FakeDocker):
            def __call__(self, cmd, **kwargs):
                if len(cmd) > 1 and cmd[1] == "inspect":
                    self._inspect_calls += 1
                    if self._inspect_calls == 1:
                        return subprocess.CompletedProcess(cmd, 0, "false\n", "")
                    return subprocess.CompletedProcess(
                        cmd, 1, "", "Cannot connect to the Docker daemon")
                return super().__call__(cmd, **kwargs)

        life = self._lifecycle(_EngineDown(rm_succeeds=False))
        self.assertEqual(life["remove_state"], "unverified")
        self.assertFalse(life["confirmed_removed"])
        self.assertFalse(life["completion_consistent"])
        detail = life.get("remove_unverified_detail") or {}
        self.assertEqual(detail.get("rm_returncode"), 1)
        self.assertEqual(detail.get("inspect_returncode"), 1)
        self.assertIn("未核实", str(detail.get("note", "")))

    def test_sandbox_refuses_unverified_removal(self):
        """unverified 的容器不得作为可供交付的成功进入业务。"""
        life = {
            "container_name": "opencoding-cand-fake",
            "create_returncode": 0,
            "confirmed_stopped": True,
            "confirmed_removed": False,
            "remove_state": "unverified",
            "exit_code": 0,
            "completion_consistent": False,
        }
        result = docker_provider.ContainerRunResult(
            ["docker", "run"], -1, "out", "", container=life)
        capability = {"kind": "docker", "available": True, "verified_boundaries": True,
                      "provider": "docker", "command": [], "reason": "替身资格"}
        with mock.patch.object(sandbox, "execution_capability",
                               return_value=capability), \
             mock.patch.object(docker_provider, "run_container", return_value=result):
            with self.assertRaises(sandbox.SandboxError) as ctx:
                sandbox.run(["python", "candidate.py"], cwd=self.root,
                            guard_dir=self.root / "guard")
        self.assertEqual(ctx.exception.code, "docker_lifecycle_unverified")
        self.assertIn("生命周期未完整核实", ctx.exception.args[0])


if __name__ == "__main__":
    unittest.main()
