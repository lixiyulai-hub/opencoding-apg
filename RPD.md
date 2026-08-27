# RPD: Requirements Planning, Orchestration, and Delivery

## Document Control

| Field | Value |
| --- | --- |
| Document ID | `APG-RPD-001` |
| Canonical name | Requirements Planning, Orchestration, and Delivery (RPD) |
| Familiar alias | Product Requirements Document (PRD); RPD is canonical because it also binds orchestration, evidence, and delivery traceability. |
| Version | `0.6.5` |
| Effective date | `2026-08-19` |
| Change ID | `adaptive-project-governance-beginner-autonomy-single-gate-v1-20260819` |
| Approved write scope | The P6-F ChangeRecord scope: one grouped P3-A/P5-A confirmation route, exact expiring delegated authorization sessions for P3-E/P3-G, version-consistent local candidate packaging, documentation/tests, and additive evidence. Global promotion, host lifecycle, runtime, deployment, publication, pilot, and release remain separate transactions. |
| Pre-state | The P6-E accepted candidate/evidence, immutable receipts, P6-C lifecycle and P6-D roadmap, P5-H S29-S33 host evidence, and dirty state are preserved. The former P6-B definition remains at its immutable history path. |
| Approval state | Owner approval `OWNER-DIRECT-APG-BEGINNER-AUTONOMY-SINGLE-GATE-20260819-001` authorizes only the exact local P6-F source, test, documentation, candidate, and additive evidence paths. Earlier approvals remain bound to their exact sources. Global promotion, host reload or invocation, target execution, deployment, pilot, publication, and release require separate transactions. |
| Product state | P3-A through P3-J remain repository-validated and closed. P5-A retains deterministic specification convergence and beginner prompt autonomy. P6-A supplies source-bound progress and a bounded non-executing continuation planner. P6-B is historical. P6-C is the completed lifecycle denominator. P6-D is accepted local validation and program-roadmap continuation. P6-E adds an additive facade for ordinary user-result, guided-intake, P3-F, and loop-completion routes. P6-F adds one exact grouped confirmation Gate and fail-closed session reuse for already-bound local child work; arbitrary host/model prose remains a later host/adapter boundary. |
| Normative language | ASCII identifiers and field values are preferred. The product promise below is retained verbatim. |

## Product Promise

APG begins with a simple user promise: **"说出来一个想法，得到一个结果"**.

The user describes an intended outcome. APG aligns questions, turns the
result into reviewable requirements, plans work, selects evidence-bound
decisions, coordinates implementation, validates results, and prepares a
delivery path. Governance is a backend safeguard; it does not replace the
user-facing outcome.

This RPD is the single requirements truth source for that flow. It does not
replace canonical P3-A through P3-J records. Instead, it binds their
requirements, tasks, artifacts, decisions, orchestration state, and evidence
to one reviewable requirement registry.

## Scope

This RPD defines:

- one stable `REQ-*` registry for ordinary users and ordinary programmers;
- GUIDED and ENGINEERING interaction modes over the same requirement truth;
- requirement-to-plan-to-evidence traceability;
- question-led alignment, requirements-before-code, task decomposition,
  parallel execution, consolidation, gap closure, and observable acceptance;
- the authority boundary for configuration, secrets, providers, network,
  real data, cost, deployment, publication, promotion, pilot, and release;
- deterministic clarification, requirements-quality checklist, cross-artifact
  plan analysis, bounded convergence, and explicit beginner prompt aliases;
- automatic planning and bounded local implementation without redundant routine
  approval, while retaining confirmation for consequential work and blocking
  incomplete or unsafe work;
- at most one real owner Gate for simultaneous consequential reasons within one
  exact bounded transaction, followed by automatic routine orchestration;
- exact, owner-bound, UTC-expiring authorization sessions that can reuse that
  Gate only for pre-bound child transactions and reject every drift;
- source-bound total and current-stage progress that distinguishes execution
  from independent verification and never fabricates a percentage;
- Status Snapshots that state completed work, next automatic work, real human
  gates, blockers, review state, and later delivery boundaries;
- a bounded loop-harness planner for inspection, planning, dispatch,
  validation, independent review, reporting, and safe requeueing;
- separate acceptance states for repository-local validation, runtime,
  deployment, publication, pilot, and release.

## Non-Goals

This RPD does not:

- create a second PRD or a second canonical requirement truth source;
- repeat or redefine the eight P3-B blueprint sections, which remain
  authoritative in `docs/project-governance/PROJECT_BLUEPRINT.md`;
- execute a plan, create a downstream project, select or install a dependency,
  configure a provider, access a network, or perform a runtime action;
- contain raw ideas, raw conversations, raw media, transcripts, prompts, PII,
  customer records, credentials, secret values, callback material, or real
  production data;
- convert a recommendation, a generated artifact, a passing local check, or a
  dashboard into owner approval or product acceptance;
- infer total, stage, or verified progress from elapsed time, token use,
  changed files, receipts, Gate count, or an unplanned delivery boundary;
- create an unbounded autonomous execution loop or use a status projection to
  bypass transaction authority, budget, rollback, independent review, scope
  drift, or first-failure-stop controls;
- authorize a concrete P3-D/P3-E target transaction, plugin or host action,
  runtime, deployment, publication, promotion, downstream pilot, release,
  source-control action, or external irreversible action.

## One Truth Source, Two Modes

GUIDED and ENGINEERING are interaction modes, not separate products and not
separate registries. Both write and review the same stable `REQ-*` identity,
statement, decision state, acceptance criterion, phase binding, and evidence
reference. A mode can change how information is collected; it cannot bypass
owner confirmation, canonical source binding, or acceptance evidence.

| Concern | GUIDED mode | ENGINEERING mode | Shared truth rule |
| --- | --- | --- | --- |
| Starting point | One natural-language idea and desired result. | A product goal plus repository, interface, architecture, stack, compatibility, test, performance, operational, or delivery constraints. | Normalize to bounded requirement statements and evidence references; do not retain raw input as the durable record. |
| Questions | Ask only questions that could change outcome, risk, scope, or acceptance. Explain defaults in plain language. | Accept concise constraints and request only missing facts that block a decision or evidence binding. | Record each resolved fact against the same `REQ-*` item and source reference. |
| Defaults | May propose safe defaults as `RECOMMEND`. | May compare alternatives as `RECOMMEND`. | A consequential recommendation still requires confirmation; an explicit bounded local `/implement` route may select a safe `RECOMMEND` default without a redundant interruption. |
| Consequential action | Stop at `CONFIRM` and explain the impact. | Stop at `CONFIRM` with the relevant technical scope and evidence need. | Cost, external access, security, data, and delivery boundaries are identical in both modes. |
| Output | A reviewable outcome, requirements, and next decision. | A reviewable constraint set, requirements, and next decision. | The canonical output is the registry plus bound P3 records, not a mode-specific document. |

