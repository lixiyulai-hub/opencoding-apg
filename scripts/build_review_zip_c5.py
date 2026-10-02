# -*- coding: utf-8 -*-
"""CP5 最终审核 ZIP 打包:source/preimage/evidence/dist-ref/reproduce + 报告 + 清单。

清单覆盖实际文件除自身;ZIP 大小/SHA 打印旁挂,不自指。
禁含:密钥/认证头/cookie/__pycache__/egg-info/构建生成物/本地治理证据区。
"""
import hashlib
import json
import sys
import zipfile
from pathlib import Path

SRC_ROOT = Path(r"E:\儿童知行星球\.tmp\opencoding-product-foundation-20260905\integration")
PREIMAGE = Path(r"D:\OpenCoding-dev\preimage")
EV = Path(r"D:\OpenCoding-dev\audit-c5-out")
DIST = Path(r"D:\OpenCoding-dev\dist")
OUT = Path(r"D:\OpenCoding-dev\OpenCoding-Review-CP5-Final-20260928-v1.zip")

SOURCE_TOP_DIRS = ["opencoding", "tests", "scripts", "docs"]
SOURCE_TOP_FILES = ["AGENTS.md", "README.md", "README_CN.md", "ARCHITECTURE.md",
                    "MANIFEST.in", "pyproject.toml", "QUALITY_GATES.md", "PROJECT_BRIEF.md"]

EV_EVIDENCE = ["test-run-index.json", "operations_log.md",
               "final_core_44_tests.log", "final_neighbors_97_tests.log",
               "real_ai_chain_c5.log", "workbench_smoke_c5.log", "portable_smoke_c5.log"]


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def want_source(rel: str) -> bool:
    low = rel.lower()
    if "__pycache__" in low or low.endswith(".pyc"):
        return False
    if "egg-info" in low or low.startswith("build/") or low.startswith("output/"):
        return False
    return True


def main():
    if OUT.exists():
        # 纯改名隔离,不删除旧半成品(删除安全规则:改名优先于删除)
        import time
        quarantine = OUT.with_name(OUT.name + ".incomplete-%d" % int(time.time()))
        OUT.rename(quarantine)
        print("QUARANTINED:", quarantine.name)
    entries: list[tuple[str, bytes]] = []  # (arcname, data)

    # ---- 报告与说明 ----
    for name in ("REPORT_CN.md", "EXPORT_NOTE.md"):
        entries.append((name, (EV / name).read_bytes()))

    start_here = """# 00_START_HERE —— CP5 最终审核包

1. 先读 `REPORT_CN.md`（按 S01–S11、REAL-01..16、A01–A28 分组的中文回报，四线分开）。
2. 口径/同版声明/脱敏清单见 `EXPORT_NOTE.md`；逐文件 SHA 见 `FILE_MANIFEST_SHA256.txt`。
3. 复现步骤见 `reproduce/REPRODUCE_CN.md`；原始日志在 `evidence/`；改动前像在 `preimage/`。
4. 历史失败与已封口事务未改写；本包只新增。真实 AI 请求共 3 次的完整记录（含执行 AI 误杀第 1 次致效果未知）在 REPORT_CN.md §7 与 evidence/operations_log.md，如实不隐瞒。
"""
    entries.append(("00_START_HERE.md", start_here.encode("utf-8")))

    # ---- source ----
    kept = 0
    for d in SOURCE_TOP_DIRS:
        base = SRC_ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(SRC_ROOT).as_posix().replace("\\", "/")
            if not want_source(rel):
                continue
            entries.append(("source/" + rel, p.read_bytes()))
            kept += 1
    for f in SOURCE_TOP_FILES:
        p = SRC_ROOT / f
        if p.is_file():
            entries.append(("source/" + f, p.read_bytes()))
            kept += 1

    # ---- preimage ----
    pre_note = ("前像来源：CP4 审核包 OpenCoding-R17C-Checkpoint4-Audit-and-Continuation-20260928-v1.zip\n"
                "（SHA256 285187b11db2a9d1e8ac8f0fba6f3aa49d557b81eb42b85d8b2c5991470487d6）。\n"
                "本批直接改动模块的前像；全量差异 = CP4 包 source 与本包 source 的 diff。\n")
    entries.append(("preimage/PREIMAGE_NOTE.txt", pre_note.encode("utf-8")))
    pre_n = 0
    for p in sorted(PREIMAGE.glob("*.preimage")):
        entries.append(("preimage/" + p.name, p.read_bytes()))
        pre_n += 1

    # ---- evidence ----
    ev_n = 0
    for name in EV_EVIDENCE:
        p = None
        for cand in (EV / "evidence" / name, EV / name):
            if cand.is_file():
                p = cand
                break
        if p is None:
            print("WARN evidence missing:", name)
            continue
        entries.append(("evidence/" + name, p.read_bytes()))
        ev_n += 1
    for p in DIST.glob("module_sha_0.2.4.json"):
        entries.append(("evidence/" + p.name, p.read_bytes()))
        ev_n += 1
    bm = DIST / "OpenCoding-0.2.4-portable-clean.build_manifest.json"
    if bm.is_file():
        entries.append(("evidence/" + bm.name, bm.read_bytes()))
        ev_n += 1

    # ---- dist-ref ----
    wheel = DIST / "opencoding_local_entry-0.2.4-py3-none-any.whl"
    entries.append(("dist-ref/" + wheel.name, wheel.read_bytes()))
    wb = wheel.read_bytes()
    dist_note = ("# 大件旁挂引用（本体不入包）\n\n"
                 "| 产物 | 大小 | SHA256 |\n|---|---|---|\n"
                 "| opencoding_local_entry-0.2.4-py3-none-any.whl | %d B（原物在 dist-ref/） | `%s` |\n"
                 "| OpenCoding-0.2.4-portable-clean.zip | 65,838,986 B | `25844edaa498512331f43c940ea02f5ee4c3c909c1d415284598e3b039c1e79b` |\n"
                 % (len(wb), sha256(wb)))
    entries.append(("dist-ref/DIST_ARTIFACTS.md", dist_note.encode("utf-8")))

    # ---- reproduce ----
    entries.append(("reproduce/REPRODUCE_CN.md", (EV / "REPRODUCE_CN.md").read_bytes()))

    # ---- 写 ZIP ----
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for arc, data in entries:
            z.writestr(arc, data)

    # ---- 清单（覆盖除自身外全部）----
    lines = []
    zb = OUT.read_bytes()
    with zipfile.ZipFile(OUT) as z:
        for info in z.infolist():
            data = z.read(info.filename)
            lines.append("%s  %s  %d" % (sha256(data), info.filename, len(data)))
    manifest = ("# FILE_MANIFEST_SHA256（覆盖除本清单外全部文件）\n"
                "# ZIP %s 大小 %d SHA256 %s\n" % (OUT.name, len(zb), sha256(zb))
                + "\n".join(lines) + "\n")
    with zipfile.ZipFile(OUT, "a", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("FILE_MANIFEST_SHA256.txt", manifest)

    zb2 = OUT.read_bytes()
    print(json.dumps({"zip": OUT.name, "size": len(zb2), "sha256": sha256(zb2),
                      "source_files": kept, "preimage": pre_n, "evidence": ev_n,
                      "total_entries": len(entries) + 2}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
