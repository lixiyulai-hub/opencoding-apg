# Agent-Native Local Use

This guide is for Codex and other agents that work in a local repository. It describes the OpenCoding 0.2.7 Python and CLI entry points. The package also includes an optional loopback browser workbench (`python -m opencoding.workbench`); agents do not need it to use the local APIs. See `OFFLINE_INSTALLATION.md` for installed-package startup and AI configuration boundaries. No MCP server, plugin, or embedded Codex App compatibility is claimed here.

## Scope and authority

Every call uses an explicit existing local project root. The caller must establish that the root, user goal, reviewed preview, and any local write are within the user's authorization. An `approved` field inside an approval object is part of the local integrity check; it is not evidence that a user authorized an arbitrary scope, and it is never authorization for external work.

The local session/document and scheduler flows below do not require a provider or network. Optional AI evaluation and generation interfaces can call configured providers, so this is not a package-wide offline guarantee. Establish separate authorization for provider calls, credentials, costs, real data, deployment, and publication. Path checks and application locks are not an operating-system sandbox. A TaskPlan is a planning result, not a scheduler success record or permission to run arbitrary code.

## Discover the local entry points

The CLI entry is:

```powershell
python -m opencoding --root C:\path\to\authorized-project --help
```

Its local session modes are the interactive flow, `--resume SESSION_ID`, `--list`, `--preview SESSION_ID`, `--evaluate SESSION_ID`, `--adopt-plan SESSION_ID`, `--change SESSION_ID QUESTION_ID ANSWER`, `--rollback TRANSACTION_ID`, and `--status [--task-id TASK_ID] [--json]`. Additional autonomous modes include `--autorun lendreg`, `--cancel-run RUN_ID`, `--query-run RUN_ID`, and `--rollback-run RUN_ID`; consult `--help` and `AUTORUN_QUICKSTART.md` for their distinct authorization and delivery checks. `--mock-ai` is a simulation, not proof of provider connectivity or a successful delivery. There are no `--create`, `--apply`, `--run-next`, `--recover`, or `--requeue` aliases.

The Python entry points are `opencoding.service` for the document/session loop and `opencoding.scheduler` for explicit local scheduler tasks. They are ordinary in-process APIs, not a wire protocol. Use return values and exception types, not human-readable CLI text, for control flow.

## Session and document loop

The following service functions have these signatures and effects:

| Function | Effect |
| --- | --- |
| `session_view(root, session_id, *, include_preview=False)` | Read session and derived view. |
| `list_sessions(root)` | Read saved sessions. |
| `preview_session(root, session_id)` | Read the complete plan, exact targets, and diff without applying documents. |
| `execution_status(root, task_id=None)` | Zero-write scheduler snapshot; it does not initialize, migrate, or recover scheduler state. |
| `create_session(root, goal)` | Writes a persisted local session. |
| `submit_answer(root, session_id, expected_revision, question_id, answer)` | Writes a revisioned answer, or returns `busy`/`stale`. |
| `approve_preview(preview, *, expires_in_seconds=300)` | Validates a complete ready preview and returns an in-memory, expiring local approval object. |
| `apply_approved(root, approval)` | Revalidates identity, revision, digests, targets, expiry, and file-plan drift before local document writes. |
| `rollback(root, transaction_id)` | Performs the transaction layer's local rollback. |

The normal agent sequence is shown below as a calling pattern, not an automatically executed script. `goal` and each `answer` must come from the user or an authorized local task context. The conflict branch returns to the caller before it can obtain a preview or approval.