### Question-Led Alignment

1. Identify the outcome, user, success signal, and material constraints.
2. Ask only the smallest set of questions that can change the result,
   acceptance criterion, risk, or required confirmation.
3. State assumptions as `RECOMMEND`, unresolved facts as `needs-evidence`, and
   consequential decisions as `CONFIRM`.
4. Freeze the reviewable requirement set before implementation work is planned.
5. Treat later discoveries as explicit requirement updates or gap records, not
   as silent changes to an accepted requirement.

## Stable REQ Registry

The registry below is normative for this RPD. New requirements receive a new
ASCII `REQ-<three-digit>` identifier. IDs are never reused, renumbered, or
silently deleted. A superseded item remains traceable to its successor.

Each requirement has these required fields:

| Field | Meaning |
| --- | --- |
| `id` | Stable ASCII `REQ-*` identifier. |
| `category` | Bounded grouping such as product, interaction, plan, quality, security, operation, delivery, or boundary. |
| `statement` | Bounded, reviewable requirement statement; not raw user input. |
| `user_modes` | `GUIDED`, `ENGINEERING`, or both. |
| `source` | Canonical source record or bounded evidence reference; never a secret value or raw transcript. |
| `decision` | Exactly `AUTO`, `RECOMMEND`, or `CONFIRM`. |
| `priority` | `P0`, `P1`, `P2`, or `P3`. |
| `acceptance` | Observable condition that an independent reviewer can evaluate. |
| `phase_binding` | P3-A through P3-J, an independent execution phase, configured Gate, runtime acceptance, or explicit unresolved state. |
| `evidence` | Required evidence reference or `needs-evidence`; evidence does not imply acceptance outside its phase. |
| `status` | One of the Acceptance and Evidence States defined below. |

| ID | Category | Statement | User modes | Source | Decision | Priority | Acceptance | Phase binding | Evidence | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `REQ-001` | product | Preserve the APG idea-to-result promise while making the intended outcome reviewable. | GUIDED, ENGINEERING | `RPD-DOC-CTRL` | AUTO | P0 | A reviewer can locate the promise and one bounded outcome statement in the shared registry. | P3-A, P3-B, P3-I. | Canonical P3-A/P3-B bindings and P3-I session contract. | repository-validated |
| `REQ-002` | interaction | Provide GUIDED and ENGINEERING modes over the same stable requirement identities and decision states. | GUIDED, ENGINEERING | `RPD-MODES` | AUTO | P0 | Mode-specific collection produces equivalent registry fields for equivalent facts. | P2-E guided intake; P3-A/P3-C compatibility. | GUIDED evidence exists; explicit named-mode equivalence and a persisted shared registry still need evidence. | needs-evidence |
| `REQ-003` | plan | Complete requirement alignment and owner decisions before code or downstream materialization is proposed. | GUIDED, ENGINEERING | `RPD-ALIGNMENT` | CONFIRM | P0 | No implementation task is eligible while a bound P3-A decision requires owner confirmation. | P3-A, P3-B, P3-C. | Canonical decision, blueprint, and readiness contracts. | repository-validated |
| `REQ-004` | plan | Decompose accepted requirements into bounded, dependency-aware `TASK-*` records. | GUIDED, ENGINEERING | `RPD-TASKS` | RECOMMEND | P0 | Every planned task names its requirement, input digest, owner, dependency set, output artifact, and verification need. | P3-B, P3-F. | Canonical task graph, execution context, route, and wave records. | repository-validated |
| `REQ-005` | orchestration | Permit parallel agents only with disjoint write ownership, bounded inputs, and a defined consolidation owner. | GUIDED, ENGINEERING | `RPD-PARALLEL` | CONFIRM | P1 | Parallel tasks have no overlapping write path unless a consolidation record resolves the overlap. | P3-F, P3-H, P3-J. | Ownership serialization, conflict records, component lanes, and consolidation contracts. | repository-validated |
| `REQ-006` | quality | Consolidate planned outputs deterministically and register gaps rather than silently accepting incomplete work. | GUIDED, ENGINEERING | `RPD-CONSOLIDATION` | AUTO | P0 | Consolidation links each input artifact to a result, conflict decision, residual gap, and next evidence requirement. | P3-G, P3-H. | Lifecycle consolidation, conflict resolution, residual gaps, and independent review. | repository-validated |
| `REQ-007` | quality | Separate repository-local validation, runtime verification, deployment preparation, and observability evidence. | GUIDED, ENGINEERING | `RPD-VALIDATION` | CONFIRM | P0 | Evidence labels identify their phase; no later-phase claim is accepted from an earlier-phase artifact. | P3-G, P3-H, P3-I. | Phase enum, phase-scoped acceptance, and no-inference contracts; only repository validation has current execution evidence. | repository-validated |
| `REQ-008` | security | Require explicit confirmation and no-secret handling for consequential external, sensitive, or costly work. | GUIDED, ENGINEERING | `RPD-BOUNDARY` | CONFIRM | P0 | Any such action remains blocked until an independent approval binds scope, risk, evidence, and rollback. | P3-D, P3-E, P3-F, P3-G. | Authorization classification and bounded approval contracts; concrete external actions remain independently gated. | repository-validated |
| `REQ-009` | delivery | Hand P3-D materialization and downstream project creation to an independent, exact-root transaction. | GUIDED, ENGINEERING | `RPD-P3D` | CONFIRM | P0 | A P3-D proposal binds exact P3-C and blueprint digests, root, manifest, baseline, changed paths, Gates, rollback, and acceptance. | P3-D preview; P3-E apply controller. | Repository-validated preview and bounded apply-controller contracts; no concrete target transaction was run in this completion audit. | repository-validated |
| `REQ-010` | delivery | Keep runtime, deployment, publication, promotion, downstream pilot, and release as separate evidence and approval boundaries. | GUIDED, ENGINEERING | `RPD-DELIVERY-BOUNDARIES` | CONFIRM | P0 | Each boundary has its own authority, execution record, rollback, and acceptance evidence. | P3-G, P3-H, P3-I, P3-J. | Repository-validated phase-isolation contracts; later execution phases are not performed or accepted. | repository-validated |
| `REQ-011` | acceptance | Require observable acceptance evidence and independent review; reject self-assertion as final proof. | GUIDED, ENGINEERING | `RPD-EVIDENCE` | AUTO | P0 | An independent reviewer can return `ACCEPT` or `BLOCK` against the requirement and cited evidence. | P3-F, P3-G, P3-H, P3-J; configured Gates. | Independent-review contracts, plan-bound Gate receipt, and independent read-only review. | repository-validated |
| `REQ-012` | specification | Provide one deterministic offline specification-convergence controller for clarification, requirements-quality checks, cross-artifact planning analysis, and bounded iterative convergence. | GUIDED, ENGINEERING | `RPD-SPEC-DRIVEN-CONVERGENCE` | AUTO | P0 | The controller emits bounded canonical records, at most five prioritized clarification questions, explicit planning findings, and `COMPLETE`, `CONTINUE`, `CONFIRM`, or `BLOCK` without executing work or widening authority. | P5-A. | `project_governance/spec_driven_convergence.py`, focused tests, and the P5-A candidate manifest. | repository-validated |
| `REQ-013` | interaction | Let beginners use explicit `/plan`, `/clarify`, `/checklist`, `/analyze`, `/converge`, and `/implement` prompts without becoming routine approval operators. | GUIDED, ENGINEERING | `RPD-BEGINNER-AUTONOMY` | AUTO | P0 | Planning aliases are automatic; a bounded safe `/implement` route is automatic; simultaneous consequential triggers produce at most one grouped `CONFIRM`; one owner session can continue only exact pre-bound child work and expires or blocks on any drift. | P3-A, P3-E, P3-G, P5-A; later host and target phases remain independent. | Route/session regressions, generated skill contract, package manifest, and Doctor evidence. | repository-validated |
| `REQ-014` | interaction | At every material checkpoint or stop, expose completed work, source-bound total and current-stage progress, next automatic work, the remaining human gate, blocker and review state, and later delivery boundaries. | GUIDED, ENGINEERING | `RPD-CONTINUITY-STATUS` | AUTO | P0 | A read-only Status Snapshot is recomputable from declared sources and either gives integer basis points or explicitly states `not-computable`; it never hides the continuation arrangement behind a terse classification. | P6-A, P6-D, P6-E; delivery phases remain independent. | Progress projection, CLI, additive presentation facade, Sol/Terra completion contract, independent review, and candidate evidence. | repository-validated |
| `REQ-015` | orchestration | Plan continuous bounded progression without turning the owner into a routine approval relay or granting autonomous execution authority. | GUIDED, ENGINEERING | `RPD-CONTINUITY-HARNESS` | AUTO | P0 | A pure harness maps lifecycle and feedback-loop facts to inspection, plan gating, dispatch, validation, independent verification, reporting, requeueing, human gate, freeze, or complete; dispatch is only possible with separately existing authority. | P6-A; P3-F/P3-G and feedback-loop boundaries remain authoritative. | Harness projection/tests, policy reference, independent review, and candidate evidence. | repository-validated |
| `REQ-016` | progress | An adopted APG project can declare one exact current roadmap so its total and current-stage percentages remain source-bound, while historical unbound work and later delivery boundaries stay excluded. | GUIDED, ENGINEERING | `RPD-SELF-ROADMAP` | AUTO | P0 | A valid active ProgressDefinition binds one canonical lifecycle, task weights, target phase, and later phase boundaries; the final Status Snapshot identifies that scope and its continuation. | P6-B historical; P6-C lifecycle; P6-D/P6-E program roadmap; host and external delivery remain independent. | APG lifecycle and program roadmap, read-only projection, Doctor source validation, P6-D Gate/review evidence, and candidate evidence. | repository-validated |

