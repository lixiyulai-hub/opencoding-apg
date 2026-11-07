"""Refresh distribution fingerprints after source changes; no network or installs."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tomllib

root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
manifest = {
    "schema_version": "1.0", "source_base_commit": base, "source_hash_mode": "sha256-crlf-normalized-to-lf",
    "runtime": {"version": version, "files": {
        path.name: sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest() for path in sorted((root / "opencoding").glob("*.py"))}},
    "skill": {"version": "0.1.1", "sha256": sha256((root / "skills/opencoding/SKILL.md").read_bytes().replace(b"\r\n", b"\n")).hexdigest()},
    "apg": {"bundled": False, "compatibility": "unverified"},
}
(root / "opencoding/installation_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
