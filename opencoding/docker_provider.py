# -*- coding: utf-8 -*-
"""Docker 受限执行后端适配层(REAL-14 路线:容器隔离)。

职责边界(依据 CP5 审核 01/02 与 03 源码证据):
- 只负责宿主路径↔容器路径映射、docker run 命令构建、本地镜像核对与配置诊断;
- **环境就绪 ≠ 执行资格**:CLI/引擎/本地镜像齐备只是 ``environment_ready``,
  不得据此放行未知候选;只有同一本镜像同一策略下通过同路由探针实测
  (``probe_boundaries``)并签发且未过期的资格(``entitlement``)才使
  ``available=True`` / ``verified_boundaries=True``;
- 不拉取镜像(联网下载需单独许可);本地无镜像时如实报缺;
- **写区不得覆盖检查区**:模型契约声明的 ``data_dir`` 只是建议,必须落在本次
  scratch 内的受控业务数据范围;与代码区/冻结规范/检查器/运行控制区重叠、
  祖先覆盖、或指向保护段的一律拒绝(T07 反例);
- 容器身份与生命周期:以 ``docker create/run -d --name`` 记录本任务容器身份,
  超时/取消后精确 stop→kill→rm 并**核实已停止/已移除**;"本地等待结束"
  与 ``--rm`` 都不等于容器已停(等待超时后效果保持未知)。

状态:适配、写区校验、资格签发与生命周期收口已完成;引擎级实测仍需用户
对"启动已有 Docker Desktop"的明确许可,未取得前一律不运行未知候选。
"""

from __future__ import annotations

import errno as _errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

CONTAINER_WORK = "/work"
DEFAULT_IMAGE = "python:3.11-slim"
IMAGE_ENV = "OPENCODING_DOCKER_IMAGE"

# 探针协议版本:任何 PROBE_SOURCE / 判据变更必须递增,使旧资格立即失效。
# v3(C6-02):classify 按 errno 语义识别只读文件系统(EROFS→denied);
# 探针环境变量由宿主显式传入容器(-e),不再假定宿主环境自动进入容器。
PROBE_VERSION = "docker-probe-v5"
# 资格有效期:过期必须重新实测,不得沿用旧结论。
ENTITLEMENT_TTL_SECONDS = 3600
ENTITLEMENT_DIRNAME = "opencoding-docker-entitlement"

# 执行策略与 entitlement 摘要、实际 docker 命令必须同源,不得散落漂移。
POLICY: dict[str, Any] = {
    "network": "none",
    "read_only_root": True,
    "tmpfs": "/tmp:rw,size=64m",
    "memory": "512m",
    "cpus": "1",
    "pull": "never",
    "code_mount": "ro",
    "data_mount": "rw",
    "container_work": CONTAINER_WORK,
    "privileged": False,
    "docker_socket_mounted": False,
}

# 写区保护:这些段属于检查/控制/冻结区,候选不得以任何形式取得其写权限。
PROTECTED_SEGMENTS: dict[str, str] = {
    "frozen_checks": "冻结规范与独立检查器",
    ".opencoding": "运行控制元数据",
}
PROTECTED_NAMES: dict[str, str] = {
    "spec.json": "独立检查规范",
    "app_contract_v2.py": "冻结检查器",
    "frozen_record.json": "冻结记录",
    "metadata.json": "控制元数据",
}

EXPECTED = {"ro_root": "denied", "data_writable": "writable", "spec_readonly": "denied"}


def _image() -> str:
    return os.environ.get(IMAGE_ENV, DEFAULT_IMAGE).strip() or DEFAULT_IMAGE


def docker_cli_available() -> bool:
    return shutil.which("docker") is not None


def daemon_reachable(timeout: int = 10) -> tuple[bool, str]:
    """只读探测引擎;不启动服务、不拉镜像。"""
    if not docker_cli_available():
        return False, "docker CLI 不在 PATH"
    try:
        proc = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "docker CLI 调用失败:" + type(exc).__name__
    if proc.returncode != 0:
        return False, "引擎未运行或不可达:" + sanitize_tail(proc.stderr)
    return True, "引擎可用,服务端版本 " + proc.stdout.strip()


def sanitize_tail(text: str) -> str:
    from .safety import sanitize_text

    return sanitize_text(str(text))[:200]


def image_present_locally(image: str | None = None) -> tuple[bool, str]:
    """核对本地是否已有镜像(不触发 pull)。"""
    image = image or _image()
    ok, reason = daemon_reachable()
    if not ok:
        return False, reason
    proc = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}", image],
                          capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
    if proc.returncode != 0:
        return False, "镜像查询失败:" + sanitize_tail(proc.stderr)
    if image in proc.stdout.split():
        return True, "本地镜像存在:" + image
    return False, ("本地无镜像 " + image + ";拉取需要单独的网络下载许可,未自动执行")


def image_id(image: str | None = None) -> tuple[str | None, str]:
    """本镜像的**实际本地身份**(image id/digest);缺失返回 None。

    资格必须绑定这个身份:同一个 tag 被重指向另一份内容时,旧资格立即失效。
    """
    image = image or _image()
    ok, reason = daemon_reachable()
    if not ok:
        return None, reason
    try:
        proc = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "镜像身份查询失败:" + type(exc).__name__
    if proc.returncode != 0:
        return None, "本地无该镜像或无法读取身份:" + sanitize_tail(proc.stderr)
    value = (proc.stdout or "").strip()
    if not value:
        return None, "镜像身份为空"
    return value, "镜像身份已取得"


# ---------------------------------------------------------------- 写区校验

def normalize_data_dir(data_dir: str | os.PathLike[str]) -> str:
    """把 data_dir 规范为容器内相对 posix 路径(不含前导/后导斜杠)。"""
    text = str(data_dir or "").replace("\\", "/").strip().strip("/")
    return text