`repository-validated` in this registry means that the APG contract and its
repository tests passed their bounded acceptance. It does not mean that a
plugin host loaded the capability, a target project executed it, or a later
runtime, deployment, publication, pilot, or release boundary passed.

## Traceability Contract

The required trace is:

```text
REQ-* -> P3-A -> P3-B section -> P3-C -> P3-F/P3-G task evidence
      -> P3-H consolidation -> P3-I session -> P3-J target plan
      -> phase-scoped acceptance evidence
```

P3-D preview and P3-E bounded materialization are a separate exact-root branch
from P3-C. They are not silently inserted into the in-memory P3-F through P3-J
chain and do not receive execution authority from that chain.

`P3-B section` is an authoritative section reference emitted by P3-B. This
RPD records the reference but deliberately does not copy, list, or redefine
P3-B's eight blueprint sections. The P3-B document remains the source for
their names, order, and semantics.

| Trace stage | Required binding | Invalid shortcut |
| --- | --- | --- |
| `REQ-*` | Stable requirement ID, versioned statement, decision state, and acceptance condition. | A prose request without a stable ID. |
| P3-A | Exact canonical `IntentDecisionResult` and digest, including unresolved owner decisions. | Treating an unconfirmed recommendation as accepted intent. |
| P3-B section | `P3B:<authoritative-section-id>` reference plus the blueprint digest. | Reconstructing a section from RPD prose or copying the P3-B schema here. |
| `TASK-*` | Task ID, parent `REQ-*`, bound source digest, inputs, output artifact, owner, dependency set, and verification target. | An agent assignment with no requirement or ownership binding. |
| Implementation artifact | Declared path or artifact ID produced only in a separately authorized execution transaction. | A generated file, agent statement, or local process treated as accepted work. |
| Consolidation | Deterministic record of inputs, conflicts, resolved outputs, residual gaps, and changed trace links. | Assuming parallel output is compatible because each agent reported success. |
| Acceptance evidence | Phase-scoped `EVID-*` reference evaluated against the `REQ-*` acceptance criterion. | A dashboard, token availability, one successful request, or deployment result alone. |

### Trace Record Shape

The following is a documentation schema, not a new runtime format:

```text
trace_id: TRACE-<id>
requirement_id: REQ-<id>
p3_a_digest: <canonical-digest-or-needs-evidence>
p3_b_blueprint_digest: <canonical-digest-or-needs-evidence>
p3_b_section_refs: [P3B:<authoritative-section-id>]
task_ids: [TASK-<id>]
artifact_refs: [ART-<id>]
consolidation_ref: CONSOLIDATION-<id>
acceptance_evidence_refs: [EVID-<id>]
status: <acceptance-and-evidence-state>
```

A trace is incomplete when any required binding is absent, digest-drifted,
unconfirmed, out of scope, or claimed by the wrong phase. Incomplete traces
remain `needs-evidence`, `pending-confirmation`, or `BLOCK`; they do not fall
back to inferred success.

## Workflow Requirements

The following workflow requirements capture the accepted product practices as
requirements. They do not authorize execution.

1. **Question alignment:** `REQ-001` through `REQ-003` require outcome-led
   questions, explicit defaults, and owner confirmation before a plan can be
   treated as ready.
2. **RPD before code:** `REQ-003` requires the canonical RPD/PRD requirement
   set and P3-A decision state before P3-B planning or later execution.
