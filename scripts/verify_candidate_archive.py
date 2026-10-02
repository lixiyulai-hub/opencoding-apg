#!/usr/bin/env python3
"""Verify a staged OpenCoding ZIP can restore its complete source tree.

The archive format stores source under ``source/`` and a JSON manifest at the
root.  This verifier checks ZIP CRC, manifest bytes/SHA256, and the key files
required to continue from the current candidate.  It does not overwrite a
checkout; extraction is performed only into a caller-selected new directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile
from typing import Any


REQUIRED = (
    "source/opencoding/agent_adapter.py",
    "source/opencoding/product_loop.py",
    "source/opencoding/executor.py",
    "source/opencoding/transactions.py",
    "source/skills/opencoding/SKILL.md",
    "source/skills/opencoding/skill.json",
    "source/.agents/skills/opencoding/SKILL.md",
    "source/.agents/skills/opencoding/skill.json",
    "source/scripts/verify_codex_skill.py",
    "source/scripts/install_codex_skill.py",
    "source/tests/test_agent_adapter.py",
    "source/tests/test_codex_host_adapter.py",
    "source/opencoding/codex_host.py",
    "source/docs/product/CODEX_HOST_LOADER_STAGE7_CN.md",
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_archive(archive: Path, *, extract_to: Path | None = None) -> dict[str, Any]:
    archive = archive.resolve()
    with zipfile.ZipFile(archive) as zf:
        bad_crc = zf.testzip()
        if bad_crc is not None:
            raise ValueError(f"ZIP CRC failed: {bad_crc}")
        names = set(zf.namelist())
        if "MANIFEST.json" not in names:
            raise ValueError("MANIFEST.json is missing")
        manifest = json.loads(zf.read("MANIFEST.json"))
        entries = manifest.get("files")
        if not isinstance(entries, list):
            raise ValueError("manifest files is not a list")
        checked = 0
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ValueError("manifest entry is malformed")
            path = item["path"]
            if path not in names:
                raise ValueError(f"manifest path missing from ZIP: {path}")
            data = zf.read(path)
            if item.get("bytes") != len(data) or item.get("sha256") != _sha(data):
                raise ValueError(f"manifest hash mismatch: {path}")
            checked += 1
        listed = {item["path"] for item in entries}
        unlisted = sorted(name for name in names if name != "MANIFEST.json" and not name.endswith("/") and name not in listed)
        if unlisted:
            raise ValueError("unlisted ZIP members: " + ", ".join(unlisted))
        missing = [path for path in REQUIRED if path not in names]
        if missing:
            raise ValueError("required source members missing: " + ", ".join(missing))
        extracted: str | None = None
        if extract_to is not None:
            extract_to = extract_to.resolve()
            if extract_to.exists():
                raise ValueError("extract_to must be a new private directory")
            extract_to.mkdir(parents=True)
            for info in zf.infolist():
                if info.filename.endswith("/"):
                    continue
                destination = (extract_to / info.filename).resolve()
                if not destination.is_relative_to(extract_to):
                    raise ValueError("archive path escapes extraction root")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(zf.read(info.filename))
            extracted = str(extract_to)
    return {
        "archive": str(archive),
        "archive_sha256": _sha(archive.read_bytes()),
        "archive_bytes": archive.stat().st_size,
        "zip_crc": "pass",
        "manifest_entries_checked": checked,
        "required_source_members": list(REQUIRED),
        "restorable": True,
        "extracted_to": extracted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--extract-to", type=Path)
    args = parser.parse_args()
    try:
        report = verify_archive(args.archive, extract_to=args.extract_to)
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(json.dumps({"restorable": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