def validate_data_dir(scratch_host: Path, data_dir: str | os.PathLike[str]) -> tuple[str, str | None]:
    """CP5 §3.3/T07:业务写区不得覆盖检查区/控制区/代码区。

    返回 (规范化相对路径, None) 表示允许;(空字符串, 拒绝原因) 表示拒绝。
    判定顺序:形式 → 是否 scratch 内 → 是否整体/祖先覆盖 → 是否保护段/保护名。
    """
    rel = normalize_data_dir(data_dir)
    if not rel or rel in (".", ".."):
        return "", "data_dir 不能为空或指向 scratch 整体(那会使整个代码区变为可写)"
    parts = [p for p in PurePosixPath(rel).parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return "", "data_dir 必须是 scratch 内的相对路径,不得含 ..:" + str(data_dir)
    for index, part in enumerate(parts):
        for protected, why in PROTECTED_SEGMENTS.items():
            if part == protected:
                sub = "/".join(parts[: index + 1])
                return "", ("data_dir 指向受保护区(" + why + "):" + sub
                            + ";候选不得取得冻结规范/检查器/运行控制的写权限")
    if parts[-1] in PROTECTED_NAMES:
        return "", ("data_dir 不能是保护文件(" + PROTECTED_NAMES[parts[-1]] + "):" + rel)
    if len(parts) == 1 and parts[0] in ("app", "src", "code", "lib", "tests"):
        return "", ("data_dir 不能是代码区根(" + parts[0] + ");业务写区必须是其下的明确数据目录")
    # 必须位于本次 scratch 之内(防绝对路径/盘符逃逸)。
    try:
        resolved_host = (Path(scratch_host).resolve() / Path(*parts)).resolve()
    except OSError as exc:
        return "", "data_dir 指向的宿主路径不可解析:" + type(exc).__name__
    try:
        resolved_host.relative_to(Path(scratch_host).resolve())
    except ValueError:
        return "", "data_dir 不在本次 scratch 之内:" + str(data_dir)
    return "/".join(parts), None


def host_to_container(host_path: Path) -> str:
    """宿主 scratch 目录 → 容器 /work。仅接受本批合成暂存目录。"""
    p = Path(host_path).resolve()
    return CONTAINER_WORK


def _mount_spec(host_path: Path) -> str:
    win_path = str(Path(host_path).resolve())
    if win_path.startswith("\\\\"):
        raise ValueError("UNC 路径不能直接挂载:" + win_path)
    mount = win_path.replace("\\", "/")
    if len(mount) > 2 and mount[1] == ":":
        mount = "/" + mount[0].lower() + mount[2:]
    return mount.rstrip("/")


def policy_digest(image_value: str, image_identity: str | None) -> str:
    payload = {
        "policy": POLICY,
        "probe_version": PROBE_VERSION,
        "image_requested": image_value,
        "image_id": image_identity or "",
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def build_run_command(scratch_host: Path, argv_in_scratch: list[str], *,
                      data_dir: str = "app/data", image: str | None = None,
                      detach: bool = False, name: str | None = None,
                      alternate_policy: Mapping[str, Any] | None = None,
                      env: Mapping[str, str] | None = None) -> list[str]:
    """构建 docker run 命令(策略与 POLICY 同源,写区经 ``validate_data_dir`` 核准)。

    - --network none:禁止容器网络;
    - --read-only:容器根只读(除显式可写挂载外不可写);
    - tmpfs /tmp:受限临时区;
    - <scratch>:/work:ro —— 代码/冻结规范**只读**挂载(候选不可改标准);
    - <scratch>/<data_dir>:/work/<data_dir>:rw —— 唯一可写业务数据区,
      经写区校验后才可挂载;
    - env:C6-02——容器**不继承宿主环境**,需要进入容器的变量(如探针对照
      目标)必须经 -e 显式传入,A/B 两侧使用同一目标。
    """
    policy = dict(POLICY)
    if alternate_policy:
        policy.update({str(k): v for k, v in alternate_policy.items()})
    scratch_resolved = Path(scratch_host).resolve()
    rel, refusal = validate_data_dir(scratch_resolved, data_dir)
    if refusal:
        raise ValueError("data_dir 未通过写区校验:" + refusal)
    mount = _mount_spec(scratch_resolved)
    data_host = mount + "/" + rel
    data_container = CONTAINER_WORK + "/" + rel
    cmd = ["docker", "run"]
    if detach:
        cmd.append("-d")
    if name:
        cmd += ["--name", name]
    if name is None:
        cmd.append("--rm")  # 具名生命周期由 stop/kill/rm 精确收尾,不依赖退出清理
    env_map = dict(env or {})
    for key in sorted(env_map):
        value = str(env_map[key])
        k = str(key)
        if not k or k[0].isdigit() \
                or any(not (ch.isascii() and (ch.isalnum() or ch == "_")) for ch in k) \
                or "\n" in value or '"' in value or "'" in value:
            raise ValueError("env_entry_invalid:" + k[:40])
        cmd += ["-e", k + "=" + value]
    cmd += [
        "--pull", policy["pull"],  # 禁止隐式拉取;缺镜像须另行取得下载许可
        "--network", policy["network"],
        "--read-only",
        "--tmpfs", policy["tmpfs"],
        "-v", mount + ":" + CONTAINER_WORK + ":" + policy["code_mount"],
        "-v", data_host + ":" + data_container + ":" + policy["data_mount"],
        "-w", CONTAINER_WORK,
        "--memory", policy["memory"], "--cpus", policy["cpus"],
        image or _image(),
        *container_argv(argv_in_scratch, scratch_resolved),
    ]
    return cmd


def container_argv(argv_in_scratch: list[str], scratch_host: Path | None = None) -> list[str]:
    """宿主 argv → 容器内 argv:解释器统一容器内 python;宿主绝对脚本路径转 /work 相对。"""
    scratch = Path(scratch_host).resolve() if scratch_host else None
    out = []
    for item in argv_in_scratch:
        if item == sys.executable or item.endswith(("python.exe", "python")):
            out.append("python")
            continue
        candidate = Path(item)
        if candidate.is_absolute() and scratch is not None:
            try:
                rel = candidate.resolve().relative_to(scratch).as_posix()
                out.append(CONTAINER_WORK + "/" + rel)
                continue
            except ValueError:
                pass
        out.append(item)
    return out


# ---------------------------------------------------------------- 探针

PROBE_SOURCE = '''# -*- coding: utf-8 -*-
"""Docker 隔离边界探针 v5(只做探测,不做业务)。

与 v2 差异(C6-02):classify 按 Python errno 语义精确分类——只读文件系统
(EROFS)本身就是一种"策略拒绝"形态,不必是 PermissionError;权限拒绝
(PermissionError/EACCES/EPERM)与 EROFS 报 denied,路径缺失、父目录不存在、
其他失败一律报 failed:*,避免"本来就写不了"被误判为边界成立,也避免
"只读被误报为一般失败"。正向关键字 amount=writable / denied / failed:<reason>。
探针目标由宿主经 docker run -e 显式传入(容器不继承宿主环境)。
"""
import errno, json, os, socket, sys
from pathlib import Path

TARGET_HOST = os.environ.get("OPENCODING_DOCKER_PROBE_HOST", "")
TARGET_PORT = int(os.environ.get("OPENCODING_DOCKER_PROBE_PORT", "0") or 0)

_EROFS = getattr(errno, "EROFS", 30)
_EACCES = getattr(errno, "EACCES", 13)
_EPERM = getattr(errno, "EPERM", 1)


def classify(action):
    try:
        action()
        return {"status": "writable"}
    except FileNotFoundError as exc:
        return {"status": "failed:path_missing", "detail": str(exc)[:120]}
    except NotADirectoryError as exc:
        return {"status": "failed:not_a_directory", "detail": str(exc)[:120]}
    except PermissionError as exc:
        return {"status": "denied", "errno": getattr(exc, "errno", None)}
    except OSError as exc:
        code = getattr(exc, "errno", None)
        if code in (_EROFS, _EACCES, _EPERM):
            # 只读文件系统/权限拒绝:按语义归为策略拒绝,而非一般失败
            return {"status": "denied", "errno": code, "semantic": "readonly_or_denied"}
        return {"status": "failed:oserror:" + str(code if code is not None else "?"),
                "detail": str(exc)[:120]}


result = {}


def write_probe(key, path):
    target = Path(path)
    result[key + "_parent_exists"] = target.parent.exists()
    result[key] = classify(lambda: target.write_text("probe", encoding="utf-8"))
    exists = target.exists()
    if exists and not target.is_dir():
        try:
            target.unlink()
        except OSError:
            pass


write_probe("ro_root", "/usr/local/lib/opencoding_probe_should_fail")
write_probe("data_writable", "/work/app/data/opencoding_probe_ok.txt")
write_probe("spec_readonly", "/work/frozen_checks/spec.json")

if TARGET_HOST and TARGET_PORT:
    def connect():
        s = socket.create_connection((TARGET_HOST, TARGET_PORT), timeout=3)
        s.close()
    result["network"] = classify(connect)
else:
    result["network"] = {"status": "skipped:no_control_target"}

result["targets"] = {
    "ro_root": "/usr/local/lib/opencoding_probe_should_fail",
    "data_writable": "/work/app/data/opencoding_probe_ok.txt",
    "spec_readonly": "/work/frozen_checks/spec.json",
}
print("PROBE:" + json.dumps(result))
'''


def _ensure_probe_fixtures(scratch_host: Path, data_rel: str) -> dict[str, Any]:
    """T07/CP5 §3.4/C6-02:探针目标必须先由宿主建立,并做**可写正向对照**。

    返回 control(宿主对照)结果;宿主机(不受容器策略约束)对这些路径必须可写,
    否则容器内的 denied 无法证明"策略拒绝"。

    C6-02(U06-5.1):spec 探测目标在宿主正向检查后必须**保留/恢复原样**——
    旧实现写完 control 就 unlink,把自己要测的目标删了(spec_exists=false),
    上层还把结果当只读保护成立。现在:对照写入后恢复原内容;上层对
    spec_exists=false 一律拒绝(fail-closed)。
    """
    scratch_host = Path(scratch_host)
    spec_dir = scratch_host / "frozen_checks"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_file = spec_dir / "spec.json"
    spec_default = json.dumps({"role": "frozen-spec-probe"}, ensure_ascii=False,
                              indent=2, sort_keys=True)
    if not spec_file.exists():
        spec_file.write_text(spec_default, encoding="utf-8", newline="\n")
    data_dir = scratch_host / Path(*data_rel.split("/"))
    data_dir.mkdir(parents=True, exist_ok=True)

    def host_write(path: Path, *, keep: bool = False, keep_content: str | None = None) -> str:
        try:
            path.write_text("host-control", encoding="utf-8")
            if keep:
                # 恢复探测目标本体,而不是删除它——正向对照不得毁掉被测对象
                path.write_text(keep_content or spec_default, encoding="utf-8", newline="\n")
            else:
                try:
                    path.unlink()
                except OSError:
                    pass
            return "writable"
        except OSError as exc:
            return "failed:" + type(exc).__name__

    return {
        "host_spec_writable": host_write(spec_file, keep=True),
        "host_data_writable": host_write(data_dir / "opencoding_host_control.txt"),
        "spec_exists": spec_file.exists(),
        "spec_bytes": spec_file.stat().st_size if spec_file.exists() else 0,
        "data_dir_exists": data_dir.is_dir(),
    }


def _classify_to_status(item: Any) -> str:
    if isinstance(item, Mapping):
        return str(item.get("status") or "missing")
    return "missing"


def _probe_control_target() -> dict[str, Any]:
    """FIX-02(U06-5.2)/R02a:对照目标的来源、归一化与合法性校验。

    默认(未配置)→ 由 :func:`_start_probe_listener` 自建本批专用监听;
    显式配置的本机回环(127.0.0.1/localhost)同样**归一化**为
    ``host.docker.internal:<port>``——容器内的 ``127.0.0.1`` 指容器自身
    回环,把回环字符串原样传进容器等于让 A/B 两侧连不同目标,不构成
    "同一可达端点"(R02a 纠正的核心)。归一化只改寻址名,不改用户配置的
    宿主端口语义;配置的目标是否真的有服务,由 A/B 对照实测判定
    (对照不可达 → fail-closed,绝不把"目标缺失"当策略成功)。
    ``::1`` 经宿主别名映射到容器网络无可靠可达性保证 → 明确拒绝并说明。
    其余一切形态维持拒绝,不自动连接外部地址。
    """
    host = os.environ.get("OPENCODING_DOCKER_PROBE_HOST", "").strip()
    port_raw = os.environ.get("OPENCODING_DOCKER_PROBE_PORT", "").strip()
    if not host or not port_raw:
        return {"configured": False,
                "reason": "未配置可控本地对照目标(不自动连接外部地址;默认将自建本批专用监听)"}
    lowered = host.lower()
    if lowered not in ("127.0.0.1", "localhost", "::1", "host.docker.internal"):
        return {"configured": False,
                "refused": "对照目标必须是本机回环地址或宿主映射别名(不自动连接外部地址):" + host[:80]}
    try:
        port = int(port_raw)
    except ValueError:
        return {"configured": False, "refused": "对照端口必须是整数:" + port_raw[:20]}
    if not 1 <= port <= 65535:
        return {"configured": False, "refused": "对照端口超出 1-65535:" + port_raw[:20]}
    if lowered == "::1":
        return {"configured": False,
                "refused": "IPv6 回环(::1)经宿主别名映射到容器网络无可靠可达性保证;"
                           "请使用 127.0.0.1/localhost(将归一化为宿主别名)"
                           "或 host.docker.internal:" + host[:80]}
    if lowered == "host.docker.internal":
        return {"configured": True, "host": lowered, "port": port,
                "source": "configured", "requested_host": host}
    # R02a:回环配置归一化——默认路径与配置路径表达**同一个受控本地服务**,
    # 统一由宿主别名寻址;端口语义保持用户配置不变。
    return {"configured": True, "host": "host.docker.internal", "port": port,
            "source": "configured_loopback_normalized", "requested_host": host}


def _start_probe_listener() -> dict[str, Any]:
    """FIX-02:为本批探针在宿主建立**专用、一次性** TCP 监听(127.0.0.1 随机端口)。

    - 归属:仅本批探针使用;探测结束由调用方立即关闭(见 probe_boundaries
      的 finally);不写盘、不留常驻服务、不改防火墙;
    - 寻址:容器侧统一用 ``host.docker.internal``(Docker Desktop 宿主映射,
      可达宿主回环监听)——受限组与对照组拿到**同一目标字符串、同一服务**;
    - 失败(端口/权限等)→ 调用方 fail-closed,不签发资格。
    """
    import socket as _socket
    import threading

    srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
    except OSError as exc:
        srv.close()
        return {"ok": False, "reason": "探针监听建立失败:" + sanitize_tail(str(exc))[:120]}
    srv.settimeout(0.2)
    port = int(srv.getsockname()[1])
    serving = {"open": True}

    def _serve():
        # 只做"接受连接并立即关闭":证明端口真实可达,不承载任何业务
        while serving["open"]:
            try:
                conn, _addr = srv.accept()
                conn.close()
            except _socket.timeout:
                continue
            except OSError:
                break

    thread = threading.Thread(target=_serve, daemon=True, name="oc-probe-listener")
    thread.start()
    return {"ok": True, "socket": srv, "port": port,
            "host": "host.docker.internal", "thread": thread, "serving": serving}


_PROBE_LISTENER_JOIN_TIMEOUT = 5.0


def _bind_probe_release(port: int) -> tuple[bool | None, str | None]:
    """R02b:Windows 兜底观测——bind 的内核语义是"端口是否存在活跃监听绑定"
    的确定性信号(平台实测:无监听绑定 → bind 成功,TIME_WAIT 不干扰;
    活跃监听 → WSAEADDRINUSE/10048)。不设 SO_REUSEADDR(其语义在 Windows
    上可与活跃监听共存,会破坏判定);不 listen、立即关闭,不留端口状态。
    返回 (True=明确无监听绑定 / False=明确仍有占用 / None=拿不到明确结论, 原因)。
    """
    import socket as _socket

    probe = None
    try:
        probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", int(port)))
        return True, "bind_probe:端口无活跃监听绑定(内核明确信号)"
    except OSError as exc:
        code = getattr(exc, "errno", None)
        if code == _errno.EADDRINUSE or code == 10048:  # 10048=WSAEADDRINUSE
            return False, "bind_probe:端口仍有监听绑定(EADDRINUSE/WSAEADDRINUSE)"
        return None, ("bind_probe_oserror:" + type(exc).__name__
                      + ";errno=" + (str(int(code)) if code is not None else "无")
                      + (";" + sanitize_tail(str(exc))[:80] if str(exc) else ""))
    finally:
        if probe is not None:
            try:
                probe.close()
            except OSError:
                pass


def _stop_probe_listener(listener: dict[str, Any] | None) -> dict[str, Any] | None:
    """R02b:一次性监听的**有界收尾**——停标志 → 关套接字 → 有界 join → 逐项核实。

    Linux P04 实测:旧实现只置标志 + close 就返回,accept 线程可能仍存活、
    端口短暂仍可连——"函数返回"不等于"监听已停止"。现在在**函数返回边界**
    上逐项核实并给出收尾结论(不做任意 sleep,不删断言,不用成功替身):
    - ``socket_closed``:close() 未抛错;
    - ``thread_stopped``:accept 线程在有界超时内结束(``join(超时)`` 后不再存活);
    - ``port_free``:返回边界上对该端口释放状态的**三态分类**(R02b:网页审核
      P06/P07——把"观测未知"当成"已释放"的肯定证据是缺口):
      - ``True``:拿到了**明确信号**——连接被明确拒绝(ConnectionRefusedError,
        POSIX 实测主信号),或 Windows 平台兜底的 bind 探测确认无活跃监听绑定
        (Windows 实测:回环栈对已关闭监听端口的 SYN 静默丢弃,connect 稳定
        超时——超时本身**不是**"已释放"的证据,必须由内核 bind 语义给出结论);
      - ``False``:明确反证——仍能连上,或 bind 探测确认端口仍被占用;
      - ``"unknown"``:观测超时/资源/权限异常且拿不到任何明确信号(POSIX 上
        一律 unknown,不做兜底二次猜测)——错误类别/errno/原因记入
        ``port_free_reason``,``confirmed=False``,调用方必须 fail-closed。
    ``confirmed`` = 关闭、线程停止、端口明确释放三项全真;任一项未确认,
    调用方必须 fail-closed,不得记录"已结束"、不得签发完整资格。
    """
    if not listener or not listener.get("ok"):
        return None
    import socket as _socket

    closed = True
    try:
        listener["serving"]["open"] = False
        listener["socket"].close()
    except OSError:
        closed = False
    thread = listener.get("thread")
    stopped = False
    if thread is not None:
        thread.join(_PROBE_LISTENER_JOIN_TIMEOUT)
        stopped = not thread.is_alive()
    port = int(listener.get("port") or 0)
    port_free: bool | str | None = None
    port_free_reason: str | None = None
    if port:
        try:
            probe = _socket.create_connection(("127.0.0.1", port), timeout=0.5)
            probe.close()
            port_free = False  # 返回边界上仍可连:监听未真正停止
            port_free_reason = "observation:端口仍可连接(明确反证)"
        except ConnectionRefusedError:
            # R02b:明确的"连接被拒绝"——端口上已无监听,按契约判已释放。
            port_free = True
            port_free_reason = "observation:连接被明确拒绝(内核明确信号)"
        except OSError as exc:
            # R02b:超时/资源/权限等观测异常——观测失败不等于端口已释放;
            # 绝不把"未知"当肯定证据。Windows 平台兜底用 bind 探测的内核
            # 语义取得明确结论(见 _bind_probe_release);POSIX 如实 unknown。
            detail = ("observation_oserror:" + type(exc).__name__
                      + ";errno=" + (str(int(exc.errno))
                                     if getattr(exc, "errno", None) is not None else "无")
                      + (";" + sanitize_tail(str(exc))[:80] if str(exc) else ""))
            if os.name == "nt":
                verdict, probe_reason = _bind_probe_release(port)
                if verdict is not None:
                    port_free = verdict
                    port_free_reason = probe_reason + "(" + detail.split(":")[0] + ")"
                else:
                    port_free = "unknown"
                    port_free_reason = detail + ";" + str(probe_reason)
            else:
                port_free = "unknown"
                port_free_reason = detail + "(unknown)"
    confirmed = bool(closed and stopped and port_free is True)
    return {"socket_closed": closed, "thread_stopped": stopped,
            "port_free": port_free, "port_free_reason": port_free_reason,
            "confirmed": confirmed}


def probe_boundaries(scratch_host: Path, *, timeout: int = 180,
                     runner: Callable[..., Any] | None = None,
                     data_rel: str = "app/data") -> dict[str, Any]:
    """Docker 专属探针(资格签发前的**唯一**实测入口)。

    通过条件(全部满足才 granted):
    1. 环境就绪(CLI/引擎/本地镜像齐备)且已取得镜像实际身份;
    2. 宿主正向对照成立(spec 文件存在且宿主可写、业务数据区宿主可写);
    3. 容器内:根只读区 denied、冻结规范区 denied、业务数据区 writable;
    4. 网络项:做 A/B 对照——默认由宿主自建本批专用一次性监听
       (``host.docker.internal`` 统一寻址,FIX-02);显式配置的本机回环
       (OPENCODING_DOCKER_PROBE_HOST/PORT)同样归一化为宿主别名寻址
       (R02a:默认路径与配置路径表达同一受控本地服务);受限容器必须
       failed/denied 且对照组可达;监听建立失败则整体 fail-closed;
       探测结束监听有界收尾,未确认(线程未停/端口仍可连)不签发资格(R02b);
    5. --rm 之外的生命周期已核实容器退出(remove_state 三分:removed /
       still_present / unverified,只有 removed 算已移除)。
    """
    runner = runner or subprocess.run
    scratch_host = Path(scratch_host).resolve()
    readiness = _readiness_detail()
    if not readiness["ready"]:
        return {"kind": "docker", "available": False, "verified_boundaries": False,
                "granted": False, "entitlement_id": None,
                "checks": {}, "control": {}, "reason": readiness["reason"]}
    target = _probe_control_target()
    if target.get("refused"):
        return {"kind": "docker", "available": False, "verified_boundaries": False,
                "granted": False, "entitlement_id": None, "checks": {}, "control": {},
                "reason": "网络对照目标配置被拒绝:" + target["refused"]}
    rel, refusal = validate_data_dir(scratch_host, data_rel)
    if refusal:
        return {"kind": "docker", "available": False, "verified_boundaries": False,
                "granted": False, "entitlement_id": None, "checks": {}, "control": {},
                "reason": "探针数据区未通过写区校验:" + refusal}
    control = _ensure_probe_fixtures(scratch_host, rel)
    if control["host_spec_writable"] != "writable" or control["host_data_writable"] != "writable":
        return {"kind": "docker", "available": False, "verified_boundaries": False,
                "granted": False, "entitlement_id": None, "checks": {}, "control": control,
                "reason": "宿主正向对照不成立(目标本就不可写),无法证明容器内拒绝是策略效果"}
    if not control.get("spec_exists"):
        # C6-02(U06-5.1):被测目标缺失时不许把结果算作只读保护成立
        return {"kind": "docker", "available": False, "verified_boundaries": False,
                "granted": False, "entitlement_id": None, "checks": {}, "control": control,
                "reason": "探针 spec 目标缺失(正向对照未保留被测对象),拒绝签发资格"}
    listener: dict[str, Any] | None = None
    listener_cleanup: dict[str, Any] | None = None
    if not target.get("configured"):
        # FIX-02(U06-5.2):默认路径不再依赖人工配置——宿主自建**本批专用一次性
        # 监听**(127.0.0.1 随机端口),容器侧统一经 host.docker.internal 寻址。
        # A/B 两侧拿同一目标字符串、连同一服务:对照组(bridge)真实可达证明
        # 端点存在,受限组(none)失败才是策略效果。监听建立失败 → fail-closed。
        listener = _start_probe_listener()
        if not listener.get("ok"):
            return {"kind": "docker", "available": False, "verified_boundaries": False,
                    "granted": False, "entitlement_id": None, "checks": {},
                    "control": control,
                    "reason": "网络对照监听建立失败,整体 fail-closed:"
                              + str(listener.get("reason") or "未知原因")}
        target = {"configured": True, "host": listener["host"],
                  "port": listener["port"], "source": "ephemeral_listener"}
    try:
        probe_host_path = scratch_host / "frozen_checks" / "docker_probe.py"
        probe_host_path.parent.mkdir(parents=True, exist_ok=True)
        probe_host_path.write_text(PROBE_SOURCE, encoding="utf-8", newline="\n")
        argv = [sys.executable, str(probe_host_path)]
        # 先在 bridge 网络中确认同一目标可达，并取得该网络实际连接到的
        # 对端 IP；受限组随后使用这个动态 IP，避免仅凭 DNS 失败放行。
        network_control = _network_control(scratch_host, timeout=timeout, runner=runner,
                                           target=target)
        probe_target = dict(target)
        resolved_target = network_control.get("resolved_target") or {}
        direct_ip = resolved_target.get("host")
        if network_control.get("control_reachable") is True and direct_ip:
            probe_target["host"] = str(direct_ip)
            probe_target["source"] = "bridge_resolved_ip"
        probe_env = {"OPENCODING_DOCKER_PROBE_HOST": str(probe_target["host"]),
                     "OPENCODING_DOCKER_PROBE_PORT": str(probe_target["port"])}
        outcome = run_container(scratch_host, argv, data_dir=rel, timeout=timeout,
                                runner=runner, label="probe", entitlement_required=False,
                                env=probe_env)
        raw_outcome = dict(getattr(outcome, "container", {}) or {})
        stdout = raw_outcome.get("logs_stdout") or getattr(outcome, "stdout", "")
        checks: dict[str, Any] = {}
        for line in str(stdout or "").splitlines():
            if line.startswith("PROBE:"):
                try:
                    checks = json.loads(line[6:])
                except ValueError:
                    pass
        problems: list[str] = []
        if not checks:
            problems.append("no_probe_output")
        for key, want in EXPECTED.items():
            got = _classify_to_status(checks.get(key))
            if got != want:
                problems.append(key + "=" + got + "(want " + want + ")")
            parent_flag = checks.get(key + "_parent_exists")
            if key == "spec_readonly" and parent_flag is not True:
                problems.append("spec_parent_missing")
        network_status = _classify_to_status(checks.get("network"))
        if network_control["performed"]:
            if network_control["control_reachable"] is not True:
                problems.append("network_control_unreachable")
            elif not network_control.get("resolved_target", {}).get("host"):
                problems.append("network_control_ip_unresolved")
            elif network_status not in ("failed:connection_refused", "failed:timeout",
                                        "failed:oserror:113", "failed:oserror:101",
                                        "failed:oserror:10065", "failed:oserror:-3",
                                        "denied"):
                problems.append("network=" + network_status)
        elif network_status.startswith("skipped"):
            problems.append("network=skipped_no_control_target")
        else:
            problems.append("network_missing_control_group:" + network_status)
        if int(raw_outcome.get("exit_code", -1)) != 0:
            problems.append("probe_exit=" + str(raw_outcome.get("exit_code")))
        if raw_outcome.get("confirmed_stopped") is not True:
            problems.append("container_stop_not_confirmed")
        if raw_outcome.get("confirmed_removed") is not True:
            # FIX-02(U06-5.4):细分"容器还在"(still_present)与"rm/查询均失败、
            # 效果未知"(unverified)——两种情况都不许当作已移除。
            problems.append("container_remove_not_confirmed:"
                            + str(raw_outcome.get("remove_state") or "unknown"))
        if raw_outcome.get("completion_consistent") is not True:
            problems.append("container_lifecycle_inconsistent")
    finally:
        # 一次性监听只在本批探针窗口内存活;无论成败,窗口关闭立即有界收尾。
        # R02b:线程未停/端口仍可连等未确认结论如实记为问题,不签发资格。
        listener_cleanup = _stop_probe_listener(listener)
    if listener is not None and not (listener_cleanup or {}).get("confirmed"):
        problems.append("listener_cleanup_unconfirmed")
    granted = not problems
    entitlement: dict[str, Any] | None = None
    if granted:
        entitlement = grant_entitlement(readiness["policy_digest"], {
            "image_id": readiness["image_id"], "policy": POLICY,
            "probe_version": PROBE_VERSION, "image_requested": readiness["image"],
            "data_dir": rel,
        })
    return {
        "kind": "docker",
        "available": granted,
        "verified_boundaries": granted,
        "granted": granted,
        "entitlement_id": (entitlement or {}).get("entitlement_id"),
        "policy_digest": readiness["policy_digest"],
        "checks": checks,
        "control": {**control, "network": network_control},
        "lifecycle": raw_outcome,
        "listener_cleanup": listener_cleanup,
        "reason": ("四项边界实测通过并签发资格(根只读/规范只读/数据区可写/网络不可达)"
                   if granted else "边界未成立:" + ";".join(problems)),
    }


def _network_control(scratch_host: Path, *, timeout: int,
                     runner: Callable[..., Any],
                     target: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """网络 A/B 对照(仅使用本批可控本地目标,不自动连外部地址)。

    C6-02(U06-5.2):对照目标由 :func:`_probe_control_target` 统一给出并校验
    (本机回环),受限探针容器与对照容器收到的是**同一目标**——对照组从宿主
    另取一套配置、两侧目标不一致的旧做法已移除。对照容器使用**默认桥接**
    policy 的同一镜像,验证目标在本机容器网络下确实可达,从而证明受限容器
    的失败来自策略而非目标不可达。
    """
    target = dict(target or {})
    if not target.get("configured"):
        return {"performed": False,
                "reason": str(target.get("refused") or target.get("reason")
                              or "未配置可控本地对照目标(不自动连接外部地址)")}
    host, port = str(target["host"]), str(target["port"])
    control_source = (
        "import json, socket, sys\n"
        "host, port = sys.argv[1], int(sys.argv[2])\n"
        "try:\n"
        "    s = socket.create_connection((host, port), timeout=5)\n"
        "    peer_ip = s.getpeername()[0]\n"
        "    s.close()\n"
        "    print('CONTROL:' + json.dumps({'status': 'connected', 'peer_ip': peer_ip}))\n"
        "except Exception as exc:\n"
        "    print('CONTROL:' + json.dumps({'status': 'failed:' + type(exc).__name__, 'detail': str(exc)[:80]}))\n"
    )
    helper = scratch_host / "frozen_checks" / "docker_network_control.py"
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text(control_source, encoding="utf-8", newline="\n")
    bridge_policy = {"network": "bridge"}
    cmd = build_run_command(scratch_host, [sys.executable, str(helper), host, port],
                            data_dir="app/data", detach=False,
                            alternate_policy=bridge_policy)
    try:
        proc = runner(cmd, capture_output=True, text=True, encoding="utf-8",
                      errors="replace", timeout=min(timeout, 120), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"performed": True, "control_reachable": False,
                "target": {"host": host, "port": port},
                "reason": "对照容器运行失败:" + type(exc).__name__}
    payload: dict[str, Any] = {}
    for line in (proc.stdout or "").splitlines():
        if line.startswith("CONTROL:"):
            try:
                payload = json.loads(line[8:])
            except ValueError:
                pass
    reachable = str(payload.get("status")) == "connected"
    resolved_target = None
    if reachable:
        peer = payload.get("peer_ip")
        if isinstance(peer, str) and peer:
            resolved_target = {"host": peer, "port": port}
    return {"performed": True, "control_reachable": reachable,
            "target": {"host": host, "port": port},
            "resolved_target": resolved_target,
            "observed": payload.get("status"), "exit_code": proc.returncode}


# ---------------------------------------------------------------- 执行资格

def _readiness_detail() -> dict[str, Any]:
    image_value = _image()
    cli = docker_cli_available()
    engine, engine_msg = daemon_reachable() if cli else (False, "docker CLI 不在 PATH")
    image_ok, image_msg = image_present_locally(image_value) if engine else (False, "引擎不可用,未核对镜像")
    identity, identity_msg = image_id(image_value) if image_ok else (None, "镜像不可用")
    ready = bool(cli and engine and image_ok and identity)
    return {
        "ready": ready,
        "cli": cli, "engine": engine, "engine_message": engine_msg,
        "image": image_value, "image_present": image_ok, "image_message": image_msg,
        "image_id": identity, "image_id_message": identity_msg,
        "policy_digest": policy_digest(image_value, identity),
        "reason": ("CLI/引擎/本地镜像/镜像身份齐备" if ready
                   else ("镜像身份未取得:" + identity_msg if image_ok else
                         (image_msg if engine else engine_msg))),
    }


def _entitlement_dir() -> Path:
    import tempfile  # noqa: PLC0415 - 避免模块导入期创建目录

    override = os.environ.get("OPENCODING_DOCKER_ENTITLEMENT_DIR")
    base = Path(override) if override else Path(tempfile.gettempdir()) / ENTITLEMENT_DIRNAME
    return base


def entitlement_path(digest: str) -> Path:
    return _entitlement_dir() / (digest[:32] + ".json")


def grant_entitlement(digest: str, facts: Mapping[str, Any]) -> dict[str, Any]:
    """签发一次性执行资格(只在实际边界探针通过后调用)。"""
    now = time.time()
    doc = {
        "schema_version": "docker-execution-entitlement-v1",
        "entitlement_id": "ent-" + digest[:24],
        "policy_digest": digest,
        "granted": True,
        "granted_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "expires_at": datetime.fromtimestamp(now + ENTITLEMENT_TTL_SECONDS,
                                             timezone.utc).isoformat(),
        "facts": dict(facts),
    }
    path = entitlement_path(digest)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True),
                        encoding="utf-8", newline="\n")
    except OSError:
        # 落盘失败不得松弛:视为未取得资格。
        return {**doc, "granted": False, "persisted": False}
    return {**doc, "persisted": True}


