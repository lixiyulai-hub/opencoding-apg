# Agent-Native Local Use

This guide is for Codex and other agents that work in a local repository. It describes the existing OpenCoding Python and CLI entry points; it does not add a browser workbench, HTTP service, MCP server, plugin, or embedded Codex App integration.

## Scope and authority

Every call uses an explicit existing local project root. The caller must establish that the root, user goal, reviewed preview, and any local write are within the user's authorization. An `approved` field inside an approval object is only a local integrity flag; it is not evidence that a user authorized an arbitrary scope, and it is never authorization for external work. The caller must issue a human confirmation receipt bound to the exact root, session revision, service digest, targets, and diff before an approval can be created or applied.

The current package does not connect to a Host, provider, network, credentials, real production data, deployment target, or publication channel. Path checks and application locks are not an operating-system sandbox. A TaskPlan is a planning result, not a scheduler success record or permission to run arbitrary code.

## Discover the local entry points

The CLI entry is:

```powershell
python -m opencoding --root C:\path\to\authorized-project --help
```

Its supported modes are the interactive session flow, `--resume SESSION_ID`, `--list`, `--preview SESSION_ID`, `--task-preview SESSION_ID [--json]`, `--change SESSION_ID QUESTION_ID ANSWER`, `--rollback TRANSACTION_ID`, and `--status [--task-id TASK_ID] [--json]`. The explicit modes are mutually exclusive; `--task-id` is only valid with `--status`. It has no `--create`, `--apply`, `--run-next`, `--recover`, or `--requeue` aliases.

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
| `build_caller_confirmation(preview, *, statement, actor=...)` | Records the caller's receipt after a person confirms the exact preview; it does not approve or write. |
| `approve_preview(preview, *, confirmation, expires_in_seconds=300)` | Validates a complete ready preview and binds the caller-issued receipt into an in-memory, expiring local approval object. |
| `apply_approved(root, approval, authorization_context=...)` | Requires the same caller-issued receipt, then revalidates identity, revision, digests, targets, expiry, and file-plan drift before local document writes. |
| `rollback(root, transaction_id)` | Performs the transaction layer's local rollback. |

The normal agent sequence is shown below as a calling pattern, not an automatically executed script. `goal` and each `answer` must come from the user or an authorized local task context. The conflict branch returns to the caller before it can obtain a preview or approval.

```python
from pathlib import Path
from opencoding.service import (
    ServiceError,
    apply_approved,
    approve_preview,
    build_caller_confirmation,
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
    # The caller's authority process must record this exact root, targets, and diff.
    receipt = build_caller_confirmation(
        preview,
        statement="用户确认以上方案、精确范围和差异，只生成本地文档。",
        actor="your-caller-id",
    )
    approval = approve_preview(preview, confirmation=receipt)
    return apply_approved(preview["root"], approval, authorization_context=receipt)


root = Path(r"C:\path\to\authorized-project")
prepared = collect_preview(root, goal, answer_for)
result = apply_after_exact_user_authorization(prepared)
```

`create_session` and `submit_answer` write session state even when documents are not applied. A `busy` or `stale` answer result has no session payload for the example to continue with; return it to the caller, re-read and reconcile there, and obtain a fresh preview later. `preview_session` is the point to inspect exact proposed document paths and diff. `build_caller_confirmation` is a caller-side receipt assertion after the person has reviewed that exact scope; it is not a service-generated grant. `approve_preview` rejects calls without that receipt, and `apply_approved` rejects calls without the same `authorization_context`. Callers must not synthesize or edit an approval dictionary. `apply_approved` can return `applied`, `stale`, or `busy` outcomes. A stale or busy result is a stop-and-reconcile condition, not a signal to reuse or broaden the prior approval. `rollback` returns the transaction layer result; inspect its `status` and receipt information before declaring recovery complete.

`ServiceError` exposes a `code`, but codes are specific to the called service operation. The existing `--status --json` protocol is unchanged. The separate TaskPlan preview convention is described below; neither it nor the status convention makes the interactive wizard, argument errors, or other CLI failures a complete, uniform JSON protocol.

## Bounded TaskPlan adapter

`opencoding.taskplan_scheduler` now provides a separate, explicit offline bridge:
`preview_task_plan`, `build_task_plan_confirmation`, `approve_task_plan`, and
`execute_task_plan`. The dedicated caller context binds the exact expiry as well
as the graph; approving the same context cannot renew it. Read
[`TASKPLAN_SCHEDULER.md`](TASKPLAN_SCHEDULER.md) for exact confirmation, task
mapping, idempotency and per-task transaction rollback. This bridge only writes
same-source Markdown; implementation and verification remain `host_missing`.
The raw Scheduler helpers below do not implicitly invoke the bridge.

For an existing session, the CLI also exposes the bridge's zero-write preview:

```powershell
python -m opencoding --root C:\path\to\authorized-project --task-preview SESSION_ID
python -m opencoding --root C:\path\to\authorized-project --task-preview SESSION_ID --json
```

The Chinese display includes the root, session ID and revision, plan status, exact targets and diff, task IDs, dependencies, and classifications. `ready` describes the plan, not execution success. `document` describes planned local document work; `offline_design` remains unactivated; `host_missing` identifies unavailable implementation or verification capability. This preview does not inspect live task/run states or establish that a task is `frozen`. Its `effects` describe possible effects of a later, separately confirmed API execution; they did not occur during preview.

With `--json`, stdout is the unmodified JSON serialization of `preview_task_plan(root, session_id)`, including its nested service preview, tasks, effects, and digest. Exit `0` means the preview was read, even when the plan has unresolved questions; it is not an execution receipt. Read failures exit `2`, with `{"error":{"code":"..."}}` on stderr in JSON mode. Argument errors still use the CLI parser's error output.

This mode does not create, initialize, migrate, recover, or repair a session or Scheduler. It never approves, executes, calls `run_next`, requeues, rolls back, or enters the wizard; even sending `确认` to stdin cannot execute the plan. Missing roots/sessions, invalid session IDs, conflicting modes, malformed data, and read-lock failures do not create paths or trigger automatic repair. TaskPlan confirmation, approval, and execution remain explicit Python API operations. Windows verification of this new CLI mode is still pending.

## Raw Scheduler control is separate from TaskPlan

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

External integration tasks for server, database, authentication/identity, storage, external data, payment, notifications, and marketplace KYC, logistics, authentication, and risk are design-only. Each task carries an explicit human Gate, an `offline_design_only` state, and a local draft rollback path. They never create provider accounts, contact real services, or accept secrets. A later activation or deactivation is a separate transaction requiring a new exact confirmation.

This guide does not claim universal Codex, agent, operating-system, or platform compatibility. W4 controlled Host/connectors and W5 independent beginner/platform/release acceptance remain later work. Activating a real Host or connector, using credentials or cost-bearing resources, and publication each require a separately authorized Gate.
