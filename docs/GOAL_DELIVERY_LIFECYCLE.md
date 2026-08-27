# P3-G Goal-to-Delivery Lifecycle

P3-G is the resumable local lifecycle controller after P3-F. It consumes one
exact canonical P3-F plan and returns a compact result for an ordinary user:

> result, current phase, and next step

The ordinary user does not need to approve every routine task or understand the
internal plan digest, wave cursor, checkpoint chain, Gate bindings, or rollback
references. The complete operator trace remains in the canonical lifecycle
record.

## State Machine

| State | Meaning | Human interruption |
| --- | --- | --- |
| `AUTO` | The current dependency-closed wave is safe, bounded, and reversible under the P3-F context. | None for routine work. |
| `RECOMMEND` | A safe choice is available but the preferred option is not an owner decision. | One task-bound decision. |
| `CONFIRM` | A consequential P3-E boundary, Git mutation, release, provider, credential, network, deployment, or materially ambiguous choice is present. | One transaction-bound approval or one exact session for the current wave. |
| `BLOCK` | Evidence, dependency, scope, digest, reviewer, rollback, or policy facts are unsafe or incomplete. | Resolve the blocker; no implicit consent. |
| `COMPLETE` | Every P3-F task has accepted evidence and consolidation trace. | Review the result, then explicitly record any next phase acceptance. |

`AUTO` is not a general permission. It is only a resumable route state after
P3-F has classified the exact task context. P3-G itself never executes an
executor, Gate process, filesystem write, provider, runtime, deployment,
publication, promotion, pilot, release, network, credential, or Git action.

## Checkpoints and Resume

`start_goal_delivery_lifecycle` requires a caller-supplied lifecycle run ID and
the canonical P3-F plan bytes. The controller records the exact P3-F plan
SHA-256 and derives one current dependency-closed wave. A caller advances the
run with `advance_goal_delivery_lifecycle` by supplying the next checkpoint
sequence, current-wave task evidence, explicit decision or approval records,
task-bound consolidation references, and optional phase acceptance records.

Each checkpoint contains a canonical event digest and a chain digest. An exact
replay of an already-applied sequence is idempotent. A changed replay, skipped
sequence, duplicate task evidence, duplicate decision or approval identifier,
stale plan digest, unknown field, duplicate JSON key, or changed previous digest
is rejected.

One owner-approved `AuthorizationSession` may be expanded into the internal
task-bound `LifecycleApproval` evidence needed by every `CONFIRM` task in one
current dependency-closed wave. The session must bind exactly one transaction,
the P3-F plan digest and policy, no P3-E physical-root preimage, the lifecycle
run ID, plan ID, wave index, exact `CONFIRM` task IDs, and the exact union of
current-wave task scopes and reason codes, the owner, and a UTC expiry. It
cannot authorize another run, a later wave, a different plan, a new reason, or
any external delivery boundary. The closed P3-G JSON schema stays unchanged:
the derived approvals and task authorization references are ordinary canonical
checkpoint evidence, and each derived approval ID commits to the complete
canonical session hash. New checkpoints require a live session. When a caller
exactly replays a session-backed checkpoint, P3-G recomputes the wave
immediately before that checkpoint and may match the original expired session
only when the rebuilt event digest is identical; it does not broaden reuse.

The controller stops at the first failed task or blocking review and marks all
dependents `BLOCK`. It never invents an approval, decision, transaction,
consolidation, or external identifier. Missing authority remains
`PENDING_USER_INPUT` through `RECOMMEND` or `CONFIRM` rather than being treated
as consent.

## Traceability

P3-F task evidence retains the executor ID, artifact/output references, Gate
references, acceptance references, rollback reference, and independent review.
P3-G adds a task-bound consolidation reference and retains all of them in the
append-only checkpoint trace. A reference is a stable code or contained
relative path; secret-shaped values and unbounded URLs are rejected.

## Phase Isolation

The lifecycle starts at `planned`. `repository-validated` can be recorded only
after the complete task route is accepted, and only with explicit
`LifecyclePhaseAcceptance` evidence whose domain exactly matches that phase.
`runtime-verified`, `deployment-verified`, `publication-verified`,
`pilot-accepted`, and `release-accepted` are separate later phases. A local
repository Gate or task result cannot infer any of those phases. Each phase
must be advanced one step at a time with its own scope and evidence references.

## User Result

`lifecycle_user_result` intentionally exposes only four fields: `status`,
`result`, `next_step`, and `phase`. Operators can inspect the canonical record
for digests, wave cursors, task status, reasons, checkpoint sequence, and
evidence bindings. This preserves governance evidence without turning a
beginner into an approval-forwarding operator.
