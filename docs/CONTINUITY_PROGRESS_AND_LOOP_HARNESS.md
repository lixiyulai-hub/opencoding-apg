# Continuity, Progress, and Loop Harness

## Purpose

P6-A prevents a material result from ending at a terse classification with no
clear continuation. It introduces a source-bound, read-only Progress Snapshot
and a pure continuity planner. They explain the work that is complete, measured
total and current-stage progress, next automatic work, any actual human gate,
blockers, review state, and later delivery boundaries without converting that
report into execution authority.

This document is normative for the P6-A/P6-C repository capability. P6-C makes
the terminal Status Snapshot and continuation contract universal while keeping
the P6-A source-bound projection and pure-planner boundaries. It does not
replace the P3-F through P3-J contracts, feedback-loop records, ChangeRecords,
or phase-scoped delivery acceptance.

## Status Snapshot

Every material checkpoint, terminal result, `BLOCK`, or `CONFIRM` MUST render
one Status Snapshot. The human form has these fixed sections:

```text
Status Snapshot
Completed work: <bounded source-derived summary>
Total progress: <execution basis points>; verified: <basis points>
Current phase: <source-bound lifecycle phase>
Lifecycle stage: <stage>; execution: <basis points>; verified: <basis points>
Next lifecycle boundary: <phase of the next source-bound task, or unavailable/not-computable>
Delivery and Gates: <delivery state>; <gate health>
Next automatic work: <immediate planner action; source task sequence when a Gate barrier is active>
Human gate: <one real decision or transaction gate, or none>
Blockers and review: <stable reason codes>; <independent-review state>
Later boundaries: <runtime/deployment/publication/pilot/release states>
Continuation: <harness state and exact resume condition>
```

The value `not-computable` is required when no valid explicit denominator or
source binding exists. The report MUST state the reason and MAY still report
source-derived task counts, delivery state, next action, and boundary state.
It MUST NOT derive a percentage from elapsed time, token use, changed files,
receipt count, Gate count, or a phase that was never declared in scope.

The canonical machine result is a JSON receipt. The human Status Snapshot is a
projection of that same source-bound result and does not become an approval,
execution record, or acceptance artifact.

The progress renderer and the CLI use the same continuation grammar. A
continuation is one compact record with `state`, `action`, `owner`,
`requires_existing_authority`, `dispatch_permitted`, and an exact
`resume_condition=resume.<stable-code>`. The renderer may show
`not-computable` percentages, but it must never replace a missing denominator
with an estimate.

When a Gate is pending, `Next automatic work` names the immediate barrier first
and retains the source task sequence after it, for example
`run-bound-validation; source_sequence=next.execute-current-wave,task.design.product`.
This makes the required validation order visible without hiding the actual next
task or suggesting that implementation has already completed.

The Status Snapshot is also the repository-controller terminal-response
contract. Every material
checkpoint, successful or failed terminal result, `BLOCK`, or `CONFIRM` MUST
end with exactly one Status Snapshot section. It MUST be the final section of
the response: no trailing classification, conclusion, or prose next step may
appear after `Continuation`.

The repository CLI can mechanically enforce this contract for its own
human-readable routes. It cannot intercept arbitrary host or model prose;
that enforcement requires a later host/adapter lifecycle transaction.

Repository-owned domain results that intentionally use compact P3 schemas may
be wrapped by `project_governance.presentation`. The facade preserves the
closed compact mapping, emits exactly one final Snapshot, and reports
`not-computable` when no declared progress source is available. Its loop view
must preserve the harness `dispatch_permitted`, `resume_condition`, and
first-failure-stop semantics; this wrapper still cannot intercept arbitrary
host/model prose.

## Program Roadmap Projection

The lifecycle denominator and the whole-program denominator are separate
measurements. When `ProgressDefinition.program_roadmap_ref` points to a
canonical `ProgramRoadmapDefinition`, the snapshot adds four lines:

```text
Program progress: <whole-program execution and verified percentages>
Program stage (current): <current program stage and stage percentages>
Immediate program transaction: <the next transaction, its stage/authority, the following stage, and one human gate if any>
Roadmap: <ordered successor transaction labels and IDs>
```

The projection also renders a `Following program stage` line between the
immediate transaction and the ordered roadmap.

Completed lifecycle work contributes only to the declared program denominator.
Historical blocked work, including an immutable failed host attempt, is
retained as evidence but excluded from that denominator and must point to a
fresh successor transaction. Missing or drifted roadmap evidence makes the
program projection `not-computable`; it never produces an estimate. The
program projection can recommend a plan gate for the next preparable
transaction, but it cannot authorize dispatch or imply that global promotion,
host, runtime, deployment, publication, pilot, or release work occurred.

## Progress Definition

The optional canonical file is `.governance/progress/active.json` unless the
read-only CLI receives an explicit contained project-relative definition path.
It MUST declare:

