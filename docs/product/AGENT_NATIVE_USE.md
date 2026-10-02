# Agent-Native Local Use

This guide is for Codex and other agents that work in a local repository. It describes the existing OpenCoding Python and CLI entry points; it does not add a browser workbench, HTTP service, MCP server, plugin, or embedded Codex App integration.

## Scope and authority

Every call uses an explicit existing local project root. The caller must establish that the root, user goal, reviewed preview, and any local write are within the user's authorization. An `approved` field inside an approval object is part of the local integrity check; it is not evidence that a user authorized an arbitrary scope, and it is never authorization for external work.

The session/document and read-only status paths described here do not activate a Host, provider or external service. Other packaged modes include provider-capable code and same-user Python/Node execution; this guide does not verify those modes or promise a network sandbox. Path checks and application locks are not an operating-system sandbox. A TaskPlan is a planning result, not a scheduler success record or permission to run arbitrary code.

## Discover the local entry points

The CLI entry is:

```powershell
python -m opencoding --root C:\path\to\authorized-project --help
```

The legacy document/status modes covered here are the interactive session flow, `--resume SESSION_ID`, `--list`, `--preview SESSION_ID`, `--change SESSION_ID QUESTION_ID ANSWER`, `--rollback TRANSACTION_ID`, and `--status [--task-id TASK_ID] [--json]`. It has no `--create`, `--apply`, `--run-next`, `--recover`, or `--requeue` aliases.

Additional plan/autorun modes are listed by `--help`. The Stage28 route is `python -m opencoding project --help` (or installed `opencoding-project --help`). `python -m opencoding.project_entry --help` is not a working module CLI. The installed wheel and sdist include the validated package skill resource used by `project preview/run`; a source checkout still takes precedence when present. See [installation boundaries](OFFLINE_INSTALLATION.md#安装产物与源码入口的边界).

The Python entry points are `opencoding.service` for the document/session loop and `opencoding.scheduler` for explicit local scheduler tasks. They are ordinary in-process APIs, not a wire protocol. Use return values and exception types, not human-readable CLI text, for control flow.

## Session and document loop

The following service functions have these signatures and effects:

| Function | Effect |
| --- | --- |
| `session_view(root, session_id, *, include_preview=False)` | Read session and derived view. |
| `list_sessions(root)` | Read saved sessions. |
| `preview_session(root, session_id)` | Read the complete plan, exact targets, and diff without applying documents. |
| `execution_status(root, task_id=None)` | Zero-write scheduler and stored autorun status; it does not initialize, migrate, or recover scheduler state. |
| `create_session(root, goal)` | Writes a persisted local session. |
| `submit_answer(root, session_id, expected_revision, question_id, answer)` | Writes a revisioned answer, or returns `busy`/`stale`. |
| `approve_preview(preview, *, expires_in_seconds=300)` | Validates a complete ready preview and returns an in-memory, expiring local approval object. |
| `apply_approved(root, approval)` | Revalidates identity, revision, digests, targets, expiry, and file-plan drift before local document writes. |
| `rollback(root, transaction_id)` | Performs the transaction layer's local rollback. |

The executable calling pattern is maintained in [the self-contained installation guide](OFFLINE_INSTALLATION.md#可调用的会话文档示例), which is embedded in wheel metadata. It defines `collect_preview` and `apply_after_exact_user_authorization` without running either function on import. The collector has a bounded answer loop; `None`, unresolved answers, `busy` and `stale` return to the caller before approval or apply. The source test suite checks these boundaries; the installed-package regression separately checks the packaged entrypoints and read-only status path.

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

The Stage28 `project status` route reads a different project/run ledger; it is not interchangeable with the legacy scheduler/autorun snapshot.

Use scheduler controls only for an already authorized, schema-valid local task. The executor accepts a small structured action set and binds a local `ActionContext`; it does not accept arbitrary shell commands or external actions. `requeue` requires the task's matching idempotency key and can return `already_queued` or `requeue_rejected` when state or attempt limits do not permit it. `recover` reports the recovered task IDs. Direct scheduler callers must handle `ValueError` and, for direct snapshots, `SchedulerSnapshotError` rather than assuming service-style errors.

## Evidence availability

The V2 plan retains separate W2-B, W2-C1 read-only views, W2-C2 CLI normalization and W3-A packaging references. Their original acceptance files are absent from this checkout and the fetched history. Historical acceptance labels are preserved as claims, not local re-verification. The recovery ZIP is a candidate source only; membership of these specific files has not been checked. New offline tests establish evidence for the current bytes and cannot reconstruct historical sign-off.

## Verification and boundaries

Before reporting a local operation as complete, inspect the returned structured result, relevant receipt or rollback result, and the applicable local verification evidence. Do not infer that planning, a preview, an approval object, a nonzero CLI exit code, or an unavailable scheduler store proves execution success.

This guide does not claim universal Codex, agent, operating-system, or platform compatibility. W4 controlled Host/connectors and W5 independent beginner/platform/release acceptance remain later work. Activating a real Host or connector, using credentials or cost-bearing resources, and publication each require a separately authorized Gate.
