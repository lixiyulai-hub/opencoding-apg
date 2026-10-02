# -*- coding: utf-8 -*-
"""C6FIX4 轮(R01-A/B/C + R02b 未知分类修复)复核 ZIP(2026-09-29)。

固定结构:EXPORT_NOTE + 00_START_HERE + report/IMPLEMENTATION_REPORT.md
+ FILE_MANIFEST_SHA256.txt + source/ + evidence/ + package/。
manifest 头**不自指** ZIP 摘要;最终 ZIP 摘要写入包外 sidecar。
不覆盖上一轮 ZIP;禁含 .env、密钥、完整认证头、虚拟环境、历史治理目录。
"""
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

SRC_ROOT = Path(r"E:\儿童知行星球\.tmp\opencoding-product-foundation-20260905\integration")
OUT_DIR = Path(r"D:\OpenCoding-dev\audit-c6fix4-out")
DIST = Path(r"D:\OpenCoding-dev\dist")
OUT = Path(r"D:\OpenCoding-dev\OpenCoding-Review-C6FIX4-20260929-v1.zip")

SOURCE_TOP_DIRS = ["opencoding", "tests", "scripts", "docs"]
SOURCE_TOP_FILES = ["AGENTS.md", "README.md", "README_CN.md", "ARCHITECTURE.md",
                    "MANIFEST.in", "pyproject.toml", "QUALITY_GATES.md", "PROJECT_BRIEF.md"]
REGRESSION_LOGS = [
    "regression_group1_c6fix4.log",
    "regression_group2_c6fix4.log",
    "regression_group3_c6fix4.log",
]


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def want_source(rel: str) -> bool:
    low = rel.lower()
    if "__pycache__" in low or low.endswith(".pyc"):
        return False
    if "egg-info" in low or low.startswith("build/") or low.startswith("output/"):
        return False
    return True


def build_run_index() -> dict:
    """从原始日志解析 test-run-index(不自写数字,以日志为准)。"""
    index = []
    for name in REGRESSION_LOGS:
        p = OUT_DIR / "evidence" / name
        text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        ran = re.search(r"Ran (\d+) tests? in ([\d.]+)s", text)
        ok = re.search(r"^(OK|FAILED)", text, re.M)
        skip = re.search(r"^(OK) \(skipped=(\d+)\)", text, re.M)
        exit_line = re.search(r"group\w+_exit=(\d+)", text)
        index.append({
            "log": name,
            "tests": int(ran.group(1)) if ran else 0,
            "seconds": float(ran.group(2)) if ran else None,
            "verdict": ok.group(1) if ok else "NO_VERDICT",
            "skipped": int(skip.group(2)) if skip else 0,
            "exit_code": int(exit_line.group(1)) if exit_line else None,
            "kind": "impact-surface unittest (no real AI / no docker engine / no network; loopback socket observations only)",
        })
    return {"round": "C6FIX4-20260929", "python": "3.13.12 (managed)",
            "runner": "python -m unittest -v", "groups": index}


