"""Offline browser-equivalent workflow and artifact boundary checks.

This module deliberately uses a local fixture directory.  It gives the UI
layer a deterministic browser contract without pretending that a real browser
or model provider was exercised.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class FixtureError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_relative(name: str) -> str:
    p = Path(name)
    if p.is_absolute() or any(part in ("", ".", "..") for part in p.parts):
        raise FixtureError("artifact_path_invalid", name)
    # Zip names may use Windows separators even on Linux.
    if "\\" in name or ":" in name:
        raise FixtureError("artifact_path_invalid", name)
    return "/".join(p.parts)


def extract_versioned_artifact(archive: str | Path, destination: str | Path,
                               *, expected_version: str) -> dict[str, Any]:
    """Extract a candidate only when its manifest and files are version-bound.

    The destination must not exist, and every archive member is checked before
    writing.  The manifest is ``candidate-manifest.json`` with ``version`` and
    ``files`` (path + sha256) entries.
    """
    archive, destination = Path(archive), Path(destination)
    if destination.exists():
        raise FixtureError("artifact_destination_exists", str(destination))
    if not expected_version:
        raise FixtureError("artifact_version_required", "expected_version required")
    temp = destination.with_name(destination.name + ".partial-" + uuid.uuid4().hex)
    try:
        temp.mkdir(parents=True)
        with zipfile.ZipFile(archive) as zf:
            names = [_safe_relative(i.filename) for i in zf.infolist() if not i.is_dir()]
            if "candidate-manifest.json" not in names:
                raise FixtureError("artifact_manifest_missing", "candidate-manifest.json")
            manifest = json.loads(zf.read("candidate-manifest.json"))
            if manifest.get("version") != expected_version:
                raise FixtureError("artifact_version_mismatch", str(manifest.get("version")))
            listed = {str(item["path"]): str(item["sha256"]) for item in manifest.get("files", [])}
            if set(listed) != set(names) - {"candidate-manifest.json"}:
                raise FixtureError("artifact_manifest_incomplete", "manifest file set differs")
            for name in names:
                if name == "candidate-manifest.json":
                    continue
                data = zf.read(name)
                if _digest(data) != listed[name]:
                    raise FixtureError("artifact_digest_mismatch", name)
                target = temp / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            (temp / "candidate-manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.rename(destination)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return {"version": expected_version, "destination": str(destination), "files": len(listed)}


class LocalBrowserFixture:
    """Small persisted contract used by browser tests and UI adapters."""
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.state = self.root / ".opencoding" / "browser-fixture"
        self.state.mkdir(parents=True, exist_ok=True)
        self.history_file = self.state / "history.jsonl"

    def open_preview(self, *, candidate_version: str, candidate_digest: str) -> dict[str, Any]:
        if not candidate_version or len(candidate_digest) != 64:
            raise FixtureError("candidate_identity_invalid", "version and sha256 required")
        run_id = "fixture-" + uuid.uuid4().hex[:12]
        record = {"run_id": run_id, "status": "preview", "candidate_version": candidate_version,
                  "candidate_digest": candidate_digest, "created_at": _now(),
                  "url": f"fixture://{run_id}/preview", "network": "disabled"}
        self._append(record)
        return record

    def confirm(self, run_id: str) -> dict[str, Any]:
        record = self._find(run_id)
        if not record or record["status"] != "preview":
            raise FixtureError("preview_not_confirmable", run_id)
        record = {**record, "status": "confirmed", "confirmed_at": _now()}
        self._append(record)
        return record

    def resume(self, run_id: str, *, candidate_version: str, candidate_digest: str) -> dict[str, Any]:
        record = self._find(run_id)
        if not record:
            raise FixtureError("run_not_found", run_id)
        if record.get("status") != "confirmed":
            raise FixtureError("resume_requires_confirmation", run_id)
        if (record.get("candidate_version"), record.get("candidate_digest")) != (candidate_version, candidate_digest):
            raise FixtureError("candidate_identity_mismatch", run_id)
        resumed = {**record, "status": "resumed", "resumed_at": _now()}
        self._append(resumed)
        return resumed

    def history(self) -> list[dict[str, Any]]:
        if not self.history_file.exists():
            return []
        return [json.loads(line) for line in self.history_file.read_text(encoding="utf-8").splitlines() if line]

    def _find(self, run_id: str) -> dict[str, Any] | None:
        records = [r for r in self.history() if r.get("run_id") == run_id]
        return records[-1] if records else None

    def _append(self, record: dict[str, Any]) -> None:
        with self.history_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


__all__ = ["FixtureError", "LocalBrowserFixture", "extract_versioned_artifact"]