def load_entitlement(digest: str) -> dict[str, Any] | None:
    """读取仍有效的资格;过期/缺 policy 匹配一律 None(不降级、不硬填 true)。"""
    path = entitlement_path(digest)
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if str(doc.get("policy_digest")) != digest or doc.get("granted") is not True:
        return None
    try:
        expires = datetime.fromisoformat(str(doc.get("expires_at")))
    except ValueError:
        return None
    if expires.timestamp() < time.time():
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return doc


def revoke_entitlement(digest: str) -> bool:
    path = entitlement_path(digest)
    if path.is_file():
        try:
            path.unlink()
            return True
        except OSError:
            return False
    return False


def current_entitlement() -> dict[str, Any] | None:
    readiness = _readiness_detail()
    if not readiness["ready"]:
        return None
    return load_entitlement(readiness["policy_digest"])


def require_entitlement() -> dict[str, Any]:
    """派发未知候选前的硬准入:无有效资格一律抛错(不清退为"环境可用")。"""
    readiness = _readiness_detail()
    if not readiness["ready"]:
        raise RuntimeError("execution_entitlement_missing:环境未就绪(" + readiness["reason"] + ")")
    entitlement = load_entitlement(readiness["policy_digest"])
    if entitlement is None:
        raise RuntimeError(
            "execution_entitlement_missing:尚未通过同路由边界实测或资格已过期/已被改动;"
            "未验证的执行环境不得运行未知候选")
    return entitlement


