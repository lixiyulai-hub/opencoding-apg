---
name: adaptive-project-governance
description: Route software-project governance by project state and risk before repository writes or external actions. Use for governance diagnosis, adoption, specification planning, quality gates, global or host changes, target-project mutation, runtime, deployment, publication, pilot, or release work. Skip ordinary chat, translation, and unrelated informational requests.
---

# Adaptive Project Governance

Use this skill only for an explicitly authorized project. The global router decides
when to invoke it; project `AGENTS.md` files own project-specific commands and Gates.

## Adaptive route

| Level | Use when | Required action |
| --- | --- | --- |
| `NONE` | Non-project chat, translation, or unrelated informational work. | Skip APG. |
| `ROUTINE` | An adopted project has a bounded local write with no shared contract, dependency, external state, or delivery effect. | Read local rules and Git state, run `doctor`, then the fast or affected check. |
| `MODERATE` | Work changes multiple modules, shared behavior, a contract, dependency, architecture, or durable requirement. | Run `doctor`, prepare `plan-change`, obtain approval before `--apply`, then run focused or affected checks. |
| `HIGH` | Work touches a global directory, host or plugin, provider or network, target-project mutation, runtime, deployment, publication, pilot, or release. | Use a separate exact-scope transaction with owner approval, preimage evidence, CAS or equivalent drift guard, rollback, and independent review. |
| `CRITICAL` | Work is destructive or irreversible, or touches production data, secrets, identity, payment, or an uncontrolled external effect. | Apply the `HIGH` route plus a distinct independent verifier and fail closed on any ambiguity or drift. |

For an adopted project, enter through `doctor`; do not run `audit` again unless adoption
or a fresh audit was explicitly requested. For an unadopted project, run read-only
`audit`, then prepare `init` or additive Route B `adopt` preview as appropriate.

Preview is not authorization. Keep writes inside the approved scope, preserve unrelated
state, and never infer host, provider, runtime, deployment, publication, pilot, or release
acceptance from repository checks. Explicit `/implement` may receive bounded AUTO execution
authority only after the controller proves exact root, write scope, Gates, rollback,
reversibility, secret safety, and a safe P3-E ActionContext. Consequential work remains
`CONFIRM`; incomplete or unsafe work is `BLOCK`.

## Beginner prompt aliases

The global router may invoke this skill implicitly for a non-trivial project request.
Recognized explicit aliases are `/plan`, `/clarify`, `/checklist`, `/analyze`, `/converge`,
and `/implement`. The first five aliases receive automatic planning authority and do not
ask the owner to approve routine planning. `/implement` receives automatic execution
authority only for the bounded local route above; it cannot widen scope or skip evidence,
Gates, rollback, phase, or consequence boundaries. The controller never executes a task,
mutates a file, calls a provider, deploys, publishes, pilots, or releases by itself.

## Status snapshot and bounded continuity

At every material checkpoint, successful or failed terminal result, `BLOCK`, or
`CONFIRM`, the final user-facing response MUST end with exactly one `Status Snapshot`
section. It is the final section: no trailing classification, conclusion, or
additional next-step text may appear after it. The snapshot MUST state:
the current phase; completed work; verified and execution progress when an
approved, source-bound ProgressDefinition makes them computable; current-stage
progress; current delivery and Gate state; the next automatic work; at most one
real human decision or transaction gate; blocking reasons; independent-review
state; later delivery boundaries that remain unperformed; and the exact
Continuation/resume condition. Never fabricate a percentage from elapsed time,
token use, changed files, receipts, or Gates. If the explicit denominator or
source binding is absent, say `not-computable` and explain the missing evidence.

Use the bounded continuity loop only as a planner:
`INSPECT -> PROGRESS -> PLAN_GATE -> DISPATCH -> VALIDATE ->
INDEPENDENT_VERIFY -> REPORT -> REQUEUE`. `DISPATCH` requires already-valid
transaction authority and never creates it. `RECOMMEND` and `CONFIRM` pause
only for a genuine decision or transaction gate. `BLOCK`, no-progress, budget
exhaustion, failure threshold, scope drift, or missing evidence freeze
dispatch, preserve evidence, and report the exact resume condition. The loop
does not grant runtime, deployment, publication, pilot, release, host,
provider, network, Git, or external execution authority.

This skill does not itself authorize any global or external action. It is a local
installable framework, not a resident daemon or a promise of zero bugs.
