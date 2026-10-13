"""TaskPlan candidate execution over the persistent scheduler.

This stage produces candidates only. It neither installs outputs nor treats a
Host's successful response as independent validation or delivery approval.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping

from .planning import validate_task_plan, _path_error
from .safety import canonical_json, sha256_bytes


def validate_host_action(action):
    if set(action) != {"type", "host_id", "session_id", "plan_digest", "outputs"}:
        raise ValueError("invalid host action fields")
    if action["type"] != "host_task":
        raise ValueError("invalid host action")
    for key in ("host_id", "session_id"):
        if not isinstance(action[key], str) or not action[key].strip():
            raise ValueError("invalid host identity")
    if not isinstance(action["plan_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", action["plan_digest"]):
        raise ValueError("invalid plan digest")
    outputs = action["outputs"]
    if not isinstance(outputs, list) or any(_path_error(path) for path in outputs):
        raise ValueError("invalid host outputs")
    if not outputs or len(set(path.casefold() for path in outputs)) != len(outputs):
        raise ValueError("empty or duplicate host outputs")


def enqueue_task_plan(scheduler, plan, *, host_id, max_requests, timeout_seconds=180,
                      inputs=None, production_binding=None):
    """Bind a complete plan and immutable input bytes to deterministic task IDs.

    The count budget is cumulative for this plan and includes failed/unknown
    Host dispatches. It does not count an agent's internal model calls or bound
    currency spend. Activation Gates remain closed.
    """
    from .scheduler import _validate_task
    checked = validate_task_plan(plan)
    if not checked["valid"] or not plan["tasks"] or plan["unresolved"]:
        raise ValueError("task plan invalid, empty or unresolved")
    if any(task["activation_gate"]["required"] for task in plan["tasks"]):
        raise ValueError("task activation requires a separately bound authorization")
    if type(max_requests) is not int or not 1 <= max_requests <= 10000:
        raise ValueError("invalid request budget")
    inputs = {} if inputs is None else inputs
    if not isinstance(inputs, Mapping) or any(_path_error(k) or not isinstance(v, str) for k, v in inputs.items()):
        raise ValueError("inputs must map safe paths to text")
    allowed_inputs = {p for t in plan["tasks"] for p in t["inputs"]}
    if set(inputs) - allowed_inputs:
        raise ValueError("undeclared input")
    if set(inputs) & {p for t in plan["tasks"] for p in t["outputs"]}:
        raise ValueError("source inputs cannot impersonate plan outputs")
    digest = sha256_bytes(canonical_json(plan))
    ids = {t["id"]: "host-" + sha256_bytes(canonical_json([plan["session_id"], digest, t["id"]])) for t in plan["tasks"]}
    by_id = {t["id"]: t for t in plan["tasks"]}
    prepared = []
    for wave in checked["waves"]:
        for task_id in wave:
            item = by_id[task_id]
            package = {"plan": plan, "task_id": task_id, "max_requests": max_requests,
                       "source_inputs": dict(inputs), "delivery_gate": "pending_independent_review"}
            if production_binding is not None:
                package["production_binding"] = json.loads(canonical_json(production_binding))
            task = {"task_id": ids[task_id], "idempotency_key": ids[task_id],
                    "input": package, "action": {"type": "host_task", "host_id": host_id,
                    "session_id": plan["session_id"], "plan_digest": digest, "outputs": item["outputs"]},
                    "depends_on": [ids[d] for d in item["depends_on"]],
                    "max_attempts": min(item["retry"]["max_attempts"], 100),
                    "timeout_seconds": timeout_seconds}
            _validate_task(task)
            prepared.append(task)
    # Validate the entire package before the first enqueue. Existing records are
    # idempotent; an interrupted enqueue is safely continued with identical data.
    receipts = [scheduler.enqueue(task) for task in prepared]
    return {"plan_digest": digest, "task_ids": ids,
            "waves": [[ids[t] for t in w] for w in checked["waves"]], "tasks": receipts,
            "delivery_gate": "pending_independent_review"}


def check_dispatch(connection, selected, registry):
    if registry is None:
        return "host_registry_unavailable"
    action = json.loads(selected["action_json"])
    package = json.loads(selected["input_json"])
    try:
        registry.require(action["host_id"])
        plan = package["plan"]
        if sha256_bytes(canonical_json(plan)) != action["plan_digest"] or not validate_task_plan(plan)["valid"]:
            return "host_plan_binding_mismatch"
        item = next(t for t in plan["tasks"] if t["id"] == package["task_id"])
        ids = {t["id"]: "host-" + sha256_bytes(canonical_json([plan["session_id"], action["plan_digest"], t["id"]])) for t in plan["tasks"]}
        if (selected["task_id"] != ids[item["id"]] or selected["idempotency_key"] != ids[item["id"]]
                or json.loads(selected["depends_json"]) != [ids[d] for d in item["depends_on"]]
                or selected["max_attempts"] != min(item["retry"]["max_attempts"], 100)):
            return "host_task_binding_mismatch"
        source = package["source_inputs"]
        allowed = {p for t in plan["tasks"] for p in t["inputs"]}
        produced = {p for t in plan["tasks"] for p in t["outputs"]}
        if (type(source) is not dict or any(_path_error(k) or type(v) is not str for k, v in source.items())
                or set(source) - allowed or set(source) & produced):
            return "host_input_binding_mismatch"
        if "production_binding" in package:
            binding = package["production_binding"]
            if (type(binding) is not dict or binding.get("plan_digest") != action["plan_digest"]
                    or binding.get("session_id") != action["session_id"]
                    or binding.get("revision") != plan["revision"]
                    or binding.get("source_inputs_digest") != sha256_bytes(canonical_json(source))):
                return "host_production_binding_mismatch"
        if (plan["unresolved"] or item["activation_gate"]["required"] or plan["session_id"] != action["session_id"]
                or item["outputs"] != action["outputs"]):
            return "host_plan_not_executable"
        budget = package["max_requests"]
        if type(budget) is not int or not 1 <= budget <= 10000:
            return "host_budget_invalid"
    except (KeyError, TypeError, StopIteration, ValueError):
        return "host_package_invalid"
    used = 0
    recorded = set()
    for row in connection.execute("SELECT t.task_id,t.action_json,t.input_json,COUNT(r.run_id) AS used FROM tasks t LEFT JOIN runs r ON r.task_id=t.task_id GROUP BY t.task_id"):
        other = json.loads(row["action_json"])
        if other.get("type") == "host_task" and other.get("plan_digest") == action["plan_digest"]:
            recorded.add(row["task_id"])
            other_package = json.loads(row["input_json"])
            if other_package.get("max_requests") != budget:
                return "host_budget_binding_mismatch"
            if other_package.get("source_inputs") != source or other["host_id"] != action["host_id"]:
                return "host_input_binding_mismatch"
            if other_package.get("production_binding") != package.get("production_binding"):
                return "host_production_binding_mismatch"
            used += row["used"]
    if recorded != set(ids.values()):
        return "host_plan_enqueue_incomplete"
    return "host_request_budget_exhausted" if used >= budget else None


def run_task_wave(scheduler, plan_digest):
    """Execute the earliest unfinished wave; preserve scheduler claim semantics."""
    from .scheduler import _connect
    with _connect(scheduler.root) as connection:
        records = [dict(row) for row in connection.execute("SELECT * FROM tasks")
                   if json.loads(row["action_json"]).get("plan_digest") == plan_digest]
    if not records:
        raise ValueError("plan is not enqueued")
    package = json.loads(records[0]["input_json"])
    plan = package["plan"]
    by_original = {json.loads(row["input_json"])["task_id"]: row for row in records}
    for wave_index, wave in enumerate(plan["waves"]):
        if any(item not in by_original for item in wave):
            return {"status": "blocked", "reason": "plan_enqueue_incomplete", "wave": wave_index}
        rows = [by_original[item] for item in wave]
        if all(row["state"] == "succeeded" for row in rows):
            continue
        if any(row["state"] in {"failed", "frozen", "cancelled", "timed_out"} for row in rows):
            return {"status": "blocked", "reason": "wave_not_successful", "wave": wave_index}
        results = [scheduler.run_next(row["task_id"]) for row in rows if row["state"] == "queued"]
        return {"status": "wave_dispatched", "wave": wave_index, "results": results,
                "delivery_gate": "pending_independent_review"}
    return {"status": "candidates_complete", "delivery_gate": "pending_independent_review"}


def execute_host_task(scheduler, task, run_id, cancel_event):
    from .scheduler import _connect, action_digest
    action = task["action"]
    package = task["input"]
    item = next(t for t in package["plan"]["tasks"] if t["id"] == package["task_id"])
    files = dict(package["source_inputs"])
    dependencies = []
    plan_tasks = {t["id"]: t for t in package["plan"]["tasks"]}
    ancestors = set()
    pending = list(item["depends_on"])
    while pending:
        original = pending.pop()
        if original not in ancestors:
            ancestors.add(original)
            pending.extend(plan_tasks[original]["depends_on"])
    from .host import candidate_digest
    from .scheduler import _receipt_digest
    with _connect(scheduler.root) as connection:
        connection.execute("BEGIN")
        for wave in package["plan"]["waves"]:
            for original in wave:
                if original not in ancestors:
                    continue
                dependency = "host-" + sha256_bytes(canonical_json([action["session_id"], action["plan_digest"], original]))
                row = connection.execute("SELECT r.receipt_json,t.last_run_id,t.attempt FROM tasks t JOIN runs r ON t.last_run_id=r.run_id WHERE t.task_id=? AND t.state='succeeded'", (dependency,)).fetchone()
                if row is None:
                    raise ValueError("host dependency no longer succeeded")
                receipt = json.loads(row["receipt_json"])
                evidence = receipt.get("host_result")
                if not evidence or not evidence.get("candidate"):
                    raise ValueError("host dependency lacks candidate evidence")
                identity = evidence["identity"]
                expected = {"root": str(scheduler.root), "session_id": action["session_id"],
                            "plan_digest": action["plan_digest"], "task_id": dependency,
                            "run_id": row["last_run_id"], "attempt": row["attempt"],
                            "host_id": action["host_id"], "outputs": plan_tasks[original]["outputs"]}
                if (receipt["receipt_digest"] != _receipt_digest(receipt)
                        or receipt["task_id"] != dependency or receipt["run_id"] != row["last_run_id"]
                        or receipt["status"] != "succeeded" or evidence["status"] != "succeeded"
                        or canonical_json(identity) != canonical_json(expected)
                        or set(evidence["candidate"]["files"]) != set(expected["outputs"])
                        or evidence["candidate"]["digest"] != candidate_digest(evidence["candidate"]["files"])):
                    raise ValueError("host dependency evidence mismatch")
                files.update(evidence["candidate"]["files"])
                dependencies.append({"task_id": dependency, "receipt_digest": receipt["receipt_digest"],
                                     "candidate_digest": evidence["candidate"]["digest"]})
    missing = set(item["inputs"]) - set(files) - set(item["outputs"])
    if missing:
        raise ValueError("host input unavailable: " + ", ".join(sorted(missing)))
    previous_outputs = {}
    if "production_binding" in package:
        from .safety import safe_target, inspect_sensitive
        for path in item["outputs"]:
            target = safe_target(scheduler.root, path)
            expected = package["production_binding"]["output_preimages"][path]["sha256"]
            raw = None
            if target.exists():
                if target.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("host output preimage size limit exceeded")
                with target.open("rb") as stream:
                    raw = stream.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError("host output preimage size limit exceeded")
            if (sha256_bytes(raw) if raw is not None else None) != expected:
                raise ValueError("host output preimage changed")
            if raw is not None:
                content = raw.decode("utf-8")
                if inspect_sensitive(content)["sensitive"]:
                    raise ValueError("host output contains sensitive material")
                previous_outputs[path] = content
    request = {"schema_version": "1.0", "host_id": action["host_id"], "root": str(scheduler.root),
               "session_id": action["session_id"], "plan_digest": action["plan_digest"],
               "task_id": task["task_id"], "run_id": run_id, "attempt": task["attempt"],
               "outputs": action["outputs"], "timeout_seconds": task["timeout_seconds"],
               "input": {"task": item, "files": {p: files[p] for p in item["inputs"] if p in files},
                         "dependencies": dependencies, "max_requests": package["max_requests"],
                         "new_outputs": [p for p in item["inputs"] if p in item["outputs"] and p not in files and p not in previous_outputs]}}
    if "production_binding" in package:
        request["input"].update(production_binding=package["production_binding"], previous_outputs=previous_outputs)
        # Recheck after constructing all messages; hold no session lock across
        # the Host call. Changes after this gate are rejected at result commit.
        from .host_workflow import verify_binding
        from .sessions import session_write_lock
        with session_write_lock(scheduler.root):
            verify_binding(scheduler.root, package["production_binding"])
            if cancel_event.is_set():
                return {"status": "cancelled", "run_id": run_id,
                        "action_digest": action_digest(action), "reason": "cancel_requested"}
    response = scheduler.host_registry.execute(request, cancel_event=cancel_event)
    return {"status": response["status"], "run_id": run_id, "action_digest": action_digest(action),
            "artifacts": [], "host_result": response,
            "validation": {"status": "pending", "acceptance": item["acceptance"]},
            "rollback": {"status": "not_applied", "instruction": item["rollback"]},
            "reason": "candidate_only" if response["status"] == "succeeded" else response.get("error")}
