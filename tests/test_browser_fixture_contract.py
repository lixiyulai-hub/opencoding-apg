import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from opencoding.browser_fixture import FixtureError, LocalBrowserFixture, extract_versioned_artifact


class BrowserFixtureContractTests(unittest.TestCase):
    def test_preview_confirm_resume_history_is_persisted_and_identity_bound(self):
        with tempfile.TemporaryDirectory() as td:
            ui = LocalBrowserFixture(td)
            digest = "a" * 64
            preview = ui.open_preview(candidate_version="v2", candidate_digest=digest)
            self.assertEqual(preview["network"], "disabled")
            with self.assertRaisesRegex(FixtureError, "resume_requires_confirmation"):
                ui.resume(preview["run_id"], candidate_version="v2", candidate_digest=digest)
            ui.confirm(preview["run_id"])
            with self.assertRaisesRegex(FixtureError, "candidate_identity_mismatch"):
                ui.resume(preview["run_id"], candidate_version="v3", candidate_digest=digest)
            resumed = ui.resume(preview["run_id"], candidate_version="v2", candidate_digest=digest)
            self.assertEqual(resumed["status"], "resumed")
            self.assertEqual([r["status"] for r in ui.history()], ["preview", "confirmed", "resumed"])

    def test_same_version_extraction_and_path_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = b"print('fixture')\n"
            manifest = {"version": "v7", "files": [{"path": "app/main.py", "sha256": hashlib.sha256(payload).hexdigest()}]}
            archive = root / "candidate.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("app/main.py", payload)
                zf.writestr("candidate-manifest.json", json.dumps(manifest))
            out = root / "extracted"
            result = extract_versioned_artifact(archive, out, expected_version="v7")
            self.assertEqual(result["files"], 1)
            self.assertEqual((out / "app/main.py").read_bytes(), payload)
            with self.assertRaisesRegex(FixtureError, "artifact_destination_exists"):
                extract_versioned_artifact(archive, out, expected_version="v7")

    def test_extraction_rejects_traversal_before_writing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../escape.txt", b"x")
            with self.assertRaisesRegex(FixtureError, "artifact_path_invalid"):
                extract_versioned_artifact(archive, root / "out", expected_version="v1")
            self.assertFalse((root / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
