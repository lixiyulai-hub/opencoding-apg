from __future__ import annotations

import json
import socket
import subprocess
import unittest
from unittest.mock import patch

from opencoding.publication_boundary import (
    MINIMUM_EVIDENCE_IDS,
    PublicationBoundaryError,
    build_publication_preview,
    verify_publication_preview,
)
from opencoding.w5_acceptance import build_synthetic_acceptance_matrix


def _candidate():
    audit = build_synthetic_acceptance_matrix()["external_audit"]
    return {
        "source_revision": "11a1e55",
        "test_summary": {"total": 688, "passed": 679, "skipped": 9, "failures": 0, "errors": 0, "returncode": 0},
        "independent_review": {"status": "observed", "focused_total": 44, "focused_passed": 44, "returncode": 0},
        "audit_snapshot": audit,
        "rollback": {"status": "simulated_receipt_scope", "scope": "git-revert-w5-boundary", "automatic": False},
        "facts": {
            "platform_compatibility": {"status": "unverified", "source": "w5-platform-matrix"},
            "real_user_acceptance": {"status": "unverified", "source": "synthetic-only"},
            "license_and_contribution_review": {"status": "unverified", "source": "not-run"},
            "public_scope_review": {"status": "observed", "source": "offline-scope"},
        },
    }


class PublicationBoundaryTests(unittest.TestCase):
    def test_preview_reports_minimum_evidence_and_stays_blocked(self):
        with patch("socket.socket.connect", side_effect=AssertionError("network")), \
             patch("subprocess.Popen", side_effect=AssertionError("process")):
            preview = build_publication_preview(_candidate())
        self.assertEqual(preview["status"], "blocked_human_gate")
        self.assertEqual(preview["minimum_evidence"]["status"], "observed")
        self.assertEqual(preview["minimum_evidence"]["required"], list(MINIMUM_EVIDENCE_IDS))
        self.assertTrue(preview["gate"]["required"])
        self.assertFalse(preview["gate"]["recorded"])
        self.assertFalse(preview["gate"]["external_action_allowed"])
        self.assertEqual(preview["actions"], {"publish_executed": False, "release_executed": False, "deployment_executed": False})

    def test_preview_exposes_facts_without_promoting_unverified_claims(self):
        preview = build_publication_preview(_candidate())
        facts = {item["id"]: item for item in preview["facts"]}
        self.assertEqual(facts["platform_compatibility"]["status"], "unverified")
        self.assertEqual(facts["real_user_acceptance"]["status"], "unverified")
        self.assertEqual(preview["privacy"], {
            "raw_values_included": False, "prompt_included": False, "cookie_included": False,
            "token_included": False, "api_key_included": False, "user_content_included": False,
        })
        self.assertTrue(preview["preview_digest"])
        self.assertTrue(verify_publication_preview(preview))
        serialized = json.dumps(preview, ensure_ascii=False)
        self.assertIn("11a1e55", serialized)  # revision is a public metadata fact
        self.assertNotIn("Prompt", serialized)
        self.assertNotIn("sk-", serialized)

    def test_failed_or_inconsistent_minimum_evidence_fails_closed(self):
        candidate = _candidate()
        candidate["test_summary"] = {"total": 1, "passed": 0, "skipped": 0, "failures": 1, "errors": 0, "returncode": 1}
        with self.assertRaisesRegex(PublicationBoundaryError, "successful test baseline"):
            build_publication_preview(candidate)
        candidate = _candidate()
        candidate["facts"]["platform_compatibility"] = {"status": "observed", "source": "unverified-platform"}
        with self.assertRaisesRegex(PublicationBoundaryError, "exceed"):
            build_publication_preview(candidate)
        candidate = _candidate()
        candidate["audit_snapshot"] = {**candidate["audit_snapshot"], "privacy": {"raw_values_included": True}}
        with self.assertRaisesRegex(PublicationBoundaryError, "privacy audit"):
            build_publication_preview(candidate)

        preview = build_publication_preview(_candidate())
        tampered = {**preview, "facts": []}
        with self.assertRaisesRegex(PublicationBoundaryError, "preview digest"):
            verify_publication_preview(tampered)

    def test_raw_secret_like_candidate_fields_are_not_accepted_as_ids(self):
        candidate = _candidate()
        candidate["source_revision"] = "api_key=sk-" + "x" * 32
        with self.assertRaises(PublicationBoundaryError):
            build_publication_preview(candidate)


if __name__ == "__main__":
    unittest.main()
