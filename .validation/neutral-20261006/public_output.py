"""Bounded public summaries of reviewed validation records; no raw-text output."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

MAX_JSON_BYTES = 2 * 1024 * 1024
LABELS = frozenset({"source-before", "prerequisites", "cargo-version", "rustc-version", "adapter",
                   "cli-preview", "evidence-projection", "installed-taskplan", "strict-validator",
                   "transaction-regression", "full", "packaging", "rust-domain", "source-after"})
TEST_LABELS = LABELS - {"source-before", "prerequisites", "cargo-version", "rustc-version", "rust-domain", "source-after"}
REASONS = frozenset({"none", "nonzero_exit", "test_count_mismatch", "skipped_tests", "result_parse_failed",
                     "source_verification_failed", "prerequisite_failed", "execution_incomplete", "log_unavailable"})
CONTEXT_KEYS = {"schema_version", "candidate_commit", "candidate_tree", "validation_commit", "binding_sha256",
                "source_files_sha256", "verifier_sha256", "windows_runner_sha256", "tracked_file_count",
                "python_version", "required_labels", "expected_test_counts"}
RECORD_KEYS = {"label", "exit_code", "acceptance_exit_code", "tests_run", "skip_count", "log_sha256", "state", "reason_code"}
SUMMARY_KEYS = {"schema_version", "kind", "workflow_scope", "candidate_commit", "candidate_tree", "validation_commit",
                "execution_commit", "binding_sha256", "python_version", "status", "source_before", "source_after", "records"}


class Rejected(ValueError):
    """Messages are fixed codes, never caller-controlled details."""


def require(condition: bool) -> None:
    if not condition:
        raise Rejected("summary_input_rejected")


def exact_keys(value, keys):
    require(type(value) is dict and set(value) == keys)


def hex_value(value, length):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value) is not None


def count(value):
    return type(value) is int and 0 <= value <= 10_000_000


def exit_value(value):
    return value is None or (type(value) is int and -(2**31) <= value < 2**32)


def text(value, limit=32768):
    return type(value) is str and len(value) <= limit


def strict_json(raw):
    require(type(raw) is bytes and len(raw) <= MAX_JSON_BYTES)
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result
    def constant(_value):
        raise Rejected("summary_input_rejected")
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise Rejected("summary_input_rejected") from None


def load_json(path, expected_sha256=None):
    with Path(path).open("rb") as handle:
        raw = handle.read(MAX_JSON_BYTES + 1)
    require(len(raw) <= MAX_JSON_BYTES)
    if expected_sha256 is not None:
        require(hex_value(expected_sha256, 64) and hashlib.sha256(raw).hexdigest() == expected_sha256)
    return strict_json(raw)


def validate_context(value):
    exact_keys(value, CONTEXT_KEYS)
    require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    for key in ("candidate_commit", "candidate_tree", "validation_commit"):
        require(hex_value(value[key], 40))
    for key in ("binding_sha256", "source_files_sha256", "verifier_sha256", "windows_runner_sha256"):
        require(hex_value(value[key], 64))
    require(count(value["tracked_file_count"]) and value["tracked_file_count"] > 0)
    require(value["python_version"] in ("3.11", "3.12"))
    labels = value["required_labels"]
    require(type(labels) is list and 3 <= len(labels) <= len(LABELS))
    require(all(type(label) is str and label in LABELS for label in labels))
    require(len(set(labels)) == len(labels) and labels[0] == "source-before" and labels[-1] == "source-after")
    counts = value["expected_test_counts"]
    require(type(counts) is dict and set(counts) == set(labels) & TEST_LABELS and "full" in counts)
    require(all(count(number) and number > 0 for number in counts.values()))
    return value


def parse_unittest(raw):
    """Require one unambiguous unittest footer; surrounding output stays private."""
    if type(raw) is not bytes:
        return None, None, "result_parse_failed"
    try:
        decoded = raw.decode("utf-8")
    except UnicodeError:
        return None, None, "result_parse_failed"
    # No terminal control processing or inferred cleanup of a malformed footer.
    ran = list(re.finditer(r"(?m)^Ran ([0-9]+) tests? in [0-9]+(?:\.[0-9]+)?s\r?$", decoded))
    if len(ran) != 1:
        return None, None, "result_parse_failed"
    tail = decoded[ran[0].end():]
    footer = re.match(r"\r?\n\r?\n(OK|FAILED)(?: \(([^\r\n]+)\))?(?:\r?\n|$)", tail)
    # Redirected stdout may flush after unittest's stderr footer; retain it privately.
    all_footers = list(re.finditer(r"(?m)^(?:OK|FAILED)(?: \([^\r\n]*\))?\r?$", decoded))
    if footer is None or len(all_footers) != 1:
        return None, None, "result_parse_failed"
    details = {}
    if footer[2]:
        for item in footer[2].split(", "):
            match = re.fullmatch(r"(failures|errors|skipped|expected failures|unexpected successes)=([0-9]+)", item)
            if match is None or match[1] in details:
                return None, None, "result_parse_failed"
            if len(match[2]) > 8:
                return None, None, "result_parse_failed"
            details[match[1]] = int(match[2])
    if len(ran[0][1]) > 8:
        return None, None, "result_parse_failed"
    number, skips = int(ran[0][1]), details.get("skipped", 0)
    if not count(number) or not count(skips) or skips > number or any(not count(n) or n > number for n in details.values()):
        return None, None, "result_parse_failed"
    failed_fields = sum(details.get(key, 0) for key in ("failures", "errors", "unexpected successes"))
    if footer[1] == "FAILED" or failed_fields:
        return number, skips, "nonzero_exit"
    return number, skips, "none"


def record(label, code, number, skips, digest, expected, *, parse_reason="none"):
    require(type(label) is str and label in LABELS and exit_value(code))
    require(number is None or count(number))
    require(skips is None or count(skips))
    require(digest is None or hex_value(digest, 64))
    require(expected is None or (count(expected) and expected > 0))
    require(type(parse_reason) is str and parse_reason in REASONS)
    reason = "none"
    if code is None:
        reason = "execution_incomplete"
    elif code != 0:
        reason = "nonzero_exit"
    elif digest is None:
        reason = "log_unavailable"
    elif parse_reason != "none":
        require(parse_reason in REASONS)
        reason = parse_reason
    elif expected is not None and (number is None or skips is None):
        reason = "result_parse_failed"
    elif expected is not None and number != expected:
        reason = "test_count_mismatch"
    elif skips:
        reason = "skipped_tests"
    return {"label": label, "exit_code": code, "acceptance_exit_code": 0 if reason == "none" else 1,
            "tests_run": number, "skip_count": skips, "log_sha256": digest,
            "state": "incomplete" if code is None else "completed", "reason_code": reason}


def _summary(context, scope, rows, before, after, status):
    return {"schema_version": 1, "kind": "public_windows_validation_summary", "workflow_scope": scope,
            "candidate_commit": context["candidate_commit"], "candidate_tree": context["candidate_tree"],
            "validation_commit": context["validation_commit"],
            "execution_commit": context["candidate_commit"] if scope == "pinned_candidate_Q" else context["validation_commit"],
            "binding_sha256": context["binding_sha256"], "python_version": context["python_version"],
            "status": status, "source_before": before, "source_after": after, "records": rows}


def build_existing(context, row):
    validate_context(context)
    require(row["label"] == "full")
    status = "passed" if row["acceptance_exit_code"] == 0 else "incomplete" if row["state"] != "completed" else "blocked_or_failed"
    return _summary(context, "existing_ci_full_W", [row], "not_applicable", "not_applicable", status)


def _source_status(value, context, row):
    if value is None or row is None or row["exit_code"] is None:
        return "not_completed"
    if row["acceptance_exit_code"] != 0:
        return "failed"
    try:
        exact_keys(value, {"status", "candidate_commit", "candidate_tree", "tracked_files", "binding_sha256",
                           "verifier_sha256", "source_files_sha256", "windows_historical_link_files", "windows_runtime_validated"})
        require(value["status"] == "source_verified" and value["windows_runtime_validated"] is False)
        for key in ("candidate_commit", "candidate_tree", "binding_sha256", "verifier_sha256", "source_files_sha256"):
            require(value[key] == context[key])
        require(type(value["tracked_files"]) is int and value["tracked_files"] == context["tracked_file_count"])
        links = value["windows_historical_link_files"]
        require(type(links) is list and len(links) <= 4096)
        require(all(text(path, 4096) and path.startswith("artifacts/") and "\\" not in path
                    and all(part not in ("", ".", "..") for part in path.split("/")) for path in links))
        return "verified"
    except (Rejected, KeyError, TypeError):
        return "failed"


def build_pinned(context, private_result, source_before, source_after):
    """Project J-shaped local result values; never copy arbitrary strings out."""
    validate_context(context)
    exact_keys(private_result, {"status", "python_version", "binding_sha256", "records", "runner", "github_event",
                                "github_sha", "github_run_id", "github_run_attempt", "github_job", "script_sha256"})
    require(private_result["status"] in ("passed_for_this_python_version", "blocked_or_failed", "pending"))
    require(private_result["python_version"] == context["python_version"])
    require(private_result["binding_sha256"] == context["binding_sha256"])
    require(private_result["github_sha"] == context["validation_commit"])
    require(private_result["script_sha256"] == context["windows_runner_sha256"])
    require(private_result["runner"] == "Windows" and private_result["github_event"] == "push")
    require(all(type(private_result[key]) is str and re.fullmatch(r"[0-9]{1,24}", private_result[key])
                for key in ("github_run_id", "github_run_attempt")))
    require(text(private_result["github_job"], 80) and re.fullmatch(r"[A-Za-z0-9_-]+", private_result["github_job"]) is not None)
    raw_rows = private_result["records"]
    require(type(raw_rows) is list and len(raw_rows) <= len(LABELS))
    by_label = {}
    for item in raw_rows:
        exact_keys(item, {"label", "program", "arguments", "exit_code", "tests_run", "skips", "log_sha256"})
        label = item["label"]
        require(type(label) is str and label in context["required_labels"] and label not in by_label)
        require(text(item["program"], 4096))
        require(type(item["arguments"]) is list and len(item["arguments"]) <= 128 and all(text(x, 32768) for x in item["arguments"]))
        require(type(item["skips"]) is list and len(item["skips"]) <= 4096 and all(text(x) for x in item["skips"]))
        require(item["tests_run"] is None or count(item["tests_run"]))
        expected = context["expected_test_counts"].get(label)
        require(expected is not None or (item["tests_run"] is None and not item["skips"]))
        by_label[label] = record(label, item["exit_code"], item["tests_run"], len(item["skips"]) if expected is not None else None,
                                 item["log_sha256"], expected)
    rows = [by_label.get(label) or record(label, None, None, None, None, context["expected_test_counts"].get(label))
            for label in context["required_labels"]]
    before = _source_status(source_before, context, by_label.get("source-before"))
    after = _source_status(source_after, context, by_label.get("source-after"))
    complete = all(row["exit_code"] is not None for row in rows)
    passed = complete and all(row["acceptance_exit_code"] == 0 for row in rows) and before == after == "verified"
    passed &= private_result["status"] == "passed_for_this_python_version"
    status = "passed" if passed else "incomplete" if not complete else "blocked_or_failed"
    return _summary(context, "pinned_candidate_Q", rows, before, after, status)


def public_json(value, context):
    """Validate even the locally written summary before publishing it."""
    validate_context(context)
    exact_keys(value, SUMMARY_KEYS)
    for key in ("candidate_commit", "candidate_tree", "validation_commit", "binding_sha256", "python_version"):
        require(value[key] == context[key])
    require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    require(value["kind"] == "public_windows_validation_summary")
    scope = value["workflow_scope"]
    require(scope in ("pinned_candidate_Q", "existing_ci_full_W"))
    for key in ("candidate_commit", "candidate_tree", "validation_commit", "execution_commit"):
        require(hex_value(value[key], 40))
    require(value["execution_commit"] == value["candidate_commit" if scope == "pinned_candidate_Q" else "validation_commit"])
    require(hex_value(value["binding_sha256"], 64) and value["python_version"] in ("3.11", "3.12"))
    require(value["status"] in ("passed", "blocked_or_failed", "incomplete"))
    states = {"verified", "failed", "not_completed"} if scope == "pinned_candidate_Q" else {"not_applicable"}
    require(value["source_before"] in states and value["source_after"] in states)
    rows = value["records"]
    require(type(rows) is list and 1 <= len(rows) <= len(LABELS))
    expected_labels = context["required_labels"] if scope == "pinned_candidate_Q" else ["full"]
    require(all(type(row) is dict for row in rows) and [row.get("label") for row in rows] == expected_labels)
    seen = set()
    for row in rows:
        exact_keys(row, RECORD_KEYS)
        require(type(row["label"]) is str and row["label"] in LABELS and row["label"] not in seen)
        seen.add(row["label"])
        require(exit_value(row["exit_code"]))
        require(type(row["acceptance_exit_code"]) is int and row["acceptance_exit_code"] in (0, 1))
        require(row["tests_run"] is None or count(row["tests_run"]))
        require(row["skip_count"] is None or count(row["skip_count"]))
        require(row["log_sha256"] is None or hex_value(row["log_sha256"], 64))
        require(type(row["state"]) is str and row["state"] in ("completed", "incomplete"))
        require(type(row["reason_code"]) is str and row["reason_code"] in REASONS)
        require(row["state"] == ("incomplete" if row["exit_code"] is None else "completed"))
        require((row["acceptance_exit_code"] == 0) == (row["reason_code"] == "none"))
        if row["acceptance_exit_code"] == 0:
            require(row["exit_code"] == 0 and row["state"] == "completed" and row["reason_code"] == "none" and row["log_sha256"] is not None)
            require(row["skip_count"] in (0, None))
            if row["label"] in TEST_LABELS:
                require(row["tests_run"] == context["expected_test_counts"].get(row["label"]) and row["skip_count"] == 0)
            else:
                require(row["tests_run"] is None and row["skip_count"] is None)
    require(scope != "existing_ci_full_W" or seen == {"full"})
    require((value["status"] == "incomplete") == any(row["exit_code"] is None for row in rows))
    if value["status"] == "passed":
        require(all(row["acceptance_exit_code"] == 0 for row in rows))
        require(scope != "pinned_candidate_Q" or value["source_before"] == value["source_after"] == "verified")
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def unavailable_json():
    return '{"kind":"public_summary_unavailable","status":"incomplete"}'
