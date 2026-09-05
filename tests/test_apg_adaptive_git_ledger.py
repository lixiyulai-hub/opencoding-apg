import unittest

from scripts.apg_adaptive_git_ledger_preview import TARGET_PATH, project


def event(status="CHECKPOINT_RECOMMENDED", checkpoint_id="apg-plan-ready-001"):
    return {
        "event_id": "ledger-0011223344556677" if checkpoint_id.endswith("001") else "ledger-8899aabbccddeeff",
        "event_type": "adaptive-git-preview",
        "payload": {
            "status": status,
            "success_node": "plan-ready",
            "checkpoint_id": checkpoint_id if status in {"CHECKPOINT_RECOMMENDED", "ALREADY_RECOMMENDED"} else None,
            "revert_point": "intake-ready",
            "resume_condition": "resume.after-checkpoint-preview-is-recorded",
        },
    }


class AdaptiveGitLedgerTests(unittest.TestCase):
    def test_success_appends_deterministic_project_projection(self):
        result = project([], event())
        self.assertEqual(result["target_path"], TARGET_PATH)
        self.assertEqual(result["write_action"], "PREVIEW_ONLY")
        self.assertEqual(result["status"], "APPEND_CANDIDATE")
        self.assertTrue(result["append_candidate"])
        self.assertEqual(result["last_checkpoint"]["checkpoint_id"], "apg-plan-ready-001")
        self.assertEqual(result, project([], event()))

    def test_duplicate_checkpoint_reuses_existing_record(self):
        first = event()
        duplicate = dict(event("ALREADY_RECOMMENDED"))
        duplicate["event_id"] = "ledger-8899aabbccddeeff"
        result = project([first], duplicate)
        self.assertEqual(result["status"], "REUSE_EXISTING")
        self.assertTrue(result["deduplicated"])
        self.assertFalse(result["append_candidate"])
        self.assertEqual(len(result["snapshot"]["records"]), 1)

    def test_freeze_does_not_append_checkpoint(self):
        result = project([event()], event("FREEZE", "unused"))
        self.assertEqual(result["status"], "NO_CHECKPOINT_APPEND")
        self.assertFalse(result["append_candidate"])
        self.assertEqual(len(result["snapshot"]["records"]), 1)

    def test_invalid_event_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "event_id"):
            project([], {"event_id": "bad", "event_type": "adaptive-git-preview", "payload": {"status": "WAIT"}})


if __name__ == "__main__":
    unittest.main()
