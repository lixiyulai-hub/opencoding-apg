"""C6 Docker 三类许可分别核对(只读,不启动引擎、不拉镜像、不改系统)。

三项许可独立记录,互不相抵:
  A. 启动现有 Docker(引擎可达性)-- 仅做只读查询,不启动 Docker Desktop
  B. 镜像下载许可 -- 仅核对本地是否已有镜像,绝不 pull
  C. 系统变更许可 -- 本轮未授予,未做任何系统级改动
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opencoding import docker_provider as dp  # noqa: E402

OUT_DIR = Path(r"D:\OpenCoding-dev\workspace")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _ro(cmd: list[str], timeout: int = 20) -> dict:
    """只读查询:不允许出现 start/pull/run/rm/system 等变更类子命令。"""
    banned = {"start", "pull", "run", "rm", "rmi", "build", "commit",
              "system", "login", "push", "tag", "save", "load", "cp", "exec"}
    if len(cmd) > 1 and cmd[1] in banned:
        return {"ran": False, "reason": "只读核对拒绝变更类子命令:" + cmd[1]}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ran": False, "reason": type(exc).__name__}
    return {"ran": True, "exit_code": proc.returncode,
            "stdout_head": (proc.stdout or "")[:400],
            "stderr_head": (proc.stderr or "")[:400]}


report: dict = {
    "script": "_diag_docker_permission_c6.py",
    "at": datetime.now(timezone.utc).isoformat(),
    "policy": dict(dp.POLICY),
    "probe_version": dp.PROBE_VERSION,
    "permission_checks": {},
    "readiness": {},
    "execution_capability": {},
    "entitlement_file": {},
    "actions_taken": [],
    "actions_refused": [],
}

# --- A. 启动现有 Docker(引擎可达性) -----------------------------------
cli_path = shutil.which("docker")
report["permission_checks"]["A_start_existing_docker"] = {
    "docker_cli_on_path": cli_path,
    "cli_version_query": _ro(["docker", "--version"]),
    "daemon_reachable_readonly": dp.daemon_reachable(timeout=15),
    "started_anything": False,
    "note": ("仅做只读可达性查询;引擎未运行时不启动 Docker Desktop"
             "(启动属于系统变更,需单独许可,本轮未授予,也未执行)"),
}
report["actions_refused"].append("未启动 Docker Desktop / 未执行 docker start")

# --- B. 镜像下载许可 ----------------------------------------------------
image_value = dp._image()
present, present_msg = dp.image_present_locally(image_value)
identity, identity_msg = dp.image_id(image_value) if present else (None, "镜像不可用")
report["permission_checks"]["B_image_download"] = {
    "image_requested": image_value,
    "present_locally": present,
    "present_message": present_msg,
    "image_id": identity,
    "image_id_message": identity_msg,
    "pull_performed": False,
    "note": "本轮未执行 docker pull(镜像下载需单独许可,未授予)",
}
report["actions_refused"].append("未执行 docker pull")

# --- C. 系统变更许可 ----------------------------------------------------
report["permission_checks"]["C_system_change"] = {
    "granted": False,
    "performed": [],
    "note": ("未安装/卸载任何软件,未修改服务、守护进程配置、环境变量、防火墙或全局安全设置;"
             "未绑定或改动任何容器运行时配置"),
}
report["actions_refused"].append("未做任何系统级变更")

# --- 综合:产品侧口径 ----------------------------------------------------
report["readiness"] = dp._readiness_detail()
report["execution_capability"] = dp.execution_capability()
ent = dp.current_entitlement()
report["entitlement_file"] = {
    "current_entitlement": ent,
    "entitlement_dir": str(dp._entitlement_dir()),
    "path_for_current_policy": str(dp.entitlement_path(report["readiness"]["policy_digest"])),
    "ttl_seconds": dp.ENTITLEMENT_TTL_SECONDS,
}
report["conclusion"] = (
    "环境未就绪或资格未签发 -> available=False -> 未知候选一律不运行"
    if not report["execution_capability"].get("available")
    else "资格有效(仅在本机实测通过后才可能出现)"
)

out = OUT_DIR / ("docker-permission-c6-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print("REPORT", out)
print(json.dumps({
    "A_engine": report["readiness"].get("engine"),
    "A_engine_msg": report["readiness"].get("engine_message"),
    "B_image_present": report["readiness"].get("image_present"),
    "B_image": report["readiness"].get("image"),
    "C_granted": False,
    "ready": report["readiness"].get("ready"),
    "capability": report["execution_capability"],
}, ensure_ascii=False, indent=1))
