"""Capture exactly the existing CI's default unittest command; no arbitrary CLI command."""
from __future__ import annotations
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys

from public_output import (Rejected, build_existing, load_json, parse_unittest, public_json,
                           record, require, unavailable_json, validate_context)


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise Rejected("arguments_rejected")


def _capture_command(argv, log_path, *, cwd=None, completed=None):
    """Private test seam; CLI always supplies the one fixed unittest argv."""
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        process = subprocess.Popen(argv, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT)
        try:
            code = process.wait()
            if completed is not None:
                completed["exit_code"] = code
        except KeyboardInterrupt:
            # Best effort for this direct child; runner cancellation can still preempt us.
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            raise
        handle.flush()
    raw = Path(log_path).read_bytes()
    return code, raw, hashlib.sha256(raw).hexdigest()


def capture_default(context, log_directory, *, completed=None):
    validate_context(context)
    directory = Path(log_directory)
    directory.mkdir(parents=True, exist_ok=False)
    code, raw, digest = _capture_command([sys.executable, "-X", "utf8", "-m", "unittest"], directory / "full.log", completed=completed)
    if completed is not None:
        completed["exit_code"] = code
    number, skips, reason = parse_unittest(raw)
    row = record("full", code, number, skips, digest, context["expected_test_counts"]["full"], parse_reason=reason)
    return build_existing(context, row)


def main(argv=None):
    completed = {"exit_code": None}
    def failed(fallback):
        # Publishing failure must not overwrite an already completed child's failure.
        try:
            print(unavailable_json())
        except Exception:
            pass
        actual = completed["exit_code"]
        return actual if actual is not None and actual != 0 else fallback
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--context", required=True)
        parser.add_argument("--context-sha256", required=True)
        parser.add_argument("--log-directory", required=True)
        args = parser.parse_args(argv)
        context = validate_context(load_json(args.context, args.context_sha256))
        # Validate immutable checkout identity without printing command output.
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.PIPE).decode().strip()
        require(head == context["validation_commit"] == os.environ.get("GITHUB_SHA"))
        require(os.environ.get("GITHUB_EVENT_NAME") == "push")
        require(os.environ.get("GITHUB_REF") == "refs/heads/validation/windows-neutral-20261006")
        require(tuple(map(int, context["python_version"].split("."))) == sys.version_info[:2])
        directory = Path(args.log_directory).resolve()
        require(not directory.is_relative_to(Path.cwd().resolve()))
        runner_temp = os.environ.get("RUNNER_TEMP")
        require(type(runner_temp) is str and bool(runner_temp))
        require(directory.is_relative_to(Path(runner_temp).resolve()) and directory != Path(runner_temp).resolve())
        result = capture_default(context, directory, completed=completed)
        print(public_json(result, context))
        actual = completed["exit_code"]
        if actual != 0:
            return actual
        return 0 if result["status"] == "passed" else 1
    except KeyboardInterrupt:
        return failed(130)
    except Exception:
        return failed(1)


if __name__ == "__main__":
    raise SystemExit(main())
