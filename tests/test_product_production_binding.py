"""正式生成入口的采用绑定反例；无网络、无候选执行，配置在临时目录。"""
import json
import os
import shutil
from copy import deepcopy
from pathlib import Path
from unittest import mock

from opencoding import generic_run, service, grants
from opencoding.workbench import WorkbenchError
from tests.test_product_task_budget import _Base


class ProductionBindingTests(_Base):
    def setUp(self):
        super().setUp()
        self.config_env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.workspace / "isolated-config")})
        self.config_env.start()
        self.addCleanup(self.config_env.stop)

    def snapshot(self, root=None):
        root = root or self.project
        return {p.relative_to(root).as_posix(): p.read_bytes() if p.is_file() else None
                for p in root.rglob("*")}

    def rejected(self, code, *, binding=None, session=None, contract=None, **extra):
        adapter = self._timeout_adapter()
        before = self.snapshot()
        with self.assertRaises(generic_run.GenericRunError) as caught:
            generic_run.run_generic_app(
                self.project, "家庭借还登记", adapter, contract=contract or self.contract,
                session_id=session, adoption_binding=binding,
                run_id="gen-0000face0101", max_repair_rounds=0, **extra)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(self.snapshot(), before, "拒绝不得新增锁、授权、回执或候选")

    def test_default_rejects_missing_binding_even_for_mock_adapter(self):
        self.rejected("adoption_binding_required")
        self.rejected("adoption_binding_required", session=self.sid, binding={})
        self.rejected("adoption_binding_required", session=self.sid, binding="invalid")

    def test_session_plan_root_and_contract_must_match_adoption(self):
        binding = self._adopt_binding()
        self.rejected("adoption_session_mismatch", binding=binding, session="other")
        self.rejected("adoption_session_mismatch", binding=binding)
        self.rejected("adoption_binding_drifted", binding=binding, session=self.sid,
                      adopted_plan_digest="different-plan")
        altered = deepcopy(binding)
        altered["root"] = str(self.workspace)
        self.rejected("adoption_root_mismatch", binding=altered, session=self.sid)
        contract = deepcopy(self.contract)
        contract["entry_module"] = "app.other"
        self.rejected("adoption_contract_mismatch", binding=binding, session=self.sid, contract=contract)
        for field in ("evaluation_id", "plan_digest", "contract_sha256"):
            changed = deepcopy(binding)
            changed[field] = "changed"
            with self.subTest(field=field):
                self.rejected("adoption_binding_drifted", binding=changed, session=self.sid)

    def test_unconfirmed_or_foreign_evaluation_rejected_before_writes(self):
        binding = self._adopt_binding()
        path = self.project / ".opencoding/evaluations" / (binding["evaluation_id"] + ".json")
        original = json.loads(path.read_text(encoding="utf-8"))
        changes = [{"status": "pending_confirmation"}, {"status": "rejected"},
                   {"confirmation": {"accepted": "true"}}, {"session_id": "other"},
                   {"session_revision_at_eval": -1}]
        for change in changes:
            with self.subTest(change=change):
                path.write_text(json.dumps({**original, **change}), encoding="utf-8")
                self.rejected("evaluation_not_confirmed", binding=binding, session=self.sid)
        path.write_text(json.dumps(original), encoding="utf-8")

    def test_contract_digest_detects_mutated_evaluation(self):
        binding = self._adopt_binding()
        path = self.project / ".opencoding/evaluations" / (binding["evaluation_id"] + ".json")
        record = json.loads(path.read_text(encoding="utf-8"))
        record["structured"]["implementation_contract"]["entry_module"] = "app.other"
        path.write_text(json.dumps(record), encoding="utf-8")
        self.rejected("adoption_contract_mismatch", binding=binding, session=self.sid)

    def test_changed_session_rejects_before_creating_run_state(self):
        binding = self._adopt_binding()
        current = service.session_view(self.project, self.sid)
        service.submit_answer(self.project, self.sid, current["session"]["revision"],
                              "outcome", "增加社区归还统计")
        self.rejected("adoption_input_drift", binding=binding, session=self.sid)

    def test_foreign_evaluation_root_rejected_before_creating_run_state(self):
        binding = self._adopt_binding()
        path = self.project / ".opencoding/evaluations" / (binding["evaluation_id"] + ".json")
        record = json.loads(path.read_text(encoding="utf-8"))
        record["root"] = str(self.workspace)
        path.write_text(json.dumps(record), encoding="utf-8")
        self.rejected("adoption_root_mismatch", binding=binding, session=self.sid)

    def test_copied_project_cannot_reuse_source_adoption(self):
        binding = self._adopt_binding()
        copy = self.workspace / "copied-project"
        shutil.copytree(self.project, copy)
        adapter = self._timeout_adapter()
        before = self.snapshot(copy)
        with self.assertRaises(generic_run.GenericRunError) as caught:
            generic_run.run_generic_app(copy, "家庭借还登记", adapter, contract=self.contract,
                                        session_id=self.sid, adoption_binding=binding)
        self.assertEqual(caught.exception.code, "adoption_root_mismatch")
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(self.snapshot(copy), before)

    def test_legacy_rootless_adoption_requires_readoption(self):
        binding = self._adopt_binding()
        path = self.project / ".opencoding/adoptions" / (self.sid + ".json")
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("root")
        path.write_text(json.dumps(record), encoding="utf-8")
        self.rejected("adoption_root_mismatch", binding=binding, session=self.sid)

    def test_valid_binding_with_mock_adapter_uses_production_path(self):
        binding = self._adopt_binding()
        adapter = self._timeout_adapter()
        receipt = self._dispatch_once(adapter, "gen-0000face0102", binding)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(receipt["status"], "failed_transport")
        self.assertEqual(receipt["execution_mode"], "production")
        self.assertEqual(receipt["adopted_plan_digest"], binding["plan_digest"])

    def test_internal_fixture_bypass_is_explicit_and_labelled(self):
        adapter = self._timeout_adapter()
        receipt = generic_run.run_generic_app(
            self.project, "internal fixture", adapter, contract=self.contract,
            _internal_test_mode=True, max_repair_rounds=0)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(receipt["execution_mode"], "internal_test")
        self.assertEqual(receipt["status"], "failed_transport")

    def test_legacy_or_internal_receipt_cannot_bypass_binding_on_resume_or_renewal(self):
        binding = self._adopt_binding()
        folder = self.project / ".opencoding/generic_runs"
        folder.mkdir()
        run_id = "gen-0000face0103"
        path = folder / (run_id + ".json")
        valid = {"run_id": run_id, "session_id": self.sid, "status": "blocked_execution",
                 "ai_binding": binding, "adopted_plan_digest": binding["plan_digest"]}
        cases = [(dict(valid, ai_binding=None), "adoption_binding_required"),
                 (dict(valid, ai_binding={}), "adoption_binding_required"),
                 (dict(valid, session_id="other"), "adoption_session_mismatch"),
                 (dict(valid, adopted_plan_digest="other"), "adoption_binding_drifted"),
                 (dict(valid, execution_mode="internal_test"), "internal_test_receipt")]
        for receipt, expected in cases:
            path.write_text(json.dumps(receipt), encoding="utf-8")
            before = self.snapshot()
            for operation in (generic_run.resume_saved_candidate,
                              generic_run.describe_resume_confirmation,
                              generic_run.confirm_new_batch_for_resume):
                with self.subTest(operation=operation.__name__, expected=expected):
                    with self.assertRaises(generic_run.GenericRunError) as caught:
                        operation(self.project, run_id)
                    self.assertEqual(caught.exception.code, expected)
                    self.assertEqual(self.snapshot(), before)

    def test_workbench_rejects_internal_receipt_for_session_operations(self):
        run_id = "gen-0000face0104"
        folder = self.project / ".opencoding/generic_runs"
        folder.mkdir()
        (folder / (run_id + ".json")).write_text(json.dumps({
            "run_id": run_id, "session_id": self.sid, "execution_mode": "internal_test",
            "status": "blocked_execution"}), encoding="utf-8")
        before = self.snapshot()
        prefix = f"/api/project/家庭借还登记/session/{self.sid}/"
        for method, action, body in (
                ("GET", "gen-status/" + run_id, {}),
                ("POST", "gen-status/" + run_id, {}),
                ("POST", "resume", {"run_id": run_id}),
                ("POST", "cancel-generation/" + run_id, {}),
                ("POST", "ack-unknown", {"run_id": run_id})):
            with self.subTest(action=action):
                with self.assertRaises(WorkbenchError) as caught:
                    self.bench.api(method, prefix + action, {}, body)
                self.assertEqual(caught.exception.code, "internal_test_receipt")
                self.assertEqual(self.snapshot(), before)

    def change_requirement(self):
        revision = service.session_view(self.project, self.sid)["session"]["revision"]
        return service.submit_answer(self.project, self.sid, revision,
                                     "outcome", "增加归还统计和导出记录")

    def assert_dispatch_count(self, receipt, count):
        self.assertEqual(receipt["ai_requests_dispatched"], count)
        self.assertEqual(generic_run.task_budget_state(
            self.project, generic_run.TASK_ID, self.sid)["dispatched"], count)
        self.assertEqual(grants.load_grant(self.project, receipt["grant_id"])
                         ["budget_used"]["ai_requests"], count)

    def test_requirement_change_while_building_initial_messages_does_not_spend_or_dispatch(self):
        binding = self._adopt_binding()
        adapter = self._timeout_adapter()
        original = generic_run._implementation_messages

        def changed(*args, **kwargs):
            messages = original(*args, **kwargs)
            self.change_requirement()
            return messages

        with mock.patch.object(generic_run, "_implementation_messages", side_effect=changed):
            receipt = self._dispatch_once(adapter, "gen-0000face0105", binding)
        self.assertEqual(adapter.calls, 0)
        self.assert_dispatch_count(receipt, 0)
        self.assertEqual(receipt["attempts"][0]["error_code"], "adoption_input_drift")
        self.assertEqual(receipt["current_request"]["effect"], "not_dispatched")

    def test_requirement_change_while_building_repair_messages_does_not_spend_or_redispatch(self):
        binding = self._adopt_binding()
        adapter = mock.Mock(real=False, provider="test", model="test")
        adapter.complete.return_value = {"structured": {"files": []}}
        original = generic_run._repair_messages

        def changed(*args, **kwargs):
            messages = original(*args, **kwargs)
            self.change_requirement()
            return messages

        with mock.patch.object(generic_run, "_repair_messages", side_effect=changed):
            receipt = generic_run.run_generic_app(
                self.project, "家庭借还登记", adapter, contract=self.contract,
                session_id=self.sid, adoption_binding=binding,
                run_id="gen-0000face0106", max_repair_rounds=1)
        self.assertEqual(adapter.complete.call_count, 1)
        self.assert_dispatch_count(receipt, 1)
        self.assertEqual(receipt["attempts"][1]["error_code"], "adoption_input_drift")
        self.assertEqual(receipt["current_request"]["effect"], "not_dispatched")

    def test_network_call_does_not_hold_session_lock_and_late_result_is_rejected(self):
        binding = self._adopt_binding()
        changes = []
        adapter = mock.Mock(real=False, provider="test", model="test")

        def complete(*args, **kwargs):
            changes.append(self.change_requirement())
            return {"structured": {"files": []}}

        adapter.complete.side_effect = complete
        receipt = self._dispatch_once(adapter, "gen-0000face0107", binding)
        self.assertNotEqual(changes[0]["status"], "busy")
        self.assertEqual(adapter.complete.call_count, 1)
        self.assert_dispatch_count(receipt, 1)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["attempts"][0]["gate_reject"]["reason"], "input_drift")
        self.assertNotIn("transaction_id", receipt)

    def blocked_candidate(self, run_id, *, data_dir="app/data"):
        from tests.test_product_generic_run_v2 import _contract, GOOD_MAIN, GOOD_SELFTEST
        from tests.test_product_full_chain_v5 import eval_response
        raw = _contract()
        raw["data_dir"] = data_dir
        self.contract = generic_run._validate_contract(raw)
        with mock.patch("tests.test_product_task_budget.eval_response",
                        return_value=eval_response(contract=raw, choice="cli")):
            binding = self._adopt_binding()
        adapter = mock.Mock(real=False, provider="test", model="test")
        adapter.complete.return_value = {"structured": {"files": [
            {"path": "app/main.py", "content": GOOD_MAIN},
            {"path": "app/selftest.py", "content": GOOD_SELFTEST}]}}
        with mock.patch.object(generic_run, "_capability", return_value={
                "available": False, "kind": "none", "command": [], "reason": "test"}):
            receipt = self._dispatch_once(adapter, run_id, binding)
        self.assertEqual(receipt["status"], "blocked_execution")
        return receipt

    def test_resume_uses_authoritative_nondefault_data_directory(self):
        receipt = self.blocked_candidate("gen-0000face0108", data_dir="app/storage")
        with mock.patch.object(generic_run, "_capability", return_value={
                "available": True, "kind": "test-stub", "command": [], "reason": "test"}), \
                mock.patch.object(generic_run, "_make_verifier", wraps=generic_run._make_verifier) as verifier, \
                mock.patch.object(generic_run, "_stage_and_verify", return_value=(True, ["test substitute"], {})):
            resumed = generic_run.resume_saved_candidate(self.project, receipt["run_id"])
        self.assertEqual(resumed["status"], "delivered")
        self.assertEqual(verifier.call_args.args[2], "app/storage")
        self.assertEqual(verifier.call_args.kwargs["expected_record"], receipt["frozen_checks"])

    def test_cross_run_frozen_bundle_replacement_rejected_without_writes(self):
        receipt = self.blocked_candidate("gen-0000face0109")
        other_id = "gen-0000face0110"
        changed = deepcopy(self.contract)
        changed["steps"] = []
        generic_run._write_frozen_checks(self.project, other_id, receipt["goal"], changed)
        folder = self.project / ".opencoding/frozen_checks"
        for file in (folder / other_id).iterdir():
            shutil.copyfile(file, folder / receipt["run_id"] / file.name)
        before = self.snapshot()
        for operation in (generic_run.resume_saved_candidate, generic_run.describe_resume_confirmation):
            with self.assertRaises(generic_run.GenericRunError) as caught:
                operation(self.project, receipt["run_id"])
            self.assertEqual(caught.exception.code, "frozen_binding_mismatch")
            self.assertEqual(self.snapshot(), before)

    def test_coherent_frozen_hash_edit_cannot_change_adopted_contract(self):
        receipt = self.blocked_candidate("gen-0000face0111")
        changed = deepcopy(self.contract)
        changed["steps"] = []
        receipt["frozen_checks"] = generic_run._write_frozen_checks(
            self.project, receipt["run_id"], receipt["goal"], changed)
        path = self.project / ".opencoding/generic_runs" / (receipt["run_id"] + ".json")
        path.write_text(json.dumps(receipt), encoding="utf-8")
        before = self.snapshot()
        with self.assertRaises(generic_run.GenericRunError) as caught:
            generic_run.resume_saved_candidate(self.project, receipt["run_id"])
        self.assertEqual(caught.exception.code, "frozen_binding_mismatch")
        self.assertEqual(self.snapshot(), before)
