from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
import tomllib
from contextvars import ContextVar
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from .baseline import BaselineViolation, Finding, compare_findings, fingerprint, load_baseline
from .gate_execution_evidence import (
    GATE_EXECUTION_EVIDENCE_MAX_ENTRIES,
    GateExecutionEvidence,
    build_gate_execution_evidence,
    capture_digest,
    gate_contract_sha256,
)
from .model import CheckResult, CheckStatus

_MAX_OUTPUT = 4096
_CAPTURE_BYTES = _MAX_OUTPUT + 512
_DEFAULT_MAX_FILES = 1000
_DEFAULT_MAX_FILE_BYTES = 1_048_576
_DEFAULT_MAX_AGGREGATE_BYTES = 8_388_608
_DEFAULT_MAX_HIT_REFS = 1000
_PHASES = {"fast": ("fast",), "full": ("fast", "full"), "release": ("fast", "full", "release")}
_SECRET_PATTERNS = (
    re.compile(r"ghp_[A-Za-z0-9_-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(?:api[_-]?key|secret|token|password|credential|authorization)[A-Za-z0-9_.-]*\s*[=:]\s*[\"']?[^\s\"']+"),
    re.compile(r"(?i)bearer\s+\S+"),
    re.compile(r"(?i)https?://[^\s/@:]+:[^\s/@]+@[^\s]+"),
)
_SUPPORTED_KINDS = {"command", "scope", "schema", "adapter", "baseline", "secret", "evidence", "forbidden", "metric"}
_SUPPORTED_COMPARATORS = {"<", "<=", ">", ">=", "==", "!=", "lt", "le", "gt", "ge", "eq", "ne"}
_GATE_FIELDS = frozenset({
    "gate_id",
    "phase",
    "command",
    "timeout_seconds",
    "required",
    "warning_exit_codes",
    "kind",
    "options",
})
_BOUNDED_OPTION_KEYS = frozenset({
    "max_files",
    "max_file_bytes",
    "max_aggregate_bytes",
    "max_hit_refs",
})
_OPTION_KEYS_BY_KIND = {
    "command": frozenset({"cwd", "env", "output_dir"}),
    "scope": frozenset({"allowed_paths"}) | _BOUNDED_OPTION_KEYS,
    "schema": frozenset({"required_files", "required_keys"}) | _BOUNDED_OPTION_KEYS,
    "adapter": frozenset({"expected", "actual"}),
    "baseline": frozenset({"baseline_path", "findings"}) | _BOUNDED_OPTION_KEYS,
    "secret": frozenset({"paths"}) | _BOUNDED_OPTION_KEYS,
    "evidence": frozenset({"required_files"}),
    "forbidden": frozenset({"paths", "patterns"}) | _BOUNDED_OPTION_KEYS,
    "metric": frozenset({"environment", "comparator", "threshold", "tolerance"}),
}
_RUNTIME_CONTROL_ENV = frozenset({
    "APG_GATE_OUTPUT_DIR",
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "PYTHONHOME",
    "PYTHONPATH",
    "VIRTUAL_ENV",
})
_MINIMAL_ENV_KEYS = (
    "SystemRoot",
    "WINDIR",
    "PATH",
    "TEMP",
    "TMP",
    "PATHEXT",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
)