3. **Bounded plan split:** `REQ-004` requires each `TASK-*` item to have a
   bounded deliverable, dependencies, owner, acceptance target, and no implied
   execution authority.
4. **Parallel agents:** `REQ-005` permits independent tasks to proceed in
   parallel only where input scope and write ownership are disjoint.
5. **Consolidation:** `REQ-006` requires one accountable consolidator to
   reconcile artifacts, conflicts, assumptions, quality results, and trace
   links before a combined result is evaluated.
6. **Iterative gap closure:** gaps become explicit `REQ-*`, `TASK-*`, or
   `needs-evidence` entries. A later iteration cannot silently change accepted
   scope or delete prior evidence.
7. **Deployment preparation:** a plan may state prerequisites, validation
   strategy, rollback needs, and owner confirmations. Preparation is not
   deployment authority.
8. **Runtime verification:** runtime behavior is separately exercised and
   evidenced after repository-local validation, under its own approved scope.
9. **Observability:** observable signals must be named with their measurement
   scope, expected behavior, and evidence reference. Observability supplements
   product acceptance; it cannot replace it.

## Decision and External-Action Policy

### Decision Values

| Value | Allowed meaning | Prohibited meaning |
| --- | --- | --- |
| `AUTO` | Normalize bounded inputs, detect missing evidence, render traceability, and route a non-consequential next step. | Approve, spend, call a provider, access a network, mutate data, deploy, publish, or release. |
| `RECOMMEND` | Present a default, option, or bounded tradeoff with its assumptions and evidence needs. | Convert a recommendation into an owner decision or a final acceptance result. |
| `CONFIRM` | Pause once for an explicit owner decision with scope, complete consequence set, evidence need, and rollback expectation; a later exact expiring session may reuse only that bounded decision. | Infer consent from silence, a prior unrelated approval, a token, a local success, or a session with drifted source, preimage, scope, reason, expiry, or transaction. |

### Mandatory CONFIRM Boundaries

`CONFIRM` is mandatory before any of the following, in either mode:

- cost, paid service, quota consumption, or budget commitment;
- configuration that changes external behavior or security posture;
- credentials, secret provisioning, provider access, or network access;
- authentication, authorization, privacy, personal data, production data, or
  real customer data;
- database mutation, destructive change, irreversible external action, or
  materially ambiguous product direction;
- runtime launch, deployment, public publication, promotion, downstream pilot,
  release, source-control push, merge, or other public delivery action.

### No-Secret and Data Rules

- Secret values are forbidden in this RPD, a requirement statement, task,
  evidence reference, log excerpt, prompt, receipt summary, or review note.
- A record may contain only a redacted secret name, a secret capability state,
  a bounded evidence reference, `needs-evidence`, or `PENDING_USER_INPUT`.
- A secret availability claim is not provider, network, runtime, deployment, or
  product acceptance evidence.
- Configuration is represented as a decision and evidence need, never as a
  copied live configuration value.
- Real, production, personal, or customer data requires an independent
  `CONFIRM` decision and its own bounded handling plan before use.

## Independent Delivery Boundaries

The boundaries below are intentionally independent. Passing an earlier row
does not satisfy a later row.

| Boundary | Required transaction | Minimum binding | Current state |
| --- | --- | --- | --- |
| P3-D materialization-preview capability | Completed P3-D repository ChangeRecord. Preview remains distinct from apply. | Exact P3-C result and blueprint digests, logical root, manifest, baseline, changed paths, approvals, Gates, rollback, and acceptance. | `repository-validated` |
| P3-E bounded apply-controller capability | Completed P3-E repository ChangeRecord plus P6-F session contract. | Exact P3-D preview, manifest bytes, physical-root fingerprint, pre-state, authorization class, exact expiring session or owner approval, snapshot, post-state, and rollback. | `repository-validated`; no concrete target transaction was run in this completion audit. |
| P3-F through P3-J orchestration capability | Completed independent repository ChangeRecords plus P6-F session contract. | Canonical source chain, task ownership, one-wave lifecycle session evidence, consolidation, capability preservation, and independent review. | `repository-validated` |
| P4-1 host-integration contract | Approved five-path documentation ChangeRecord. | Exact host identity, source and installed-byte evidence, reload state, bounded invocation, optional provider/network authority, rollback, and independent review. | `planned`; contract only, with zero host action. |
| P4-2A first-host selection | Approved two-path host-selection ChangeRecord. | `OpenAI.Codex` package `26.803.10989.0`, current process observation, installed APG `0.3.0` manifest, candidate `0.4.0-dev.20260813` manifest, exclusions, and zero-action evidence. | `planned`; Codex App selected, with zero global or host action. |
| P4-2B0 adaptive routing | Approved five-path repository ChangeRecord. | Canonical skill routing table, Doctor-versus-audit entry, five severity levels, initial disabled implicit invocation, deterministic candidate package, focused tests, and zero global action. | `repository-validated`; routing candidate built, with zero Codex global or host action. P5-A is the successor repository contract for beginner prompt aliases and bounded implicit invocation. |
| P4-2B1 installed bytes and managed router | Approved exact-global-scope promotion ChangeRecord. | Complete installed skill and global `AGENTS.md` preimages, candidate manifest, pyc rollback disposition, same-volume staging, CAS guards, backup, and post-state verification. | `disk-promotion-accepted`; APG bytes and only the APG-managed router block were promoted, with `host_reload=false` and `host_invocation=false`. |
| P4-2B2 GitHub publication preflight | Approved GitHub-only publication ChangeRecord. | Exact remote ancestry, candidate manifest, public extras, line-ending control, staged-byte validation, rollback, and post-push readback. | `BLOCK` before commit and push because packaged CLI version did not match package `VERSION` and manifest; zero GitHub bytes changed. |
| Plugin host execution | New exact-host ChangeRecord and owner approval. | Selected host product/version, installed-root and manifest, current process/window identity, action authority, reload and invocation evidence, cleanup, rollback, and host acceptance. | `not-authorized` |
| Concrete target-project execution | Independent exact-root ChangeRecord. | Target identity, approved scope, pre-state, transaction evidence, Gates, rollback, and target acceptance. | `not-authorized`; no target project is selected and no execution was performed. |
| Runtime verification | Independent runtime ChangeRecord. | Runtime scope, process ownership, configuration evidence, bounded interaction, observability, rollback, and runtime acceptance. | `not-authorized` |
| Deployment | Independent deployment ChangeRecord. | Target class, manifest, approvals, rollback, deployment evidence, and deployment acceptance. | `not-authorized` |
| Publication | Independent publication ChangeRecord. | Public surface, content scope, confirmation, publication evidence, and rollback or takedown plan. | `not-authorized` |
| Promotion | Independent promotion ChangeRecord. | Source version, target population, eligibility evidence, rollback, and promotion acceptance. | `not-authorized` |
| Downstream pilot | Independent pilot ChangeRecord. | Exact downstream root, pilot cohort, real-data decision, runtime evidence, observability, rollback, and pilot acceptance. | `not-authorized` |
| Release | Independent release ChangeRecord. | Version, release scope, evidence closure, rollback, downstream status, and release acceptance. | `not-authorized` |

