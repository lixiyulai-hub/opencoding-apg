"""C6 交付打包:源码包 + 脱敏复核/证据包(不动 git、不改动任何源文件)。"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(r"D:\OpenCoding-dev\dist")
OUT.mkdir(parents=True, exist_ok=True)
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
VERSION = "0.2.6"

# ---- 源码范围(白名单目录 + 白名单根文件) --------------------------------
INCLUDE_DIRS = ["opencoding", "tests", "scripts", "docs", "apps", "services"]
ROOT_FILES = ["AGENTS.md", "ARCHITECTURE.md", "IMPLEMENTATION_REPORT.md", "MANIFEST.in",
              "PROJECT_BRIEF.md", "QUALITY_GATES.md", "README.md", "README_CN.md",
              "pyproject.toml"]
EXCLUDE_DIR_NAMES = {"__pycache__", ".git", ".tmp", "artifacts", "review-envs",
                     "build", "output", "$stage", ".venv", "venv", "node_modules",
                     "dist", "egg-info"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".lock", ".db", ".sqlite3", ".log"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_source_files():
    seen: list[Path] = []
    for name in INCLUDE_DIRS:
        base = ROOT / name
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT)
            if any(part in EXCLUDE_DIR_NAMES for part in rel.parts):
                continue
            if p.suffix in EXCLUDE_SUFFIX:
                continue
            if p.name.endswith(".egg-info"):
                continue
            seen.append(p)
    for name in ROOT_FILES:
        p = ROOT / name
        if p.is_file():
            seen.append(p)
    return seen


def main() -> None:
    files = iter_source_files()
    staging = OUT / f"_src_stage_{STAMP}"
    if staging.exists():
        shutil.rmtree(staging)  # 仅本脚本自建的一次性暂存目录
    staging.mkdir(parents=True)

    manifest_lines = []
    for p in files:
        rel = p.relative_to(ROOT)
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        manifest_lines.append(f"{sha256(p)}  {p.stat().st_size:>9}  {rel.as_posix()}")
    (staging / "FILE_MANIFEST_SHA256.txt").write_text(
        "\n".join(sorted(manifest_lines)), encoding="utf-8")

    src_zip = OUT / f"OpenCoding-{VERSION}-source.zip"
    if src_zip.exists():
        src_zip.unlink()
    shutil.make_archive(str(src_zip.with_suffix("")), "zip", root_dir=staging)
    shutil.rmtree(staging)

    print("SOURCE_ZIP", src_zip, src_zip.stat().st_size, sha256(src_zip))
    print("FILE_COUNT", len(files))
    (OUT / f"source_manifest_{VERSION}_{STAMP}.txt").write_text(
        "\n".join(sorted(manifest_lines)), encoding="utf-8")
    print("MANIFEST", OUT / f"source_manifest_{VERSION}_{STAMP}.txt")


if __name__ == "__main__":
    main()
