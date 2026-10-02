# -*- coding: utf-8 -*-
"""C6 轮次复核 ZIP:EXPORT_NOTE + FILE_MANIFEST + source/ + report/ + evidence/ + package/。

禁含:.env、密钥、完整认证头、虚拟环境、其他项目资料、历史治理目录。
"""
import hashlib
import json
import sys
import zipfile
from pathlib import Path

SRC_ROOT = Path(r"E:\儿童知行星球\.tmp\opencoding-product-foundation-20260905\integration")
EV = Path(r"D:\OpenCoding-dev\audit-c6-out")
DIST = Path(r"D:\OpenCoding-dev\dist")
WS = Path(r"D:\OpenCoding-dev\workspace")
OUT = Path(r"D:\OpenCoding-dev\OpenCoding-Review-CP5-CONT-20260928-v1.zip")

SOURCE_TOP_DIRS = ["opencoding", "tests", "scripts", "docs"]
SOURCE_TOP_FILES = ["AGENTS.md", "README.md", "README_CN.md", "ARCHITECTURE.md",
                    "MANIFEST.in", "pyproject.toml", "QUALITY_GATES.md", "PROJECT_BRIEF.md"]
EXTRA_EVIDENCE = [
    "docker-permission-c6-20260928-212245.json",
    "novice-http-smoke-20260928-205051.json",
    "module_identity_c6.log",
    "module_sha_0.2.6.json",
    "OpenCoding-0.2.6-portable-clean.build_manifest.json",
    "source_manifest_0.2.6_20260928-231012.txt",
    "full_suite_c6_errors.txt",
    "full_suite_c6_final.log",
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


def main() -> int:
    if OUT.exists():
        import time
        OUT.rename(OUT.with_name(OUT.name + ".incomplete-%d" % int(time.time())))
        print("QUARANTINED old:", OUT.name)
    entries: list[tuple[str, bytes]] = []

    # ---- 说明与报告 ----
    for name in ("EXPORT_NOTE.md", "REPORT_CN.md"):
        p = EV / name
        if p.is_file():
            entries.append((name, p.read_bytes()))
    entries.append(("report/REPORT_CN.md", (EV / "REPORT_CN.md").read_bytes()))

    start = """# 00_START_HERE —— CP5 后续轮(C6)复核包

1. 先读 `REPORT_CN.md`(中文回报:许可核对 / 五项接通 / 真实执行 / 真实修复 / 新手验收 / 同版交付)。
2. 口径与脱敏说明见 `EXPORT_NOTE.md`;逐文件 SHA-256 见 `FILE_MANIFEST_SHA256.txt`。
3. `source/`= 完整源码;`evidence/`= 原始证据;`package/`= 同版分发物(wheel 本体,便携包旁挂引用)。
4. 本轮不提交 Git、不发布、不部署、不付费、不改全局安全设置。
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

    # ---- evidence ----
    ev_n = 0
    for name in EXTRA_EVIDENCE:
        p = EV / "evidence" / name
        if p.is_file():
            entries.append(("evidence/" + name, p.read_bytes()))
            ev_n += 1
    for p in sorted(WS.glob("c6-raw-capture-*.json")):
        entries.append(("evidence/" + p.name, p.read_bytes()))
        ev_n += 1
    for p in sorted(WS.glob("*.log")):
        entries.append(("evidence/" + p.name, p.read_bytes()))
        ev_n += 1

    # ---- package ----
    wheel = DIST / "opencoding_local_entry-0.2.6-py3-none-any.whl"
    wb = wheel.read_bytes()
    entries.append(("package/" + wheel.name, wb))
    clean = DIST / "OpenCoding-0.2.6-portable-clean.zip"
    note = ("# 同版分发物\n\n"
            "| 产物 | 大小(B) | SHA256 | 在包内 |\n|---|---|---|---|\n"
            "| opencoding_local_entry-0.2.6-py3-none-any.whl | %d | `%s` | 是(package/) |\n"
            "| OpenCoding-0.2.6-portable-clean.zip | %d | `%s` | 否(65MB 大件,单独交付) |\n"
            "| OpenCoding-0.2.6-source.zip | %d | `%s` | 否(单独交付) |\n"
            "| opencoding_local_entry-0.2.6.tar.gz | %d | `%s` | 否(单独交付) |\n"
            % (len(wb), sha256(wb), clean.stat().st_size, sha256(clean.read_bytes()),
               (DIST / "OpenCoding-0.2.6-source.zip").stat().st_size,
               sha256((DIST / "OpenCoding-0.2.6-source.zip").read_bytes()),
               (DIST / "opencoding_local_entry-0.2.6.tar.gz").stat().st_size,
               sha256((DIST / "opencoding_local_entry-0.2.6.tar.gz").read_bytes())))
    entries.append(("package/PACKAGE_ARTIFACTS.md", note.encode("utf-8")))

    # 同名去重(后写覆盖先写),保证包内无重复条目
    dedup: dict[str, bytes] = {}
    for arc, data in entries:
        dedup[arc] = data
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for arc, data in dedup.items():
            z.writestr(arc, data)

    lines = []
    with zipfile.ZipFile(OUT) as z:
        for info in z.infolist():
            data = z.read(info.filename)
            lines.append("%s  %s  %d" % (sha256(data), info.filename, len(data)))
    zb = OUT.read_bytes()
    manifest = ("# FILE_MANIFEST_SHA256(覆盖除本清单外全部文件)\n"
                "# ZIP %s 大小 %d SHA256 %s\n" % (OUT.name, len(zb), sha256(zb))
                + "\n".join(lines) + "\n")
    with zipfile.ZipFile(OUT, "a", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("FILE_MANIFEST_SHA256.txt", manifest)

    zb2 = OUT.read_bytes()
    print(json.dumps({"zip": OUT.name, "size": len(zb2), "sha256": sha256(zb2),
                      "source_files": kept, "evidence": ev_n,
                      "entries": len(entries) + 2}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
