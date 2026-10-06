# Independent review: bounded TaskPlan → Scheduler adapter

Review date: 2026-10-06. Repository: `lixiyulai-hub/opencoding-apg`.
Original reviewed change: `afc1614e3106b8d72adda35d92f8b70dc570c2a4` relative to `5d68c2075620df0be005c76f904b256b980d303e`.

The independent reviewer performed read-only repository inspection and synthetic probes in temporary directories. The reviewer did not modify repository files, install tools, commit, push, merge, release, or perform the separately blocked public-path cleanup. The author subsequently repaired the finding; the reviewer independently examined the repair and tested it.

## Original finding: P1, approval deadline not bound to caller context

Location in the original commit: `opencoding/taskplan_scheduler.py:107-108` and `:129-131` (approval deadline construction and execution validation).

`approve_task_plan` placed `expires_at` outside the caller confirmation. `execute_task_plan` verified that this field was in the future, but the independently supplied `authorization_context` did not bind it. A caller carrying an otherwise unchanged confirmation could change a one-second approval's deadline to the year 2999 and execute the document tasks. Re-approving an unchanged old confirmation also minted a fresh deadline.

The original independent probe observed 9 successful document runs after mutating only `approval['expires_at']`, while retaining the original caller context. The overall result was correctly `blocked` due to `host_missing`; this does not negate the fact that the changed approval caused document writes. The issue was deadline integrity, not a claim of identity-authentication bypass: this API explicitly uses trusted caller assertions.

Original evidence was reproduced from an exact `git archive` snapshot of `afc1614e` in a temporary directory without changing current repair files:

- `original_probe.py`: independent probe script.
- `original_output.json`: unedited successful process output, exit status 0.
- Original adapter SHA-256: `23fb4955b4f4fe09df94eb9fcc5c5d139f4f6c946034cd77e548cb311bac262b`.

The same original-commit probe separately replaced approved `PRG.md` with a symlink to an external synthetic sentinel. The requirement task failed with the link/reparse rejection, overall execution was blocked, and the external sentinel was unchanged. That check passed; it was not another defect.

## Repair review and independent verification

The repair adds `build_task_plan_confirmation`, wrapping the existing service receipt with the exact execution deadline. Approval copies this complete context and does not renew it. Execution compares its embedded context with the caller's independently supplied complete context and validates the deadline and bounded duration. Ordinary service receipts cannot substitute for this wrapper. Documentation tells callers to retain the context independently and not derive authorization from the approval being validated.

Independent repair probes (`probe.py`, raw output in `output.json`, exit status 0) verified:

1. Preview, confirmation construction and approval caused no project file changes.
2. Adding the original unbound top-level deadline shape was rejected with `approval_fields_invalid`.
3. Extending the nested deadline by 60 seconds while retaining the original independently held context was rejected with `human_confirmation_scope_mismatch`.
4. A year-2999 deadline was rejected with `approval_expiry_invalid`, even when a matching modified context was supplied.
5. After an actual 1.1-second wall-clock wait, re-approving the one-second confirmation returned `approval_expired`; this used no patched clock.
6. Execution with that expired confirmation also returned `approval_expired`.
7. Every rejection above left the synthetic project's file inventory and hashes unchanged.
8. An unchanged valid independently held context still executed 9 successful document tasks, returned overall `blocked` due to the missing host, and did not create a source directory.

The original P1 is closed for the reviewed repair contents. No second definite defect was found.

Reviewed repair adapter SHA-256: `f16cacce59b811f193d7c90a5ce9147df8ba5531132394d2d094705689209278`.
`output.json` records exact SHA-256 values for all seven reviewed runtime/test/document files. The repair had not yet been committed when reviewed; the final commit must contain these same reviewed contents, or subsequent differences need separate review.

## Review scope and rationale

Read the applicable repository `AGENTS.md`. The current work branch has no repository `.agents` skill directory. Inspected the full original change, including three runtime code files, new adapter tests, documentation and saved verification artifacts. Followed the affected paths into existing service confirmation validation, TaskPlan mapping, Scheduler enqueue/run/finalization/requeue/recovery, path validation, and document transaction apply/rollback code.

- Mapping creates only `document` and `host_missing` actions. Unsupported TaskPlan actions are rejected, and implementation/verification do not dispatch arbitrary Python modules or fake success.
- Exact task content is checked by Scheduler idempotent enqueue. Dependency readiness uses recorded predecessors, failures recursively freeze successors, and `max_attempts=1` prevents automatic retry of partial or unknown document writes.
- The adapter dispatches explicit IDs from its graph. Existing unrelated queued tasks are not dispatched by the adapter.
- Document execution reuses existing transactions rather than introducing another rollback engine. Transaction validation checks canonical paths, aliases, file preimages, content hashes and plan/root digests; repeated safe-target checks reject symlinks, reparse points, protected metadata and hardlinks.
- Successful output hashes are checked before continuing the same approval. Partial failures preserve transaction evidence; rollback is per document task, and preceding successful tasks are not silently rolled back.
- Scheduler success artifacts and transaction JSON supply task evidence. `host_missing` and dependent frozen states remain visibly distinct from success.

The reviewer independently compared the original saved `baseline.log` and `full.log`: all 19 FAIL/ERROR headers matched in order (14 failures and 5 errors). This was verification of saved logs, not an independent full-suite rerun. The author is separately producing cargo-enabled comparison evidence; this review does not claim those results as independently executed.

## Remaining boundaries

The confirmation mechanism remains a trusted local caller assertion, not a cryptographic signature or proof of human identity. If a caller fabricates both the approval and its supposedly independent context, that caller has crossed the documented trust boundary. Cooperative locks and validation do not establish an operating-system sandbox against malicious concurrent processes. Raw Scheduler APIs remain a separate trusted low-level interface.

New code has not been independently executed on Windows. Windows-specific locking/reparse behavior and new-code Windows validation remain outstanding; this review does not declare the whole project or cross-platform suite green.

## Status Snapshot

Phase: report. Original difference and repair independently reviewed; P1 closed for the recorded file hashes. Overall and stage progress: not-computable because no agreed progress denominator was provided. Delivery: read-only review, independent probe scripts and raw outputs; no human Gate. Next automatic work: parent verifies matching contents, archives this evidence and records the final local commit. Blocker: new-code Windows validation remains outstanding. Independent-review state: complete for listed hashes, no unresolved definite finding. Later delivery boundaries: no push/merge/release and no public-path cleanup. Continuation: use this report with the final commit containing these contents; review any later runtime changes separately.
