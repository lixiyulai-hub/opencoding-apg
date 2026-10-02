# -*- coding: utf-8 -*-
"""打包本批唯一交付 ZIP(W4)。

内容:报告、当前状态、完整源码、新构建产品(嵌套便携 ZIP)、验收矩阵、
权限与受阻、原始证据、脚本、清单。清单不含自身,ZIP 不自指哈希。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

BATCH = "v2-integrated-batch01-20260930"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def add(zf: zipfile.ZipFile, root: Path, arc_prefix: str, collected: list[dict],
        exclude=None) -> None:
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if exclude and exclude(rel):
            continue
        low = rel.lower()
        if "__pycache__" in low or low.endswith((".pyc", ".pyo")):
            continue
        data = p.read_bytes()
        name = f"{arc_prefix}/{rel}" if arc_prefix else rel
        zf.writestr(name, data)
        collected.append({"path": name, "bytes": len(data), "sha256": sha(data)})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--integration", required=True, help="integration 源码根")
    parser.add_argument("--batch", required=True, help="本批事务目录")
    parser.add_argument("--out", required=True, help="输出 ZIP 路径")
    args = parser.parse_args()

    integration = Path(args.integration).resolve()
    batch = Path(args.batch).resolve()
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        parser.error(f"输出已存在,不覆盖:{out}")

    collected: list[dict] = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # 报告与状态
        for name in ("IMPLEMENTATION_REPORT.md", "CURRENT_STATE.json"):
            p = batch / name
            if p.is_file():
                zf.writestr(name, p.read_bytes())
                collected.append({"path": name, "bytes": p.stat().st_size,
                                  "sha256": sha(p.read_bytes())})

        # 完整源码(与 C6FIX5 同口径:产品模块/测试/脚本/文档/配置)
        src_root = integration
        add(zf, src_root / "opencoding", "source/opencoding", collected)
        add(zf, src_root / "tests", "source/tests", collected)
        add(zf, src_root / "docs", "source/docs", collected)
        add(zf, src_root / "scripts", "source/scripts", collected)
        for name in ("pyproject.toml", "AGENTS.md", "README.md", "README_CN.md",
                     "MANIFEST.in", "ARCHITECTURE.md", "PROJECT_BRIEF.md",
                     "QUALITY_GATES.md"):
            p = src_root / name
            if p.is_file():
                zf.writestr(f"source/{name}", p.read_bytes())
                collected.append({"path": f"source/{name}", "bytes": p.stat().st_size,
                                  "sha256": sha(p.read_bytes())})

        # 新构建产品(嵌套便携 ZIP)
        product_zip = batch / "product3" / "OpenCoding-0.2.7-portable-clean.zip"
        if product_zip.is_file():
            data = product_zip.read_bytes()
            zf.writestr(f"product/{product_zip.name}", data)
            collected.append({"path": f"product/{product_zip.name}",
                              "bytes": len(data), "sha256": sha(data)})
            pmanifest = (batch / "product3" / "OpenCoding-0.2.7-portable"
                         / "build_manifest.json")
            if pmanifest.is_file():
                zf.writestr("product/build_manifest.json", pmanifest.read_bytes())
                collected.append({"path": "product/build_manifest.json",
                                  "bytes": pmanifest.stat().st_size,
                                  "sha256": sha(pmanifest.read_bytes())})
        else:
            zf.writestr("product/STATUS.txt",
                        "本批未生成新构建(说明见报告)。\n")

        # review / evidence / scripts
        for sub, arc in (("review", "review"), ("scripts", "scripts"),
                         ("probes", "scripts/probes")):
            d = batch / sub
            if d.is_dir():
                add(zf, d, arc, collected)

        ev = batch / "evidence"
        if ev.is_dir():
            for child in sorted(ev.iterdir()):
                if child.is_file():
                    zf.writestr(f"evidence/{child.name}", child.read_bytes())
                    collected.append({"path": f"evidence/{child.name}",
                                      "bytes": child.stat().st_size,
                                      "sha256": sha(child.read_bytes())})
                elif child.is_dir():
                    add(zf, child, f"evidence/{child.name}", collected)

        # 前一轮输入包(被审基线)与审核材料
        for rel, arc in (("input/OpenCoding-Review-C6FIX5-20260930-v1.zip", None),):
            pass  # 不嵌套历史大包;基线以 source/ 与 review/CHANGELOG 说明

        manifest = {
            "schema": "opencoding-batch-delivery-v1",
            "batch": BATCH,
            "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "files": collected,
            "note": "清单不含自身;外层 ZIP 的哈希由交付消息给出,不写入本清单。",
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))

    data = out.read_bytes()
    print("PACKED", out)
    print("BYTES", len(data))
    print("SHA256", sha(data))
    print("ENTRIES", len(collected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
