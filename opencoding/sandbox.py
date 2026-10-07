"""受控执行环境：候选代码与验收器子进程的真实路径与网络限制（F07/S01）。

设计边界（如实说明，C01 复核后收窄定位）：
- 这是一个**进程内的辅助拦截层**，不是操作系统级沙箱，也**不宣称**能限制
  任意未知 Python 代码的全部行为。它通过解释器启动时加载的 ``sitecustomize.py``
  改写已知文件写入入口（``builtins.open``/``io.open``/``_io.open``/``os.open``
  及 ``os`` 写操作族）与网络出口，对以 ``sys.executable`` 启动的 Python 子进程生效。
- 守卫的完整性**不由接口清单声明，而由 ``self_check`` 实测保证**：每次使用前用
  真实子进程验证越界写、低层接口写、pathlib 写、控制区写与网络全部被拒绝；
  任一探针未拒绝即判定环境不可用（fail-closed），调用方必须**拒绝执行未知生成
  代码**并冻结相关任务，不得换目录、放宽或降级后继续自动执行。
- 边界不可用时，未知候选的自动运行一律受阻；静态检查、协议与不执行候选的
  流程不受影响。
- 可写路径只限于本次验证的工作副本；控制元数据区（``.opencoding``）除本次
  工作副本外一律禁止读写；网络出口一律拒绝。
- 若自检发现限制没有生效，调用方必须**拒绝在真实项目中执行未知生成代码**，
  不得改为“换个临时目录继续跑”或放宽检查。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping


class SandboxError(ValueError):
    """A fail-closed sandbox error with a stable machine code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_GUARD_TEMPLATE = '''"""OpenCoding 受控执行环境守卫（解释器启动时加载，勿手工修改）。"""
import builtins
import io
import json
import os
import socket
import sys

_CONFIG = json.loads({config!r})
_WRITE_ROOT = _CONFIG["write_root"]
_PROTECTED = _CONFIG["protected"]
_READ_EXEMPT = _CONFIG["read_exempt"]
_TOKEN = _CONFIG["token"]


class SandboxViolation(PermissionError):
    """受控执行环境拒绝该操作。"""


def _real(path):
    try:
        return os.path.realpath(os.path.abspath(str(path)))
    except Exception:
        return None


def _under(child, parent):
    if not child or not parent:
        return False
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _write_denied(path):
    target = _real(path)
    if target is None:
        return True
    if _under(target, _WRITE_ROOT):
        return False
    return True


def _read_denied(path):
    target = _real(path)
    if target is None:
        return False
    for exempt in _READ_EXEMPT:
        if _under(target, exempt):
            return False
    for root in _PROTECTED:
        if _under(target, root):
            return True
    return False


def _probe(path, *, write):
    denied = _write_denied(path) if write else _read_denied(path)
    if denied:
        raise SandboxViolation(
            "受控执行环境拒绝" + ("写入" if write else "读取") + "：" + str(path)
        )


_open_real = builtins.open


def _guarded_open(file, mode="r", buffering=-1, *args, **kwargs):
    text = mode if isinstance(mode, str) else "r"
    write = any(flag in text for flag in ("w", "a", "x", "+"))
    _probe(file, write=write)
    return _open_real(file, mode, buffering, *args, **kwargs)


builtins.open = _guarded_open
io.open = _guarded_open
# C01（S01 残余边界）：builtins.open 与 _io.open 本是同一对象，直接 `import _io`
# 再调用 _io.open 可绕过上面的替换。把底层模块入口一并指向守卫，覆盖 pathlib 等
# 经由 _io/io 的写入路径。这仍属辅助拦截：守卫的完整性由 self_check 实测保证，
# 任何探针未被拒绝都会判定环境不可用并拒绝执行未知候选。
import _io as _io_module

_io_module.open = _guarded_open


def _flag_writes(flags):
    masks = (
        getattr(os, "O_WRONLY", 0),
        getattr(os, "O_RDWR", 0),
        getattr(os, "O_CREAT", 0),
        getattr(os, "O_TRUNC", 0),
        getattr(os, "O_APPEND", 0),
        getattr(os, "O_TMPFILE", 0),
    )
    value = int(flags)
    return any(value & mask for mask in masks if mask)


_os_open_real = os.open


def _guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):
    if dir_fd is None:
        _probe(path, write=_flag_writes(flags))
    return _os_open_real(path, flags, mode, dir_fd=dir_fd)


os.open = _guarded_os_open


def _wrap_write(name):
    real = getattr(os, name)

    def guarded(path, *args, **kwargs):
        _probe(path, write=True)
        return real(path, *args, **kwargs)

    guarded.__name__ = name
    setattr(os, name, guarded)


for _name in ("mkdir", "makedirs", "remove", "unlink", "rmdir", "truncate", "symlink", "link"):
    if hasattr(os, _name):
        _wrap_write(_name)


def _wrap_pair(name):
    real = getattr(os, name)

    def guarded(src, dst, *args, **kwargs):
        _probe(src, write=True)
        _probe(dst, write=True)
        return real(src, dst, *args, **kwargs)

    guarded.__name__ = name
    setattr(os, name, guarded)


for _name in ("rename", "replace", "renames"):
    if hasattr(os, _name):
        _wrap_pair(_name)


class _GuardedSocket(socket.socket):
    def connect(self, *args, **kwargs):
        raise SandboxViolation("受控执行环境拒绝网络出口")

    def connect_ex(self, *args, **kwargs):
        raise SandboxViolation("受控执行环境拒绝网络出口")


socket.socket = _GuardedSocket


def _guarded_create_connection(*args, **kwargs):
    raise SandboxViolation("受控执行环境拒绝网络出口")


socket.create_connection = _guarded_create_connection

sys.opencoding_sandbox = _TOKEN
'''


