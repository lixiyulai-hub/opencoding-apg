"""Offline installation provenance inspection; never imports discovered packages."""
from __future__ import annotations

import argparse
import hashlib
from importlib.machinery import PathFinder
import json
from pathlib import Path
import re
import sys


def _hash(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix in {".py", ".md"}:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def inspect_installation(*, skill_path=None, apg_root=None, runtime_root=None,
                         search_path=None, manifest_path=None) -> dict:
    manifest_file = Path(manifest_path) if manifest_path else Path(__file__).with_name("installation_manifest.json")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    issues = []
    paths = list(sys.path if search_path is None else search_path)
    if runtime_root is not None:
        paths.insert(0, str(Path(runtime_root).resolve()))
    spec = PathFinder.find_spec("opencoding", paths)
    native = {"available": False, "origin": None, "version": None, "integrity": "missing"}
    if spec and spec.origin:
        origin = Path(spec.origin)
        native["origin"] = str(origin.resolve())
        package = origin.parent
        mismatches = []
        for relative, expected in manifest["runtime"]["files"].items():
            # The manifest is trusted distribution data, but still refuse traversal.
            if Path(relative).name != relative:
                raise ValueError("invalid runtime manifest path")
            file = package / relative
            if not file.is_file() or file.is_symlink() or _hash(file) != expected:
                mismatches.append(relative)
        native.update(available=not mismatches, integrity="matched" if not mismatches else "mismatch",
                      version=manifest["runtime"]["version"] if not mismatches else None,
                      mismatched_files=mismatches)
        if mismatches:
            issues.append("native_source_mismatch_or_shadowed")
    else:
        issues.append("native_missing")
    skill = {"available": False, "path": str(skill_path) if skill_path else None,
             "version": None, "sha256": None, "integrity": "not_selected"}
    if skill_path:
        file = Path(skill_path)
        if file.is_file():
            content = file.read_text(encoding="utf-8")
            version = re.search(r'^\s+version:\s*["\']?([^\s"\']+)', content, re.M)
            digest = _hash(file)
            skill.update(available=True, version=version[1] if version else None, sha256=digest,
                         integrity="matched" if digest == manifest["skill"]["sha256"] else "mismatch")
            if skill["integrity"] != "matched":
                issues.append("skill_content_mismatch")
        else:
            issues.append("skill_missing")
    apg = {"available": False, "root": str(apg_root) if apg_root else None,
           "version": None, "controller_sha256": None, "compatibility": "unverified"}
    if apg_root:
        root = Path(apg_root).resolve()
        controller = root / "scripts" / "project_governance.py"
        module = root / "project_governance" / "commands" / "check.py"
        if controller.is_file() and module.is_file():
            apg.update(available=True, root=str(root), controller_sha256=_hash(controller), check_sha256=_hash(module))
            version = root / "VERSION"
            if version.is_file():
                apg["version"] = version.read_text(encoding="utf-8").strip()
        else:
            issues.append("apg_incomplete")
    route = ("native+apg" if apg["available"] else "native-only") if native["available"] else (
        "apg-only" if apg["available"] else "unavailable")
    return {"schema_version": "1.0", "interpreter": sys.executable,
            "manifest": str(manifest_file.resolve()), "manifest_sha256": _hash(manifest_file),
            "source_base_commit": manifest.get("source_base_commit"),
            "native": native, "skill": skill, "apg": apg, "route": route,
            "issues": issues, "project_modified": False, "model_called": False,
            "limits": ["Source hashes identify build contents, not successful Host execution.",
                       "APG compatibility requires a combination test; presence alone is not acceptance."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="OpenCoding 只读安装诊断；不安装依赖、不调用模型")
    parser.add_argument("--skill", type=Path)
    parser.add_argument("--apg-root", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    args = parser.parse_args(argv)
    try:
        report = inspect_installation(skill_path=args.skill, apg_root=args.apg_root, runtime_root=args.runtime_root)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["native"]["available"] and not report["issues"] else 2
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "diagnostic_failed", "message": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
