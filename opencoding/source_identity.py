"""Deterministic identity for the local skill/execution source.

The identity binds a reviewed runner and its imported OpenCoding implementation
to one source tree.  It is a content fingerprint, not a signature, sandbox, or
proof that a managed host loaded the same bytes.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


SOURCE_FINGERPRINT_SCHEMA = "opencoding-source-fingerprint-v1"


def _source_files(root: Path) -> list[Path]:
    """Return the executable skill source and resource files in stable order."""
    root = Path(root)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError("source root must be an existing absolute directory")
    files: list[Path] = []
    for prefix in (root / "opencoding", root / "skills" / "opencoding", root / ".agents" / "skills" / "opencoding"):
        if not prefix.exists():
            continue
        for path in prefix.rglob("*"):
            if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root.resolve()):
                raise ValueError(f"source file escapes root: {path}")
            files.append(path)
    for relative in ("scripts/run_skill_contract.py", "scripts/recover_skill_transaction.py"):
        path = root / relative
        if path.is_file() and not path.is_symlink():
            files.append(path)
    unique = {path.relative_to(root).as_posix(): path for path in files}
    if not unique:
        raise ValueError("source root has no executable OpenCoding source")
    return [unique[key] for key in sorted(unique)]


def source_fingerprint(root: str | Path) -> str:
    """Hash relative names and bytes for the source used by the skill runner."""
    root = Path(root).resolve()
    digest = hashlib.sha256()
    digest.update(SOURCE_FINGERPRINT_SCHEMA.encode("ascii"))
    digest.update(b"\0")
    for path in _source_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


__all__ = ["SOURCE_FINGERPRINT_SCHEMA", "source_fingerprint"]