_GUARD_CACHE: dict[str, Path] = {}
_SELF_CHECKED: set[str] = set()

_BASE_ENV_KEYS = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "PYTHONHOME", "COMSPEC", "PATHEXT")


def _real(value: str | Path) -> str:
    return os.path.realpath(os.path.abspath(str(value)))


def build_guard(
    *,
    writable_root: Path,
    protected_roots: list[Path],
    read_exempt: list[Path] | None = None,
) -> Path:
    """生成（或复用）一个守卫目录；其 ``sitecustomize.py`` 在子进程启动时生效。"""

    config = {
        "write_root": _real(writable_root),
        "protected": [_real(item) for item in protected_roots],
        "read_exempt": [_real(item) for item in (read_exempt or [])],
        "token": uuid.uuid4().hex,
    }
    key = json.dumps(config, sort_keys=True)
    cached = _GUARD_CACHE.get(key)
    if cached is not None and (cached / "sitecustomize.py").exists():
        return cached
    directory = Path(tempfile.mkdtemp(prefix="opencoding-sandbox-"))
    (directory / "sitecustomize.py").write_text(
        _GUARD_TEMPLATE.format(config=json.dumps(config, sort_keys=True)),
        encoding="utf-8",
        newline="\n",
    )
    (directory / "config.json").write_text(json.dumps(config, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    _GUARD_CACHE[key] = directory
    return directory


def child_env(base: Mapping[str, str] | None, guard_dir: Path) -> dict[str, str]:
    """构造子进程环境：只做变量传递，限制本身由守卫在解释器内强制执行。"""

    env: dict[str, str] = {}
    for key in _BASE_ENV_KEYS:
        value = os.environ.get(key, "")
        if value:
            env[key] = value
    if base:
        env.update({str(key): str(value) for key, value in base.items()})
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["OPENCODING_SANDBOX"] = "1"
    previous = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(guard_dir) + (os.pathsep + previous if previous else "")
    return env


RESTRICTED_ENV_VARIABLE = "OPENCODING_EXECUTION_ENV"
RESTRICTED_ENV_COMMAND_VARIABLE = "OPENCODING_EXECUTION_ENV_CMD"
# N01-a：受信任通道不再使用环境变量；绑定已审查夹具身份，仅由测试代码显式传入。
REVIEWED_FIXTURES = ("tests-reviewed-synthetic-v1",)

_CAPABILITY_PROBE_SOURCE = (
    "import json, os, subprocess, sys\n"
    "out = sys.argv[1]\n"
    "control = sys.argv[2]\n"
    "host, port = sys.argv[3], int(sys.argv[4])\n"
    "result = {}\n"
    "def attempt(key, fn):\n"
    "    try:\n"
    "        fn()\n"
    "        result[key] = {'status': 'allowed'}\n"
    "    except FileNotFoundError as exc:\n"
    "        result[key] = {'status': 'failed:path_missing', 'evidence': str(exc)}\n"
    "    except ConnectionRefusedError as exc:\n"
    "        result[key] = {'status': 'failed:connection_refused', 'evidence': str(exc)}\n"
    "    except subprocess.TimeoutExpired as exc:\n"
    "        result[key] = {'status': 'failed:timeout', 'evidence': str(exc)}\n"
    "    except Exception as exc:\n"
    "        result[key] = {'status': 'denied', 'evidence': type(exc).__name__ + ': ' + str(exc)[:120]}\n"
    "def w_out():\n"
    "    with open(out, 'w') as h:\n"
    "        h.write('x')\n"
    "attempt('outside_write', w_out)\n"
    "def w_ctl():\n"
    "    with open(os.path.join(control, 'capability.marker'), 'w') as h:\n"
    "        h.write('x')\n"
    "attempt('control_write', w_ctl)\n"
    "def net():\n"
    "    import socket\n"
    "    s = socket.create_connection((host, port), 1.0)\n"
    "    s.close()\n"
    "attempt('network', net)\n"
    "def child():\n"
    "    code = \"open(%r, 'w').write('x')\" % out\n"
    "    r = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=20)\n"
    "    result.setdefault('subprocess_detail', {'rc': r.returncode, 'stderr': r.stderr.decode('utf-8', 'replace')[-120:]})\n"
    "    if r.returncode != 0:\n"
    "        raise RuntimeError('child rc=' + str(r.returncode))\n"
    "attempt('subprocess', child)\n"
    "result['outside_marker_exists'] = os.path.exists(out)\n"
    "result['control_marker_exists'] = os.path.exists(os.path.join(control, 'capability.marker'))\n"
    "print(json.dumps(result))\n"
)


def _run_probe(argv_tail: list[str], timeout: int = 120) -> tuple[int, dict[str, Any], list[str]]:
    """运行能力探针并返回 (退出码, 结构化结果, 越界标记清单)。"""

    import shlex
    import socket
    import threading
    import tempfile

    directory = Path(tempfile.mkdtemp(prefix="oc-capability-"))
    script = directory / "_capability_probe.py"
    outside = Path(tempfile.gettempdir()) / f".oc-capability-{uuid.uuid4().hex}.marker"
    control = directory / "control"
    control.mkdir(parents=True, exist_ok=True)
    script.write_text(_CAPABILITY_PROBE_SOURCE, encoding="utf-8", newline="\n")
    sink = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sink.bind(("127.0.0.1", 0))
    sink.listen(4)
    host, port = sink.getsockname()
    accepted: list[bool] = []

    def _accept_once() -> None:
        try:
            sink.settimeout(timeout)
            conn, _ = sink.accept()
            conn.close()
            accepted.append(True)
        except OSError:
            accepted.append(False)

    watcher = threading.Thread(target=_accept_once, daemon=True)
    watcher.start()
    exit_code = -1
    payload: dict[str, Any] = {}
    markers: list[str] = [str(outside), str(control / "capability.marker")]
    cleanup_error: str | None = None
    try:
        result = subprocess.run(
            argv_tail + [sys.executable, str(script), str(outside), str(control), str(host), str(port)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout + 10, check=False,
        )
        exit_code = result.returncode
        try:
            payload = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
        except (ValueError, IndexError):
            payload = {}
    except (OSError, subprocess.SubprocessError) as exc:
        exit_code = -1
        payload = {"_launch_error": str(exc)[:300]}
    watcher.join(timeout=2)
    sink.close()
    # N01-b：先核对标记，再清理；清理失败如实记录（不掩盖越界事实）。
    leaked = [m for m in markers if os.path.exists(m)]
    for m in leaked:
        try:
            os.unlink(m)
        except OSError as exc:
            cleanup_error = str(exc)
    payload["_verification"] = {
        "exit_code": exit_code,
        "leaked_markers": leaked,
        "cleanup_error": cleanup_error,
        "listener_accepted": bool(accepted) and accepted[0],
        "stderr_tail": (result.stderr or "")[-400:] if 'result' in dir() else "",
    }
    return exit_code, payload, leaked


def _checks_denied(payload: Mapping[str, Any]) -> list[str]:
    """四项检查必须全部 status=denied（策略拒绝）；failed:* 表示无法证明限制。"""

    required = ("outside_write", "control_write", "network", "subprocess")
    problems: list[str] = []
    for key in required:
        check = payload.get(key)
        status = str(check.get("status")) if isinstance(check, Mapping) else "missing"
        if status != "denied":
            problems.append(key + "=" + status)
    if payload.get("outside_marker_exists"):
        problems.append("outside_marker_leaked")
    if payload.get("control_marker_exists"):
        problems.append("control_marker_leaked")
    return problems


def _verify_provider(provider: str) -> tuple[bool, str, list[str]]:
    """实际受限执行环境验证（N01-b 强化）：

    1. A/B 对照：先以**普通解释器**跑同一探针，四项必须全部 allowed——否则环境本身
       无法证明限制存在（如端口本就连不上），直接判不可用，不做猜测。
    2. 再经 provider 启动命令跑沙箱化探针：退出码 0、四项全部 denied、无越界标记。
    3. 区分策略拒绝与其他故障：failed:* / 非零退出 / 标记泄漏一律不可用。
    """

    command = str(os.environ.get(RESTRICTED_ENV_COMMAND_VARIABLE, "")).strip()
    if not command:
        return False, "未配置实际受限执行环境的启动命令", []
    import shlex

    # Windows 路径含反斜杠，POSIX 模式会把它当转义符吃掉（N01-b 修正）。
    posix_split = os.name != "nt"
    control_code, control_payload, control_leaked = _run_probe([])
    if control_code != 0:
        return False, f"对照组运行失败（exit={control_code}），无法证明限制存在", []
    control_problems = []
    for key in ("outside_write", "control_write", "network", "subprocess"):
        check = control_payload.get(key)
        status = str(check.get("status")) if isinstance(check, Mapping) else "missing"
        if status != "allowed":
            control_problems.append(key + "=" + status)
    if control_problems:
        return False, "对照组未能全部放行（" + ",".join(control_problems) + "），无法证明限制存在", []
    # 对照组写出标记属预期（证明写入本可成功）；泄漏检查仅适用于沙箱化运行。
    provider_argv = shlex.split(command, posix=posix_split)
    sandboxed_code, sandboxed_payload, leaked = _run_probe(provider_argv)
    problems = _checks_denied(sandboxed_payload)
    verification = sandboxed_payload.get("_verification")
    if leaked:
        problems.append("observed_marker_leak=" + ",".join(sorted(leaked)))
    if isinstance(verification, Mapping) and verification.get("listener_accepted"):
        problems.append("observed_forbidden_listener_connection")
    if sandboxed_code != 0:
        problems.append(f"exit_code={sandboxed_code}")
    if problems:
        return False, "实际受限执行环境未成立：" + ",".join(problems), []
    return True, "实测通过：越界写/控制区写/网络/子进程在沙箱化运行中全部拒绝，对照组全部放行（provider=" + provider + "）", provider_argv


def execution_capability(*, guard_verified: bool = False) -> dict[str, Any]:
    """N01：把"辅助拦截可用"与"具备实际执行边界"分开记录。

    返回值 `available` 为 True 只表示**存在并实测通过**的真实受限执行环境；
    仅有辅助拦截时一律 `available=False`，调用方不得据此运行未知候选。
    `command` 仅在 available=True 时提供：能力检查与候选执行共用同一后端（N01-b）。
    """

    provider = str(os.environ.get(RESTRICTED_ENV_VARIABLE, "")).strip().lower()
    if provider == "docker":
        # CP5 §3.2:Docker 后端必须区分"环境就绪"与"执行资格已验证"。
        # docker_provider 已保证 available 只在同镜像同策略的边界实测资格有效期内为 True;
        # 这里再核一次资格本体,fail-closed:任何不一致都不给可执行结论。
        from .docker_provider import (  # noqa: PLC0415
            execution_capability as _docker_cap, load_entitlement, require_entitlement,
        )

        cap = _docker_cap()
        cap["guard_verified"] = bool(guard_verified)
        if cap.get("available"):
            try:
                require_entitlement()
            except Exception as exc:  # noqa: BLE001 - 资格缺失必须降级为不可用
                cap["available"] = False
                cap["verified_boundaries"] = False
                cap["entitlement_error"] = str(exc)[:200]
                cap["kind"] = "docker_unverified"
                cap["command"] = []
                cap["reason"] = "执行资格未通过复核(" + str(exc)[:120] + ");不得执行未知候选"
        elif isinstance(load_entitlement(str(cap.get("policy_digest") or "")), dict):
            cap["reason"] = "资格存在但与当前镜像/策略不匹配;" + str(cap.get("reason") or "")
        return cap
    if provider:
        verified, reason, provider_argv = _verify_provider(provider)
        return {
            "kind": "restricted_process" if verified else "unverified_provider",
            "available": verified,
            "provider": provider or None,
            "command": provider_argv if verified else [],
            "guard_verified": bool(guard_verified),
            "reason": reason,
        }
    return {
        "kind": "auxiliary_guard_only" if guard_verified else "unavailable",
        "available": False,
        "provider": None,
        "command": [],
        "guard_verified": bool(guard_verified),
        "reason": "本机只有进程内辅助拦截，未提供可实测的实际受限执行环境",
    }


def run(argv: list[str], *, cwd: Path, guard_dir: Path, timeout: int = 30, base_env: Mapping[str, str] | None = None, trusted_fixture: str | None = None, backend_options: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """执行一个子进程；未知候选的自动执行必须通过执行能力准入（N01）。

    - 具备实际受限执行环境（已实测）时：候选命令经**同一后端启动命令**执行（N01-b）。
      Docker 后端：整条命令交 docker_provider 构建容器运行（只读代码区+可写数据区，
      backend_options.data_dir 指定契约数据目录）。
    - 仅有辅助拦截时：默认拒绝；仅当调用方显式传入已登记的受信任夹具身份
      （`trusted_fixture ∈ REVIEWED_FIXTURES`，用于已审查的固定合成测试）才放行，
      且夹具身份记入调用记录。环境变量不再构成豁免（N01-a）。
    """

    capability = execution_capability(guard_verified=str(guard_dir) in _SELF_CHECKED)
    if capability["available"]:
        if capability.get("kind") == "docker":
            # CP5 §3.2/§3.3:资格已实测且写区经校验后才真正执行。
            from .docker_provider import run_container  # noqa: PLC0415

            data_dir = str((backend_options or {}).get("data_dir", "app/data"))
            try:
                result = run_container(cwd, list(argv), data_dir=data_dir, timeout=timeout)
            except RuntimeError as exc:  # 资格缺失/过期:等同"拒绝派发"
                raise SandboxError(
                    "execution_capability_unavailable",
                    "Docker 后端未取得有效执行资格(" + str(exc)[:160] + ");未知候选不得执行",
                ) from exc
            except ValueError as exc:  # 写区覆盖检查区/控制区(T07 反例)
                raise SandboxError("write_area_refused", "业务写区未通过覆盖检查:" + str(exc)[:200]) from exc
            # C6-02(U06-5.3):create/停止/移除/退出码任一环未核实一致,
            # 不得把结果当可供业务交付的成功——执行效果按"未知/受阻"如实上报。
            container = getattr(result, "container", {}) or {}
            if container.get("completion_consistent") is not True:
                raise SandboxError(
                    "docker_lifecycle_unverified",
                    "容器生命周期未完整核实(create/stop/remove/exit 存在矛盾:"
                    + "stopped=" + str(container.get("confirmed_stopped"))
                    + ",removed=" + str(container.get("confirmed_removed"))
                    + ");候选执行效果未知,不得作为已验证结果交付",
                )
            return result
        command = list(capability.get("command") or [])
        if command:
            argv = command + list(argv)
    elif trusted_fixture is not None:
        if trusted_fixture not in REVIEWED_FIXTURES:
            raise SandboxError(
                "execution_capability_unavailable",
                "受信任夹具身份未登记：" + str(trusted_fixture) + "；合法值：" + ",".join(REVIEWED_FIXTURES),
            )
    else:
        raise SandboxError(
            "execution_capability_unavailable",
            "未知候选的自动执行不可用（" + capability["kind"] + "）：" + capability["reason"]
            + "。静态检查、协议与中文流程不受影响；未知候选需在实际受限执行环境中运行。",
        )
    return subprocess.run(
        argv,
        cwd=str(cwd),
        env=child_env(base_env, guard_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


_SELF_CHECK_SOURCE = (
    "import json, sys\n"
    "result = {}\n"
    "try:\n"
    "    open(sys.argv[1], 'w').write('x')\n"
    "    result['outside_write'] = 'allowed'\n"
    "except Exception as exc:\n"
    "    result['outside_write'] = 'denied:' + type(exc).__name__\n"
    "try:\n"
    "    import _io\n"
    "    _io.open(sys.argv[1], 'w').write('x')\n"
    "    result['lowlevel_write'] = 'allowed'\n"
    "except Exception as exc:\n"
    "    result['lowlevel_write'] = 'denied:' + type(exc).__name__\n"
    "try:\n"
    "    import pathlib\n"
    "    pathlib.Path(sys.argv[1]).write_text('x', encoding='utf-8')\n"
    "    result['pathlib_write'] = 'allowed'\n"
    "except Exception as exc:\n"
    "    result['pathlib_write'] = 'denied:' + type(exc).__name__\n"
    "try:\n"
    "    open(sys.argv[2], 'w').write('x')\n"
    "    result['protected_write'] = 'allowed'\n"
    "except Exception as exc:\n"
    "    result['protected_write'] = 'denied:' + type(exc).__name__\n"
    "try:\n"
    "    import socket\n"
    "    socket.create_connection(('127.0.0.1', 9), 0.2)\n"
    "    result['network'] = 'allowed'\n"
    "except Exception as exc:\n"
    "    result['network'] = 'denied:' + type(exc).__name__\n"
    "try:\n"
    "    import sys as _s\n"
    "    result['guard_loaded'] = bool(getattr(_s, 'opencoding_sandbox', None))\n"
    "except Exception:\n"
    "    result['guard_loaded'] = False\n"
    "print(json.dumps(result))\n"
)


def self_check(guard_dir: Path, *, writable_root: Path, protected_probe_dir: Path) -> dict[str, Any]:
    """自检守卫是否真的生效；任一限制未生效即抛错（fail-closed）。"""

    key = str(guard_dir)
    if key in _SELF_CHECKED:
        return {"cached": True, "guard_dir": key}
    outside = Path(tempfile.gettempdir()) / f".opencoding-sandbox-probe-{uuid.uuid4().hex}.marker"
    protected = Path(protected_probe_dir) / f".opencoding-sandbox-probe-{uuid.uuid4().hex}.marker"
    # 保护探针目录必须真实存在，否则子进程只会报 FileNotFoundError，
    # 无法证明“控制区写入被策略拒绝”。目录由父进程（不受守卫约束）创建。
    try:
        Path(protected_probe_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SandboxError("sandbox_unavailable", "受控执行环境不可用：保护探针目录无法创建：" + str(exc)) from exc
    try:
        script = Path(guard_dir) / "_self_check.py"
        script.write_text(_SELF_CHECK_SOURCE, encoding="utf-8", newline="\n")
        result = subprocess.run(
            [sys.executable, str(script), str(outside), str(protected)],
            cwd=str(writable_root),
            env=child_env(None, guard_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    finally:
        pass
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
    except (ValueError, IndexError):
        payload = {}
    finally:
        for probe in (outside, protected):
            if probe.exists():
                try:
                    probe.unlink()
                except OSError:
                    pass
    problems: list[str] = []
    if not payload.get("guard_loaded"):
        problems.append("守卫未在子进程加载")
    if not str(payload.get("outside_write", "")).startswith("denied"):
        problems.append("工作副本之外的写入未被拒绝")
    if not str(payload.get("lowlevel_write", "")).startswith("denied"):
        problems.append("低层文件接口（_io）的越界写入未被拒绝")
    if not str(payload.get("pathlib_write", "")).startswith("denied"):
        problems.append("pathlib 路径的越界写入未被拒绝")
    if not str(payload.get("protected_write", "")).startswith("denied"):
        problems.append("控制元数据区的写入未被拒绝")
    if not str(payload.get("network", "")).startswith("denied"):
        problems.append("网络出口未被拒绝")
    if problems:
        raise SandboxError("sandbox_unavailable", "受控执行环境不可用：" + "；".join(problems))
    _SELF_CHECKED.add(key)
    return {"guard_dir": key, **payload}


def discard(guard_dir: Path) -> None:
    """删除一个守卫目录（仅本会话自建的一次性目录）。"""

    try:
        shutil.rmtree(guard_dir, ignore_errors=True)
    except OSError:
        pass
    _SELF_CHECKED.discard(str(guard_dir))
    for key, value in list(_GUARD_CACHE.items()):
        if value == guard_dir:
            _GUARD_CACHE.pop(key, None)


__all__ = ["SandboxError", "build_guard", "child_env", "discard", "run", "self_check"]