`CursorVIP_Dev` was used only as a test carrier for APG requirement and safety
evidence. Those tests are complete for this milestone. It is not a delivery
target or a continuing dependency, and APG will not read, write, start,
configure, deploy, release, or otherwise change it under this closeout.

The following do not independently prove product acceptance: one-round
generation, an agent self-report, a passing localhost interaction, deployment
completion, secret availability, provider availability, or an observability
dashboard. Each can be a bounded evidence input only when the appropriate
future phase defines and evaluates it.

## Acceptance and Evidence States

| State | Meaning | Minimum evidence |
| --- | --- | --- |
| `specified` | Requirement is defined in the registry with an observable acceptance criterion. | RPD registry row. |
| `PENDING_USER_INPUT` | An owner decision or missing bounded fact is required. | Explicit question, impact, and required decision. |
| `pending-confirmation` | A `CONFIRM` action is known but not authorized. | Scoped confirmation request. |
| `needs-evidence` | A decision or acceptance claim lacks required evidence. | Missing-evidence statement and next evidence requirement. |
| `planned` | Bound P3-A/P3-B/P3-C evidence supports a later proposal only. | Canonical source digests and task trace. |
| `repository-validated` | Configured repository-local Gates passed in the approved transaction. | Gate receipts and independent review. |
| `not-performed` | A capability boundary exists, but no execution instance was attempted for this milestone. | Explicit scope decision and absence of execution authority or result. |
| `not-authorized` | A later action has not received transaction-specific authority. | New owner request and bounded ChangeRecord are required before execution. |
| `runtime-verified` | Approved runtime acceptance criteria passed. | Runtime evidence scoped to the approved runtime transaction. |
| `deployment-verified` | Approved deployment criteria passed. | Deployment evidence scoped to the deployment transaction. |
| `publication-verified` | Approved public publication criteria passed. | Publication evidence scoped to the publication transaction. |
| `pilot-accepted` | Approved downstream pilot acceptance passed. | Pilot evidence, observability, and independent acceptance. |
| `release-accepted` | Approved release acceptance passed. | Release evidence and independent acceptance. |
| `BLOCK` | Work is prohibited or cannot proceed because authority, evidence, scope, or safety conditions are missing. | Blocking reason and required successor transaction. |

Evidence is valid only for the phase and scope it names. Every acceptance claim
must connect a `REQ-*` acceptance criterion to a phase-scoped `EVID-*` record
and an independent `ACCEPT` or `BLOCK` review result. A failed or incomplete
check is retained as evidence; it is not silently reclassified as a baseline.

## Open Decisions

| ID | Decision | State | Required successor |
| --- | --- | --- | --- |
| `OD-001` | Canonical persisted representation for future RPD registry instances. | `PENDING_USER_INPUT` | Separate design and ChangeRecord; this document introduces no runtime format. |
| `OD-002` | Concrete downstream root, manifest, write set, and P3-E transaction. | `not-performed` | No successor is required for P3 closeout. A future target request requires its own exact-root ChangeRecord. |
| `OD-003` | Provider, network, configuration, credential, and cost posture. | `PENDING_USER_INPUT` | Independent scoped confirmation; no secret values belong in this RPD. |
| `OD-004` | Runtime, deployment, publication, promotion, pilot, and release success criteria. | `not-authorized` | One independent ChangeRecord per selected boundary with phase-specific acceptance evidence. |
| `OD-005` | Product success metrics and observability thresholds for a concrete downstream project. | `needs-evidence` | Later requirement update with measured baseline and owner confirmation. |
| `OD-006` | Explicit named GUIDED/ENGINEERING mode equivalence and persisted shared registry. | `needs-evidence` | A future product requirement and bounded design ChangeRecord only if this capability is requested. |
| `OD-007` | P4 host-integration acceptance contract. | `planned` | P4-1 defines the evidence and rollback contract; focused validation and independent read-only review are required, but no host execution is authorized. |
| `OD-008` | Exact first plugin host selection. | `planned` | Windows `OpenAI.Codex` version `26.803.10989.0` is selected; the evidence is selection-only and authorizes no global or host action. |
| `OD-009` | Real target-project execution after P3. | `not-authorized` | No execution was performed. A future exact-root request requires its own ChangeRecord and cannot reuse P4-1 authority. |
| `OD-010` | Promote APG `0.4.0-dev.20260814` into the selected Codex App global skill root and synchronize only the APG-managed global router block. | `disk-promotion-accepted` | P4-2B1 promoted the original candidate and only the APG-managed router block. The version-consistency correction changes candidate bytes, so one successor exact-hash global correction is required before publication continues. |
| `OD-011` | Reload the selected Codex App host after promotion. | `not-authorized` | A later reload transaction requires a fresh bindable host identity or explicit manual-restart handoff; the current read-only process observation has no bindable main window. |
| `OD-012` | Invoke APG from the reloaded Codex App and accept host integration. | `not-authorized` | A later invocation transaction must bind the promoted manifest, reloaded host session, bounded input/output evidence, cleanup, and independent review. |
| `OD-013` | Single adaptive routing policy for global invocation, skill severity, and project-specific Gates. | `repository-validated` | P4-2B0 binds `NONE`, `ROUTINE`, `MODERATE`, `HIGH`, and `CRITICAL`; P5-A permits implicit beginner-facing invocation and explicit planning aliases, while adopted projects still enter through Doctor and bounded implementation still requires controller checks. |

## P3 Completion Evidence

P3-A through P3-J repository capabilities are closed as of `2026-08-13`.
The completion decision is bounded to the repository capability milestone and
uses the following evidence:

- the prior full-suite result supplied at handoff: `665 passed, 1 skipped`;
- the prior source-compilation result supplied at handoff: 57 Python sources;
- the prior independent read-only review verdict supplied at handoff: `ACCEPT`;
- P3-J plan-bound Gate receipt
  `.governance/receipts/20260813T021223993194Z-check-f18af3762258.json`,
  SHA-256
  `d30cf961bba8b5e65b425f22afaae9c6e9548908d7292fb6591ad0abb914851e`;
