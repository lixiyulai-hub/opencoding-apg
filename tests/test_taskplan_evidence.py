"""Synthetic memory captures: consistency is not authenticated runtime evidence."""
from collections import UserDict
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from opencoding.decisions import build_recommendation
from opencoding.documents import render_documents
from opencoding.executor import action_digest
from opencoding.intake import QUESTION_DEFINITIONS, answer_question, new_session
from opencoding.planning import _compute_waves, build_task_plan
from opencoding.safety import sha256_bytes
from opencoding.scheduler import _receipt_digest
from opencoding.service import _digest, _service_bundle, derive_frontier
from opencoding.taskplan_evidence import project_taskplan_evidence
import opencoding.taskplan_evidence as module
from opencoding.taskplan_scheduler import _envelope


def fixture(*, unresolved=False, external=False, poisoned_chain=False):
    root = "/synthetic-authorized-project"
    session = new_session("社区借还登记")
    if not unresolved:
        answers = {"audience": "社区居民", "outcome": "登记并查看记录", "platform": "网页"}
        if external:
            answers["data_persistence"] = "需要"
        for question in QUESTION_DEFINITIONS:
            session = answer_question(session, question["id"], answers.get(question["id"], "不需要"))
    recommendation = build_recommendation(session)
    plan = build_task_plan(recommendation)
    documents = render_documents(recommendation, plan)
    if poisoned_chain:
        by_id = {task["id"]: task for task in plan["tasks"]}
        tail = ["implement-scenario-1", "requirements-confirmed", "product-and-ui"]
        ordered = [task for task in plan["tasks"] if task["id"] not in tail] + [by_id[name] for name in tail]
        # An explicitly synthetic serial DAG (not a C-generated approval)
        # exercises this pure projection's transitive negative rule. Reuse the
        # captured output bytes; none may become verified execution evidence.
        for index, task in enumerate(ordered):
            task["depends_on"] = [ordered[index - 1]["id"]] if index else []
        plan["waves"] = _compute_waves(plan["tasks"])
    entries = [{"path": path, "operation": "create", "before_sha256": None,
                "after_sha256": sha256_bytes(content.encode()), "content": content}
               for path, content in sorted(documents.items(), key=lambda pair: pair[0].casefold())]
    core = {"schema_version": "1.0", "root": root, "entries": entries}
    file_plan = {**core, "status": "preview", "plan_digest": _digest(core)}
    base = {"schema_version": "1.0", "root": root, "session": session, "frontier": derive_frontier(session),
            "recommendation": recommendation, "task_plan": plan, "documents": documents,
            "status": recommendation["status"], "file_plan": file_plan,
            "targets": [entry["path"] for entry in entries], "diff": "synthetic original captured diff"}
    base["service_digest"] = _digest(_service_bundle(root, session, recommendation, plan, documents, file_plan))
    preview = _envelope(base)
    data = {"expected_root": root, "expected_preview_digest": preview["service_digest"], "original_preview": preview,
            "scheduler_snapshot": {"schema_version": "1.0", "root": root, "status": "ready", "tasks": [], "runs": []},
            "transaction_evidence": {}, "file_evidence": {}}
    for index, original in enumerate(preview["tasks"]):
        run_id = f"run-synthetic-{index}"
        tx_id = f"tx-20261006T000000000000Z-{index:012x}"
        artifacts = []
        if original["action"]["type"] == "document":
            for entry in original["action"]["plan"]["entries"]:
                artifacts.append({"path": entry["path"], "sha256": entry["after_sha256"], "sha256_kind": "file_bytes", "transaction_id": tx_id})
                data["file_evidence"][entry["path"]] = {"root": root, "path": entry["path"],
                    "content_bytes": entry["content"].encode(), "acquisition_error": None}
            data["transaction_evidence"][tx_id] = {"root": root, "transaction_id": tx_id,
                "raw_files": {"receipt.json": b'{"status":"applied"}', "events.jsonl": b'opaque'}, "acquisition_error": None}
        # This intentionally fabricated all-success snapshot is not a runtime
        # claim. Even host_missing success and matching docs cannot be verified.
        task = {**deepcopy(original), "schema_version": "1.0", "timeout_seconds": 30.0,
                "state": "succeeded", "attempt": 1, "last_run_id": run_id, "last_error": "succeeded"}
        receipt = {"schema_version": "1.0", "run_id": run_id, "task_id": task["task_id"], "attempt": 1,
                   "input_sha256": _digest(original["input"]), "action_digest": action_digest(original["action"]),
                   "status": "succeeded", "exit_code": 0, "timed_out": False, "cancelled": False,
                   "stdout_summary": "private-output /private/absolute/receipt.json", "stderr_summary": "", "artifacts": artifacts,
                   "reason": "succeeded"}
        receipt["receipt_digest"] = _receipt_digest(receipt)
        run = {**deepcopy(receipt), "started_at": "2026-10-06T00:00:00+00:00", "finished_at": "2026-10-06T00:00:01+00:00", "receipt": receipt}
        data["scheduler_snapshot"]["tasks"].append(task)
        data["scheduler_snapshot"]["runs"].append(run)
    return data


