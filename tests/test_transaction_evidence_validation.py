"""Pure content checks and public-input bounds, separate from file provenance."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

from opencoding import transaction_evidence_validation as validation
from opencoding import transactions
from opencoding.executor import Executor
from opencoding.scheduler import Scheduler
from tests.test_product_transactions import (
    evidence_events, evidence_hash, evidence_json, transaction_bytes_fixture,
)


def replace_documents(values, *, receipt=None, manifest=None, payloads=None):
    result = deepcopy(values)
    if manifest is not None:
        result["manifest_bytes"] = evidence_json(manifest)
        receipt = json.loads(result["receipt_bytes"]) if receipt is None else receipt
        receipt["manifest_sha256"] = evidence_hash(result["manifest_bytes"][:-1])
    if receipt is not None:
        result["receipt_bytes"] = evidence_json(receipt)
    if payloads is not None:
        result["events_bytes"] = evidence_events(payloads)
    return result


def input_size(values):
    return sum(len(values[key]) for key in ("receipt_bytes", "manifest_bytes", "events_bytes")) + sum(len(key.encode("utf-8")) + len(raw) for key, raw in values["preimage_bytes"].items())


def node_count(value):
    if isinstance(value, dict):
        return 1 + sum(node_count(key) + node_count(child) for key, child in value.items())
    if isinstance(value, list):
        return 1 + sum(node_count(child) for child in value)
    return 1


def input_nodes(values):
    return sum(node_count(json.loads(values[key])) for key in ("receipt_bytes", "manifest_bytes")) + node_count([json.loads(row) for row in values["events_bytes"].splitlines()]) + node_count(values["preimage_bytes"])


class TransactionEvidenceValidationTests(unittest.TestCase):
    def validate(self, values):
        return validation.validate_transaction_bytes(**values)

    def rejected(self, values, message=None):
        with self.assertRaises(ValueError) as caught:
            self.validate(values)
        if message is not None:
            self.assertEqual(str(caught.exception), message)

    def test_valid_mixed_create_and_update_return_plain_parsed_content(self):
        for create_only in (False, True):
            with self.subTest(create_only=create_only):
                values = transaction_bytes_fixture(create_only=create_only)
                receipt, manifest, entries, events = self.validate(values)
                self.assertEqual(receipt, json.loads(values["receipt_bytes"]))
                self.assertEqual(manifest, json.loads(values["manifest_bytes"]))
                self.assertIs(entries, manifest["entries"])
                self.assertEqual(events, [json.loads(row) for row in values["events_bytes"].splitlines()])
        values = transaction_bytes_fixture()
        manifest = json.loads(values["manifest_bytes"])
        manifest["entries"].pop()
        receipt = json.loads(values["receipt_bytes"])
        receipt["planned_paths"] = receipt["changed_paths"] = ["existing.txt"]
        self.assertEqual(len(self.validate(replace_documents(values, receipt=receipt, manifest=manifest))[2]), 1)

    def test_repeated_call_and_caller_inputs_are_unchanged(self):
        values = transaction_bytes_fixture()
        original = deepcopy(values)
        first = self.validate(values)
        self.assertEqual(self.validate(values), first)
        self.assertEqual(values, original)
        first[2][0]["path"] = "changed-only-in-return.txt"
        self.assertEqual(self.validate(values)[2][0]["path"], "existing.txt")

    def test_identity_is_checked_first_and_uses_shared_regex(self):
        self.assertIs(validation._TRANSACTION_ID, transactions._TRANSACTION_ID)
        for identifier in (None, 1, "tx-anything", "tx-20261006T000000000000Z-000000000001\n", "../transaction"):
            values = transaction_bytes_fixture()
            values.update(transaction_id=identifier, receipt_bytes=object(), preimage_bytes=object())
            self.rejected(values, "invalid transaction id")
        values = transaction_bytes_fixture()
        bad = "tx-self-consistent-but-invalid"
        receipt, manifest = json.loads(values["receipt_bytes"]), json.loads(values["manifest_bytes"])
        receipt["transaction_id"] = manifest["transaction_id"] = bad
        payloads = [{"transaction_id": bad, "status": "applied", "changed_paths": [], "at": ""}]
        values = replace_documents(values, receipt=receipt, manifest=manifest, payloads=payloads)
        values["transaction_id"] = bad
        self.rejected(values, "invalid transaction id")

    def test_exact_builtin_types_reject_without_subclass_hooks(self):
        calls = []
        def forbidden(*args, **kwargs):
            calls.append("hook")
            raise AssertionError("subclass hook was called")
        class Bytes(bytes):
            __len__ = endswith = count = __iter__ = forbidden
        class Text(str):
            __len__ = encode = __str__ = forbidden
        class Mapping(dict):
            __len__ = __iter__ = items = keys = forbidden
        values = transaction_bytes_fixture()
        replacements = [("transaction_id", Text(values["transaction_id"])), ("receipt_bytes", Bytes(b"{}\n")),
                        ("manifest_bytes", bytearray(b"{}\n")), ("events_bytes", memoryview(b"x")),
                        ("preimage_bytes", Mapping()), ("preimage_bytes", {Text("preimage/0.bin"): b"before"}),
                        ("preimage_bytes", {"preimage/0.bin": Bytes(b"before")}),
                        ("receipt_bytes", Path("synthetic.json")), ("events_bytes", lambda: b""),
                        ("preimage_bytes", {1: b""})]
        for key, value in replacements:
            with self.subTest(key=key, type=type(value).__name__):
                changed = dict(values)
                changed[key] = value
                self.rejected(changed)
        self.assertEqual(calls, [])

    def test_canonical_json_bytes_are_never_repaired(self):
        values = transaction_bytes_fixture()
        malformed = [b'{"a":1,"a":2}\n', b'{"b":2,"a":1}\n', b'{"a": 1}\n', b'\xef\xbb\xbf{}\n',
                     b'"\xff"\n', b'{}', b'{}\n\n', b'{}\r\n', b'NaN\n', b'Infinity\n', b'[]\n', b'null\n']
        for field in ("receipt_bytes", "manifest_bytes", "events_bytes"):
            for raw in malformed:
                with self.subTest(field=field, raw=raw):
                    changed = dict(values)
                    changed[field] = raw
                    self.rejected(changed)
        changed = dict(values, manifest_bytes=values["manifest_bytes"].replace(b':', b': ', 1))
        receipt = json.loads(values["receipt_bytes"])
        receipt["manifest_sha256"] = evidence_hash(changed["manifest_bytes"][:-1])
        changed["receipt_bytes"] = evidence_json(receipt)
        self.rejected(changed, "manifest is not canonical JSON")

    def test_single_manifest_buffer_cannot_use_second_buffer_digest(self):
        values = transaction_bytes_fixture()
        other = json.loads(values["manifest_bytes"])
        other["entries"][0]["after_sha256"] = "2" * 64
        other_bytes = evidence_json(other)
        receipt = json.loads(values["receipt_bytes"])
        receipt["manifest_sha256"] = evidence_hash(other_bytes[:-1])
        values["receipt_bytes"] = evidence_json(receipt)
        self.rejected(values, "manifest digest mismatch")
        values["manifest_bytes"] = other_bytes
        self.assertEqual(self.validate(values)[1], other)

    def test_receipt_structure_identity_paths_and_state_errors(self):
        values = transaction_bytes_fixture()
        cases = [{"unknown": True}, {"schema_version": "other"}, {"transaction_id": "other"},
                 {"plan_digest": "bad"}, {"status": "rolled_back"}, {"planned_paths": ["../escape"]},
                 {"planned_paths": ["existing.txt", "existing.txt"]}, {"changed_paths": ["existing.txt"] * 2},
                 {"uncertain_paths": ["existing.txt"] * 2}, {"rollback_status": True},
                 {"rollback_changed_paths": ["unknown.txt"]}, {"rollback_residual_paths": ["x"] * 2},
                 {"started_at": 1}, {"reason_codes": [1]}, {"manifest_sha256": "bad"},
                 {"planned_paths": ["elsewhere.txt"]}, {"changed_paths": ["elsewhere.txt"]}]
        for change in cases:
            with self.subTest(change=change):
                receipt = json.loads(values["receipt_bytes"])
                receipt.update(change)
                self.rejected(replace_documents(values, receipt=receipt))
        receipt = json.loads(values["receipt_bytes"])
        del receipt["started_at"]
        self.rejected(replace_documents(values, receipt=receipt), "receipt fields are incompatible")

    def test_manifest_fields_paths_metadata_and_index_errors(self):
        values = transaction_bytes_fixture()
        for change in ({"unknown": True}, {"schema_version": "bad"}, {"transaction_id": "bad"}, {"plan_digest": "2" * 64}, {"entries": []}, {"entries": {}}):
            manifest = json.loads(values["manifest_bytes"])
            manifest.update(change)
            self.rejected(replace_documents(values, manifest=manifest))
        for change in ({"unknown": True}, {"path": "../escape"}, {"path": "nested//new.txt"}, {"path": "nested/new.txt"},
                       {"path": 1}, {"before_exists": 1}, {"before_sha256": "bad"}, {"after_sha256": None},
                       {"preimage_file": "preimage/1.bin"}, {"before_identity": [1]}):
            with self.subTest(change=change):
                manifest = json.loads(values["manifest_bytes"])
                manifest["entries"][0].update(change)
                self.rejected(replace_documents(values, manifest=manifest))
        manifest = json.loads(values["manifest_bytes"])
        manifest["entries"][1]["before_identity"] = [1, 2]
        self.rejected(replace_documents(values, manifest=manifest), "manifest identity is invalid")

    def test_preimage_bytes_and_inventory_are_exact(self):
        values = transaction_bytes_fixture()
        for preimages in ({}, {"preimage/0.bin": b"before\n"}, {"preimage/0.bin": b"before\r\n", "extra": b""},
                          {"preimage/": b""}, {"preimage/0.bin": "before"}):
            self.rejected(dict(values, preimage_bytes=preimages))
        created = transaction_bytes_fixture(create_only=True)
        self.rejected(dict(created, preimage_bytes={"preimage/0.bin": b""}), "transaction preimage inventory disagrees")
        self.assertEqual(self.validate(created)[2][0]["before_exists"], False)

    def test_event_schema_digest_and_full_prefix_chain(self):
        values = transaction_bytes_fixture()
        rows = values["events_bytes"].splitlines(keepends=True)
        variations = [b"", rows[0] + b"\n" + b"".join(rows[1:]), values["events_bytes"][:-1],
                      rows[1] + rows[0] + rows[2], rows[0] + rows[2], rows[0] + values["events_bytes"]]
        for raw in variations:
            self.rejected(dict(values, events_bytes=raw))
        for wrong in (evidence_hash(rows[1]), json.loads(rows[1])["event_sha256"]):
            last = json.loads(rows[2])
            last["previous_event_sha256"] = wrong
            self.rejected(dict(values, events_bytes=b"".join(rows[:2]) + evidence_json(last)), "event chain is broken")
        payload = {"transaction_id": values["transaction_id"], "status": "applied", "changed_paths": [], "at": ""}
        for change in ({"unknown": True}, {"transaction_id": "bad"}, {"status": "unknown"}, {"changed_paths": ["x", "x"]},
                       {"changed_paths": ["../escape"]}, {"at": None}, {"path": "absent"}, {"reason_codes": [1]},
                       {"after_identity": ["not-int", 1]}, {"parent_paths": ["x", "x"]}, {"parent_identities": {"x": [1]}}):
            self.rejected(replace_documents(values, payloads=[dict(payload, **change)]))
        first = json.loads(rows[0])
        first["event_sha256"] = "0" * 64
        self.rejected(dict(values, events_bytes=evidence_json(first)), "event digest mismatch")

    def test_legacy_loose_values_and_raw_status_are_not_strengthened(self):
        values = transaction_bytes_fixture()
        manifest, receipt = json.loads(values["manifest_bytes"]), json.loads(values["receipt_bytes"])
        manifest["entries"][0]["before_identity"] = [2 ** 128, {"any": True}]
        receipt.update(started_at="", finished_at="not-ISO", rollback_status="arbitrary", rollback_changed_paths=["existing.txt"] * 2)
        payload = {"transaction_id": values["transaction_id"], "status": "write_intent", "changed_paths": ["outside-manifest.txt"], "at": "",
                   "before_sha256": None, "after_sha256": "1" * 64, "before_identity": [True, False], "after_identity": [False, True]}
        # These are legacy content rules, not a complete event state machine.
        for status in ("started", "failed", "partial_failure", "applied"):
            receipt["status"] = status
            result = self.validate(replace_documents(values, receipt=receipt, manifest=manifest, payloads=[payload]))
            self.assertEqual(result[0]["status"], status)
            self.assertEqual(result[1]["entries"][0]["before_identity"][0], 2 ** 128)
            self.assertNotIn("observed", result[0])

    def test_default_aggregate_byte_boundary_and_one_over(self):
        small = transaction_bytes_fixture(before=b"")
        amount = 16 * 1024 * 1024 - input_size(small)
        values = transaction_bytes_fixture(before=b"x" * amount)
        self.assertEqual(input_size(values), 16777216)
        self.assertEqual(self.validate(values)[2][0]["before_sha256"], evidence_hash(b"x" * amount))
        values["preimage_bytes"]["preimage/0.bin"] += b"x"
        self.assertEqual(input_size(values), 16777217)
        self.rejected(values, validation._LIMIT_ERROR)

    def test_utf8_preimage_keys_count_toward_aggregate_bytes(self):
        values = transaction_bytes_fixture()
        values["preimage_bytes"]["名字"] = b""
        total = input_size(values)
        with mock.patch.object(validation, "_MAX_BYTES", total - 1):
            self.rejected(values, validation._LIMIT_ERROR)
        with mock.patch.object(validation, "_MAX_BYTES", total):
            self.rejected(values, "transaction preimage inventory disagrees")
        values["preimage_bytes"] = {"\ud800": b""}
        self.rejected(values, "preimage key is not valid UTF-8")

    def test_default_depth_boundary_with_legacy_nested_identity(self):
        for depth in (64, 65):
            values = transaction_bytes_fixture()
            manifest = json.loads(values["manifest_bytes"])
            nested = 0
            for _ in range(depth - 4):
                nested = [nested]
            manifest["entries"][0]["before_identity"] = [nested, 0]
            values = replace_documents(values, manifest=manifest)
            if depth == 64:
                self.validate(values)
            else:
                self.rejected(values, validation._LIMIT_ERROR)

    def test_default_node_boundary_counts_keys_events_and_preimage_map(self):
        values = transaction_bytes_fixture()
        manifest = json.loads(values["manifest_bytes"])
        blocks = []
        manifest["entries"][0]["before_identity"] = [blocks, 0]
        values = replace_documents(values, manifest=manifest)
        remaining = 100000 - input_nodes(values)
        while remaining:
            if remaining == 1:
                blocks.append(0)
                remaining -= 1
            else:
                count = min(4096, remaining - 1)
                blocks.append([0] * count)
                remaining -= count + 1
        values = replace_documents(values, manifest=manifest)
        self.assertEqual(input_nodes(values), 100000)
        self.validate(values)
        blocks.append(0)
        values = replace_documents(values, manifest=manifest)
        self.assertEqual(input_nodes(values), 100001)
        self.rejected(values, validation._LIMIT_ERROR)

    def test_default_container_boundary_and_bounded_event_list(self):
        values = transaction_bytes_fixture()
        receipt = json.loads(values["receipt_bytes"])
        receipt["reason_codes"] = [""] * 4096
        self.validate(replace_documents(values, receipt=receipt))
        receipt["reason_codes"].append("")
        self.rejected(replace_documents(values, receipt=receipt), validation._LIMIT_ERROR)
        payload = {"transaction_id": values["transaction_id"], "status": "failed", "changed_paths": [], "at": ""}
        with mock.patch.object(validation, "_MAX_CONTAINER", 16):
            bounded = replace_documents(values, payloads=[payload] * 16)
            self.assertEqual(len(self.validate(bounded)[3]), 16)
            self.rejected(replace_documents(values, payloads=[payload] * 17), validation._LIMIT_ERROR)
            self.rejected(dict(values, events_bytes=b"\r\n" * 17), validation._LIMIT_ERROR)
            self.rejected(dict(values, events_bytes=b"\n" * 16 + b"{}"), validation._LIMIT_ERROR)

    def test_preimage_map_container_is_bounded_before_content_checks(self):
        values = transaction_bytes_fixture()
        with mock.patch.object(validation, "_MAX_CONTAINER", 16):
            with mock.patch.object(validation, "_strict_json_bytes", side_effect=AssertionError("must stop before parse")) as parser:
                self.rejected(dict(values, preimage_bytes={str(index): b"" for index in range(17)}), validation._LIMIT_ERROR)
                parser.assert_not_called()
        # The exact map boundary reaches content validation, not a budget error.
        with mock.patch.object(validation, "_MAX_CONTAINER", 16):
            self.rejected(dict(values, preimage_bytes={str(index): b"" for index in range(16)}), "transaction preimage inventory disagrees")

    def test_recursion_error_is_stable_only_at_new_entry(self):
        values = transaction_bytes_fixture()
        values["receipt_bytes"] = b"[" * (sys.getrecursionlimit() + 20) + b"0" + b"]" * (sys.getrecursionlimit() + 20) + b"\n"
        self.rejected(values, validation._LIMIT_ERROR)
        with mock.patch.object(validation.json, "loads", side_effect=RecursionError("synthetic parser recursion")):
            self.rejected(transaction_bytes_fixture(), validation._LIMIT_ERROR)
            with self.assertRaisesRegex(RecursionError, "synthetic parser recursion"):
                transactions._strict_json_bytes(b"{}\n", "receipt")

    def test_new_guard_limits_do_not_apply_to_legacy_reader(self):
        values = transaction_bytes_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / ".opencoding/transactions" / values["transaction_id"]
            (evidence / "preimage").mkdir(parents=True)
            for key, name in (("receipt_bytes", "receipt.json"), ("manifest_bytes", "manifest.json"), ("events_bytes", "events.jsonl")):
                (evidence / name).write_bytes(values[key])
            for name, raw in values["preimage_bytes"].items():
                (evidence / name).write_bytes(raw)
            with mock.patch.object(validation, "_MAX_BYTES", 1):
                self.rejected(values, validation._LIMIT_ERROR)
                with mock.patch.object(validation, "validate_transaction_bytes", side_effect=AssertionError("aggregate called")) as aggregate, mock.patch.object(validation, "_check_input_bytes", side_effect=AssertionError("guard called")) as guard, mock.patch.object(validation, "_structure_nodes", side_effect=AssertionError("nodes called")) as nodes:
                    result = transactions._load_receipt_manifest(root, values["transaction_id"])
                    events = transactions._load_events(evidence, values["transaction_id"])
                self.assertEqual(result[1], json.loads(values["receipt_bytes"]))
                self.assertEqual(len(events), 3)
                for spy in (aggregate, guard, nodes):
                    spy.assert_not_called()

    def test_valid_and_rejected_inputs_never_call_io_clock_or_runtime(self):
        values = transaction_bytes_fixture()
        bad_json = dict(values, receipt_bytes=b"bad")
        bad_hash = dict(values, preimage_bytes={"preimage/0.bin": b"wrong"})
        bad_events = dict(values, events_bytes=b"{}\n")
        bad_id = dict(values, transaction_id="bad")
        bad_budget = dict(values, preimage_bytes={"x": b""})
        bad_budget["receipt_bytes"] = b"x" * (16777216 + 1)
        cases = [values, bad_json, bad_hash, bad_events, bad_id, bad_budget]
        originals = deepcopy(cases)
        spies = []
        output, errors = StringIO(), StringIO()
        with ExitStack() as stack:
            targets = [(io, "open"), (os, "open"), (os, "stat"), (os, "lstat"), (os, "listdir"), (os, "scandir"),
                       (time, "time"), (time, "monotonic"), (time, "perf_counter"), (uuid, "uuid4"),
                       (subprocess, "Popen"), (subprocess, "run"), (socket, "socket"), (socket, "create_connection"), (sqlite3, "connect"),
                       (Scheduler, "__init__"), (Executor, "__init__")]
            targets.extend((Path, name) for name in ("open", "read_bytes", "write_bytes", "read_text", "write_text", "resolve", "stat", "lstat", "exists", "mkdir", "iterdir", "unlink"))
            targets.extend((transactions, name) for name in ("_load_receipt_manifest", "_load_events", "_cooperative_lock", "apply_changes", "rollback_changes", "_now", "_new_transaction_id"))
            spies.append(stack.enter_context(mock.patch("builtins.open", side_effect=AssertionError("unexpected IO"))))
            for owner, name in targets:
                spies.append(stack.enter_context(mock.patch.object(owner, name, side_effect=AssertionError("unexpected IO/runtime"))))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            self.validate(cases[0])
            for case in cases[1:]:
                self.rejected(case)
        for spy in spies:
            spy.assert_not_called()
        self.assertEqual(cases, originals)
        self.assertEqual((output.getvalue(), errors.getvalue()), ("", ""))


if __name__ == "__main__":
    unittest.main()