def execution_capability() -> dict[str, Any]:
    """Docker 受限后端能力判定(只读)。

    ``available`` **只**在资格有效期内、且同一镜像身份与同一策略下通过
    ``probe_boundaries`` 实测后为 True;仅 CLI/引擎/镜像齐备时
    ``environment_ready=True`` 而 ``available=False``(不允许运行未知候选)。
    """
    readiness = _readiness_detail()
    entitlement = load_entitlement(readiness["policy_digest"]) if readiness["ready"] else None
    verified = entitlement is not None
    reason = ("已实测并签发执行资格(" + str(entitlement.get("entitlement_id")) + ")"
              if verified else
              ("环境就绪但未验证边界,不得执行未知候选;" + readiness["reason"]
               if readiness["ready"] else readiness["reason"]))
    return {
        "kind": "docker" if verified else ("docker_unverified" if readiness["ready"] else "docker_unavailable"),
        "available": verified,
        "verified_boundaries": verified,
        "environment_ready": bool(readiness["ready"]),
        "provider": "docker",
        "command": ["docker"] if verified else [],
        "cli_available": readiness["cli"],
        "engine_reachable": readiness["engine"],
        "engine_message": readiness["engine_message"],
        "image": readiness["image"],
        "image_present": readiness["image_present"],
        "image_message": readiness["image_message"],
        "image_id": readiness["image_id"],
        "policy_digest": readiness["policy_digest"],
        "entitlement_id": (entitlement or {}).get("entitlement_id"),
        "entitlement_expires_at": (entitlement or {}).get("expires_at"),
        "probe_version": PROBE_VERSION,
        "reason": reason,
    }