def reseal_run(run):
    receipt = {key: deepcopy(run[key]) for key in module._RECEIPT_FIELDS}
    receipt["receipt_digest"] = _receipt_digest(receipt)
    run["receipt"] = receipt
    run["receipt_digest"] = receipt["receipt_digest"]


def reseal_preview(data):
    base = data["original_preview"]["service_preview"]
    plan = base["file_plan"]
    plan["plan_digest"] = _digest({key: plan[key] for key in ("schema_version", "root", "entries")})
    base["service_digest"] = _digest(_service_bundle(base["root"], base["session"], base["recommendation"], base["task_plan"], base["documents"], plan))
    data["original_preview"] = _envelope(base)
    data["expected_preview_digest"] = data["original_preview"]["service_digest"]


class TaskPlanEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture(external=True)

    def project(self, data=None):
        data = self.data if data is None else data
        before = deepcopy(data)
        result = project_taskplan_evidence(**data)
        self.assertEqual(data, before)
        self.assertFalse(result["live_verified"])
        self.assertEqual(result["activation_status"], "not_activated")
        json.dumps(result, allow_nan=False)
        for task in result["tasks"]:
            if task["classification"] in ("document", "offline_design"):
                self.assertEqual(task["evidence_status"], "unverified")
                self.assertIn("strict_transaction_validator_unavailable", task["reason_codes"])
        encoded = json.dumps(result, ensure_ascii=False)
        for private in ("private-output", "/private/absolute", "content_bytes", "stdout_summary", "observed", '"all_success"', '"live_ready"'):
            self.assertNotIn(private, encoded)
        return result

    def first_document(self, result):
        return next(task for task in result["tasks"] if task["classification"] == "document")

    def assert_blocked_bytes(self, result):
        task = self.first_document(result)
        self.assertTrue(task["reason_codes"])
        self.assertTrue(all(output["status"] == "unverified" for output in task["outputs"]))

    def test_complete_synthetic_success_keeps_docs_unverified_and_bytes_only(self):
        result = self.project()
        self.assertEqual(result["binding"], {"status": "matched", "reason_codes": []})
        self.assertEqual(result["scheduler"], {"status": "ready", "reason_codes": []})
        self.assertEqual(result["root_sha256"], sha256_bytes(self.data["expected_root"].encode()))
        self.assertTrue(any(task["classification"] == "offline_design" for task in result["tasks"]))
        for task in result["tasks"]:
            if task["classification"] != "host_missing":
                if "dependency_evidence_inconsistent" in task["reason_codes"]:
                    self.assertTrue(all(output["status"] == "unverified" for output in task["outputs"]))
                else:
                    self.assertEqual(task["reason_codes"], ["strict_transaction_validator_unavailable"])
                    self.assertTrue(all(output["status"] == "byte_match" for output in task["outputs"]))
                self.assertEqual(task["transaction_capture"], "opaque_supplied")
            else:
                self.assertEqual(task["evidence_status"], "blocked")
                self.assertIn("host_missing_claimed_success", task["reason_codes"])
        self.assertEqual(result["capabilities"]["implementation"], {"status": "blocked", "reason": "host_missing"})
        self.assertNotIn(self.data["expected_root"], json.dumps(result))

    def test_fixture_with_actual_C_preview_and_scheduler_dictionary_contract(self):
        from opencoding.taskplan_scheduler import preview_task_plan
        from tests.test_product_service import _complete
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            view = _complete(root)
            preview = preview_task_plan(root, view["session"]["id"])
        data = {"expected_root": str(root), "expected_preview_digest": preview["service_digest"],
                "original_preview": preview, "scheduler_snapshot": {"schema_version": "1.0", "root": str(root), "status": "not_initialized", "tasks": [], "runs": []},
                "transaction_evidence": {}, "file_evidence": {}}
        result = self.project(data)
        self.assertEqual(result["binding"]["status"], "matched")
        self.assertEqual(result["scheduler"]["reason_codes"], ["snapshot_not_ready"])

    def test_missing_or_changed_independent_binding_never_recomputed(self):
        for key, value in (("expected_preview_digest", None), ("expected_preview_digest", "0" * 64), ("expected_root", "/different"), ("expected_root", "relative")):
            data = deepcopy(self.data)
            data[key] = value
            with self.subTest(key=key, value=value):
                result = self.project(data)
                self.assertEqual(result["binding"]["status"], "rejected")
                self.assertEqual(result["tasks"], [])
        with self.assertRaises(TypeError):
            project_taskplan_evidence(**{key: value for key, value in self.data.items() if key != "expected_preview_digest"})

    def test_original_fields_and_partial_or_nested_rehashes_reject(self):
        mutations = [
            lambda p: p.update(root="/wrong"),
            lambda p: p["session"].update(revision=p["session"]["revision"] + 1),
            lambda p: p["tasks"][0]["action"]["plan"].update(root="/wrong"),
            lambda p: p["tasks"][0].update(depends_on=[]),
            lambda p: p["service_preview"]["file_plan"]["entries"][0].update(content="tampered document"),
            lambda p: p["effects"].update(external=True),
        ]
        # Use a nonempty dependency for that mutation.
        mutations[3] = lambda p: p["tasks"][0].update(idempotency_key="different")
        for mutate in mutations:
            data = deepcopy(self.data)
            mutate(data["original_preview"])
            data["original_preview"]["service_digest"] = _digest({key: value for key, value in data["original_preview"].items() if key != "service_digest"})
            # Even supplying the changed outer expected hash cannot make the
            # derived mapping or nested digest checks self-validating.
            data["expected_preview_digest"] = data["original_preview"]["service_digest"]
            self.assertEqual(self.project(data)["binding"]["status"], "rejected")

    def test_internally_rehashed_invalid_file_paths_hashes_and_session_graph_reject(self):
        for kind in ("content", "traversal", "alias", "revision", "boolean"):
            data = deepcopy(self.data)
            base = data["original_preview"]["service_preview"]
            entry = base["file_plan"]["entries"][0]
            if kind == "content":
                entry["after_sha256"] = "0" * 64
            elif kind in ("traversal", "alias"):
                path = "../escape.md" if kind == "traversal" else "dir\\file.md"
                old = entry["path"]
                entry["path"] = path
                base["targets"][0] = path
                base["documents"][path] = base["documents"].pop(old)
            elif kind == "revision":
                base["task_plan"]["revision"] += 1
            else:
                base["session"]["revision"] = True
            # _envelope may reject some paths itself; preserve an otherwise
            # derivable envelope and let the projection reject its binding.
            try:
                reseal_preview(data)
            except ValueError:
                data["expected_preview_digest"] = "0" * 64
            self.assertEqual(self.project(data)["binding"]["status"], "rejected")

    def test_missing_duplicate_filtered_foreign_and_wrong_root_snapshots(self):
        for kind in ("missing", "duplicate_task", "duplicate_run", "foreign", "root", "filtered", "unknown"):
            data = deepcopy(self.data)
            snapshot = data["scheduler_snapshot"]
            if kind == "missing":
                snapshot["tasks"].pop(0)
            elif kind == "duplicate_task":
                snapshot["tasks"].append(deepcopy(snapshot["tasks"][0]))
            elif kind == "duplicate_run":
                snapshot["runs"].append(deepcopy(snapshot["runs"][0]))
            elif kind == "foreign":
                snapshot["runs"][0]["task_id"] = "foreign-task"
                reseal_run(snapshot["runs"][0])
            elif kind == "root":
                snapshot["root"] = "/wrong-root"
            elif kind == "filtered":
                snapshot["status"] = "not_found"
            else:
                snapshot["status"] = "unexpected-status"
            result = self.project(data)
            self.assertEqual(result["binding"]["status"], "matched")
            self.assert_blocked_bytes(result)

    def test_definition_drift_boolean_attempt_and_historical_success_cannot_win(self):
        for field, value in (("attempt", True), ("attempt", 2), ("max_attempts", True), ("timeout_seconds", True),
                             ("input", {}), ("action", {"type": "host_missing"}), ("idempotency_key", "other")):
            data = deepcopy(self.data)
            data["scheduler_snapshot"]["tasks"][0][field] = value
            self.assert_blocked_bytes(self.project(data))
        data = deepcopy(self.data)
        run = deepcopy(data["scheduler_snapshot"]["runs"][0])
        run.update(run_id="run-new-failed", attempt=2, status="failed", exit_code=1)
        reseal_run(run)
        data["scheduler_snapshot"]["runs"].append(run)
        data["scheduler_snapshot"]["tasks"][0].update(attempt=2, state="failed", last_run_id=run["run_id"])
        self.assert_blocked_bytes(self.project(data))

    def test_receipt_hash_and_self_consistent_but_cross_run_mismatches(self):
        for field, value in (("receipt_digest", "0" * 64), ("task_id", "another"), ("input_sha256", "1" * 64),
                             ("action_digest", "2" * 64), ("status", "failed"), ("artifacts", []), ("attempt", True)):
            data = deepcopy(self.data)
            receipt = data["scheduler_snapshot"]["runs"][0]["receipt"]
            receipt[field] = value
            if field != "receipt_digest":
                receipt["receipt_digest"] = _receipt_digest(receipt)
                data["scheduler_snapshot"]["runs"][0]["receipt_digest"] = receipt["receipt_digest"]
            self.assert_blocked_bytes(self.project(data))

    def test_artifact_inventory_type_transaction_and_cross_task_claims(self):
        for kind in ("missing", "extra", "duplicate", "kind", "hash", "mixed_tx", "reused_tx"):
            data = deepcopy(self.data)
            run = data["scheduler_snapshot"]["runs"][0]
            if kind == "missing":
                run["artifacts"] = []
            elif kind in ("extra", "duplicate"):
                run["artifacts"].append(deepcopy(run["artifacts"][0]))
                if kind == "extra":
                    run["artifacts"][-1]["path"] = "extra.md"
            elif kind == "kind":
                run["artifacts"][0]["sha256_kind"] = "logical"
            elif kind == "hash":
                run["artifacts"][0]["sha256"] = "0" * 64
            elif kind == "mixed_tx":
                run = next(candidate for candidate in data["scheduler_snapshot"]["runs"] if len(candidate["artifacts"]) > 1)
                run["artifacts"][0]["transaction_id"] = "tx-20261006T000000000000Z-ffffffffffff"
            else:
                second = next(candidate for candidate in data["scheduler_snapshot"]["runs"][1:] if candidate["artifacts"])
                for artifact in second["artifacts"]:
                    artifact["transaction_id"] = run["artifacts"][0]["transaction_id"]
                reseal_run(second)
            reseal_run(run)
            result = self.project(data)
            if kind == "mixed_tx":
                item = next(task for task in result["tasks"] if task["task_id"] == run["task_id"])
                self.assertIn("artifacts_inconsistent", item["reason_codes"])
                self.assertTrue(all(output["status"] == "unverified" for output in item["outputs"]))
            else:
                self.assert_blocked_bytes(result)

    def test_states_are_preserved_and_dependency_contradictions_are_not_frozen_inventions(self):
        for state in ("queued", "running", "failed", "frozen", "cancelled", "timed_out"):
            data = deepcopy(self.data)
            task = data["scheduler_snapshot"]["tasks"][0]
            task["state"] = state
            run = data["scheduler_snapshot"]["runs"][0]
            run.update(status=state, exit_code=None)
            reseal_run(run)
            result = self.project(data)
            self.assertEqual(result["tasks"][0]["scheduler_state"], state)
            self.assert_blocked_bytes(result)
            self.assertTrue(any("dependency_state_inconsistent" in item["reason_codes"] for item in result["tasks"][1:]))

    def test_impossible_host_success_propagates_only_negative_dependency_evidence(self):
        result = self.project(fixture(poisoned_chain=True))
        by_id = {task["plan_task_id"]: task for task in result["tasks"]}
        for task_id in ("requirements-confirmed", "product-and-ui"):
            task = by_id[task_id]
            self.assertEqual(task["scheduler_state"], "succeeded")
            self.assertEqual(task["evidence_status"], "unverified")
            self.assertIn("dependency_evidence_inconsistent", task["reason_codes"])
            self.assertTrue(all(output["status"] == "unverified" for output in task["outputs"]))
        independent = by_id["architecture"]
        self.assertNotIn("dependency_evidence_inconsistent", independent["reason_codes"])
        self.assertTrue(all(output["status"] == "byte_match" for output in independent["outputs"]))

    def test_unready_preview_execution_is_inconsistent_but_absent_execution_is_not(self):
        data = fixture(unresolved=True)
        result = self.project(data)
        self.assertEqual(result["binding"]["status"], "matched")
        self.assertIn("plan_not_executable", result["scheduler"]["reason_codes"])
        self.assertTrue(all(output["status"] == "unverified" for task in result["tasks"] for output in task["outputs"]))
        data["scheduler_snapshot"].update(status="not_initialized", tasks=[], runs=[])
        result = self.project(data)
        self.assertEqual(result["binding"]["status"], "matched")
        self.assertNotIn("plan_not_executable", result["scheduler"]["reason_codes"])
        self.assertEqual(result["capabilities"]["implementation"]["reason"], "not_in_evidence_scope")

    def test_opaque_tx_payload_never_parsed_and_never_returned(self):
        for raw in (b'{"a":1,"a":2}', b'\xff', b'{"n":NaN}', b'rolled_back', b'', b'/private/absolute/receipt.json'):
            data = deepcopy(self.data)
            capture = next(iter(data["transaction_evidence"].values()))
            capture["raw_files"] = {"receipt.json": raw}
            with mock.patch("json.loads", side_effect=AssertionError("opaque means no JSON parse")):
                result = self.project(data)
            self.assertEqual(self.first_document(result)["transaction_capture"], "opaque_supplied")

    def test_file_bytes_missing_mismatch_and_negative_acquisition_information(self):
        path = self.data["original_preview"]["tasks"][0]["action"]["plan"]["entries"][0]["path"]
        for kind in ("missing", "mismatch", "error", "tx_error"):
            data = deepcopy(self.data)
            if kind == "missing":
                data["file_evidence"].pop(path)
            elif kind == "mismatch":
                data["file_evidence"][path]["content_bytes"] = b"different supplied bytes"
            elif kind == "error":
                data["file_evidence"][path]["acquisition_error"] = "private error text"
            else:
                transaction_id = next(artifact["transaction_id"] for run in data["scheduler_snapshot"]["runs"] for artifact in run["artifacts"] if artifact["path"] == path)
                data["transaction_evidence"][transaction_id]["acquisition_error"] = "private error text"
            result = self.project(data)
            output = next(output for task in result["tasks"] for output in task["outputs"] if output["path"] == path)
            self.assertEqual(output["status"], "byte_mismatch" if kind == "mismatch" else "unverified")
            self.assertNotIn("private error text", json.dumps(result))

    def test_unknown_capture_fields_and_root_path_key_aliases_fail_closed(self):
        path = next(iter(self.data["file_evidence"]))
        for kind in ("trusted", "root", "path", "traversal", "windows", "case_alias", "nfc_alias", "bytes_type", "tx_key", "tx_root", "tx_raw_name"):
            data = deepcopy(self.data)
            file = data["file_evidence"][path]
            if kind == "trusted":
                file["trusted_validated"] = True
            elif kind == "root":
                file["root"] = "/different"
            elif kind == "path":
                file["path"] = "different.md"
            elif kind in ("traversal", "windows", "case_alias", "nfc_alias"):
                alias = {"traversal": "../escape", "windows": "C:\\outside.md", "case_alias": path.swapcase(), "nfc_alias": "cafe\u0301.md"}[kind]
                file["path"] = alias
                data["file_evidence"][alias] = file
            elif kind == "bytes_type":
                file["content_bytes"] = "not-bytes"
            elif kind == "tx_key":
                next(iter(data["transaction_evidence"].values()))["transaction_id"] = "tx-other"
            elif kind == "tx_root":
                next(iter(data["transaction_evidence"].values()))["root"] = "/different"
            else:
                next(iter(data["transaction_evidence"].values()))["raw_files"] = {"../receipt.json": b"ignored"}
            result = self.project(data)
            self.assertEqual(result["capture_reason_codes"], ["capture_invalid"])
            self.assert_blocked_bytes(result)

    def test_custom_types_nonfinite_cycles_depth_and_resource_bounds_reject(self):
        class EvilDict(dict):
            def items(self):
                raise AssertionError("must not call custom objects")
        bad = [None, UserDict(), EvilDict(), float("nan"), float("inf"), True]
        cycle = []
        cycle.append(cycle)
        bad.append(cycle)
        for value in bad:
            data = deepcopy(self.data)
            data["original_preview"] = value
            result = project_taskplan_evidence(**data)
            self.assertEqual(result["binding"]["status"], "rejected")
            self.assertEqual(result["tasks"], [])
        for name, limit in (("MAX_BYTES", 10), ("MAX_ITEMS", 2), ("MAX_NODES", 10), ("MAX_DEPTH", 2), ("MAX_TASKS", 1)):
            with mock.patch.object(module, name, limit):
                result = project_taskplan_evidence(**self.data)
            self.assertEqual(result["binding"]["reason_codes"], ["resource_limit"])

    def test_invalid_snapshot_or_capture_does_not_reject_valid_original_binding(self):
        for key in ("scheduler_snapshot", "transaction_evidence", "file_evidence"):
            for bad in (None, UserDict(), float("nan"), b"not-json"):
                data = deepcopy(self.data)
                data[key] = bad
                result = project_taskplan_evidence(**data)
                self.assertEqual(result["binding"], {"status": "matched", "reason_codes": []})
                if key == "scheduler_snapshot":
                    self.assertEqual(result["scheduler"]["reason_codes"], ["snapshot_invalid"])
                else:
                    self.assertEqual(result["capture_reason_codes"], ["capture_invalid"])
                self.assert_blocked_bytes(result)

    def test_transaction_identifier_must_match_core_format(self):
        data = deepcopy(self.data)
        run = data["scheduler_snapshot"]["runs"][0]
        run["artifacts"][0]["transaction_id"] = "tx-not-a-core-id"
        reseal_run(run)
        result = self.project(data)
        self.assertIn("artifacts_inconsistent", self.first_document(result)["reason_codes"])
        self.assert_blocked_bytes(result)

    def test_later_resource_limits_keep_binding_and_label_the_affected_section(self):
        data = deepcopy(self.data)
        data["scheduler_snapshot"]["tasks"] = [None] * (module.MAX_ITEMS + 1)
        result = project_taskplan_evidence(**data)
        self.assertEqual(result["binding"]["status"], "matched")
        self.assertEqual(result["scheduler"]["reason_codes"], ["snapshot_invalid", "resource_limit"])
        self.assert_blocked_bytes(result)
        data = deepcopy(self.data)
        capture = next(iter(data["transaction_evidence"].values()))
        capture["raw_files"]["receipt.json"] = b"x" * (module.MAX_BYTES + 1)
        result = project_taskplan_evidence(**data)
        self.assertEqual(result["binding"]["status"], "matched")
        self.assertEqual(result["capture_reason_codes"], ["capture_invalid", "resource_limit"])
        self.assert_blocked_bytes(result)

    def test_deterministic_ordering_no_input_mutation_and_no_host_claim_without_scope(self):
        first = self.project()
        self.assertEqual(first, self.project())
        data = deepcopy(self.data)
        data["scheduler_snapshot"]["tasks"].reverse()
        data["scheduler_snapshot"]["runs"].reverse()
        self.assertEqual(first, self.project(data))
        empty_scope = fixture(unresolved=True)
        result = self.project(empty_scope)
        self.assertEqual(result["binding"]["status"], "matched")
        self.assertEqual(result["capabilities"]["implementation"], {"status": "unverified", "reason": "not_in_evidence_scope"})
        self.assertEqual(result["capabilities"]["tests"], {"status": "unverified", "reason": "not_in_evidence_scope"})

    def test_purity_spies_cover_io_clock_uuid_network_execution_and_locks(self):
        import datetime
        import io
        import socket
        import sqlite3
        import subprocess
        import time
        import uuid
        # Imports and fixture generation precede all spies; the projection is
        # exercised with valid, rejected and negative acquisition captures.
        import opencoding.transactions
        import opencoding.sessions
        import opencoding.scheduler_migrations
        targets = [
            "builtins.open", "io.open", "os.open", "os.stat", "os.lstat", "os.scandir", "os.listdir", "os.mkdir", "os.makedirs",
            "pathlib.Path.resolve", "pathlib.Path.read_bytes", "pathlib.Path.read_text", "pathlib.Path.exists", "pathlib.Path.stat",
            "sqlite3.connect", "socket.socket", "socket.create_connection", "subprocess.Popen", "subprocess.run",
            "time.time", "time.monotonic", "time.perf_counter", "uuid.uuid4",
            "opencoding.scheduler.Scheduler.__init__", "opencoding.scheduler.Scheduler.recover", "opencoding.scheduler.read_snapshot",
            "opencoding.scheduler._connect", "opencoding.scheduler_migrations.migrate", "opencoding.executor.Executor.__init__",
            "opencoding.service.preview_session", "opencoding.service._graph", "opencoding.service.execution_status",
            "opencoding.taskplan_scheduler.preview_task_plan", "opencoding.taskplan_scheduler.execute_task_plan",
            "opencoding.taskplan_scheduler.approve_task_plan", "opencoding.service.apply_approved", "opencoding.service.rollback",
            "opencoding.sessions.save_session", "opencoding.sessions.session_write_lock", "opencoding.sessions._acquire_lock",
            "opencoding.transactions._load_receipt_manifest", "opencoding.transactions._load_events", "opencoding.transactions._cooperative_lock",
            "opencoding.transactions.apply_changes", "opencoding.transactions.rollback_changes",
        ]
        values = [deepcopy(self.data) for _ in range(3)]
        values[1]["expected_preview_digest"] = "0" * 64
        next(iter(values[2]["file_evidence"].values()))["acquisition_error"] = "private sharing failure"
        with ExitStack() as stack:
            calls = [stack.enter_context(mock.patch(target, side_effect=AssertionError(target))) for target in targets]
            for name in ("opencoding.service.datetime", "opencoding.taskplan_scheduler.datetime", "opencoding.transactions.datetime", "opencoding.scheduler.datetime"):
                clock = stack.enter_context(mock.patch(name))
                clock.now.side_effect = AssertionError("no clock")
                calls.append(clock.now)
            for data in values:
                self.project(data)
            for call in calls:
                call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
