"""同版校验:wheel / portable 内的 opencoding 模块逐字节比对工作树源码。"""
from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = Path(r"D:\OpenCoding-dev\dist")
VERSION = "0.2.6"
WHEEL = DIST / f"opencoding_local_entry-{VERSION}-py3-none-any.whl"
PORTABLE = DIST / f"OpenCoding-{VERSION}-portable"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main() -> None:
    src = {p.name: p.read_bytes() for p in sorted((ROOT / "opencoding").glob("*.py"))}
    report: dict = {"version": VERSION, "at": datetime.now().isoformat(),
                    "source_modules": len(src), "wheel": str(WHEEL), "checks": {}}

    # wheel
    with zipfile.ZipFile(WHEEL) as z:
        wheel_mods = {n.split("/", 1)[1]: z.read(n)
                      for n in z.namelist()
                      if n.startswith("opencoding/") and n.endswith(".py")}
    same, diff, missing = [], [], []
    for name, data in src.items():
        w = wheel_mods.get(name)
        if w is None:
            missing.append(name)
        elif sha(w) == sha(data):
            same.append(name)
        else:
            diff.append(name)
    report["checks"]["wheel_vs_source"] = {
        "modules_in_wheel": len(wheel_mods), "same": len(same), "diff": diff,
        "missing_in_wheel": missing,
        "wheel_sha256": sha(WHEEL.read_bytes()), "wheel_bytes": WHEEL.stat().st_size,
    }

    # portable
    pdir = PORTABLE / "site-packages" / "opencoding"
    if pdir.is_dir():
        psame, pdiff, pmiss = [], [], []
        for name, data in src.items():
            f = pdir / name
            if not f.is_file():
                pmiss.append(name)
            elif sha(f.read_bytes()) == sha(data):
                psame.append(name)
            else:
                pdiff.append(name)
        report["checks"]["portable_vs_source"] = {
            "same": len(psame), "diff": pdiff, "missing": pmiss,
            "plansource_present": (pdir / "plansource.py").is_file(),
        }

    # 便携包 dist-info 版本
    for d in sorted((PORTABLE / "site-packages").glob("*.dist-info")):
        meta = d / "METADATA"
        if meta.is_file():
            for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Version:"):
                    report["portable_dist_info"] = {"dir": d.name, "version": line.split(":", 1)[1].strip()}
        break

    clean = DIST / f"OpenCoding-{VERSION}-portable-clean.zip"
    if clean.is_file():
        report["clean_zip"] = {"path": str(clean), "bytes": clean.stat().st_size,
                               "sha256": sha(clean.read_bytes())}

    out = DIST / f"module_sha_{VERSION}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
