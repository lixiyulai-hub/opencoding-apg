"""Verify an externally pinned candidate binding, Git objects and checkout bytes.

Read-only; does not import the product, contact a remote, or run its tests.
The caller must obtain the binding digest from the reviewed handoff.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys


def sha(data):
    return hashlib.sha256(data).hexdigest()


def verify(root, binding_path, expected_binding_hash):
    if not re.fullmatch(r"[0-9a-f]{64}", expected_binding_hash):
        raise ValueError("invalid binding digest")
    raw = binding_path.read_bytes()
    if sha(raw) != expected_binding_hash:
        raise ValueError("binding digest mismatch")
    binding = json.loads(raw)
    script_hash = sha(Path(__file__).read_bytes())
    if script_hash != binding["verifier_sha256"]:
        raise ValueError("verifier digest mismatch")
    manifest_path = binding_path.parent / "source-files.json"
    raw_manifest = manifest_path.read_bytes()
    if sha(raw_manifest) != binding["source_files_sha256"]:
        raise ValueError("source manifest digest mismatch")
    entries = json.loads(raw_manifest)
    if len(entries) != binding["tracked_file_count"]:
        raise ValueError("source manifest count mismatch")
    expected = {}
    for entry in entries:
        name = entry["path"]
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or ".." in path.parts
                or "\\" in name or ":" in name or str(path) != name
                or any(part.lower() == ".git" for part in path.parts)
                or name in expected):
            raise ValueError("invalid or repeated source path")
        expected[name] = entry

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)

    head = git("rev-parse", "HEAD").decode().strip()
    tree = git("rev-parse", "HEAD^{tree}").decode().strip()
    if head != binding["candidate_commit"] or tree != binding["candidate_tree"]:
        raise ValueError("candidate commit or tree mismatch")
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("checkout is not clean")
    actual = {}
    for row in git("ls-tree", "-rz", "--full-tree", "HEAD").split(b"\0"):
        if not row:
            continue
        meta, name = row.split(b"\t", 1)
        mode, kind, blob = meta.decode().split()
        if kind != "blob":
            raise ValueError("unsupported tree entry")
        actual[name.decode()] = (mode, blob)
    if set(actual) != set(expected):
        raise ValueError("tracked path set mismatch")
    declared_link_files = set(binding["windows_historical_symlink_files"])
    if not declared_link_files.issubset({
        n for n, e in expected.items()
        if e["git_mode"] == "120000" and n.startswith("artifacts/")
    }):
        raise ValueError("invalid historical symlink allowance")
    link_files = []
    for name, entry in expected.items():
        mode, blob = actual[name]
        if (mode, blob) != (entry["git_mode"], entry["git_blob"]):
            raise ValueError("Git mode/blob mismatch: " + name)
        data = git("cat-file", "blob", blob)
        if sha(data) != entry["sha256"] or len(data) != entry["bytes"]:
            raise ValueError("Git byte fingerprint mismatch: " + name)
        path = root / name
        if any(parent.is_symlink() for parent in path.parents if parent != root and parent.is_relative_to(root)):
            raise ValueError("linked parent in checkout: " + name)
        st = path.lstat()
        if mode == "120000" and path.is_symlink():
            checkout_bytes = os.fsencode(os.readlink(path))
        elif mode == "120000" and os.name == "nt" and name in declared_link_files and stat.S_ISREG(st.st_mode):
            # Explicit Git-for-Windows checkout representation of six historical
            # fixture links. Content must still match the exact Git link blob.
            checkout_bytes = path.read_bytes()
            link_files.append(name)
        elif mode in {"100644", "100755"} and stat.S_ISREG(st.st_mode):
            checkout_bytes = path.read_bytes()
            if os.name != "nt" and bool(st.st_mode & 0o111) != (mode == "100755"):
                raise ValueError("checkout executable mode mismatch: " + name)
        else:
            raise ValueError("checkout entry type mismatch: " + name)
        if checkout_bytes != data:
            raise ValueError("checkout byte mismatch: " + name)
    return {"status": "source_verified", "candidate_commit": head,
            "candidate_tree": tree, "tracked_files": len(expected),
            "binding_sha256": expected_binding_hash,
            "verifier_sha256": script_hash,
            "source_files_sha256": sha(raw_manifest),
            "windows_historical_link_files": link_files,
            "windows_runtime_validated": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--binding-sha256", required=True)
    args = parser.parse_args()
    try:
        result = verify(args.root.resolve(), args.binding.resolve(), args.binding_sha256)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError):
        # Do not expose local paths, credentials, or raw private Git diagnostics.
        print(json.dumps({"status": "blocked", "reason": "source_binding_or_checkout_verification_failed"}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
