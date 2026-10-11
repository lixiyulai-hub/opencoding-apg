"""Credential consumption must bind the operation, with no write on rejection."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from opencoding import autorun, grants
from opencoding.aiadapter import MockAdapter
from tests import REVIEWED_FIXTURE


def _grant(root):
    return grants.issue_batch_grant(
        root, goal="凭据绑定反例", allowed_paths=["app", "lendreg", "tests", "data", "scripts"],
        action_kinds=list(grants.ACTION_KINDS), issued_by="synthetic-test",
        budget={"max_ai_requests": 20},
    )


def _binding(**overrides):
    operation = dict(task_id="task-1", attempt=1, action_kind="local_write",
                     targets=["app/main.py"], action_digest="action-1", input_digest="input-1")
    operation.update(overrides)
    return operation


def _expected(**overrides):
    return {"expect_" + key: value for key, value in _binding(**overrides).items()}


def _snapshot(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


class CredentialBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "profile")})
        environment.start()
        self.addCleanup(environment.stop)
        self.grant = _grant(self.root)
        self.credential = grants.issue_step_credential(self.root, self.grant["grant_id"], **_binding())

    def _check(self, **expected):
        return grants.check_step_credential(self.root, self.credential["credential_id"], **expected)

    def _reject_without_writes(self, expected, code):
        before = _snapshot(self.root)
        with self.assertRaises(grants.GrantError) as caught:
            self._check(**expected)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(_snapshot(self.root), before)

    def test_each_missing_binding_is_rejected_before_legacy_store_migration(self):
        (self.root / ".opencoding/grants/.state.lock").unlink()
        for missing in _expected():
            expected = _expected()
            del expected[missing]
            with self.subTest(missing=missing):
                self._reject_without_writes(expected, "credential_binding_required")
        self._reject_without_writes({}, "credential_binding_required")

    def test_each_different_binding_rejects_before_any_store_write(self):
        (self.root / ".opencoding/grants/.state.lock").unlink()
        cases = (
            ("task_id", "task-2", "task"), ("attempt", 2, "attempt"),
            ("action_kind", "local_run", "kind"),
            ("action_digest", "action-2", "action"), ("input_digest", "input-2", "input"),
            ("targets", ["app/other.py"], "targets"), ("targets", [], "targets"),
            ("targets", ["app/main.py", "app/other.py"], "targets"),
        )
        for field, value, code in cases:
            with self.subTest(field=field, value=value):
                self._reject_without_writes(_expected(**{field: value}), "credential_" + code + "_mismatch")

    def test_malformed_expectations_and_non_boolean_consume_are_rejected(self):
        cases = (("task_id", 1), ("attempt", True), ("attempt", 1.0), ("attempt", 0),
                 ("action_kind", []), ("action_digest", ""), ("input_digest", {}),
                 ("targets", "app/main.py"), ("targets", ["../outside"]),
                 ("targets", [True]))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self._reject_without_writes(_expected(**{field: value}), "credential_binding_invalid")
        for consume in (0, 1, "false", None):
            with self.subTest(consume=consume):
                self._reject_without_writes({**_expected(), "consume": consume}, "credential_binding_invalid")

    def test_exact_binding_consumes_once_and_preserves_read_only_compatibility(self):
        before = _snapshot(self.root)
        self.assertFalse(self._check(consume=False)["credential"]["used"])
        self.assertFalse(self._check(expect_task_id="task-1", consume=False)["credential"]["used"])
        self.assertEqual(_snapshot(self.root), before)
        self._reject_without_writes({"expect_input_digest": "other", "consume": False}, "credential_input_mismatch")
        self.assertTrue(self._check(**_expected(), expect_grant_id=self.grant["grant_id"])["credential"]["used"])
        self._reject_without_writes(_expected(), "credential_replay")

    def test_targets_use_issuance_normalization_but_not_prefix_coverage(self):
        self.assertTrue(self._check(**_expected(targets=["app\\main.py", "app/main.py"]))["credential"]["used"])

    def test_empty_targets_are_valid_for_ai_requests(self):
        binding = _binding(action_kind="ai_request", targets=[])
        self.credential = grants.issue_step_credential(self.root, self.grant["grant_id"], **binding)
        used = grants.load_grant(self.root, self.grant["grant_id"])["budget_used"].copy()
        self.assertTrue(self._check(**_expected(action_kind="ai_request", targets=[]))["credential"]["used"])
        self.assertEqual(grants.load_grant(self.root, self.grant["grant_id"])["budget_used"], used)

    def test_other_grant_is_rejected_when_caller_binds_the_current_batch(self):
        another = _grant(self.root)
        self._reject_without_writes({**_expected(), "expect_grant_id": another["grant_id"]}, "credential_grant_mismatch")

    def test_boolean_or_float_stored_attempt_cannot_equal_integer_expectation(self):
        path = self.root / ".opencoding/grants/credentials" / (self.credential["credential_id"] + ".json")
        for attempt in (True, 1.0):
            raw = dict(self.credential, attempt=attempt)
            raw["fingerprint"] = grants._credential_fingerprint(raw)
            path.write_text(json.dumps(raw), encoding="utf-8")
            self._reject_without_writes(_expected(), "credential_attempt_mismatch")

    def test_rechecks_revocation_between_preflight_and_consumption(self):
        original_lock = grants._grant_store_lock
        armed = [True]

        @contextmanager
        def revoke_before_mutating_lock(root, *, create=True):
            if create and armed[0]:
                armed[0] = False
                grants.revoke_batch_grant(root, self.grant["grant_id"], reason="预检后撤销")
            with original_lock(root, create=create):
                yield

        with mock.patch.object(grants, "_grant_store_lock", side_effect=revoke_before_mutating_lock):
            with self.assertRaises(grants.GrantError) as caught:
                self._check(**_expected())
        self.assertEqual(caught.exception.code, "grant_revoked")
        self.assertEqual(grants.grant_consumption_journal(self.root, self.grant["grant_id"]), [])


class AutorunCredentialBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "profile")})
        environment.start()
        self.addCleanup(environment.stop)
        self.grant = _grant(self.root)
        self.task = {"task_id": "task-1", "outputs": ["app/main.py"]}

    def test_swapped_verification_credential_never_runs_verifier(self):
        verifier = mock.Mock(return_value=(True, []))
        foreign = grants.issue_step_credential(
            self.root, self.grant["grant_id"],
            **autorun._run_credential_binding("task-1", 1, {"app/main.py": "foreign-input"}, verifier),
        )
        for operation in ("stage", "copy"):
            with self.subTest(operation=operation), mock.patch.object(autorun, "_issue_run_credential", return_value=foreign):
                with self.assertRaises(grants.GrantError) as caught:
                    if operation == "stage":
                        autorun._stage_and_verify(self.root, self.grant, self.task,
                                                 [{"path": "app/main.py", "content": "print('ok')\n"}],
                                                 "run-binding", 1, verifier, trusted_fixture=REVIEWED_FIXTURE)
                    else:
                        autorun._verify_copy(self.root, self.grant, self.task, "run-binding-copy", 1,
                                             verifier, trusted_fixture=REVIEWED_FIXTURE)
                self.assertEqual(caught.exception.code, "credential_input_mismatch")
        verifier.assert_not_called()
        self.assertFalse((self.root / "app/main.py").exists())
        self.assertEqual(grants.grant_consumption_journal(self.root, self.grant["grant_id"]), [])

    def test_swapped_write_credential_never_applies_candidate(self):
        foreign = grants.issue_step_credential(self.root, self.grant["grant_id"], **_binding())
        with mock.patch.object(grants, "issue_step_credential", return_value=foreign), \
                mock.patch.object(autorun.transactions, "apply_changes") as apply:
            with self.assertRaises(grants.GrantError) as caught:
                autorun._commit_candidate(
                    self.root, self.grant, self.task,
                    [{"path": "app/main.py", "content": "print('ok')\n"}],
                    "run-binding", "task-1", 1, expected={"app/main.py": None},
                )
        self.assertEqual(caught.exception.code, "credential_action_mismatch")
        apply.assert_not_called()
        self.assertFalse((self.root / "app/main.py").exists())
        self.assertEqual(grants.grant_consumption_journal(self.root, self.grant["grant_id"]), [])

    def test_verification_uses_actual_copied_bytes_before_execution(self):
        verifier = mock.Mock(return_value=(True, []))
        (self.root / "app").mkdir()
        (self.root / "app/main.py").write_text("original", encoding="utf-8")

        def changed_copy(project, grant, scratch):
            (scratch / "app").mkdir(exist_ok=True)
            (scratch / "app/main.py").write_text("changed during copy", encoding="utf-8")
            return []

        with mock.patch.object(autorun, "_copy_granted_paths", side_effect=changed_copy):
            with self.assertRaises(grants.GrantError) as caught:
                autorun._verify_copy(self.root, self.grant, self.task, "run-copy-drift", 1,
                                     verifier, trusted_fixture=REVIEWED_FIXTURE)
        self.assertEqual(caught.exception.code, "credential_input_mismatch")
        verifier.assert_not_called()
        self.assertEqual(grants.grant_consumption_journal(self.root, self.grant["grant_id"]), [])

    def test_swapped_ai_credential_never_dispatches_or_spends_again(self):
        from tests.test_product_autorun_security import D01, load_ledger, scenario
        task = scenario(D01)["tasks"][0]
        foreign = grants.issue_step_credential(
            self.root, self.grant["grant_id"], task_id=D01, attempt=1,
            action_kind="ai_request", targets=task["outputs"],
            action_digest=autorun._ai_request_action_digest(D01, 1, "implement"),
            input_digest="foreign-request",
        )
        adapter = MockAdapter()
        before_budget = grants.load_grant(self.root, self.grant["grant_id"])["budget_used"].copy()
        with mock.patch.object(grants, "issue_step_credential", return_value=foreign), \
                mock.patch.object(adapter, "complete") as complete:
            result = autorun.run_batch(
                self.root, scenario(D01), grant_id=self.grant["grant_id"], adapter=adapter,
                run_id="run-binding-ai", trusted_fixture=REVIEWED_FIXTURE,
            )
        complete.assert_not_called()
        self.assertEqual(result["states"][D01], "frozen")
        self.assertEqual(load_ledger(self.root, "run-binding-ai")["tasks"][D01]["error_code"], "credential_input_mismatch")
        self.assertEqual(grants.load_grant(self.root, self.grant["grant_id"])["budget_used"], before_budget)
        self.assertFalse((self.root / "lendreg").exists())


if __name__ == "__main__":
    unittest.main()