```python
from pathlib import Path
from opencoding.service import (
    ServiceError,
    apply_approved,
    approve_preview,
    create_session,
    preview_session,
    submit_answer,
)

def collect_preview(root, goal, answer_for):
    view = create_session(root, goal)
    session_id = view["session"]["id"]

    while view["frontier"]:
        question = view["frontier"][0]
        outcome = submit_answer(
            root,
            session_id,
            view["session"]["revision"],
            question["id"],
            answer_for(question),
        )
        if outcome.get("status") in {"busy", "stale"}:
            # Do not read outcome["session"] or continue to preview/apply.
            return {
                "status": "conflict",
                "session_id": session_id,
                "reason_codes": outcome.get("reason_codes", []),
                "result": outcome,
            }
        view = outcome
        session_id = view["session"]["id"]

    return {
        "status": "ready_for_authorization",
        "preview": preview_session(root, session_id),
    }


def apply_after_exact_user_authorization(prepared):
    if prepared["status"] != "ready_for_authorization":
        # Return the conflict to the calling control loop unchanged.
        return prepared
    preview = prepared["preview"]
    # Call this function only after the user has authorized this exact reviewed root,
    # targets, and diff through the caller's own recorded authority process.
    approval = approve_preview(preview)
    return apply_approved(preview["root"], approval)


root = Path(r"C:\path\to\authorized-project")
prepared = collect_preview(root, goal, answer_for)
result = apply_after_exact_user_authorization(prepared)
```

`create_session` and `submit_answer` write session state even when documents are not applied. A `busy` or `stale` answer result has no session payload for the example to continue with; return it to the caller, re-read and reconcile there, and obtain a fresh preview later. `preview_session` is the point to inspect exact proposed document paths and diff. `approve_preview` accepts only a complete, ready, canonical preview and gives the caller an expiry-bound object; callers must not synthesize an approval dictionary. `apply_after_exact_user_authorization` is an illustrative caller-side guard, not an authorization mechanism: it must be invoked only after separate authorization for that exact reviewed local scope. `apply_approved` can return `applied`, `stale`, or `busy` outcomes. A stale or busy result is a stop-and-reconcile condition, not a signal to reuse or broaden the prior approval. `rollback` returns the transaction layer result; inspect its `status` and receipt information before declaring recovery complete.

`ServiceError` exposes a `code`, but codes are specific to the called service operation. The CLI only promises the machine-readable status-error convention for `--status --json`; the interactive wizard and other CLI failures are not a complete, uniform JSON protocol.

## Scheduler control is separate from TaskPlan

The public scheduler module exports these functions:

```python
from opencoding.scheduler import (
    cancel,
    enqueue,
    get_task,
    list_runs,
    read_snapshot,
    recover,
    requeue,
    run_next,
)
```

Their signatures are `enqueue(root, task)`, `run_next(root, task_id=None)`, `cancel(root, task_id)`, `requeue(root, task_id, idempotency_key)`, `recover(root)`, `get_task(root, task_id)`, `list_runs(root, task_id=None)`, and `read_snapshot(root, task_id=None)`.

Only `read_snapshot` is a zero-write scheduler view: it does not initialize, migrate, or recover a store. It returns `not_initialized` when no scheduler store exists, `not_found` for an absent requested task, or `ready` with task/run summaries.

All other module helpers construct `Scheduler(root)`. Construction invokes `recover()` before the requested operation, so these helpers can initialize or migrate local scheduler state and can record recovery for abandoned runs. `enqueue`, `cancel`, `requeue`, and `recover` are mutating controls. `run_next` can execute an already validated structured local action and write local artifacts. `get_task` and `list_runs` should not be labelled zero-write merely because their final query is observational.

Use scheduler controls only for an already authorized, schema-valid local task. The executor accepts a small structured action set and binds a local `ActionContext`; it does not accept arbitrary shell commands or external actions. `requeue` requires the task's matching idempotency key and can return `already_queued` or `requeue_rejected` when state or attempt limits do not permit it. `recover` reports the recovered task IDs. Direct scheduler callers must handle `ValueError` and, for direct snapshots, `SchedulerSnapshotError` rather than assuming service-style errors.

## Verification and boundaries

Before reporting a local operation as complete, inspect the returned structured result, relevant receipt or rollback result, and the applicable local verification evidence. Do not infer that planning, a preview, an approval object, a nonzero CLI exit code, or an unavailable scheduler store proves execution success.

This guide does not claim universal Codex, agent, operating-system, or platform compatibility. W4 controlled Host/connectors and W5 independent beginner/platform/release acceptance remain later work. Activating a real Host or connector, using credentials or cost-bearing resources, and publication each require a separately authorized Gate.