def main() -> int:
    if OUT.exists():
        import time
        OUT.rename(OUT.with_name(OUT.name + ".incomplete-%d" % int(time.time())))
        print("QUARANTINED old:", OUT.name)
    entries: list[tuple[str, bytes]] = []

    # ---- 说明与报告 ----
    entries.append(("EXPORT_NOTE.md", (OUT_DIR / "EXPORT_NOTE.md").read_bytes()))
    report = (OUT_DIR / "IMPLEMENTATION_REPORT.md").read_bytes()
    entries.append(("report/IMPLEMENTATION_REPORT.md", report))

    start = """# 00_START_HERE —— C6FIX4 轮复核包(2026-09-29)

1. 先读 `report/IMPLEMENTATION_REPORT.md`(中文回报:R01-A confirm 严格布尔契约 / R01-B 签发边界复验与孤儿撤销 / R01-C 预览-签发同源与既有预算展示 / R02b 端口观测三态分类与平台实测差异 / 三项报告数字更正 / 回归口径 / 未验证边界)。
2. 口径与脱敏说明见 `EXPORT_NOTE.md`;逐文件 SHA-256 见 `FILE_MANIFEST_SHA256.txt`(清单头不自指 ZIP 摘要;最终 ZIP 摘要见交付消息与包外 sidecar)。
3. `source/`= 完整源码(含 C6FIX3 及本轮全部修复);`evidence/`= 三组影响面回归原始日志 + 解析索引;`package/`= 历史 0.2.6 wheel(**不含任何 C6FIX 轮修复**,未重建,如实标注)。
4. 本轮 0 次真实 AI 请求、未启动 Docker、未下载镜像、未扩额;网络活动仅限本机回环受控端点;不提交 Git、不发布、不部署、不付费、不改全局安全设置。
"""
    entries.append(("00_START_HERE.md", start.encode("utf-8")))

    # ---- source ----
    kept = 0
    for d in SOURCE_TOP_DIRS:
        base = SRC_ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(SRC_ROOT).as_posix()
            if not want_source(rel):
                continue
            entries.append(("source/" + rel, p.read_bytes()))
            kept += 1
    for f in SOURCE_TOP_FILES:
        p = SRC_ROOT / f
        if p.is_file():
            entries.append(("source/" + f, p.read_bytes()))
            kept += 1

    # ---- evidence:回归原始日志 + 解析索引 ----
    ev_n = 0
    for name in REGRESSION_LOGS:
        p = OUT_DIR / "evidence" / name
        if p.is_file():
            entries.append(("evidence/" + name, p.read_bytes()))
            ev_n += 1
    index = build_run_index()
    entries.append(("evidence/test-run-index.json",
                    json.dumps(index, ensure_ascii=False, indent=1).encode("utf-8")))

    # ---- package:历史构建物(如实标注不含任何 C6FIX 轮修复) ----
    wheel = DIST / "opencoding_local_entry-0.2.6-py3-none-any.whl"
    wb = wheel.read_bytes()
    entries.append(("package/" + wheel.name, wb))
    note = ("# 同版分发物说明(**重要**:wheel 为历史 0.2.6 构建物,不含本轮及"
            "任何历史 C6FIX 轮修复;全部修复以 source/ 为准,未重建,本轮未安装)\n\n"
            "| 产物 | 大小(B) | SHA256 | 在包内 |\n|---|---|---|---|\n"
            "| opencoding_local_entry-0.2.6-py3-none-any.whl | %d | `%s` | 是(package/) |\n"
            % (len(wb), sha256(wb)))
    entries.append(("package/PACKAGE_ARTIFACTS.md", note.encode("utf-8")))

    # 同名去重后写包
    dedup: dict[str, bytes] = {}
    for arc, data in entries:
        dedup[arc] = data
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for arc, data in dedup.items():
            z.writestr(arc, data)

    # manifest:覆盖除本清单自身外全部文件;头部不自指 ZIP 摘要
    lines = []
    with zipfile.ZipFile(OUT) as z:
        for info in z.infolist():
            data = z.read(info.filename)
            lines.append("%s  %s  %d" % (sha256(data), info.filename, len(data)))
    manifest = ("# FILE_MANIFEST_SHA256(覆盖除本清单外全部文件;"
                "最终 ZIP 摘要由外部 sidecar 与交付消息提供)\n"
                + "\n".join(lines) + "\n")
    with zipfile.ZipFile(OUT, "a", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("FILE_MANIFEST_SHA256.txt", manifest)

    zb2 = OUT.read_bytes()
    sidecar = OUT.with_suffix(OUT.suffix + ".SHA256.txt")
    sidecar.write_text("%s  %s  %d\n" % (sha256(zb2), OUT.name, len(zb2)),
                       encoding="utf-8", newline="\n")
    print(json.dumps({"zip": OUT.name, "size": len(zb2), "sha256": sha256(zb2),
                      "sidecar": sidecar.name, "source_files": kept,
                      "evidence_logs": ev_n, "regression": index["groups"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
