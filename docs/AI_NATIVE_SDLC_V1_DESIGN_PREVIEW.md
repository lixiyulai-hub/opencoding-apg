# APG AI-Native SDLC V1: Local Design Preview and Slice A Plan

## Status and boundary

This is a **repository-local design preview**, not an apply record, acceptance
record, runtime execution, deployment, publication, pilot, or release.  It
implements the confirmed V1 product principles as a bounded local design:

- local-first and enabled per project;
- evidence-driven rather than inference-driven;
- external boundaries independently require confirmation; and
- existing Receipt 1.0, phase isolation, rollback evidence, and final Status
  Snapshot contracts remain authoritative.

`RPD.md`, the repository's canonical schemas, and their tests are the source
of truth.  This document does not make an external framework or vendor
documentation normative.

## V1 operating model

The V1 control plane is a pure local projection over existing canonical
records.  It creates no provider request, network call, runtime action, Git
mutation, deployment, or release action.

```text
local intent + approved change scope
  -> autonomous task plan (P3-F)
  -> goal-delivery lifecycle (P3-G)
  -> Work Item Board
  -> vertical-slice evaluation
  -> verification evidence + feedback-loop decision
  -> continuous-delivery / program-progress projection
  -> [preview | local apply candidate | frozen | one human Gate]
```

Every arrow is a canonical, hash-bindable local record.  A later apply route
must preserve the originating change ID, source references, bounded paths,
acceptance references, rollback reference, gate evidence, and Receipt 1.0
chain.  A projection is never evidence that the described operation occurred.

## Data contracts

### Work Item

`work_item_board.WorkItem` remains the durable unit of execution.  V1 adds no
parallel backlog format.  Each item must continue to bind:

| Field | V1 role |
| --- | --- |
| `task_id`, `wave_index`, `depends_on` | stable identity and dependency closure |
| `source_refs`, `slice_goal` | intent and source binding |
| `integration_surfaces`, `acceptance_refs` | verification-first test and acceptance surface |
| `rollback_ref` | reversible local change evidence |
| `state`, `next_action_code` | derived status, never agent-asserted completion |

The Work Item Board is built from the P3-F plan and, when present, P3-G
lifecycle.  `vertical_slice_evaluator` is the eligibility check: it must
report `accepted`, `blocked`, or `needs-evidence`; it cannot dispatch work.

### Agent task pack

A Slice A task pack is a **derived local input**, not a capability grant.  Its
canonical content is:

```text
change_id, task_id, wave_index, source_refs, bounded_changed_paths,
integration_surfaces, acceptance_refs, rollback_ref, dependency_task_ids,
ActionContext, input_digests, execution_performed=false
```

`ActionContext` and the P3-E readiness rules remain the authority boundary.
The pack grants only the declared repository-relative path set and read-only
evidence references.  It contains no credential, provider configuration,
network endpoint, Git command, runtime target, or deployment target.

### Verification-first evidence

Before a task becomes an apply candidate, the pack names its focused test or
Gate surfaces and its acceptance/rollback references.  The intended sequence
is: construct plan and lifecycle; project Work Item Board; evaluate vertical
slice invariants; run the declared local verification; capture redacted
`GateExecutionEvidence`; record independent review; advance P3-G only from
that evidence.  `FeedbackLoopSidecar`, `LoopRun`, and `LoopDecision` supply
bounded iteration, cost/failure/no-progress accounting, and a deterministic
stop state.  `GateExecutionEvidenceDocument` binds any local Gate capture to
the Gate contract and process result without treating a capture as authority.

## Risk, continuation, and least privilege

### Local risk routing

| Risk | Allowed V1 route | Required evidence | Stop condition |
| --- | --- | --- | --- |
| P0: documentation or pure projection | preview; bounded local apply candidate | source refs, acceptance, rollback | invalid source binding or failed validation |
| P1: deterministic local source/test change | bounded apply candidate | P3-E readiness, focused tests, independent review | failed test, drift, or missing review |
| P2: multi-file local integration | frozen until plan-bound checks close | P3-E readiness, dependency closure, focused and full local gates | failed/ambiguous Gate or rollback gap |
| P3: external, irreversible, credentialed, runtime, Git, deployment, publication, pilot, or release action | frozen; independent confirmation route only | explicit external-boundary record and applicable Gate contract | no confirmation or non-local request |

Risk is derived from the declared action and bounded paths; it is not guessed
from iteration count, model output, elapsed time, or receipt count.

### Automatic continuation and freeze

Auto-continuation is allowed only when all of these are true: a canonical Work
Item exists; its dependencies are accepted; its vertical-slice evaluation is
accepted; P3-E readiness permits the local action; the last loop decision is
`continue`; and no Gate or external boundary is pending.  The next task is
the source-derived `next_action_code` in its wave.

The control plane freezes on `blocked`, `needs-evidence`, failed validation,
receipt/plan hash drift, a feedback-loop budget/no-progress/failure stop,
unresolved independent review, a rollback gap, or any P3 route.  A freeze
returns a reason code and exact resume condition; it does not silently retry
or broaden scope.

### Least privilege

