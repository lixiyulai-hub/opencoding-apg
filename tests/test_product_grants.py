"""有限批次授权、每步凭据与事实来源分层的契约测试。"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from opencoding import grants
from opencoding.facts import (
    FactError,
    add_fact,
    build_decision_record,
    list_facts,
    mark_fact_confirmed,
)
from opencoding.intake import answer_question, new_session
from opencoding.decisions import build_recommendation


def make_grant(root: Path, **overrides):
    options = {
        "goal": "合成借还登记批次",
        "allowed_paths": ["lendreg", "docs"],
        "action_kinds": ["local_write", "ai_request"],
        "issued_by": "用户在一次确认中授权本批次（合成测试）",
    }
    options.update(overrides)
    return grants.issue_batch_grant(root, **options)


class BatchGrantLifecycleTests(unittest.TestCase):
    def test_issue_load_and_revoke_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            loaded = grants.load_grant(root, grant["grant_id"])
            self.assertEqual(loaded["goal"], "合成借还登记批次")
            valid, reason = grants.grant_valid(loaded)
            self.assertTrue(valid, reason)
            revoked = grants.revoke_batch_grant(root, grant["grant_id"], reason="用户撤销。")
            self.assertTrue(revoked["revoked"])
            valid, reason = grants.grant_valid(grants.load_grant(root, grant["grant_id"]))
            self.assertFalse(valid)
            self.assertEqual(reason, "grant_revoked")

    def test_expired_grant_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root, ttl_seconds=0.2)
            time.sleep(0.3)
            valid, reason = grants.grant_valid(grants.load_grant(root, grant["grant_id"]))
            self.assertFalse(valid)
            self.assertEqual(reason, "grant_expired")
            with self.assertRaises(grants.GrantError) as caught:
                grants.issue_step_credential(
                    root, grant["grant_id"],
                    task_id="task-1", attempt=1, action_kind="local_write",
                    targets=["lendreg/a.py"], action_digest="d1", input_digest="i1",
                )
            self.assertEqual(caught.exception.code, "grant_expired")

    def test_tampered_grant_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            path = root / ".opencoding" / "grants" / f"{grant['grant_id']}.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["allowed_paths"] = ["everywhere"]
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(grants.GrantError) as caught:
                grants.load_grant(root, grant["grant_id"])
            self.assertEqual(caught.exception.code, "grant_tampered")

    def test_scope_checks_kind_targets_and_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root, excluded_paths=["lendreg/secrets"])
            self.assertIsNone(grants.check_grant_scope(grant, action_kind="local_write", targets=["lendreg/pkg.py"]))
            self.assertEqual(
                grants.check_grant_scope(grant, action_kind="local_run", targets=["lendreg/pkg.py"]),
                "step_kind_not_allowed",
            )
            self.assertEqual(
                grants.check_grant_scope(grant, action_kind="local_write", targets=["outside/pkg.py"]),
                "step_target_out_of_scope",
            )
            self.assertEqual(
                grants.check_grant_scope(grant, action_kind="local_write", targets=["lendreg/secrets/key.py"]),
                "step_target_excluded",
            )
            self.assertEqual(
                grants.check_grant_scope(grant, action_kind="local_write", targets=["../escape.py"]),
                "step_target_invalid",
            )


class StepCredentialTests(unittest.TestCase):
    def _credential(self, root, grant, **overrides):
        options = {
            "task_id": "task-1", "attempt": 1, "action_kind": "local_write",
            "targets": ["lendreg/pkg.py"], "action_digest": "d1", "input_digest": "i1",
        }
        options.update(overrides)
        return grants.issue_step_credential(root, grant["grant_id"], **options)

    def test_issue_and_consume_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            credential = self._credential(root, grant)
            result = grants.check_step_credential(
                root, credential["credential_id"], expect_task_id="task-1", expect_action_digest="d1"
            )
            self.assertTrue(result["credential"]["used"])
            with self.assertRaises(grants.GrantError) as caught:
                grants.check_step_credential(root, credential["credential_id"], consume=False)
            self.assertEqual(caught.exception.code, "credential_replay")

    def test_wrong_task_or_action_digest_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            credential = self._credential(root, grant)
            with self.assertRaises(grants.GrantError) as caught:
                grants.check_step_credential(root, credential["credential_id"], expect_task_id="other-task")
            self.assertEqual(caught.exception.code, "credential_task_mismatch")
            credential = self._credential(root, grant)
            with self.assertRaises(grants.GrantError) as caught:
                grants.check_step_credential(root, credential["credential_id"], expect_action_digest="tampered")
            self.assertEqual(caught.exception.code, "credential_action_mismatch")

    def test_expired_credential_is_rejected_but_grant_can_reissue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            credential = self._credential(root, grant, ttl_seconds=0.2)
            time.sleep(0.3)
            with self.assertRaises(grants.GrantError) as caught:
                grants.check_step_credential(root, credential["credential_id"], consume=False)
            self.assertEqual(caught.exception.code, "credential_expired")
            fresh = self._credential(root, grant)
            result = grants.check_step_credential(root, fresh["credential_id"], expect_task_id="task-1")
            self.assertTrue(result["credential"]["used"])

    def test_ai_budget_is_enforced_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root, budget={"max_ai_requests": 1})
            self._credential(root, grant, action_kind="ai_request")
            with self.assertRaises(grants.GrantError) as caught:
                self._credential(root, grant, action_kind="ai_request")
            self.assertEqual(caught.exception.code, "step_budget_exhausted")
            reloaded = grants.load_grant(root, grant["grant_id"])
            self.assertEqual(reloaded["budget_used"]["ai_requests"], 1)

    def test_revoked_grant_blocks_new_and_existing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root)
            credential = self._credential(root, grant)
            grants.revoke_batch_grant(root, grant["grant_id"], reason="范围变化。")
            with self.assertRaises(grants.GrantError) as caught:
                self._credential(root, grant)
            self.assertEqual(caught.exception.code, "grant_revoked")
            with self.assertRaises(grants.GrantError) as caught:
                grants.check_step_credential(root, credential["credential_id"], consume=False)
            self.assertEqual(caught.exception.code, "grant_revoked")

    def test_repair_budget_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            grant = make_grant(root, budget={"max_repair_rounds": 1})
            grants.consume_budget_counter(root, grant["grant_id"], counter="repair_rounds")
            with self.assertRaises(grants.GrantError) as caught:
                grants.consume_budget_counter(root, grant["grant_id"], counter="repair_rounds")
            self.assertEqual(caught.exception.code, "budget_exhausted")


class FactSourceTests(unittest.TestCase):
    def test_sources_are_recorded_separately_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            user_fact = add_fact(root, "session-x", content="只有一台设备使用", source_type="user")
            self.assertTrue(user_fact["confirmed"])
            ai_fact = add_fact(root, "session-x", content="建议本地 SQLite", source_type="ai", source_ref="offline-model")
            self.assertFalse(ai_fact["confirmed"])
            duplicate = add_fact(root, "session-x", content="只有一台设备使用", source_type="user")
            self.assertEqual(duplicate["fact_id"], user_fact["fact_id"])
            self.assertEqual(len(list_facts(root, "session-x")), 2)
            self.assertEqual(len(list_facts(root, "session-x", source_type="ai")), 1)

    def test_ai_or_assumption_cannot_default_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaises(FactError):
                add_fact(root, "session-x", content="推断", source_type="ai", confirmed=True)
            assumption = add_fact(root, "session-x", content="暂按十项任务拆分", source_type="assumption", scope="batch")
            self.assertFalse(assumption["confirmed"])
            confirmed = mark_fact_confirmed(root, "session-x", assumption["fact_id"])
            self.assertTrue(confirmed["confirmed"])

    def test_list_on_missing_book_is_zero_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(list_facts(root, "session-y"), [])
            self.assertFalse((root / ".opencoding").exists())

    def test_secret_shaped_content_is_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fact = add_fact(root, "session-x", content="token=sk-test-1234567890", source_type="repository")
            self.assertNotIn("sk-test-1234567890", fact["content"])


class DecisionRecordTests(unittest.TestCase):
    def _ready_session(self):
        session = new_session("社区借还工具")
        answers = {
            "audience": "社区居民",
            "outcome": "登记借用并确认归还",
            "platform": "命令行",
            "data_persistence": "需要",
            "cross_device": "不需要",
            "file_storage": "不需要",
            "external_data": "不需要",
            "admin_access": "不需要",
            "account_access": "不需要",
            "notifications": "不需要",
            "payments": "不需要",
            "multi_user": "不需要",
        }
        for question_id, answer in answers.items():
            session = answer_question(session, question_id, answer)
        return session

    def test_decision_record_carries_reasoning_and_triggers(self):
        session = self._ready_session()
        recommendation = build_recommendation(session)
        record = build_decision_record(recommendation)
        self.assertEqual(record["schema_version"], "1.0")
        self.assertEqual(record["chosen"]["platform"], "cli")
        self.assertTrue(record["reasoning"])
        self.assertTrue(all(item["reason"] and item["evidence"] for item in record["reasoning"]))
        self.assertTrue(record["reevaluation_triggers"])
        self.assertIn("consistent", record["contradiction_check"])

    def test_decision_record_surfaces_facts(self):
        session = self._ready_session()
        recommendation = build_recommendation(session)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            add_fact(root, session["id"], content="设备必须离线可用", source_type="user")
            add_fact(root, session["id"], content="暂按单机单操作者", source_type="assumption", scope="batch")
            facts = list_facts(root, session["id"])
            record = build_decision_record(recommendation, facts)
            self.assertIn("暂按单机单操作者", record["assumptions"])
            self.assertTrue(record["auto_continuable"])

    def test_invalid_recommendation_is_rejected(self):
        with self.assertRaises(ValueError):
            build_decision_record({"schema_version": "1.1"})


if __name__ == "__main__":
    unittest.main()
