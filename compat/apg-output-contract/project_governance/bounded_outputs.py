"""Candidate phase-check output contract, opt-in through a bound policy.

Every command gets its own newly allocated directory. No directory is ignored.
This detects persistent changes, not a sandbox against hostile same-user code.
"""
import hashlib
import os
from pathlib import Path
import stat
import unicodedata
import uuid

from .gate_execution_evidence import gate_execution_evidence_document
from .gates import GateRun, _command_output_dir, _execute_gate, _validate_gate_options
from .model import CheckStatus
from .path_guard import WorkspaceGuard, WorkspaceTransaction
from .storage import canonical_json_bytes, digest


def _regular(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("link or reparse point: " + str(path))
    if not stat.S_ISDIR(info.st_mode) and (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1):
        raise ValueError("nonregular file or hardlink: " + str(path))
    return info


def _snapshot(root):
    result = {}
    total = 0
    def visit(directory):
        nonlocal total
        info = _regular(directory)
        result[directory.relative_to(root).as_posix()] = ["directory", info.st_dev, info.st_ino]
        names = set()
        for path in sorted(directory.iterdir()):
            key = unicodedata.normalize("NFC", path.name).casefold()
            if key in names:
                raise ValueError("ambiguous output/workspace path")
            names.add(key)
            if len(result) >= 10000:
                raise ValueError("bounded snapshot exceeds 10000 entries")
            info = _regular(path)
            if stat.S_ISDIR(info.st_mode):
                visit(path)
            else:
                total += info.st_size
                if total > 128 * 1024 * 1024:
                    raise ValueError("bounded snapshot exceeds 128 MiB")
                with path.open("rb") as stream:
                    opened = os.fstat(stream.fileno())
                    data = stream.read(128 * 1024 * 1024 + 1)
                    end = os.fstat(stream.fileno())
                after = _regular(path)
                def identity(s):
                    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                if identity(info) != identity(opened) or identity(info) != identity(end) or identity(info) != identity(after):
                    raise ValueError("file changed during snapshot")
                result[path.relative_to(root).as_posix()] = ["file", *identity(info), hashlib.sha256(data).hexdigest()]
    visit(root)
    return result


def _allocate(root, relative):
    current = root
    for part in relative.split("/"):
        aliases = [p for p in current.iterdir() if unicodedata.normalize("NFC", p.name).casefold() == part.casefold()]
        if aliases and (len(aliases) != 1 or aliases[0].name != part):
            raise ValueError("output path alias")
        current = current / part
        if current.exists():
            info = _regular(current)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("output parent must be directory")
        else:
            current.mkdir()
    leaf = current / uuid.uuid4().hex
    leaf.mkdir()
    return leaf


def run_output_check(target, gates, *, phase, policy_digest):
    from .commands.check import (CheckOutcome, _adopted, _build_check_receipt,
                                 _validate_policy_binding, _PHASE_MEMBERSHIP)
    root_path = Path(target).absolute()
    for parent in (root_path, *root_path.parents):
        _regular(parent)
    root = root_path.resolve(strict=True)
    if phase not in _PHASE_MEMBERSHIP or not policy_digest:
        raise ValueError("output permission requires an explicit current policy digest and phase")
    _validate_policy_binding(root, gates, policy_digest)
    selected = sorted((g for g in gates if g.phase in _PHASE_MEMBERSHIP[phase]),
                      key=lambda g: (("fast", "full", "release").index(g.phase), g.gate_id))
    if len({g.gate_id for g in selected}) != len(selected) or len(selected) > 256:
        raise ValueError("invalid gate selection")
    for gate in selected:
        _validate_gate_options(gate.kind, gate.options)
    initial = _snapshot(root)
    checks, evidence, outputs, violations = [], [], [], []
    exit_code = 0
    last = initial
    for index, gate in enumerate(selected):
        try:
            _validate_policy_binding(root, gates, policy_digest)
            if _snapshot(root) != last:
                raise ValueError("workspace_changed_between_gates")
        except (OSError, ValueError) as error:
            violations.append(str(error))
            exit_code = 4
            break
        leaf = _allocate(root, gate.options["output_dir"]) if "output_dir" in gate.options else None
        before = _snapshot(root)
        relative = leaf.relative_to(root).as_posix() if leaf else None
        allocation_changes = [p for p in set(last) | set(before) if last.get(p) != before.get(p)]
        expected_new_dirs = set()
        if relative:
            parts = relative.split("/")
            expected_new_dirs = {"/".join(parts[:i]) for i in range(1, len(parts) + 1)}
        if any(p in last or p not in expected_new_dirs or before[p][0] != "directory" for p in allocation_changes):
            violations.append("workspace_changed_during_output_allocation")
            exit_code = 4
            break
        token = _command_output_dir.set(str(leaf) if leaf else None)
        try:
            check, item = _execute_gate(gate, root, index)
        finally:
            _command_output_dir.reset(token)
        checks.append(check)
        evidence.append(item)
        row = {"gate_id": gate.gate_id, "run_directory": relative,
               "gate_contract_sha256": item.gate_contract_sha256, "policy_sha256": policy_digest,
               "process_exit_code": item.process_exit_code, "files": [], "scope": "passed"}
        try:
            after = _snapshot(root)
            changes = [p for p in set(before) | set(after) if before.get(p) != after.get(p)]
            outside = [p for p in changes if relative is None or not p.startswith(relative + "/")]
            # The allocated directory itself must retain identity and type.
            if relative and before.get(relative) != after.get(relative):
                outside.append(relative)
            _validate_policy_binding(root, gates, policy_digest)
            if outside:
                violations.extend(sorted(set(outside)))
                row["scope"] = "failed"
            if relative:
                row["files"] = [{"path": p, "sha256": value[-1], "bytes": value[3]}
                                for p, value in after.items() if p.startswith(relative + "/") and value[0] == "file"]
            last = after
        except (OSError, ValueError) as error:
            violations.append(str(error))
            row["scope"] = "failed"
        outputs.append(row)
        if violations:
            exit_code = 4
            break
        if check.status in (CheckStatus.FAIL, CheckStatus.INCONCLUSIVE):
            exit_code = 1 if check.status is CheckStatus.FAIL else 3
            break
    receipt_outputs = {"exit_code": exit_code, "bounded_outputs": outputs, "changed_paths": violations,
        "skipped_gates": [g.gate_id for g in selected[len(checks):]],
        "gate_execution_evidence": gate_execution_evidence_document(tuple(evidence), phase=phase,
             policy_sha256=policy_digest, selection_mode="phase", performed=bool(evidence))}
    receipt = _build_check_receipt(command="check", policy_digest=policy_digest,
        target_fingerprint=digest(initial), authorized_scope=(".governance/receipts",) + tuple(r["run_directory"] for r in outputs if r["run_directory"]),
        inputs={"phase": phase, "output_contract": "bounded-output-v1-candidate"}, outputs=receipt_outputs,
        checks=tuple(checks), classification="scope-violation" if violations else "check", evidence_refs=tuple(violations))
    # Even failed attempts preserve their receipt. No cleanup hides failed output.
    receipt_path = ".governance/receipts/" + uuid.uuid4().hex + "-bounded-check.json"
    transaction = WorkspaceTransaction(WorkspaceGuard(root), (receipt_path,), apply=True)
    transaction.stage_bytes(receipt_path, canonical_json_bytes(receipt))
    transaction.commit()
    return CheckOutcome(GateRun(tuple(checks), exit_code, tuple(evidence)), receipt, _adopted(root), tuple(violations))
