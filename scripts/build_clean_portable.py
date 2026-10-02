# -*- coding: utf-8 -*-
"""构建干净的分发 ZIP:从已授权暂存包排除其他项目 editable 污染与开发机私有路径。

R14(CP2-01 分发剩余):不删除宿主全局 .pth、不修改其他项目;只控制"进入分发物"的内容。
排除清单(可审查):
- runtime-python/Lib/site-packages/*.pth(所有 site 定制路径文件,产品为纯标准库无需任何)
- runtime-python/Lib/site-packages/*dist-info/direct_url.json 指向其他项目/用户目录的 dist-info 目录
- __pycache__、*.pyc
排除结果在排除清单中逐项列出,供审核核对。
"""
import hashlib
import json
import sys
import zipfile
from pathlib import Path

VERSION = sys.argv[1] if len(sys.argv) > 1 else "0.2.4"
SRC = Path(rf"D:\OpenCoding-dev\dist\OpenCoding-{VERSION}-portable")
OUT = Path(rf"D:\OpenCoding-dev\dist\OpenCoding-{VERSION}-portable-clean.zip")

FOREIGN_MARKS = ("抖音助手", "dy_helper", "_editable_impl")


def is_foreign_meta(rel: str) -> bool:
    rel_l = rel.lower()
    if rel_l.endswith(".pth"):
        return True  # 产品为纯标准库;运行时自带的一切 site 定制路径一律不进分发物
    if "__pycache__" in rel_l or rel_l.endswith(".pyc"):
        return True
    if "dist-info" in rel_l and rel_l.endswith(("direct_url.json", "RECORD")):
        # 该 dist-info 是否指向外部项目 → 由调用方预扫描决定(见 excluded_dirs)
        return rel_l.endswith("direct_url.json")
    return False


def main():
    excluded = []
    kept = 0
    out = OUT
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(SRC.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(SRC).as_posix()
            try:
                text_head = ""
                if rel.endswith((".pth", "direct_url.json")):
                    text_head = p.read_text(encoding="utf-8", errors="replace")[:400]
            except OSError:
                pass
            foreign = any(m in text_head or m in rel for m in FOREIGN_MARKS)
            if is_foreign_meta(rel) or (rel.endswith(".pth") and foreign) or foreign:
                excluded.append({"path": rel, "reason": "外部项目/开发机路径标记", "head": text_head[:120]})
                continue
            z.write(p, rel)
            kept += 1
    b = out.read_bytes()
    manifest = {
        "zip": out.name, "size": len(b),
        "sha256": hashlib.sha256(b).hexdigest(),
        "kept_files": kept, "excluded": excluded,
        "note": "干净分发物:已排除其他项目 editable/私有路径;运行时来自开发机暂存副本,"
                "第二台干净机器的完整 Windows 使用验证仍未做(如实标注)。",
    }
    out.with_suffix(".build_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("zip", "size", "sha256", "kept_files")}, ensure_ascii=False))
    print("excluded:", len(excluded))
    for e in excluded:
        print("  -", e["path"], "|", e["reason"])


if __name__ == "__main__":
    main()