- a supported schema version and stable definition ID;
- one exact source lifecycle path and run ID plus plan ID/SHA-256 binding;
- target delivery phase and declared out-of-scope phase boundaries;
- a non-empty canonical work-package list;
- stable unique work-package IDs, positive integer weights, source-bound task
  or evidence IDs, and one current delivery stage for each package.

The definition is the only denominator authority. A package may enter the
denominator only when the definition explicitly gives it a positive integer
weight. Work that is blocked stays in that denominator and retains any
previously achieved work state. A definition is never generated, repaired,
normalized, or updated by `progress` or Doctor.

## Progress Snapshot

`ProgressSnapshot` is a pure, bounded, recomputable projection. It separately
reports:

- execution progress: work supported by source execution evidence;
- verified progress: work with the required independent validation or review;
- total and current-stage values in integer basis points from 0 through 10,000;
- total/current task counts and source binding;
- the declared definition ID and denominator task/weight meaning;
- delivery state, delivery phase, and Gate health;
- zero through five stable ordered next actions;
- blocker reason codes, review state, one actual human gate where present, and
  later delivery boundaries.

Verified progress MUST NOT exceed execution progress. Progress does not imply a
later phase: repository validation remains distinct from runtime, deployment,
publication, pilot, and release acceptance. Absent or invalid scope leaves
progress values null and assigns `not-computable` with a stable reason.

## Read-Only Command and Doctor

`project-governance progress <target>` loads the optional active definition,
recomputes the snapshot, plans a bounded continuation, and emits one receipt.
`--json` emits canonical receipt JSON; the ordinary form emits the fixed Status
Snapshot. A malformed human invocation emits a not-computable Status Snapshot;
a malformed invocation that requests `--json` emits one canonical `invalid`
receipt instead of an argparse document. The command is read-only and must
retain an empty changed-path proof.

Doctor treats a missing optional definition as a pass diagnostic. When a
definition is present, it validates its containment, canonical form, and source
binding without repairing, generating, or reformatting it. Invalid present
definitions are visible diagnostics rather than silent replacements.

## Bounded Continuity Harness

The harness is a planner, not a worker. Its normal arrangement is:

```text
INSPECT -> PROGRESS -> PLAN_GATE -> DISPATCH -> VALIDATE
-> INDEPENDENT_VERIFY -> REPORT -> REQUEUE
```

The primary state can also be `HUMAN_GATE`, `FREEZE`, or `COMPLETE`.

| State | Meaning | Owner | Authority rule |
| --- | --- | --- | --- |
| `INSPECT` | Read and validate declared sources or explain why progress is unavailable. | harness controller | Read-only only. |
| `PROGRESS` | Recompute the source-bound total and current-stage projection. | harness controller | Read-only only; no authority is created. |
| `PLAN_GATE` | Prepare the exact next transaction and its Gate/rollback binding. | plan owner | Does not apply it. |
| `DISPATCH` | Queue existing authorized work after fresh scope and authority checks. | authorized executor | Requires existing authority; never creates it. |
| `VALIDATE` | Run the already-bound validation route. | validator | Requires existing authority and preserves evidence. |
| `INDEPENDENT_VERIFY` | Obtain the required independent review. | independent reviewer | Must remain distinct from the executor when required. |
| `REPORT` | Render the Status Snapshot and retained evidence references. | status reporter | Read-only reporting. |
| `REQUEUE` | Present the next bounded iteration after a valid continue decision. | harness controller | Requires existing loop and transaction bounds. |
| `HUMAN_GATE` | Pause for the one genuine decision or transaction confirmation. | project owner | No routine approval request. |
| `FREEZE` | Preserve evidence and stop dispatch. | harness controller | Required on unsafe or exhausted state. |
| `COMPLETE` | Report the completed scoped result and remaining later boundaries. | status reporter | Completion does not imply later-phase acceptance. |

`RECOMMEND` and `CONFIRM` do not create an approval queue. They stop only at
the actual decision or transaction boundary. Routine inspection, projection,
reporting, planning, and requeue preparation proceed without redundant owner
interruption.

Gate validation is an ordering barrier. If Gate evidence is pending, the
primary state MUST be `VALIDATE` (or `PLAN_GATE` when a new acceptance
transaction must first be bound) before `DISPATCH`, `INDEPENDENT_VERIFY`, or
`REQUEUE` can be recommended. `dispatch_permitted` is therefore `false` on a
`VALIDATE` plan: validation may consume existing authority, but it cannot
authorize a new dispatch or imply that work was executed.

## Stop and Resume Conditions

The harness MUST use `FREEZE` when source scope is invalid, a lifecycle is
blocked, an input drifts, feedback-loop evidence reports no progress, a budget
is exhausted, a failure threshold is reached, or a required review/validation
is missing. It MUST retain the exact source references and stable reason codes.

