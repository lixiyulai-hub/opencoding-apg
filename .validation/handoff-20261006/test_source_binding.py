"""Adversarial checks for the handoff verifier using disposable Git fixtures."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("source_verifier", HERE / "verify_source.py")
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


class BindingChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        (self.root / "fixture.txt").write_bytes(b"reviewed bytes\n")
        self.git("add", "fixture.txt")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "fixture")
        blob = self.git("rev-parse", "HEAD:fixture.txt").strip().decode()
        self.rows = [{"path": "fixture.txt", "git_mode": "100644", "git_blob": blob,
                      "sha256": VERIFIER.sha(b"reviewed bytes\n"), "bytes": 15}]
        self.folder = Path(self.temp.name) / "binding"
        self.folder.mkdir()
        self.binding = {"candidate_commit": self.git("rev-parse", "HEAD").decode().strip(),
                        "candidate_tree": self.git("rev-parse", "HEAD^{tree}").decode().strip(),
                        "tracked_file_count": 1, "windows_historical_symlink_files": [],
                        "verifier_sha256": VERIFIER.sha((HERE / "verify_source.py").read_bytes())}

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], stderr=subprocess.PIPE)

    def write_binding(self):
        raw = json.dumps(self.rows).encode()
        (self.folder / "source-files.json").write_bytes(raw)
        self.binding["source_files_sha256"] = VERIFIER.sha(raw)
        path = self.folder / "candidate-binding.json"
        path.write_text(json.dumps(self.binding), encoding="utf-8")
        return path, VERIFIER.sha(path.read_bytes())

    def verify(self):
        path, digest = self.write_binding()
        return VERIFIER.verify(self.root, path, digest)

    def test_matching_source(self):
        self.assertEqual(self.verify()["tracked_files"], 1)

    def test_changed_binding_is_rejected(self):
        path, digest = self.write_binding()
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "binding digest"):
            VERIFIER.verify(self.root, path, digest)

    def test_wrong_commit_with_same_tree_is_rejected(self):
        self.binding["candidate_commit"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "commit or tree"):
            self.verify()

    def test_dirty_checkout_is_rejected(self):
        (self.root / "fixture.txt").write_bytes(b"unreviewed content\n")
        with self.assertRaisesRegex(ValueError, "not clean"):
            self.verify()

    def test_assume_unchanged_does_not_hide_byte_drift(self):
        self.git("update-index", "--assume-unchanged", "fixture.txt")
        (self.root / "fixture.txt").write_bytes(b"unreviewed content\n")
        with self.assertRaisesRegex(ValueError, "checkout byte mismatch"):
            self.verify()

    def test_missing_manifest_path_is_rejected(self):
        self.rows = []
        self.binding["tracked_file_count"] = 0
        with self.assertRaisesRegex(ValueError, "path set"):
            self.verify()

    def test_path_traversal_is_rejected(self):
        self.rows[0]["path"] = "../fixture.txt"
        with self.assertRaisesRegex(ValueError, "source path"):
            self.verify()

    def test_manifest_byte_fingerprint_is_enforced(self):
        self.rows[0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "byte fingerprint"):
            self.verify()

    def test_link_type_cannot_replace_a_regular_source_file(self):
        self.git("update-index", "--assume-unchanged", "fixture.txt")
        external = Path(self.temp.name) / "outside.txt"
        external.write_bytes(b"reviewed bytes\n")
        (self.root / "fixture.txt").unlink()
        try:
            (self.root / "fixture.txt").symlink_to(external)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(ValueError, "entry type mismatch"):
            self.verify()


if __name__ == "__main__":
    unittest.main(verbosity=2)
