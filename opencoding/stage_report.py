"""Read-only stage evidence. Adoption, checks, execution and acceptance stay separate."""
import json
from pathlib import Path

from .safety import _root_path, sanitize_text, sha256_bytes
from .sessions import _check_existing_chain


def summarize_gate_receipt(value):
    receipt = value.get("receipt", value)
    outputs = receipt.get("outputs", {})
    entries = outputs.get("gate_execution_evidence", {}).get("entries", [])
    commands = [{key: item.get(key) for key in ("gate_id", "process_exit_code", "status", "reason_code")}
                for item in entries if item.get("kind") == "command"]
    return {"classification": receipt.get("classification"), "exit_code": outputs.get("exit_code"),
            "commands": commands, "checks": [{"gate_id": c.get("gate_id"), "status": c.get("status")}
                                              for c in receipt.get("checks", [])],
            "scope": "failed" if receipt.get("classification") == "scope-violation" else "not_separately_recorded",
            "overall": "passed" if outputs.get("exit_code") == 0 else "failed" if outputs.get("exit_code") is not None else "unknown",
            "changed_paths": outputs.get("changed_paths", [])}


def stage_report(root, session_id):
    from . import service
    from .transactions import read_receipt_state
    root = _root_path(root)
    report = {"schema_version": "1.0", "session_id": session_id, "warnings": [],
              "plan": {}, "documents": {}, "execution": {}, "governance": [],
              "independent_review": "not_verified_by_this_report", "deployment": "not_recorded",
              "user_acceptance": "not_recorded", "transactions": [],
              "limits": ["Current preview is recomputed, not historical preview evidence.",
                         "Project receipts may belong to other sessions; they are not aggregated as this session's acceptance.",
                         "A source hash does not establish reviewer independence or live execution."]}
    # Validate session identity via existing reader before constructing any paths.
    from .sessions import read_session_snapshot
    session = read_session_snapshot(root, session_id)
    report["plan"]["revision"] = session["revision"]
    try:
        evaluated = service.evaluate_session(root, session_id)
        report["plan"].update(status="evaluated", input_digest=evaluated["input_digest"],
                              plan_digest=evaluated["adopted_plan_digest"],
                              unresolved=evaluated["recommendation"]["unresolved"])
        report["plan"]["adoption"] = service.adoption_input_status(root, session_id)
    except (ValueError, OSError) as exc:
        report["plan"]["error"] = sanitize_text(str(exc))
    try:
        preview = service.preview_session(root, session_id)
        report["documents"] = {"current_preview": "ready", "targets": preview["targets"],
                               "applied": "see_transaction_evidence"}
    except (ValueError, OSError) as exc:
        report["documents"] = {"current_preview": "failed", "error": sanitize_text(str(exc))}
    try:
        snapshot = service.execution_status(root)
        report["execution"] = {"status": snapshot["status"], "task_count": len(snapshot["tasks"]),
                               "run_count": len(snapshot["runs"]), "scope": "project", "runs": []}
        for run in snapshot["runs"]:
            report["execution"]["runs"].append({key: run.get(key) for key in ("run_id", "task_id", "status", "exit_code")})
        report["execution"]["live_verification"] = "not_inferred_from_run_success"
    except (ValueError, OSError) as exc:
        report["execution"] = {"status": "unavailable", "error": sanitize_text(str(exc))}
    for relative, kind in ((".opencoding/transactions", "transaction"), (".governance/receipts", "gate")):
        directory = root / relative
        try:
            _check_existing_chain(directory, root=root)
            if not directory.exists():
                continue
            paths = sorted(directory.iterdir(), key=lambda p: p.name)
            if len(paths) > 1000:
                raise ValueError("evidence inventory exceeds 1000 items")
            for path in paths:
                try:
                    _check_existing_chain(path, root=root)
                    if kind == "transaction":
                        if not path.is_dir():
                            continue
                        state = read_receipt_state(root, path.name)
                        report["transactions"].append({"ref": path.relative_to(root).as_posix(),
                            **{key: state.get(key) for key in ("transaction_id", "status", "rollback_status", "planned_paths")}})
                    elif path.suffix == ".json":
                        if path.stat().st_size > 2 * 1024 * 1024 or path.stat().st_nlink > 1:
                            raise ValueError("unsafe or oversized receipt")
                        raw = path.read_bytes()
                        value = json.loads(raw)
                        summary = summarize_gate_receipt(value)
                        report["governance"].append({"ref": path.relative_to(root).as_posix(),
                            "sha256": sha256_bytes(raw), **summary})
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    report["warnings"].append({"ref": path.relative_to(root).as_posix(), "error": sanitize_text(str(exc))})
        except (OSError, ValueError) as exc:
            report["warnings"].append({"ref": relative, "error": sanitize_text(str(exc))})
    return report
