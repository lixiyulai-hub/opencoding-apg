import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone

from opencoding.safety import action_digest, evaluate_action, inspect_sensitive, safe_target, sanitize_text


class SafetyContractTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def action(self, **overrides):
        value = {
            "kind": "local_write",
            "root": str(self.root),
            "plan_digest": "plan-123",
            "targets": ["notes/result.txt"],
            "external": False,
            "cost_limit": 0,
            "data_scope": "synthetic",
            "irreversible": False,
        }
        value.update(overrides)
        return value

    def test_sensitive_inspection_and_redaction_never_return_secret(self):
        secret = "Bearer fake-super-secret-token-123456"
        result = inspect_sensitive(f"Authorization: {secret}")
        self.assertTrue(result["sensitive"])
        self.assertIn("bearer_token", result["categories"])
        sanitized = sanitize_text(f"Authorization: {secret}")
        self.assertNotIn(secret, sanitized)
        self.assertNotIn("fake-super-secret-token-123456", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_generic_and_algorithm_private_key_blocks_are_redacted(self):
        for header in ("PRIVATE", "RSA", "EC", "ENCRYPTED"):
            text = f"-----BEGIN {header} PRIVATE KEY-----\nprivate-material\n-----END {header} PRIVATE KEY-----"
            result = inspect_sensitive(text)
            self.assertTrue(result["sensitive"])
            self.assertIn("private_key", result["categories"])
            self.assertNotIn("private-material", sanitize_text(text))

    def test_reserved_aliases_and_hardlinks_are_rejected(self):
        for relative in (".git/config", "file.txt:stream", "NUL.txt", "trailing.", "trailing ", "C:/outside.txt"):
            with self.assertRaises(ValueError):
                safe_target(self.root, relative)
        source = self.root / "source.txt"
        source.write_text("same", encoding="utf-8")
        hardlink = self.root / "alias.txt"
        try:
            hardlink.hardlink_to(source)
        except (OSError, NotImplementedError):
            self.skipTest("hardlink creation is unavailable")
        with self.assertRaises(ValueError):
            safe_target(self.root, "alias.txt")

    def test_ancestor_link_is_rejected(self):
        outside = Path(tempfile.mkdtemp())
        link = self.root / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink creation is unavailable")
        with self.assertRaises(ValueError):
            safe_target(self.root, "linked/file.txt")

    def test_preview_is_allowed_but_requested_false_cannot_authorize_execution(self):
        preview = self.action(kind="preview", targets=[])
        self.assertEqual(evaluate_action(preview)["decision"], "allow")
        denied = evaluate_action(self.action(requested=False))
        self.assertEqual(denied["decision"], "block")
        self.assertIn("action_not_requested", denied["reason_codes"])

    def test_external_action_requires_digest_bound_live_approval(self):
        action = self.action(kind="payment", external=True, irreversible=True, cost_limit=10)
        pending = evaluate_action(action)
        self.assertEqual(pending["decision"], "confirm")
        approval = {
            "action_digest": action_digest(action),
            "root": str(self.root.absolute()),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "approved": True,
        }
        self.assertEqual(evaluate_action(action, approval)["decision"], "allow")
        wrong_root = dict(approval, root=str(self.root.parent))
        self.assertEqual(evaluate_action(action, wrong_root)["decision"], "block")

    def test_invalid_kind_and_unknown_fields_are_blocked_without_exception(self):
        invalid = evaluate_action(self.action(kind="shell"))
        self.assertEqual(invalid["decision"], "block")
        unknown = evaluate_action(dict(self.action(), arbitrary="unsafe"))
        self.assertEqual(unknown["decision"], "block")


if __name__ == "__main__":
    unittest.main()