- the `2026-08-13` pre-alignment Doctor result: 216 canonical receipts,
  0 invalid, with only the existing `.governance/previews` warning.

The full suite, compilation, and independent review above were not rerun during
the read-only completion audit. This alignment transaction has its own
plan-bound validation and receipt. No evidence in either transaction implies
plugin-host, target-project, runtime, deployment, publication, promotion,
pilot, release, or product acceptance.

## P4-2A Host Selection Evidence

The owner selected the Windows `OpenAI.Codex` package version
`26.803.10989.0` as APG's first host on `2026-08-14`. The bounded evidence is
stored under `outputs/host-integration/codex-app-host-selection-20260814`.

The current APG core installation remains version `0.3.0` with manifest
SHA-256 `9fd3e425199d1e9036149a3b7daf2ac3da7407529bfedaa5f1a1a6602e44bf1c`.
The proposed repository candidate is `0.4.0-dev.20260813` with manifest SHA-256
`6fedb7075fec853a2b7912ca7b840bd21a2c63d6b9cb1ebb567561eb72556bcf`.
The read-only comparison found 65 common equal files, 6 common different files,
25 candidate-only files, and 22 installed-only files. Those installed-only
files have no deletion disposition and require a separate promotion plan.

Codex++ version `1.2.47` is excluded from host ownership. The installed Sol/Luna
APG Orchestrator remains an optional plugin layer. The volatile Codex process
observation had no bindable main window, so no reload action was attempted or
authorized. This evidence does not prove installed-byte, reload, invocation,
runtime, deployment, publication, pilot, or release acceptance.

## P4-2B0 Adaptive Routing Evidence

P4-2B0 resolves the instruction-surface issue before any global promotion. The
global router is responsible only for deciding when APG is needed. The generated
`SKILL.md` owns severity and transaction routing. Project-local `AGENTS.md` files
continue to own only their exact project commands and Gates.

The generated skill now routes five levels:

| Level | Route |
| --- | --- |
| `NONE` | Skip APG for non-project chat, translation, and unrelated informational work. |
| `ROUTINE` | For a bounded adopted-project local write, run Doctor and the fast or affected check. |
| `MODERATE` | For multi-module, shared-contract, dependency, architecture, or durable-requirement work, use Doctor, `plan-change`, approval, and focused or affected checks. |
| `HIGH` | For global, host, plugin, provider, network, target mutation, runtime, deployment, publication, pilot, or release work, require an exact-scope transaction, owner approval, preimage evidence, drift guard, rollback, and independent review. |
| `CRITICAL` | Add a distinct independent verifier and fail closed for destructive, irreversible, production-data, secret, identity, payment, or uncontrolled external effects. |

Adopted projects enter through Doctor. Audit is reserved for unadopted projects,
explicit adoption, or an explicitly requested fresh audit. The generated Codex
skill metadata sets `allow_implicit_invocation: false`, preventing the skill
description from becoming a second broad trigger.

The deterministic repository candidate is
`outputs/publication/adaptive-project-governance-v0.4.0-dev.20260814`.
It contains 91 manifest-declared package files plus `MANIFEST.json` (92 actual
files) and has no undeclared extras. The superseded `0.4.0-dev.20260813`
publication directory had 96 actual files, but five publication extras
(`LICENSE`, two README files, and two diagram files) were outside its
`MANIFEST.json`; P4-2B0 does not treat those historical extras as package
membership.
P4-2B0 performs no write to the installed APG `0.3.0` root, the global
`AGENTS.md`, Codex App, any plugin, any provider, or any target project. Its
evidence is stored under `outputs/host-integration/adaptive-routing-20260814`.

## P4-2B1 Disk Promotion and P4-2B2 Preflight Evidence

P4-2B1 promoted the exact `0.4.0-dev.20260814` candidate available on
`2026-08-14` into the global APG skill root and replaced only the APG-managed
block in global `AGENTS.md`. The complete `0.3.0` installation, its 22 pyc
extras, and the full prior `AGENTS.md` remain in the same-volume rollback
transaction. The promotion receipt state is
`DISK_PROMOTION_ACCEPTED_PENDING_HOST_RELOAD`; no host reload or invocation was
performed.

The first P4-2B2 GitHub publication transaction used a fresh isolated clone and
stopped before staging, commit, or push when its package runtime check showed
that `VERSION` and `MANIFEST.json` identified `0.4.0-dev.20260814` but the
packaged CLI reported `0.4.0-dev.20260813`. The failed preflight is retained as
evidence. This P4-2B2A correction aligns source, tests, package runtime version,
RPD, and changelog, then rebuilds the deterministic candidate. Because its
manifest changes, a separate exact-hash global correction must complete before
a successor GitHub publication may proceed.

## Milestone Decision

The P3 repository-capability milestone remains closed. P4-1 remains the
repository-only host-integration contract. P4-2A selects Codex App, P4-2B0
repository-validates adaptive routing, and P4-2B1 accepts installed bytes and
the managed router block on disk. The split-version correction reopens none of
those capability decisions; it corrects their package identity evidence.

The corrected manifest requires one successor exact-hash global byte sync.
Reload, APG invocation, provider or network use, real target-project execution,
runtime acceptance, deployment, pilot, and release remain later independent
boundaries. GitHub publication remains blocked until the correction and global
sync both pass.

## Approval State

The approved P4-2B2A ChangeRecord authorizes only the repository paths in
Document Control. It authorizes the version fix, focused regression coverage,
RPD and changelog alignment, deterministic candidate rebuild, retained
candidate preimage, full validation required by the new defect, and bounded
evidence. It does not authorize global correction, host reload or invocation,
provider or target access, deployment, publication, pilot, release, push,
merge, or any action against `CursorVIP_Dev`.

Later actions have no transaction-specific approval. Their state is therefore
`not-authorized`, `PENDING_USER_INPUT`, or `BLOCK`, never inferred consent.

## Change History