def _freeze(value):
    if isinstance(value, Finding):
        return value
    if type(value) in (type(None), bool, int, float, str):
        return value
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise TypeError("option keys must be strings")
        return MappingProxyType({key: _freeze(v) for key, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        raise TypeError("unordered gate options are not supported")
    raise TypeError("gate options must contain only immutable scalar, mapping, sequence, or Finding values")


def _finite_number(value, *, nonnegative=False):
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("metric numbers must be finite and non-bool") from error
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(numeric):
        raise ValueError("metric numbers must be finite and non-bool")
    if nonnegative and value < 0:
        raise ValueError("metric number must be non-negative")
    return float(value)


@dataclass(frozen=True)
class GateDefinition:
    gate_id: str
    phase: str
    command: tuple[str, ...] = ()
    timeout_seconds: int = 60
    required: bool = True
    warning_exit_codes: tuple[int, ...] = ()
    kind: str = "command"
    options: dict[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if type(self.gate_id) is not str or not self.gate_id.strip(): raise ValueError("gate_id is required")
        if self.phase not in _PHASES: raise ValueError("invalid phase")
        if type(self.command) not in (tuple, list) or any(type(x) is not str or not x for x in self.command): raise TypeError("command must be a string sequence")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, int) or self.timeout_seconds <= 0: raise ValueError("timeout_seconds must be positive")
        if type(self.required) is not bool: raise TypeError("required must be bool")
        if type(self.warning_exit_codes) not in (tuple, list) or any(isinstance(x, bool) or not isinstance(x, int) for x in self.warning_exit_codes): raise TypeError("warning_exit_codes must be integers")
        if type(self.kind) is not str or self.kind not in _SUPPORTED_KINDS: raise ValueError("unsupported gate kind")
        if not isinstance(self.options, dict): raise TypeError("options must be a mapping")
        object.__setattr__(self, "command", tuple(self.command))
        object.__setattr__(self, "warning_exit_codes", tuple(self.warning_exit_codes))
        object.__setattr__(self, "options", _freeze(self.options))


def _thaw_gate_value(value):
    if isinstance(value, Mapping):
        return {key: _thaw_gate_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_thaw_gate_value(item) for item in value]
    return value


def _string_sequence(value, label, *, required=False):
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{label} must be a sequence of strings")
    items = tuple(value)
    if required and not items:
        raise ValueError(f"{label} must not be empty")
    if any(type(item) is not str or not item for item in items):
        raise TypeError(f"{label} must contain non-empty strings")
    return items


def _relative_option_path(value, label):
    if type(value) is not str or not value or "\x00" in value:
        raise TypeError(f"{label} must be a non-empty project-relative string")
    candidate = Path(value)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ValueError(f"{label} must remain project-relative")


def _validate_path_sequence(options, key, *, required=False):
    if key not in options:
        if required:
            raise ValueError(f"gate options.{key} is required")
        return
    for index, value in enumerate(_string_sequence(options[key], f"gate options.{key}", required=required)):
        _relative_option_path(value, f"gate options.{key}[{index}]")


def _validate_command_options(options):
    if "output_dir" in options:
        value = options["output_dir"]
        if not isinstance(value, str) or not re.fullmatch(r"\.cache/gate-runs/[a-z][a-z0-9-]{0,63}", value):
            raise ValueError("output_dir must be .cache/gate-runs/<bounded-name>")
    if "cwd" in options:
        _relative_option_path(options["cwd"], "gate options.cwd")
    if "env" not in options:
        return
    configured_env = options["env"]
    if not isinstance(configured_env, Mapping):
        raise TypeError("gate options.env must be a mapping")
    for key, value in configured_env.items():
        if type(key) is not str or type(value) is not str or not key or "=" in key or "\x00" in key or "\x00" in value:
            raise ValueError("gate options.env must contain valid string entries")
        if key.upper() in _RUNTIME_CONTROL_ENV:
            raise ValueError("gate options.env cannot override runtime control variables")


def _validate_gate_options(kind, options):
    unknown = set(options) - _OPTION_KEYS_BY_KIND[kind]
    if unknown:
        raise ValueError(f"gate options contain unsupported fields for {kind}: {', '.join(sorted(unknown))}")
    for key in _BOUNDED_OPTION_KEYS & set(options):
        value = options[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"gate options.{key} must be a non-negative integer")
    if kind == "command":
        _validate_command_options(options)
    elif kind == "scope":
        _validate_path_sequence(options, "allowed_paths", required=True)
    elif kind == "schema":
        _validate_path_sequence(options, "required_files")
        if "required_keys" in options:
            _string_sequence(options["required_keys"], "gate options.required_keys")
    elif kind == "adapter":
        for key in ("expected", "actual"):
            if type(options.get(key)) is not str or not options[key]:
                raise ValueError(f"gate options.{key} is required")
    elif kind == "baseline":
        if "baseline_path" in options:
            _relative_option_path(options["baseline_path"], "gate options.baseline_path")
        if "findings" in options:
            findings = options["findings"]
            if isinstance(findings, (str, bytes)) or not isinstance(findings, Sequence):
                raise TypeError("gate options.findings must be a sequence")
            if any(not isinstance(item, Finding) for item in findings):
                raise TypeError("gate options.findings must contain canonical findings")
    elif kind == "secret":
        _validate_path_sequence(options, "paths")
    elif kind == "evidence":
        _validate_path_sequence(options, "required_files", required=True)
    elif kind == "forbidden":
        _validate_path_sequence(options, "paths")
        patterns = _string_sequence(options.get("patterns", ()), "gate options.patterns", required=True)
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError("gate options.patterns contains an invalid regular expression") from error
    elif kind == "metric":
        if type(options.get("environment")) is not str or not options["environment"].strip():
            raise ValueError("gate options.environment is required")
        if options.get("comparator") not in _SUPPORTED_COMPARATORS:
            raise ValueError("gate options.comparator is unsupported")
        _finite_number(options.get("threshold"))
        _finite_number(options.get("tolerance", 0), nonnegative=True)


def parse_gate_definitions(gates: object) -> tuple[GateDefinition, ...]:
    """Parse policy gate mappings through one fail-closed canonical contract."""
    if isinstance(gates, (str, bytes)) or not isinstance(gates, Sequence):
        raise TypeError("gates must be a sequence of mappings")
    definitions: list[GateDefinition] = []
    seen: set[str] = set()
    for index, raw in enumerate(gates):
        label = f"gates[{index}]"
        if not isinstance(raw, Mapping):
            raise TypeError(f"{label} must be a mapping")
        if any(type(key) is not str for key in raw):
            raise TypeError(f"{label} keys must be strings")
        if "id" in raw:
            raise ValueError(f"{label}.id is not supported; use gate_id")
        unknown = set(raw) - _GATE_FIELDS
        if unknown:
            raise ValueError(f"{label} has unsupported fields: {', '.join(sorted(unknown))}")
        missing = {"gate_id", "phase"} - set(raw)
        if missing:
            raise ValueError(f"{label} is missing fields: {', '.join(sorted(missing))}")
        item = _thaw_gate_value(raw)
        gate_id = item["gate_id"]
        if type(gate_id) is not str or not gate_id.strip():
            raise ValueError(f"{label}.gate_id is required")
        if gate_id in seen:
            raise ValueError(f"duplicate gate_id: {gate_id}")
        kind = item.get("kind", "command")
        options = item.get("options", {})
        if not isinstance(options, dict):
            raise TypeError(f"{label}.options must be a mapping")
        if kind == "baseline" and "findings" in options:
            findings = options["findings"]
            if isinstance(findings, (str, bytes)) or not isinstance(findings, Sequence):
                raise TypeError(f"{label}.options.findings must be a sequence")
            options["findings"] = [
                Finding(**finding) if isinstance(finding, dict) else finding
                for finding in findings
            ]
        definition = GateDefinition(
            gate_id=gate_id,
            phase=item["phase"],
            command=item.get("command", ()),
            timeout_seconds=item.get("timeout_seconds", 60),
            required=item.get("required", True),
            warning_exit_codes=item.get("warning_exit_codes", ()),
            kind=kind,
            options=options,
        )
        if definition.kind in {"command", "metric"} and not definition.command:
            raise ValueError(f"{label} {definition.kind} command must not be empty")
        if definition.kind not in {"command", "metric"} and definition.command:
            raise ValueError(f"{label} {definition.kind} gate must not define command argv")
        _validate_gate_options(definition.kind, definition.options)
        seen.add(gate_id)
        definitions.append(definition)
    return tuple(definitions)


def _redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS: text = pattern.sub("[REDACTED]", text)
    return text


def _result(gate, status, message, evidence=(), duration_ms=0):
    safe = _redact(str(message))
    refs = tuple(_redact(str(x)) for x in evidence)
    return CheckResult(gate.gate_id, gate.phase, status, safe[:_MAX_OUTPUT], refs[:_DEFAULT_MAX_HIT_REFS], max(0, int(duration_ms)))


def _minimal_env():
    return {key: os.environ[key] for key in _MINIMAL_ENV_KEYS if key in os.environ}


_command_output_dir = ContextVar("apg_command_output_dir", default=None)


def _command_context(gate, root):
    root_path = Path(root).resolve(strict=True)
    raw_cwd = gate.options.get("cwd", ".")
    if type(raw_cwd) is not str or not raw_cwd:
        raise ValueError("command cwd must be a project-relative string")
    candidate = Path(raw_cwd)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ValueError("command cwd must remain project-relative")
    cwd = (root_path / candidate).resolve(strict=False)
    if not cwd.is_relative_to(root_path) or not cwd.is_dir():
        raise ValueError("command cwd is outside the project or missing")
    configured_env = gate.options.get("env", {})
    if not isinstance(configured_env, Mapping):
        raise ValueError("command env must be a mapping")
    env = _minimal_env()
    for key, value in configured_env.items():
        if type(key) is not str or type(value) is not str or not key or "=" in key or "\x00" in key or "\x00" in value:
            raise ValueError("command env must contain valid string entries")
        if key.upper() in _RUNTIME_CONTROL_ENV:
            raise ValueError("command env cannot override runtime control variables")
        env[key] = value
    output_dir = _command_output_dir.get()
    if output_dir is not None:
        env["APG_GATE_OUTPUT_DIR"] = output_dir
    return cwd, env


@dataclass(frozen=True)
class _BoundedProcessResult:
    return_code: int
    stdout: bytes
    stdout_observed_bytes: int
    stderr: bytes
    stderr_observed_bytes: int
    timed_out: bool


@dataclass(frozen=True)
class _ProcessExecution:
    reason_code: str
    process_exit_code: int | None
    stdout: bytes = b""
    stdout_observed_bytes: int = 0
    stderr: bytes = b""
    stderr_observed_bytes: int = 0
    duration_ms: int = 0
    error_name: str | None = None


def _elapsed_ms(started):
    return max(0, int((time.monotonic() - started) * 1000))


def _start_bounded_command(gate, cwd, env):
    return subprocess.Popen(
        list(gate.command),
        cwd=cwd,
        env=env,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _collect_bounded_command(process, timeout_seconds):
    buffers = (bytearray(), bytearray())
    observed = [0, 0]

    def drain(stream, buffer, index):
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                observed[index] += len(chunk)
                remaining = _CAPTURE_BYTES - len(buffer)
                if remaining > 0:
                    buffer.extend(chunk[:remaining])
        finally:
            stream.close()

    threads = tuple(
        threading.Thread(target=drain, args=(stream, buffer, index), daemon=True)
        for index, (stream, buffer) in enumerate(zip((process.stdout, process.stderr), buffers))
    )
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        return_code = process.wait()
    for thread in threads:
        thread.join()
    return _BoundedProcessResult(
        return_code=return_code,
        stdout=bytes(buffers[0]),
        stdout_observed_bytes=observed[0],
        stderr=bytes(buffers[1]),
        stderr_observed_bytes=observed[1],
        timed_out=timed_out,
    )


def _execute_process(gate, root, started):
    try:
        cwd, env = _command_context(gate, root)
    except (OSError, TypeError, ValueError) as error:
        return _ProcessExecution(
            reason_code="command_context_invalid",
            process_exit_code=None,
            duration_ms=_elapsed_ms(started),
            error_name=type(error).__name__,
        )
    try:
        process = _start_bounded_command(gate, cwd, env)
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        return _ProcessExecution(
            reason_code="process_spawn_failed",
            process_exit_code=None,
            duration_ms=_elapsed_ms(started),
            error_name=type(error).__name__,
        )
    outcome = _collect_bounded_command(process, gate.timeout_seconds)
    return _ProcessExecution(
        reason_code="process_timed_out" if outcome.timed_out else "process_exited",
        process_exit_code=outcome.return_code,
        stdout=outcome.stdout,
        stdout_observed_bytes=outcome.stdout_observed_bytes,
        stderr=outcome.stderr,
        stderr_observed_bytes=outcome.stderr_observed_bytes,
        duration_ms=_elapsed_ms(started),
        error_name="TimeoutExpired" if outcome.timed_out else None,
    )


def _process_output(execution):
    return _redact((execution.stdout + execution.stderr).decode("utf-8", errors="replace"))[:_MAX_OUTPUT]


def _process_evidence(check_index, gate, result, execution):
    kwargs = {}
    if execution.reason_code in {"process_exited", "process_timed_out"}:
        stdout_capture = _redact(execution.stdout.decode("utf-8", errors="replace"))
        stderr_capture = _redact(execution.stderr.decode("utf-8", errors="replace"))
        kwargs = {
            "stdout_capture_sha256": capture_digest(stdout_capture, stream="stdout"),
            "stdout_captured_bytes": len(execution.stdout),
            "stdout_observed_bytes": execution.stdout_observed_bytes,
            "stdout_truncated": len(execution.stdout) < execution.stdout_observed_bytes,
            "stderr_capture_sha256": capture_digest(stderr_capture, stream="stderr"),
            "stderr_captured_bytes": len(execution.stderr),
            "stderr_observed_bytes": execution.stderr_observed_bytes,
            "stderr_truncated": len(execution.stderr) < execution.stderr_observed_bytes,
        }
    return build_gate_execution_evidence(
        check_index=check_index,
        gate=gate,
        status=result.status.value,
        reason_code=execution.reason_code,
        process_exit_code=execution.process_exit_code,
        duration_ms=execution.duration_ms,
        **kwargs,
    )


def _nonprocess_evidence(check_index, gate, result, reason_code, duration_ms):
    return build_gate_execution_evidence(
        check_index=check_index,
        gate=gate,
        status=result.status.value,
        reason_code=reason_code,
        duration_ms=duration_ms,
    )


def _execute_command_gate(gate, root, check_index):
    started = time.monotonic()
    if not gate.command:
        duration_ms = _elapsed_ms(started)
        result = _result(gate, CheckStatus.INCONCLUSIVE, "command is missing", duration_ms=duration_ms)
        return result, _nonprocess_evidence(check_index, gate, result, "command_missing", duration_ms)
    execution = _execute_process(gate, root, started)
    if execution.reason_code != "process_exited":
        result = _result(
            gate,
            CheckStatus.INCONCLUSIVE,
            f"gate execution inconclusive: {execution.error_name}",
            duration_ms=execution.duration_ms,
        )
        return result, _process_evidence(check_index, gate, result, execution)
    output = _process_output(execution)
    status = (
        CheckStatus.PASS
        if execution.process_exit_code == 0
        else CheckStatus.WARN
        if execution.process_exit_code in gate.warning_exit_codes
        else CheckStatus.FAIL
    )
    result = _result(
        gate,
        status,
        f"exit {execution.process_exit_code}" + (f": {output}" if output else ""),
        duration_ms=execution.duration_ms,
    )
    return result, _process_evidence(check_index, gate, result, execution)


def run_gate(gate, root, *, selected=True):
    if not selected: return _result(gate, CheckStatus.NOT_APPLICABLE, "gate not selected")
    try:
        gate_contract_sha256(gate)
    except (TypeError, ValueError) as error:
        return _result(
            gate,
            CheckStatus.INCONCLUSIVE,
            f"gate execution inconclusive: {type(error).__name__}",
        )
    return _execute_gate(gate, root, 0)[0]


def _root(root): return Path(root).resolve(strict=True)


def _bounded_options(gate):
    o = gate.options
    values = []
    for key, default in (("max_files", _DEFAULT_MAX_FILES), ("max_file_bytes", _DEFAULT_MAX_FILE_BYTES), ("max_aggregate_bytes", _DEFAULT_MAX_AGGREGATE_BYTES), ("max_hit_refs", _DEFAULT_MAX_HIT_REFS)):
        value = o.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0: raise ValueError(f"{key} must be a non-negative integer")
        values.append(value)
    return values


def _safe_target(root, value):
    raw = str(value).replace("\\", "/")
    candidate = Path(raw)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts): raise ValueError("configured path escapes project root")
    path = (root / candidate).resolve(strict=False)
    if not path.is_relative_to(root): raise ValueError("configured path escapes project root")
    if os.path.lexists(path) and path.resolve(strict=False) != path: raise ValueError("symlink or reparse path is not allowed")
    return path


def _read_text_bounded(path, gate):
    max_file_bytes = _bounded_options(gate)[1]
    if path.stat().st_size > max_file_bytes:
        raise OverflowError("file byte limit exhausted")
    payload = path.read_bytes()
    if len(payload) > max_file_bytes:
        raise OverflowError("file byte limit exhausted")
    return payload.decode("utf-8")


def _iter_files(root, paths, gate):
    max_files, max_file_bytes, max_aggregate, max_hits = _bounded_options(gate)
    selected = []
    for item in (paths or ("",)):
        target = _safe_target(root, item)
        if target.is_file(): selected.append(target)
        elif target.is_dir(): selected.extend(p for p in sorted(target.rglob("*")) if p.is_file())
        elif not target.exists(): raise FileNotFoundError(str(item))
    if len(selected) > max_files: raise OverflowError("maximum file count exhausted")
    total = 0
    for path in selected:
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root): raise ValueError("file path escapes project root")
        size = path.stat().st_size
        if size > max_file_bytes or total + size > max_aggregate: raise OverflowError("byte limit exhausted")
        total += size
        yield path


def _rel(path, root): return path.relative_to(root).as_posix()


def _builtin_scope(gate, root):
    try:
        allowed = tuple(str(x).replace("\\", "/").rstrip("/") for x in gate.options.get("allowed_paths", ()))
        files = list(_iter_files(root, (), gate))
        violations = [ _rel(p, root) for p in files if allowed and not any(_rel(p,root) == a or _rel(p,root).startswith(a+"/") for a in allowed)]
        return _result(gate, CheckStatus.FAIL if violations else CheckStatus.PASS, "scope drift" if violations else "scope is clean", violations)
    except (ValueError, OSError, OverflowError) as error: return _result(gate, CheckStatus.INCONCLUSIVE, f"scope evaluation inconclusive: {type(error).__name__}")


def _builtin_schema(gate, root):
    try:
        refs=[]
        for name in gate.options.get("required_files", (".governance/policy.toml",)):
            path=_safe_target(root,name)
            if not path.is_file(): return _result(gate, CheckStatus.INCONCLUSIVE, "required schema file missing", (name,))
            data=tomllib.loads(_read_text_bounded(path,gate))
            refs.extend(f"{name}:{key}" for key in gate.options.get("required_keys", ()) if key not in data)
        return _result(gate, CheckStatus.FAIL if refs else CheckStatus.PASS, "required schema keys missing" if refs else "schema is valid", refs)
    except (OSError, ValueError, OverflowError, tomllib.TOMLDecodeError): return _result(gate, CheckStatus.INCONCLUSIVE, "schema evaluation inconclusive")


def _content_hits(gate, root, patterns):
    hits=[]
    max_files, max_file_bytes, max_aggregate, max_hits = _bounded_options(gate)
    consumed = 0
    for path in _iter_files(root, gate.options.get("paths"), gate):
        payload = path.read_bytes()
        actual_size = len(payload)
        if actual_size > max_file_bytes or consumed + actual_size > max_aggregate:
            raise OverflowError("byte limit exhausted")
        consumed += actual_size
        text = payload.decode("utf-8", errors="replace")
        if any(p.search(text) for p in patterns):
            hits.append(_rel(path,root))
        if len(hits) > max_hits:
            raise OverflowError("hit reference limit exhausted")
    return hits


def _builtin_secret(gate, root):
    try:
        hits=_content_hits(gate,root,_SECRET_PATTERNS)
        return _result(gate, CheckStatus.FAIL if hits else CheckStatus.PASS, "secret pattern detected" if hits else "no secret patterns detected", hits)
    except (ValueError,OSError,OverflowError,FileNotFoundError): return _result(gate, CheckStatus.INCONCLUSIVE,"secret evaluation inconclusive")


def _builtin_forbidden(gate,root):
    try:
        hits=_content_hits(gate,root,tuple(re.compile(str(p)) for p in gate.options.get("patterns",())))
        return _result(gate, CheckStatus.FAIL if hits else CheckStatus.PASS,"forbidden pattern detected" if hits else "no forbidden patterns detected",hits)
    except (ValueError,OSError,OverflowError,FileNotFoundError,re.error): return _result(gate,CheckStatus.INCONCLUSIVE,"forbidden evaluation inconclusive")


def _metric_parameters(gate):
    if not gate.command: raise ValueError("metric command is missing")
    if not isinstance(gate.options.get("environment"),str) or not gate.options["environment"].strip(): raise ValueError("metric environment is required")
    comparator=gate.options.get("comparator")
    if comparator not in _SUPPORTED_COMPARATORS: raise ValueError("unsupported comparator")
    threshold=_finite_number(gate.options.get("threshold")); tolerance=_finite_number(gate.options.get("tolerance",0),nonnegative=True)
    return comparator, threshold, tolerance


def _metric_from_command_result(result, parameters):
    comparator, threshold, tolerance = parameters
    if result.status is not CheckStatus.PASS: raise ValueError("metric command unavailable")
    raw=result.message.split(": ",1)[1] if ": " in result.message else ""
    data=json.loads(raw)
    if not isinstance(data,dict) or set(data) != {"value","unit"} or isinstance(data["value"],bool) or not isinstance(data["unit"],str) or not data["unit"].strip(): raise ValueError("malformed metric object")
    value=_finite_number(data["value"])
    limit=threshold+tolerance if comparator in {"<","<=","lt","le"} else threshold-tolerance
    passed={"<":value<limit,"<=":value<=limit,"lt":value<limit,"le":value<=limit,">":value>limit,">=":value>=limit,"gt":value>limit,"ge":value>=limit,"==":abs(value-threshold)<=tolerance,"eq":abs(value-threshold)<=tolerance,"!=":abs(value-threshold)>tolerance,"ne":abs(value-threshold)>tolerance}[comparator]
    return CheckStatus.PASS if passed else CheckStatus.FAIL, f"metric {value:g} {data['unit']}"


def _execute_metric_gate(gate, root, check_index):
    started = time.monotonic()
    if not gate.command:
        duration_ms = _elapsed_ms(started)
        result = _result(gate, CheckStatus.INCONCLUSIVE, "metric output is malformed or unavailable", duration_ms=duration_ms)
        return result, _nonprocess_evidence(check_index, gate, result, "command_missing", duration_ms)
    try:
        parameters = _metric_parameters(gate)
    except (KeyError, TypeError, ValueError):
        duration_ms = _elapsed_ms(started)
        result = _result(gate, CheckStatus.INCONCLUSIVE, "metric output is malformed or unavailable", duration_ms=duration_ms)
        return result, _nonprocess_evidence(check_index, gate, result, "builtin_evaluated", duration_ms)
    execution = _execute_process(gate, root, started)
    if execution.reason_code != "process_exited":
        result = _result(
            gate,
            CheckStatus.INCONCLUSIVE,
            "metric output is malformed or unavailable",
            duration_ms=execution.duration_ms,
        )
        return result, _process_evidence(check_index, gate, result, execution)
    output = _process_output(execution)
    command_status = (
        CheckStatus.PASS
        if execution.process_exit_code == 0
        else CheckStatus.WARN
        if execution.process_exit_code in gate.warning_exit_codes
        else CheckStatus.FAIL
    )
    command_result = _result(
        gate,
        command_status,
        f"exit {execution.process_exit_code}" + (f": {output}" if output else ""),
        duration_ms=execution.duration_ms,
    )
    try:
        status, message = _metric_from_command_result(command_result, parameters)
        result = _result(gate, status, message, duration_ms=execution.duration_ms)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        result = _result(
            gate,
            CheckStatus.INCONCLUSIVE,
            "metric output is malformed or unavailable",
            duration_ms=execution.duration_ms,
        )
    return result, _process_evidence(check_index, gate, result, execution)


def _builtin_adapter(gate):
    expected,actual=gate.options.get("expected"),gate.options.get("actual")
    return _result(gate,CheckStatus.PASS if expected==actual else CheckStatus.FAIL,"adapter is synchronized" if expected==actual else "adapter drift detected")


def _builtin_evidence(gate,root):
    try:
        missing=[str(x) for x in gate.options.get("required_files",()) if not _safe_target(root,x).is_file()]
        return _result(gate,CheckStatus.INCONCLUSIVE if missing else CheckStatus.PASS,"required evidence is unavailable" if missing else "required evidence is present",missing)
    except (ValueError,OSError): return _result(gate,CheckStatus.INCONCLUSIVE,"evidence evaluation inconclusive")


def _builtin_baseline(gate,root):
    try:
        baseline_path=_safe_target(root,gate.options.get("baseline_path",".governance/baseline.json"))
        findings=tuple(gate.options.get("findings",()))
        if not baseline_path.is_file(): return _result(gate,CheckStatus.INCONCLUSIVE,"baseline evidence is missing")
        for item in findings:
            if not isinstance(item, Finding):
                return _result(gate,CheckStatus.INCONCLUSIVE,"baseline findings are malformed")
            if not item.baselinable:
                return _result(gate,CheckStatus.FAIL,"non-baselinable finding detected",(item.path,))
            try:
                fingerprint(item, project_root=str(root))
            except BaselineViolation:
                return _result(gate,CheckStatus.FAIL,"non-baselinable finding detected",(item.path,))
        comparison=compare_findings((),findings,baseline=load_baseline(_read_text_bounded(baseline_path,gate)),project_root=str(root))
        bad=tuple(x.path if isinstance(x,Finding) else str(x) for x in comparison.new+comparison.worsened)
        return _result(gate,CheckStatus.FAIL if bad else CheckStatus.PASS,"baseline ratchet failed" if bad else "baseline ratchet passed",bad)
    except (OSError,ValueError,OverflowError,BaselineViolation,TypeError): return _result(gate,CheckStatus.INCONCLUSIVE,"baseline evidence is malformed or unavailable")


def _run_builtin_gate_result(gate,root):
    root_path=_root(root)
    try:
        if gate.kind=="scope": return _builtin_scope(gate,root_path)
        if gate.kind=="schema": return _builtin_schema(gate,root_path)
        if gate.kind=="adapter": return _builtin_adapter(gate)
        if gate.kind=="secret": return _builtin_secret(gate,root_path)
        if gate.kind=="evidence": return _builtin_evidence(gate,root_path)
        if gate.kind=="forbidden": return _builtin_forbidden(gate,root_path)
        if gate.kind=="baseline": return _builtin_baseline(gate,root_path)
    except (ValueError,TypeError,OSError): pass
    return _result(gate,CheckStatus.INCONCLUSIVE,"gate definition is unknown or malformed")


def _with_duration(result, duration_ms):
    return CheckResult(
        result.gate_id,
        result.phase,
        result.status,
        result.message,
        result.evidence_refs,
        duration_ms,
    )


def _execute_gate(gate, root, check_index):
    if gate.kind == "command":
        return _execute_command_gate(gate, root, check_index)
    if gate.kind == "metric":
        return _execute_metric_gate(gate, root, check_index)
    started = time.monotonic()
    result = _run_builtin_gate_result(gate, root)
    duration_ms = _elapsed_ms(started)
    result = _with_duration(result, duration_ms)
    return result, _nonprocess_evidence(check_index, gate, result, "builtin_evaluated", duration_ms)


def run_builtin_gate(gate,root):
    try:
        gate_contract_sha256(gate)
    except (TypeError, ValueError) as error:
        return _result(
            gate,
            CheckStatus.INCONCLUSIVE,
            f"gate execution inconclusive: {type(error).__name__}",
        )
    return _execute_gate(gate, root, 0)[0]


@dataclass(frozen=True)
class GateRun:
    checks: tuple[CheckResult,...]
    exit_code: int
    evidence: tuple[GateExecutionEvidence,...] = ()


def orchestrate_gates(gates: Iterable[GateDefinition], root, *, phase="fast"):
    if phase not in _PHASES: raise ValueError("invalid phase")
    ordered=sorted(tuple(gates),key=lambda g:(_PHASES["release"].index(g.phase),g.gate_id))
    if len({gate.gate_id for gate in ordered}) != len(ordered):
        raise ValueError("gate IDs must be unique")
    selected=[g for g in ordered if g.phase in _PHASES[phase]]
    if len(selected) > GATE_EXECUTION_EVIDENCE_MAX_ENTRIES:
        raise ValueError("selected Gate count exceeds the execution evidence limit")
    for gate in selected:
        gate_contract_sha256(gate)
    executions=tuple(_execute_gate(g,root,index) for index,g in enumerate(selected))
    checks=tuple(result for result,_ in executions)
    evidence=tuple(item for _,item in executions)
    required_by_id={g.gate_id:g.required for g in selected}
    exit_code=1 if any(c.status is CheckStatus.FAIL and required_by_id.get(c.gate_id,True) for c in checks) else 3 if any(c.status is CheckStatus.INCONCLUSIVE and required_by_id.get(c.gate_id,True) for c in checks) else 0
    return GateRun(checks,exit_code,evidence)