Resumption requires a new valid source snapshot plus the separately required
decision, transaction authority, budget, scope, rollback, Gate, or review
evidence. The exact condition is state-bound and is serialized in the
continuation field:

| Primary state | Exact resume condition |
| --- | --- |
| `INSPECT` | `resume.after-progress-source-is-readable` |
| `PROGRESS` | `resume.after-source-bound-progress-is-computed` |
| `PLAN_GATE` | `resume.after-plan-gate-pass-and-authority-is-bound` |
| `DISPATCH` | `resume.after-authorized-executor-is-available` |
| `VALIDATE` | `resume.after-selected-gates-pass` |
| `INDEPENDENT_VERIFY` | `resume.after-independent-review-accepts-evidence` |
| `REPORT` | `resume.after-status-snapshot-is-recorded` |
| `REQUEUE` | `resume.after-bounded-loop-continues-without-stop-condition` |
| `HUMAN_GATE` | `resume.after-owner-decision-is-recorded` |
| `FREEZE` | `resume.after-blocker-scope-drift-or-missing-evidence-is-resolved` |
| `COMPLETE` | `resume.only-on-an-explicit-successor-transaction-or-new-scope` |

Requeueing never retries blindly and never restarts a historical host, runtime,
deployment, publication, pilot, or release action.

## Boundaries

P6-A does not execute work, modify a target, issue an approval, start a host,
call a provider, access a network, run a runtime, deploy, publish, pilot,
release, mutate Git, or promote global bytes. A report does not prove product
completion, host activation, runtime behavior, deployment, public delivery, or
release. Those actions remain their own exact-scope transactions with their own
preimages, Gates, rollback, review, and acceptance evidence.

## P6-C APG Self-Roadmap (Current)

P6-C is the current active repository denominator for universal status
continuity. `.governance/progress/active.json` has definition ID
`progress.apg.p6c.status-continuity.v1` and binds the contained
`apg-p6-c-status-continuity-v1.lifecycle.json` source. It declares seven
equally weighted work packages for the reporting/adapter contract, continuity
architecture, source binding, implementation, verification, delivery
preparation, and independent acceptance. The target is
`repository-validated`; at creation, execution and independently verified
progress are both `0.00%` and the current lifecycle stage is source-derived,
not inferred from time, receipts, changed files, or prior P6-B completion.

The active P6-C percentage describes only those seven repository work packages.
It is not a whole-program, installed-package, host, runtime, deployment,
publication, pilot, or release percentage. Those remain later delivery
boundaries and require separate transactions.

### P6-C Loop Continuation

Each terminal human-readable route reports the same bounded loop contract:
`INSPECT -> PROGRESS -> PLAN_GATE -> DISPATCH -> VALIDATE ->
INDEPENDENT_VERIFY -> REPORT -> REQUEUE`. The harness is a planner and
serializes `execution_performed=false`; it does not run a task, create
authority, or turn routine work into an approval queue. The next automatic work
and the one real human gate are derived from the current source snapshot.

## P6-B APG Self-Roadmap (Historical)

P6-B made this APG repository a consumer of the progress contract. Its immutable
definition is retained at
`.governance/progress/history/progress.apg.self-roadmap.v1.definition-57b785db849d93abfdca5a9fb0123317ce12e059c5b917541b3e9a05f76b698f.json`;
the active `.governance/progress/active.json` is now P6-C and does not bind the
P6-B scope. The historical P6-B definition binds exactly one canonical P3-G
lifecycle at `.governance/progress/apg-self-roadmap-v1.lifecycle.json`.
The denominator covers only its declared current repository work packages. It
does not backfill P3/P5 history or use elapsed time, receipts, token use, or
changed-file counts as work evidence.

The roadmap target is `repository-validated`. The resulting total and
current-stage percentages therefore describe the current APG repository work,
not host ownership/reload, runtime, deployment, publication, pilot, or release.
The latter delivery phases remain explicit later boundaries. P5-H host evidence
continues to be reported separately and is never converted into a lifecycle
percentage.

Doctor must validate a present active definition, its contained lifecycle
source, lifecycle run ID, plan ID, plan digest, and computable task scope. It
remains read-only and must never repair, normalize, or create a roadmap.

### P6-B S2 Evidence Advance

Lifecycle advancement is fail-closed. A task cannot count as executed or
verified because a file says `ACCEPT`. The accepted evidence namespace binds
each route to its exact plan digest, pre-advance lifecycle digest, executor,
Gate, acceptance, rollback, source-path hashes, and output-path hashes. One
independent review is created after all seven records and binds every record
hash plus the current `PASS` Gate artifact and local candidate tree. The older
unbound `evidence/task-*.json` records remain retained for audit but are never
used for progress.

When the repository target is reached, the Status Snapshot still names the
next delivery phase (currently `runtime-verified`) while keeping that phase a
separate transaction. This is a continuation signal, not runtime authority or
runtime acceptance.
