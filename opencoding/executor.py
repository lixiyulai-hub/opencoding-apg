"""Strict, offline local executor for W2 structured actions."""

from __future__ import annotations

import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from .safety import (
    SCHEMA_VERSION,
    _is_reparse,
    _root_path,
    canonical_json,
    inspect_sensitive,
    safe_target,
    sanitize_text,
    sha256_bytes,
)

MAX_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_BYTES = 16 * 1024
MAX_TEXT_BYTES = 4 * 1024 * 1024
MAX_MODULE_LENGTH = 255
MAX_ARGUMENT_BYTES = 16 * 1024
PROCESS_EXIT_WAIT_SECONDS = 5.0
OUTPUT_DRAIN_SECONDS = 0.25
OUTPUT_STOP_SECONDS = 1.0
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DATA_SCOPES = {"synthetic", "synthetic-local", "synthetic-local-documents", "local"}
_CONTEXT_FIELDS = {
    "schema_version", "root", "action_digest", "targets", "external",
    "cost_limit", "data_scope", "irreversible",
}


def _now_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _explicit_root(root: str | os.PathLike[str] | Path) -> Path:
    path = Path(root)
    if not path.is_absolute():
        raise ValueError("root must be an explicit absolute path")
    return _root_path(path)


def _decode(data: bytes) -> str:
    return sanitize_text(data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"))


def _validate_action(action: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(action, Mapping):
        raise ValueError("action must be an object")
    action = dict(action)
    kind = action.get("type")
    if kind == "write_text":
        if set(action) != {"type", "path", "content"}:
            raise ValueError("write_text has unknown or missing fields")
        if not isinstance(action["path"], str) or not action["path"]:
            raise ValueError("write_text path must be a string")
        if not isinstance(action["content"], str):
            raise ValueError("write_text content must be a string")
        if len(action["content"].encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("write_text content is too large")
        if inspect_sensitive(action["content"])["sensitive"]:
            raise ValueError("write_text content contains sensitive material")
    elif kind == "python_module":
        if set(action) != {"type", "module", "args"}:
            raise ValueError("python_module has unknown or missing fields")
        module = action["module"]
        args = action["args"]
        if not isinstance(module, str) or len(module) > MAX_MODULE_LENGTH or not _MODULE.fullmatch(module) or module.startswith("__"):
            raise ValueError("python_module module is invalid")
        if not isinstance(args, list) or len(args) > 64 or any(not isinstance(arg, str) for arg in args):
            raise ValueError("python_module args must be a short string list")
        if any(len(arg) > 4096 or "\x00" in arg for arg in args):
            raise ValueError("python_module args are invalid")
        if sum(len(arg.encode("utf-8")) for arg in args) > MAX_ARGUMENT_BYTES:
            raise ValueError("python_module args are too large")
        if inspect_sensitive(" ".join(args))["sensitive"]:
            raise ValueError("python_module args contain sensitive material")
    else:
        raise ValueError("unknown action type")
    return action


def action_digest(action: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json(_validate_action(action)))


def _validate_context(context: Mapping[str, Any], root: Path, action: Mapping[str, Any]) -> None:
    if not isinstance(context, Mapping) or set(context) != _CONTEXT_FIELDS:
        raise ValueError("action context fields are invalid")
    if context["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported action context schema")
    if context["root"] != str(root):
        raise ValueError("action context root mismatch")
    if context["action_digest"] != action_digest(action):
        raise ValueError("action digest mismatch")
    targets = context["targets"]
    if not isinstance(targets, list) or any(not isinstance(item, str) for item in targets):
        raise ValueError("action context targets are invalid")
    if len(set(targets)) != len(targets):
        raise ValueError("action context targets are duplicated")
    if type(context["external"]) is not bool or context["external"]:
        raise ValueError("external actions are not supported")
    if context["cost_limit"] != 0 or type(context["irreversible"]) is not bool or context["irreversible"]:
        raise ValueError("action context is not local and reversible")
    if not isinstance(context["data_scope"], str) or context["data_scope"] not in _DATA_SCOPES:
        raise ValueError("action context data_scope is not allowed")
    for target in targets:
        safe_target(root, target)
    expected = [action["path"]] if action["type"] == "write_text" else []
    if targets != expected:
        raise ValueError("action context targets do not precisely match action")


def _atomic_write(root: Path, relative: str, content: bytes) -> tuple[Path, str]:
    target = safe_target(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target = safe_target(root, relative)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        safe_target(root, relative)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    verified = safe_target(root, relative, allow_missing=False)
    final_bytes = verified.read_bytes()
    if final_bytes != content:
        raise RuntimeError("write target changed during replacement")
    return verified, sha256_bytes(final_bytes)


def _module_environment() -> dict[str, str]:
    """Keep normal platform variables while excluding Python import injection."""

    environment = dict(os.environ)
    for name in tuple(environment):
        if name.upper().startswith("PYTHON"):
            environment.pop(name, None)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """End the process group/tree created for a local action.

    This is a best-effort application control, not an OS sandbox: a process with
    the same user permissions can still escape its group or alter the filesystem.
    """

    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill_succeeded = False
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
            taskkill_succeeded = completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
        if not taskkill_succeeded and process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            if process.poll() is None:
                process.kill()


def _wait_for_process_exit(process: subprocess.Popen[bytes]) -> int:
    """Wait for an owned process with a bounded direct-process fallback."""

    try:
        return process.wait(timeout=PROCESS_EXIT_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        try:
            return process.wait(timeout=PROCESS_EXIT_WAIT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("local process did not exit after termination") from error


def _windows_pipe_available(fd: int) -> int | None:
    """Inspect this collector's byte-mode pipe without starting a pending read."""

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.PeekNamedPipe.argtypes = (
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    available = wintypes.DWORD()
    handle = msvcrt.get_osfhandle(fd)
    if not kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
        error = ctypes.get_last_error()
        if error in (109, 232):  # Broken or closing pipe: the writer is gone.
            return None
        raise ctypes.WinError(error)
    return available.value


class _Collector:
    def __init__(self, stream) -> None:
        self.stream = stream
        self.fd = stream.fileno()
        self.data = bytearray()
        self._stop = threading.Event()
        self.error: OSError | ValueError | None = None
        self._stream_closed = False

    def _append(self, chunk: bytes) -> None:
        if len(self.data) < MAX_OUTPUT_BYTES:
            self.data.extend(chunk[: MAX_OUTPUT_BYTES - len(self.data)])

    def run(self) -> None:
        try:
            if os.name == "nt":
                while not self._stop.is_set():
                    available = _windows_pipe_available(self.fd)
                    if available is None:
                        return
                    if not available:
                        self._stop.wait(0.02)
                        continue
                    # This thread is the sole reader and the stream is closed
                    # only after it joins, so checked bytes cannot be consumed
                    # by another reader between the query and this raw read.
                    chunk = os.read(self.fd, min(4096, available))
                    if not chunk:
                        return
                    self._append(chunk)
            else:
                os.set_blocking(self.fd, False)
                while not self._stop.is_set():
                    readable, _unused_write, _unused_error = select.select([self.fd], [], [], 0.05)
                    if not readable:
                        continue
                    try:
                        chunk = os.read(self.fd, 4096)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        return
                    self._append(chunk)
        except (OSError, ValueError) as error:
            if not self._stop.is_set():
                self.error = error

    def stop(self) -> None:
        self._stop.set()

    def close_after_join(self) -> None:
        if self._stream_closed:
            return
        try:
            self.stream.close()
        except (OSError, ValueError):
            pass
        self._stream_closed = True


def _join_collectors(collectors: list[tuple[_Collector, threading.Thread]], deadline: float) -> None:
    for _collector, thread in collectors:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        thread.join(remaining)


def _finalize_collectors(collectors: list[tuple[_Collector, threading.Thread]]) -> None:
    _join_collectors(collectors, time.monotonic() + OUTPUT_DRAIN_SECONDS)
    active = [(collector, thread) for collector, thread in collectors if thread.is_alive()]
    if active:
        for collector, _thread in active:
            collector.stop()
        _join_collectors(active, time.monotonic() + OUTPUT_STOP_SECONDS)
    active = [(collector, thread) for collector, thread in collectors if thread.is_alive()]
    if active:
        raise RuntimeError("output collector did not stop after bounded shutdown")
    for collector, _thread in collectors:
        collector.close_after_join()
    for collector, _thread in collectors:
        if collector.error is not None:
            raise RuntimeError("output collector failed") from collector.error


class Executor:
    """Run validated local actions; this is not an operating-system sandbox."""
    def __init__(self, root: str | os.PathLike[str] | Path) -> None:
        self.root = _explicit_root(root)

    def execute(
        self,
        action: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        run_id: str | None = None,
        timeout_seconds: float = 30.0,
        cancel_event: threading.Event | None = None,
        input_payload: Any = None,
    ) -> dict[str, Any]:
        action = _validate_action(action)
        _validate_context(context, self.root, action)
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds is outside the local limit")
        run_id = run_id or f"run-{uuid.uuid4().hex}"
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("run_id is invalid")
        started = _now_ms()
        if cancel_event is None:
            cancel_event = threading.Event()
        if cancel_event.is_set():
            return self._result(run_id, "cancelled", None, False, True, started, "", "", [], "cancel_requested", action, input_payload)
        if action["type"] == "write_text":
            payload = action["content"].encode("utf-8")
            _target, artifact_hash = _atomic_write(self.root, action["path"], payload)
            artifact = {"path": action["path"].replace("\\", "/"), "sha256": artifact_hash, "sha256_kind": "file_bytes"}
            return self._result(run_id, "succeeded", 0, False, False, started, "", "", [artifact], None, action, input_payload)

        process = subprocess.Popen(
            [sys.executable, "-m", action["module"], *action["args"]],
            cwd=str(self.root),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=_module_environment(),
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        out = _Collector(process.stdout)
        err = _Collector(process.stderr)
        out_thread = threading.Thread(target=out.run, name="opencoding-output-stdout", daemon=True)
        err_thread = threading.Thread(target=err.run, name="opencoding-output-stderr", daemon=True)
        out_thread.start()
        err_thread.start()
        deadline = time.monotonic() + float(timeout_seconds)
        timed_out = False
        cancelled = False
        while process.poll() is None:
            if cancel_event.is_set():
                cancelled = True
                _terminate_process_tree(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_process_tree(process)
                break
            time.sleep(0.01)
        try:
            exit_code = _wait_for_process_exit(process)
        finally:
            _finalize_collectors([(out, out_thread), (err, err_thread)])
        status = "cancelled" if cancelled else "timed_out" if timed_out else "succeeded" if exit_code == 0 else "failed"
        reason = "cancel_requested" if cancelled else "timeout" if timed_out else None
        return self._result(run_id, status, exit_code, timed_out, cancelled, started, _decode(bytes(out.data)), _decode(bytes(err.data)), [], reason, action, input_payload)

    def _result(self, run_id, status, exit_code, timed_out, cancelled, started, stdout, stderr, artifacts, reason, action, input_payload):
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "status": status,
            "exit_code": exit_code,
            "timed_out": bool(timed_out),
            "cancelled": bool(cancelled),
            "duration_ms": max(0, _now_ms() - started),
            "input_sha256": sha256_bytes(canonical_json(input_payload)),
            "action_digest": action_digest(action),
            "stdout_summary": stdout,
            "stderr_summary": stderr,
            "artifacts": artifacts,
            "reason": reason,
            "live_verified": False,
        }


def execute_action(root, action, context, **kwargs) -> dict[str, Any]:
    return Executor(root).execute(action, context, **kwargs)


__all__ = ["Executor", "MAX_OUTPUT_BYTES", "MAX_TIMEOUT_SECONDS", "action_digest", "execute_action"]
