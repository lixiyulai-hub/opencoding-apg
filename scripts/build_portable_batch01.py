# -*- coding: utf-8 -*-
"""W4(2026-09-30 集成批次 01):构建包含本批修复的**新**便携产品候选。

做法(全部为复制/写入新目录,不修改既有 0.2.6 便携包,不删除任何东西):
1. 以既有 0.2.6 便携包(含私有运行时)为基底,复制到新的 0.2.7 目录;
2. 用**当前源码**的 opencoding 模块替换包内模块(本批修复进入新产品);
3. 生成 dist-info 0.2.7 与构建清单(逐文件字节+SHA256);
4. 按 build_clean_portable 的排除规则输出干净 ZIP。

用法:
  python scripts/build_portable_batch01.py \
      --source <源码根> --base <0.2.6便携目录> --dist <输出目录>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

VERSION_OLD = "0.2.6"
VERSION_NEW = "0.2.7"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--dist", required=True)
    parser.add_argument("--skip-zip", action="store_true")
    args = parser.parse_args()

    source = Path(args.source).resolve()
    base = Path(args.base).resolve()
    dist = Path(args.dist).resolve()
    if not (source / "opencoding").is_dir():
        parser.error("--source 必须包含 opencoding/")
    if not (base / "runtime-python" / "python.exe").is_file():
        parser.error("--base 必须是含 runtime-python 的便携包目录")
    dist.mkdir(parents=True, exist_ok=True)

    target = dist / f"OpenCoding-{VERSION_NEW}-portable"
    manifest: dict = {
        "product": "OpenCoding 便携版",
        "version": VERSION_NEW,
        "base_version": VERSION_OLD,
        "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_root": str(source),
        "base_portable": str(base),
        "target": str(target),
        "modules": {},
        "notes": [],
    }

    if target.exists():
        parser.error(f"目标已存在,不覆盖:{target}(请换目录或手动清理)")
    print("复制运行时基底…", flush=True)
    shutil.copytree(base, target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    sp = target / "site-packages"
    pkg = sp / "opencoding"
    pkg.mkdir(parents=True, exist_ok=True)
    # 逐文件覆盖为当前源码(不整目录删除:目录树刚从基底复制而来,
    # 只覆盖同名模块并记录差异,避免任何递归删除)。
    source_files = sorted((source / "opencoding").glob("*.py"))
    overwritten = []
    for p in source_files:
        dst = pkg / p.name
        # 先写临时名再原子替换:E: 盘对"覆盖刚复制的文件"存在间歇性拒绝,
        # 换名写入 + os.replace 既能落地也避免半写状态。
        staging = dst.with_name(dst.name + ".new")
        shutil.copyfile(p, staging)
        # E: 盘的过滤/同步驱动会短暂锁住刚复制出来的文件;带退避重试
        last_error: OSError | None = None
        for attempt in range(6):
            try:
                os.replace(staging, dst)
                last_error = None
                break
            except PermissionError as exc:
                last_error = exc
                time.sleep(1.0 + attempt)
        if last_error is not None:
            raise last_error
        try:
            dst.chmod(0o644)
        except OSError:
            pass
        overwritten.append(p.name)
    residual = [p.name for p in pkg.glob("*.py") if p.name not in {q.name for q in source_files}]
    if residual:
        manifest["notes"].append("包内存在源码中没有的模块(保留原样):" + "、".join(residual))
    manifest["overwritten_modules"] = overwritten

    # dist-info 0.2.6 → 0.2.7(只改刚复制出来的副本)
    for old_info in list(sp.glob(f"opencoding_local_entry-{VERSION_OLD}.dist-info")):
        new_info = sp / f"opencoding_local_entry-{VERSION_NEW}.dist-info"
        shutil.move(str(old_info), str(new_info))
        meta = new_info / "METADATA"
        if meta.is_file():
            text = meta.read_text(encoding="utf-8")
            meta.write_text(text.replace(f"Version: {VERSION_OLD}",
                                         f"Version: {VERSION_NEW}"), encoding="utf-8")
        record = new_info / "RECORD"
        if record.is_file():
            manifest["notes"].append("RECORD 沿用 0.2.6 行集,哈希以 build manifest 为准")

    modules = {}
    for p in sorted(pkg.glob("*.py")):
        data = p.read_bytes()
        modules[p.name] = {"bytes": len(data), "sha256": sha(data)}
        src = source / "opencoding" / p.name
        modules[p.name]["matches_source"] = sha(src.read_bytes()) == sha(data)
    manifest["modules"] = modules
    manifest["modules_matching_source"] = all(
        m["matches_source"] for m in modules.values())

    cli = target / "scripts" / "opencoding-version.txt"
    cli.parent.mkdir(exist_ok=True)
    cli.write_text(VERSION_NEW + "\n", encoding="utf-8")

    manifest_path = target / "build_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    print("已构建:", target)

    if not args.skip_zip:
        zip_path = dist / f"OpenCoding-{VERSION_NEW}-portable-clean.zip"
        excluded = []
        kept = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in sorted(target.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(target).as_posix()
                low = rel.lower()
                if "__pycache__" in low or low.endswith(".pyc") or low.endswith(".pth"):
                    excluded.append({"path": rel, "reason": "缓存/site 定制路径"})
                    continue
                head = ""
                if low.endswith("direct_url.json"):
                    try:
                        head = p.read_text(encoding="utf-8", errors="replace")[:300]
                    except OSError:
                        head = ""
                    if any(m in head for m in ("抖音助手", "dy_helper", "_editable_impl", "儿童知行星球")):
                        excluded.append({"path": rel, "reason": "外部项目/开发机路径标记"})
                        continue
                z.write(p, rel)
                kept += 1
        data = zip_path.read_bytes()
        summary = {
            "zip": zip_path.name, "bytes": len(data), "sha256": sha(data),
            "kept": kept, "excluded": excluded,
            "modules_matching_source": manifest["modules_matching_source"],
        }
        (dist / f"OpenCoding-{VERSION_NEW}-portable-clean.build_manifest.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        manifest["zip"] = summary
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        print("ZIP:", zip_path, len(data), summary["sha256"])
    print("BUILD_COMPLETE", json.dumps({"modules": len(modules),
                                        "all_match_source": manifest["modules_matching_source"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
