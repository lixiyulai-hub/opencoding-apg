# Specification-Driven Convergence

P5-A adds a deterministic, offline controller for turning a beginner's
project request into a bounded specification and a safe next action. It uses
the useful workflow shape of specification-first development, but APG remains
the canonical governance, evidence, rollback, and consequence-boundary layer.
No external tool source, prompt set, template, dependency, or trademark is
vendored.

## Contract

The implementation is `project_governance/spec_driven_convergence.py`. It has
no filesystem, subprocess, provider, network, deployment, publication, pilot,
release, or target-project side effects. All records are closed dataclasses,
bounded tuples, stable codes, and canonical JSON projections.

| Surface | Output | Purpose |
| --- | --- | --- |
| `clarify_requirements` | `ClarificationAssessment` | Select at most five questions, ordered by material impact. |
| `evaluate_requirement_checklist` | `RequirementChecklist` | Check specification quality, not implementation behavior. |
| `analyze_planning_consistency` | `PlanningConsistencyAnalysis` | Cross-check requirement and task artifacts before execution. |
| `route_prompt_execution` | `PromptExecutionRoute` | Classify explicit beginner prompt authority. |
| `build_convergence_plan` | `ConvergencePlan` | Bound iterative gap closure and its next action. |

## Clarification

Requirements contain only bounded identity, statement, acceptance, dependency,
ambiguity, boundary, and evidence codes. Missing acceptance, evidence,
dependencies, or material ambiguity produce canonical questions. Questions are
ordered by category priority and stable IDs, capped at five, and retain no raw
prompt, transcript, PII, credential, secret, or unbounded prose.

The category order is:

`acceptance`, `scope`, `boundary`, `dependency`, `data`, `integration`, `ux`,
`error`, `performance`, `terminology`, `evidence`, `other`.

An empty question set means the requirement set is ready for planning. It does
not mean implementation or delivery is accepted.

## Requirements checklist

The checklist evaluates whether a specification is sufficiently explicit to be
planned. It reports:

- missing acceptance coverage;
- missing evidence binding;
- unresolved ambiguity;
- unknown requirement dependencies; and
- an undeclared or unknown consequential boundary.

The checklist state is `pass` only when no finding remains. A pass is a
specification-quality result, not a test result or product acceptance.

## Planning analysis

The analysis does not read a target project or execute a task. It detects:

- requirements with no task coverage;
- orphan tasks;
- unknown requirement or task dependencies;
- dependency cycles;
- tasks without Gates, rollback, or output ownership; and
- overlapping output paths.

Unknown references and cycles are `block` findings. Other repairable planning
defects are `revise` findings. Implementation readiness is true only for a
clean `pass` analysis.

## Beginner prompt routing

The explicit aliases are `/plan`, `/clarify`, `/checklist`, `/analyze`,
`/converge`, and `/implement`.

The five planning aliases receive `AUTO` planning authority and do not ask a
beginner to approve routine planning. They do not receive execution authority.

`/implement` receives `AUTO` execution authority only when all of these facts
are bound:

- implementation readiness;
- exact physical project root;
- bounded write scope;
- required Gates;
- rollback;
- P3-E `ActionContext` evidence;
- reversible, secret-safe, offline, no-cost, no-credential, no-real-data local
  action; and
- no provider, network, runtime, deployment, security or privacy posture,
  Git mutation, release, irreversible action, or material ambiguity.

A safe P3-E `RECOMMEND` is selected automatically for this explicit bounded
route, so the user is not interrupted for a redundant preference. A
consequential route is one grouped `CONFIRM` route with the complete canonical
reason set, even when several consequential reasons apply. Missing root, scope,
readiness, Gates, rollback, or secret safety is `BLOCK`.

The grouped route is a request for one exact bounded transaction, not generic
consent. A later authorization session may reuse that one owner Gate only while
the pre-bound transaction IDs, one exact source digest, required preimage,
scope, reason set, policy, owner, and UTC expiry all still match. A P3-G reuse
also binds one lifecycle run, plan, wave, and `CONFIRM` task set. New or drifted
facts remain a fresh `CONFIRM` or `BLOCK` condition.

`allow_implicit_invocation: true` in the generated package only allows the
global router to invoke APG for a non-trivial project request. It never grants
write authority. Exact-root binding, scope, Gates, rollback, evidence, CAS,
phase isolation, and the existing APG action classifier remain authoritative.

## Convergence loop

`build_convergence_plan` is a pure decision function with a maximum of eight
iterations. It returns exactly one of:

| State | Meaning |
| --- | --- |
| `COMPLETE` | All acceptance and Gate evidence is passing. |
| `CONTINUE` | Bounded remediation or measurement remains and may continue automatically. |
| `CONFIRM` | A consequential decision or external authority is required. |
| `BLOCK` | A blocking fact exists or the iteration budget is exhausted. |

The controller returns next action codes only. A caller must still use the
appropriate APG transaction and project-local Gates to perform work.

## Evidence boundary

P5-A repository acceptance consists of focused unit tests, affected package and
version tests, required project governance validation, source compilation,
deterministic package comparison, manifest validation, and Doctor. Those checks
do not imply global promotion, host reload or invocation, target execution,
runtime, deployment, publication, pilot, or formal release.