| Version | Date | Change | Scope |
| --- | --- | --- | --- |
| `0.6.5` | `2026-08-19` | Group simultaneous P3-A/P5-A consequential triggers into one real human Gate, add exact UTC-expiring delegated authorization sessions for P3-E/P3-G, and preserve compact schemas while routine local work continues automatically. | P6-F repository-only source, documentation/tests, deterministic candidate, and additive evidence; no global, host, target, runtime, deployment, publication, pilot, or release action. |
| `0.6.2` | `2026-08-18` | Make Status Snapshots universal across human-readable terminal routes, expose P6-C source-bound total/current-stage progress and exact continuation, add bounded loop-harness ordering/resume semantics, and preserve the P6-B definition under an immutable history path. | P6-C repository-only source, templates, documentation/tests, candidate, and one additive history file; no global, host, target, runtime, or external action. |
| `0.6.4` | `2026-08-19` | Add the additive universal user-result presentation facade, preserve all closed P3 compact schemas, require one final source-bound Status Snapshot or explicit `not-computable` continuation, and align Sol/Terra completion guidance. | P6-E repository-only facade, roadmap reconciliation, package/version, documentation/tests, and additive evidence; no global, host, target, runtime, deployment, publication, pilot, or release action. |
| `0.6.3` | `2026-08-19` | Add a separate source-bound APG program roadmap projection with whole-program/current-stage percentages, readable successor transactions, historical S33 exclusion with fresh S34 successor, and program-aware bounded loop continuation. | P6-D repository-only projection, active roadmap binding, templates, documentation/tests, and additive evidence; no global, host, target, runtime, deployment, publication, pilot, or release action. |
| `0.5.1` | `2026-08-18` | Add the P6-B APG self-roadmap baseline, immutable historical definition, active source-bound progress definition, final-response Status Snapshot contract, and Doctor lifecycle-source validation. | P6-B repository-only roadmap/history, tests, documentation, and one local candidate; no global, host, target, runtime, or external action. |
| `0.5.0` | `2026-08-18` | Add P6-A source-bound progress definitions and snapshots, mandatory Status Snapshot reporting, read-only progress diagnostics, and a bounded non-executing loop-harness planner, with the `0.6.0-dev.20260818` repository candidate. | P6-A controller, tests, package contract, documentation, and one new candidate; no global, host, target, runtime, or external action. |
| `0.4.0` | `2026-08-16` | Add P5-A deterministic specification convergence, beginner prompt aliases, bounded automatic local implementation, consequential confirmation, incomplete-work blocking, generated implicit-invocation metadata, and the `0.5.0-dev.20260816` repository candidate. | P5-A controller, tests, package contract, documentation, and one new candidate; no global or external action. |
| `0.3.3` | `2026-08-15` | Correct the source and packaged CLI version to `0.4.0-dev.20260814`, record P4-2B1 disk promotion and the blocked pre-push P4-2B2 attempt, add a package-runtime regression assertion, and rebuild the candidate without claiming host or publication acceptance. | Five repository files, one bounded evidence directory, and the canonical candidate plus retained preimage. |
| `0.3.2` | `2026-08-14` | Add P4-2B0 adaptive routing with five severity levels, adopted-project Doctor entry, unadopted-project audit entry, disabled implicit invocation, a deterministic `0.4.0-dev.20260814` candidate, and zero global or host action. | Five approved repository paths. |
| `0.3.1` | `2026-08-14` | Select Codex App `26.803.10989.0` as APG's first host, bind installed `0.3.0` and candidate `0.4.0-dev.20260813` manifest evidence, exclude Codex++ and `CursorVIP_Dev`, and leave promotion, reload, invocation, provider/network, and later phases not-authorized. | `RPD.md` plus one bounded host-selection evidence directory. |
| `0.3.0` | `2026-08-13` | Start P4 at P4-1 with a repository-only host-integration contract covering exact identity, installed bytes, reload, invocation, provider/network classification, independent review, failure-stop, rollback, and continued `CursorVIP_Dev` exclusion; perform zero host actions. | Five approved documentation paths. |
| `0.2.0` | `2026-08-13` | Align P3-D through P3-J repository completion, close the P3 repository-capability milestone, record `REQ-002` residual evidence needs, preserve independent delivery boundaries, exclude further `CursorVIP_Dev` action, and leave P4 not started. | `RPD.md` only. |
| `0.1.0` | `2026-08-10` | Initial canonical RPD for dual-mode requirements, orchestration, traceability, and delivery boundaries. | `RPD.md` only. |

## Rollback

The P4-2B2A pre-state hashes are recorded in Document Control. The prior
canonical candidate has manifest SHA-256
`1e366a81086561f6c894607045a1b4a6be4226a259961637725e6d0258d0b937`
and seven runtime pyc extras. It is retained complete under
`outputs/publication/.p4-2b2a-version-correction-20260815/preimage` before the
corrected candidate is promoted by same-volume rename.

Rollback may restore only the five exact repository-file preimages and the
retained candidate preimage, and only while every live correction postimage
matches the recorded evidence. Any hash drift requires a new bounded
disposition.

Rollback must not restore, normalize, rewrite, delete, baseline, or otherwise
modify unrelated dirty files, prior ChangeRecords, receipts, historical
evidence, generated outputs, P3-A through P3-J records, plugins,
`CursorVIP_Dev`, target projects, or external artifacts.

## P5-A specification convergence and beginner autonomy

P5-A is the repository capability that absorbs the useful workflow shape of
specification-first development without copying or vendoring any external
tool. APG remains the authority, evidence, rollback, and consequence-boundary
controller; free-form Markdown and prompt text are never canonical authority.

The pure controller in `project_governance/spec_driven_convergence.py` exposes
five bounded surfaces:

1. `clarify_requirements` prioritizes missing acceptance, scope, boundary,
   dependency, data, and other material gaps and emits at most five canonical
   questions without retaining raw prompts, transcripts, PII, credentials, or
   unbounded prose.
2. `evaluate_requirement_checklist` checks specification quality: acceptance
   coverage, evidence binding, dependency closure, ambiguity, and boundary
   declaration. It does not claim implementation success.
3. `analyze_planning_consistency` cross-checks requirements and tasks for
   uncovered or orphan records, unknown dependencies, cycles, missing Gates or
   rollback, missing outputs, and overlapping ownership.
4. `route_prompt_execution` recognizes explicit `/plan`, `/clarify`,
   `/checklist`, `/analyze`, `/converge`, and `/implement` aliases. The first
   five are automatic planning. `/implement` can be automatic only when exact
   root, bounded write scope, readiness, Gates, rollback, reversibility,
   secret safety, and the P3-E `ActionContext` all prove a safe local path.
5. `build_convergence_plan` returns only `COMPLETE`, `CONTINUE`, `CONFIRM`, or
   `BLOCK` under a maximum eight-iteration budget. It never executes a task,
   mutates a file, calls a provider, uses a network, deploys, publishes,
   pilots, releases, or widens authority.

The beginner policy is layered rather than indiscriminately permissive:

| Route | Controller result | User experience |
| --- | --- | --- |
| Explicit planning alias | `AUTO` planning authority | Continue without redundant approval. |
| Explicit `/implement`, safe local facts complete | `AUTO` execution authority | Execute only the exact bounded reversible local work. |
| Explicit `/implement`, safe alternative only | `AUTO` with selected default | Use the safe default and record the recommendation. |
| Consequential or materially ambiguous work | One grouped `CONFIRM` | Ask once for the exact transaction-specific decision and retain every reason. |
| Missing root, scope, Gates, rollback, readiness, or secret safety | `BLOCK` | Explain the missing fact; do not guess or execute. |