The executor receives only the task pack's declared repository-relative paths,
read-only evidence references, and the local verification command identifiers.
It may not create authority, mutate Git, use credentials, contact a provider,
run a runtime target, or cross an external delivery boundary.  Parallel tasks
are permitted only when the existing orchestration conflict checks show
non-overlapping execution contexts.

## Single human Gate data flow

There is at most one human decision per pending external transaction.  It is
outside local preview/apply and is never synthesized by the agent:

```text
validated local preview
  -> P3-E ActionContext + risk route
  -> P0/P1/P2: local evidence route (no human Gate unless declared by source)
  -> P3: freeze with gate_id, reason code, and rollback reference
  -> owner records one confirm / revise / defer / stop decision
  -> a fresh, source-bound preview recomputes eligibility
```

This Gate is distinct from acceptance: acceptance records whether local
evidence satisfies the work item's declared criteria; the Gate authorizes only
the separately declared external transaction.  Runtime, deployment,
publication, pilot, and release remain later independent boundaries and use
their existing program-roadmap transactions.

## Slice A: pure local preview contract

Slice A implements a small pure facade named `ai_native_sdlc` that composes,
but does not replace, the existing modules.  It accepts an already canonical
P3-F plan, optional P3-G lifecycle, and an ActionContext; it returns a
canonical preview containing the Work Item Board, vertical-slice evaluation,
risk route, continuation or freeze decision, and no side effect.  It neither
executes a task nor writes receipts or invokes the CLI.

### Bounded implementation files

| File | Slice A change |
| --- | --- |
| `project_governance/ai_native_sdlc.py` | new pure parser/builder/renderer for the V1 preview contract; it reuses `work_item_board`, `vertical_slice_evaluator`, and `feedback_loops` directly, while later transactions consume the existing `continuous_delivery_harness`, `gate_execution_evidence`, and `program_progress` outputs |
| `tests/project_governance/test_ai_native_sdlc.py` | canonical round-trip, P0/P1/P2/P3 routing, auto-continue, every freeze path, declared-path least privilege, one-Gate projection, and no-execution assertions |
| `docs/project-governance/AI_NATIVE_SDLC_V1.md` | normative repository capability contract after Slice A passes; derived from this preview |
| `docs/project-governance/AI_NATIVE_SDLC_V1_DESIGN_PREVIEW.md` | retain this planning rationale and update only with accepted Slice A links |

Slice A deliberately excludes CLI commands, persistent state, receipt writing,
model/provider adapters, network, runtime execution, Git operations, and all
delivery transitions.  Those require separate plan previews and, where
applicable, independent external confirmation.

### Acceptance and verification plan

1. New unit tests prove render/parse round trips and recomputation from the
   bound P3-F/P3-G source records.
2. Unit tests prove no preview can claim `execution_performed=true`, widen a
   path, bypass independent review, turn Gate evidence into authority, or
   auto-continue across P3.
3. Unit tests exercise feedback-loop stop states and confirm that each maps to
   a stable freeze reason and resume condition.
4. Run the project-required focused governance tests, full governance test
   discovery, and Python compilation gate recorded in `AGENTS.md`.
5. Preserve the original source hashes; record baseline, modified, and
   isolated-copy rollback results with the changed Slice A module left in its
   verified state.

## Reused capability mapping

| Existing capability | Slice A use |
| --- | --- |
| `autonomous_task_orchestration` | source plan, bounded task route, wave and conflict semantics |
| `goal_delivery_lifecycle` | lifecycle cursor, phase isolation, independent review and acceptance evidence |
| `work_item_board` | canonical work-unit projection |
| `vertical_slice_evaluator` | invariant-based local eligibility result |
| `feedback_loops` | bounded feedback budget and deterministic stop decision |
| `gate_execution_evidence` | redacted, contract-bound local Gate evidence model |
| `continuous_delivery_harness` | read-only continuation projection |
| `program_progress` | read-only later-boundary and single-human-Gate projection |

## Implementation sequencing

1. Add the pure Slice A schema and source-recompute builder with no I/O.
2. Add tests for canonicality, routing, privilege, freeze, and no-effect
   invariants before connecting any new public surface.
3. Add the normative capability document only after the tests establish the
   contract.
4. Run local verification and obtain independent review for the Slice A
   acceptance record.
5. Treat any CLI, persistence, provider, runtime, Git, deployment, or release
   extension as a new transaction with its own preview, Gate, and rollback.

### Slice A delivery record

Slice A is implemented by `project_governance/ai_native_sdlc.py` and covered
by `tests/project_governance/test_ai_native_sdlc.py`.  The normative capability
contract is `AI_NATIVE_SDLC_V1.md`.  The facade derives its ActionContext facts
from each bound P3-F task context; it does not accept an unbound authority
override.

## Rollback

Slice A rejection uses its hash-bound `ROLLBACK.sh` only against an isolated
evidence copy.  It removes the Slice A module, focused test, and capability
documentation from that copy while preserving all pre-existing APG sources,
receipts, lifecycle state, and program progress.  The verified modified
artifacts remain present in this repository as the formal delivery evidence.