# ---------------------------------------------------------------- 容器执行

class ContainerRunResult(subprocess.CompletedProcess):
    """带容器生命周期事实的 CompletedProcess(兼容既有调用方)。"""

    container: dict[str, Any]

    def __init__(self, args, returncode, stdout=None, stderr=None, *, container=None):
        super().__init__(args=args, returncode=returncode, stdout=stdout, stderr=stderr)
        self.container = dict(container or {})


def _run(cmd: list[str], *, runner: Callable[..., Any], timeout: int | None = None) -> Any:
    return runner(cmd, capture_output=True, text=True, encoding="utf-8",
                  errors="replace", timeout=timeout, check=False)


def run_container(scratch_host: Path, argv: list[str], *,
                  data_dir: str = "app/data", image: str | None = None,
                  timeout: int = 120, runner: Callable[..., Any] | None = None,
                  label: str = "cand", entitlement_required: bool = True,
                  entitlement: Mapping[str, Any] | None = None,
                  env: Mapping[str, str] | None = None) -> ContainerRunResult:
    """在本任务专属容器中执行一条命令,并**核实**其已停止/已移除。

    - 业务写区经 ``validate_data_dir`` 核准;非法写区直接拒绝(T07);
    - 默认要求有效执行资格(探针模式可传 entitlement_required=False);
    - 生命周期:create(-d --name) → wait(超时则 stop→kill→再核实) → logs → rm -f
      → inspect(区分"证实不存在"与"引擎/查询失败",见 remove_state 三分)。
      "等待返回"不再单独充当"已停止"的证据;
    - C6-02(U06-5.3):``completion_consistent`` 汇总 create/stop/remove/exit 的
      一致性——任何一环未确认,调用方不得把它当可供业务交付的成功。
    """
    runner = runner or subprocess.run
    scratch_host = Path(scratch_host).resolve()
    rel, refusal = validate_data_dir(scratch_host, data_dir)
    if refusal:
        raise ValueError("execution_write_area_refused:" + refusal)
    if entitlement_required:
        try:
            entitlement_doc = dict(entitlement or require_entitlement())
        except RuntimeError as exc:
            raise RuntimeError(str(exc)) from exc
    else:
        entitlement_doc = dict(entitlement or {})
    (scratch_host / Path(*rel.split("/"))).mkdir(parents=True, exist_ok=True)
    name = "opencoding-" + str(label) + "-" + uuid.uuid4().hex[:10]
    cmd = build_run_command(scratch_host, list(argv), data_dir=rel, image=image,
                            detach=True, name=name, env=env)
    lifecycle: dict[str, Any] = {
        "container_name": name,
        "container_id": None,
        "entitlement_id": entitlement_doc.get("entitlement_id"),
        "policy_digest": entitlement_doc.get("policy_digest"),
        "command": cmd,
        "timed_out": False,
        "stop_sequence": [],
        "confirmed_stopped": False,
        "confirmed_removed": False,
        "exit_code": -1,
        "logs_stdout": "",
        "logs_stderr": "",
    }
    try:
        created = _run(cmd, runner=runner, timeout=timeout + 60)
        stdout = str(getattr(created, "stdout", "") or "").strip()
        lifecycle["container_id"] = stdout.splitlines()[-1] if stdout else None
        lifecycle["create_returncode"] = getattr(created, "returncode", None)
        if lifecycle["create_returncode"] != 0:
            lifecycle["create_error"] = sanitize_tail(getattr(created, "stderr", "") or "")
        wait_timeout = max(5, int(timeout))
        try:
            waited = _run(["docker", "wait", name], runner=runner, timeout=wait_timeout + 30)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            waited = None
        except OSError as exc:
            lifecycle["wait_error"] = type(exc).__name__
            waited = None
        if timed_out or waited is None or getattr(waited, "returncode", 1) != 0:
            lifecycle["timed_out"] = bool(timed_out)
            for killer in (["docker", "stop", "-t", "5", name], ["docker", "kill", name]):
                try:
                    proc = _run(killer, runner=runner, timeout=60)
                    lifecycle["stop_sequence"].append({"cmd": killer,
                                                       "rc": getattr(proc, "returncode", None)})
                except (OSError, subprocess.TimeoutExpired) as exc:
                    lifecycle["stop_sequence"].append({"cmd": killer,
                                                       "error": type(exc).__name__})
        # 独立核实:--rm 与 wait 返回都不构成证据,必须查到 State.Running=false
        inspect = _run(["docker", "inspect", "--format", "{{.State.Running}}", name],
                       runner=runner, timeout=60)
        state = str(getattr(inspect, "stdout", "") or "").strip().lower()
        lifecycle["inspect_state"] = state
        lifecycle["confirmed_stopped"] = (state == "false")
        logs = _run(["docker", "logs", name], runner=runner, timeout=120)
        lifecycle["logs_stdout"] = str(getattr(logs, "stdout", "") or "")
        lifecycle["logs_stderr"] = str(getattr(logs, "stderr", "") or "")
        removed = _run(["docker", "rm", "-f", name], runner=runner, timeout=60)
        lifecycle["remove_returncode"] = getattr(removed, "returncode", None)
        after = _run(["docker", "inspect", "--format", "{{.Id}}", name], runner=runner, timeout=60)
        after_rc = getattr(after, "returncode", 0)
        rm_rc = getattr(removed, "returncode", None)
        # FIX-02(U06-5.4):``inspect 非零``不全是"已证明容器不存在"——引擎不可达、
        # 权限拒绝、输出不可解释同样非零。移除结论必须按**可核对证据**三分:
        #   removed       = rm 明确成功 且 随后查询证实容器不存在;
        #   still_present = 查询成功且容器仍在(无论 rm 结果如何);
        #   unverified    = rm 失败且查询也失败——效果未知,禁止当成已移除。
        # 只管理本任务登记的容器;不为探测升权限,不做全局清理。
        if rm_rc == 0 and after_rc != 0:
            lifecycle["confirmed_removed"] = True
            lifecycle["remove_state"] = "removed"
        elif after_rc == 0:
            lifecycle["confirmed_removed"] = False
            lifecycle["remove_state"] = "still_present"
            lifecycle["remove_unverified_detail"] = {
                "rm_returncode": rm_rc,
                "inspect_stderr_tail": sanitize_tail(getattr(after, "stderr", "") or ""),
            }
        else:
            lifecycle["confirmed_removed"] = False
            lifecycle["remove_state"] = "unverified"
            lifecycle["remove_unverified_detail"] = {
                "rm_returncode": rm_rc,
                "inspect_returncode": after_rc,
                "inspect_stderr_tail": sanitize_tail(getattr(after, "stderr", "") or ""),
                "note": "rm 与查询均未成功:无法区分'已不存在'与'引擎/查询失败',按未核实处理",
            }
        if waited is not None:
            raw = str(getattr(waited, "stdout", "") or "").strip()
            try:
                lifecycle["exit_code"] = int(raw.splitlines()[-1]) if raw else -1
            except ValueError:
                lifecycle["exit_code"] = -1
    finally:
        if not lifecycle["confirmed_removed"]:
            # 尽力精确清理自己创建的容器;不触碰任何他人的容器/进程
            try:
                _run(["docker", "rm", "-f", name], runner=runner, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                pass
    # C6-02(U06-5.3):一致完成判定——create 成功、容器确认已停止、确认已移除、
    # 拿到真实退出码,四者齐备才算一致;否则调用方必须按"效果未知/受阻"处理,
    # 不得把 returncode 单独当成可供业务交付的成功。
    lifecycle["completion_consistent"] = bool(
        lifecycle.get("create_returncode") == 0
        and lifecycle.get("confirmed_stopped") is True
        and lifecycle.get("confirmed_removed") is True
        and isinstance(lifecycle.get("exit_code"), int)
        and lifecycle["exit_code"] >= 0)
    stdout_text = lifecycle["logs_stdout"]
    stderr_text = lifecycle["logs_stderr"]
    if lifecycle["exit_code"] != 0 and not stderr_text:
        stderr_text = "容器执行失败:exit_code=" + str(lifecycle["exit_code"]) + \
                      ("（超时后被停止）" if lifecycle["timed_out"] else "")
    return ContainerRunResult(lifecycle["command"], lifecycle["exit_code"],
                              stdout_text, stderr_text, container=lifecycle)


def diagnose() -> dict[str, Any]:
    """给工作台/报告用的诊断视图(只读,不启动引擎)。"""
    readiness = _readiness_detail()
    capability = execution_capability()
    return {
        "kind": "docker",
        "cli_available": readiness["cli"],
        "engine_reachable": readiness["engine"],
        "engine_message": readiness["engine_message"],
        "image": readiness["image"],
        "image_present": readiness["image_present"],
        "image_message": readiness["image_message"],
        "image_id": readiness["image_id"],
        "environment_ready": readiness["ready"],
        "available": capability["available"],
        "verified_boundaries": capability["verified_boundaries"],
        "entitlement_id": capability["entitlement_id"],
        "policy_digest": readiness["policy_digest"],
        "probe_version": PROBE_VERSION,
        "note": ("环境就绪但未做边界实测前不得执行未知候选" if readiness["ready"]
                 and not capability["available"] else
                 ("已实测并通过,资格有效期内可用" if capability["available"] else
                  "引擎/镜像/边界实测需在对启动已有 Docker 的明确许可后进行")),
    }


__all__ = ["ContainerRunResult", "POLICY", "PROBE_VERSION", "PROTECTED_SEGMENTS",
           "build_run_command", "container_argv", "current_entitlement",
           "daemon_reachable", "diagnose", "docker_cli_available", "execution_capability",
           "grant_entitlement", "image_id", "image_present_locally", "load_entitlement",
           "normalize_data_dir", "probe_boundaries", "require_entitlement", "run_container",
           "policy_digest", "revoke_entitlement", "validate_data_dir"]