This policy reduces routine approval burden for beginners without weakening
P3-E action classification, exact-root binding, write ownership, evidence,
compare-and-swap, rollback, phase isolation, or the later host/target/runtime/
deployment/publication/pilot/release boundaries. The installable skill package
sets `allow_implicit_invocation: true` so a beginner's non-trivial project
request can reach the controller; invocation is not execution authority.

When several consequential facts belong to one exact transaction, P3-A and
P5-A surface one grouped Gate with the complete canonical reason set. One
`AuthorizationSession` may then reuse that Gate only for its named child
transactions and only while owner, policy, source digest, required preimage,
scope, reason set, and UTC expiry match. It is not generic consent and cannot
cross a new root, provider/network, Git, host, runtime, deployment,
publication, pilot, or release boundary.

P5-A repository acceptance is limited to focused tests, affected package and
version tests, required project validation, deterministic candidate checks, and
Doctor. No global promotion, host reload or invocation, target execution,
deployment, pilot, publication, or formal release is implied.

## P6-A continuity, progress, and loop harness

P6-A makes the user-facing continuation explicit without treating a status
report as product acceptance or execution authority. Its source-bound
`ProgressDefinition` declares the in-scope work denominator, positive integer
weights, lifecycle source binding, exact plan identity and digest, target
delivery phase, and boundaries that are intentionally out of scope. An absent,
incomplete, malformed, or drifted definition produces `not-computable`; it
never becomes a fabricated percentage.

The derived `ProgressSnapshot` separates execution from independently verified
progress and reports total and current-stage integer basis points, task counts,
delivery and Gate state, a bounded action list, real human gate, independent
review state, stable blocker reasons, and later delivery boundaries. Blocked
work remains in the declared denominator and retains already achieved progress.
Time spent, tokens, file counts, receipt count, and Gate count are never work
weights.

The read-only `progress` command renders canonical JSON or the fixed Status
Snapshot. Every material checkpoint, terminal result, `BLOCK`, or `CONFIRM`
must report: completed work; measured progress or why it cannot be measured;
the current stage; next automatic work; one genuine human decision or
transaction gate when present; blockers; independent review; and unperformed
delivery phases. A short classification alone is not an adequate user-facing
continuation report.

The continuity harness is only a planner. Its normal route is:

```text
INSPECT -> PROGRESS -> PLAN_GATE -> DISPATCH -> VALIDATE
-> INDEPENDENT_VERIFY -> REPORT -> REQUEUE
```

`DISPATCH` does not execute or create authority. It can name only work whose
separate transaction already proves root, scope, Gates, rollback, budget, and
required review. `RECOMMEND` and `CONFIRM` pause only for their real decision
or transaction boundary. `BLOCK`, no progress, budget exhaustion, a failure
threshold, scope drift, or missing evidence produces `FREEZE`, preserves
evidence, and reports the exact resume condition. Runtime, deployment,
publication, pilot, release, host, provider/network, Git, and target actions
remain separate later transactions.

P6-A repository acceptance is limited to projection and harness tests, CLI and
Doctor diagnostics, package tests, the project Gates, deterministic candidate
verification, and Doctor. It performs no global promotion, host action, target
execution, provider/network access, runtime, deployment, publication, pilot,
or release.

## P6-C universal status, progress, and loop continuity

P6-C is the current active repository denominator at
`.governance/progress/active.json`. It declares seven equally weighted,
source-bound work packages for the universal terminal snapshot, adapter parity,
continuity/harness contract, implementation, verification, candidate delivery,
and independent acceptance. Its target is `repository-validated`; at creation
the total and current-stage execution and verified progress are `0.00%`.

The historical P6-B definition is preserved at
`.governance/progress/history/progress.apg.self-roadmap.v1.definition-57b785db849d93abfdca5a9fb0123317ce12e059c5b917541b3e9a05f76b698f.json`.
Its percentages had a deliberately narrow denominator: the seven
source-bound work packages in the named lifecycle, with `repository-validated`
as the target. It does not rewrite the past, infer completion from receipt
volume, or collapse the P5-H host boundary into this roadmap. P6-C likewise
does not infer whole-program completion. Runtime, deployment, publication,
pilot, and release remain explicit later boundaries; host ownership/reload
remains a separate external boundary.

Every material checkpoint, successful or failed final result, `BLOCK`, or
`CONFIRM` must end with exactly one `Status Snapshot` section. The snapshot is
the final section of the response, with no trailing conclusion. It must name
the current phase, completed work, total/current-stage execution and verified
progress (or the precise reason it is not computable), delivery/Gate state,
next automatic work, any real human gate, blockers/review, later boundaries,
and the exact continuation/resume condition.

## P6-D program roadmap and successor continuation

P6-D keeps the P6-C lifecycle denominator intact and adds a separate
source-bound program denominator at
`.governance/progress/apg-p6-d-program-roadmap-v1.json`. The current roadmap
records P6-C repository validation as completed, reports its whole-program and
current-program-stage percentages independently, and orders P6-D local
validation, global promotion, a fresh P5-H S34 host transaction, runtime,
deployment, publication, pilot, and release. P5-H S33 remains immutable
historical BLOCK evidence outside the denominator. Missing or drifted program
evidence is `not-computable`; the bounded loop harness may prepare a plan gate
for the next transaction but never creates authority or claims a later
delivery boundary passed.

## P6-F beginner autonomy and one bounded Gate

P6-F preserves the principle that a beginner describes an outcome and APG owns
routine planning, execution routing, and validation. It groups simultaneous
consequential P3-A decisions into one canonical confirmation bundle, without
dropping any trigger or evidence reference. P5-A likewise returns one
consequential route containing the complete reason set instead of a sequence of
approval prompts.

The canonical `AuthorizationSession` is owner-bound, non-secret, exact-scope,
single-source-digest-bound, policy-bound, preimage-bound where P3-E requires it,
and UTC-expiring. P3-E may reuse it only for explicitly listed child
transactions that share that one exact source, preimage, scope, and reason set.
P3-G additionally binds one lifecycle run, plan, wave, and `CONFIRM` task set,
and may expand it only into that wave's existing approval fields. New source,
preimage, scope, reason, policy, transaction, run, plan, wave, task, or external
boundary facts fail closed. A new checkpoint requires a live session; an expired
one can appear only in an event-identical replay of an already recorded
checkpoint. Repository acceptance of this capability does not imply global
promotion, host lifecycle, runtime, deployment, publication, pilot, or release
acceptance.
