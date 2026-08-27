# Changelog

## 0.6.2-dev.20260818

### Added

- P6-B S2 makes lifecycle advancement fail closed on hash-bound task evidence,
  a post-evidence independent review, and current Gate/candidate bindings.
- A completed repository target now reports the next separate delivery phase
  instead of ending with an opaque terminal classification.

### Compatibility

- Retained legacy task evidence remains audit-only and cannot contribute to
  progress. Host, runtime, deployment, publication, pilot, and release remain
  separate boundaries.

## 0.6.1-dev.20260818

### Added

- P6-B gives APG a canonical active self-roadmap and lifecycle source. The
  read-only progress report now has a declared repository denominator rather
  than forcing APG itself to return `not-computable`.
- The local managed AGENTS contract, default project template, and generated
  skill require exactly one final Status Snapshot after every material success,
  failure, `BLOCK`, or `CONFIRM`, with no trailing conclusion.
- Doctor validates a present active definition's lifecycle source and exact
  run/plan bindings. Missing or invalid scope does not claim that later
  boundaries are absent.

### Compatibility

- P6-B does not convert P5-H host ownership/reload, runtime, deployment,
  publication, pilot, release, provider/network, Git, or target execution into
  APG repository progress. The active denominator is explicitly limited to the
  current self-roadmap.

## 0.6.0-dev.20260818

### Added

- P6-A adds a source-bound `ProgressDefinition` and read-only
  `ProgressSnapshot` that report separate execution and independently verified
  progress with integer basis points. A percentage is unavailable rather than
  inferred when the declared denominator or source binding is absent.
- The `progress` CLI renders canonical JSON or a fixed human Status Snapshot
  with total/current-stage progress, completed work, delivery and Gate state,
  next actions, the remaining human gate when one exists, blockers, review
  state, and later delivery boundaries.
- The pure continuous-delivery harness maps lifecycle and feedback-loop stop
  facts to bounded inspection, planning, dispatch, validation, independent
  review, reporting, and requeue recommendations without executing work or
  widening authority.
- Generated APG skill guidance now requires a Status Snapshot at every
  material checkpoint, terminal result, `BLOCK`, or `CONFIRM`.

### Compatibility

- P3-F through P3-J compact user-result schemas and the existing feedback-loop
  decision schema remain closed and unchanged. P6-A projects independent,
  read-only data from their source records.
- `DISPATCH` remains a recommendation that requires existing transaction
  authority. The harness does not create approval, execute a task, call a
  provider, access a network, control a host, or cross runtime, deployment,
  publication, pilot, or release boundaries.
- This repository candidate does not promote global APG bytes, reload or
  invoke a host, mutate a target project, access providers or networks, deploy,
  publish, pilot, or release.

## 0.5.0-dev.20260816

### Added

- P5-A adds the deterministic offline specification-convergence controller for
  prioritized clarification, requirements-quality checklists, cross-artifact
  plan analysis, explicit prompt routing, and bounded convergence.
- Beginner-facing aliases `/plan`, `/clarify`, `/checklist`, `/analyze`,
  `/converge`, and `/implement` now have an explicit authority contract.
- Planning aliases proceed automatically. A safe bounded `/implement` route may
  self-authorize local reversible work without redundant approval; consequential
  work remains `CONFIRM`, and incomplete or unsafe work is `BLOCK`.
- The generated skill package allows implicit invocation so ordinary beginners
  can reach APG without manually naming the skill. Invocation does not grant
  write authority.
- The deterministic development candidate version is
  `0.5.0-dev.20260816`; the accepted `0.4.0-dev.20260814` candidate remains
  unchanged.

### Compatibility

- P3-E `AUTO`, `RECOMMEND`, `CONFIRM`, and `BLOCK` semantics remain the action
  authority source. P5-A only removes redundant routine interruptions on the
  explicit bounded local route.
- Exact-root binding, bounded scope, Gates, rollback, evidence, compare-and-
  swap, phase isolation, host integration, target execution, runtime,
  deployment, publication, pilot, and release remain separate boundaries.
- P5-A performs no global promotion, Codex reload or invocation, target
  execution, deployment, pilot, GitHub publication, tag, or formal release.

## 0.4.0-dev.20260813

### Added

- P3-A through P3-J repository capabilities and the aligned `RPD.md` are now
  included in the development snapshot package.
- The package records the repository-only close boundary: host integration,
  concrete target execution, runtime, deployment, promotion, pilot, tag,
  GitHub Release, and formal release remain separate transactions.

### Compatibility

- The immutable `v0.3.0` package and tag are unchanged.
- This GitHub `main` snapshot does not perform global installation, host reload,
  provider access, downstream execution, deployment, pilot, or formal release.

## 0.4.0-dev.20260814

### Fixed

- Align the source and packaged CLI version with the package `VERSION` file and
  `MANIFEST.json`; all now report `0.4.0-dev.20260814`.
- Retain the first P4-2B2 GitHub publication attempt as a pre-push block: the
  version mismatch was found before commit or push, so no inconsistent public
  bytes were published.

### Added

- P4-2A selects the official Codex App package `26.803.10989.0` as the first
  host while keeping Codex++, `CursorVIP_Dev`, and optional plugins outside the
  APG core-host boundary.
- P4-2B0 adds the adaptive `NONE`, `ROUTINE`, `MODERATE`, `HIGH`, and `CRITICAL`
  router, adopted-project Doctor entry, unadopted-project audit entry, and
  `allow_implicit_invocation: false`.
- P4-2B1 promotes the manifest-bound APG package and only the APG-managed global
  routing block. Disk promotion is accepted; host reload and APG invocation
  remain separate and pending.
- P4-1 adds a repository-only Host Integration Contract and Acceptance Plan
  for exact host identity, source and installed-byte binding, reload evidence,
  bounded APG invocation, optional provider/network classification,
  first-failure-stop behavior, independent review, and compare-and-swap
  rollback.
- P4-1 remains `planned`: no host was selected or changed, no plugin was
  installed or promoted, no process was reloaded, no APG invocation or
  provider/network call occurred, and no target project, runtime, deployment,
  pilot, release, or Git action was performed.
- `CursorVIP_Dev` remains excluded as a completed historical test carrier and
  is not a P4 host, target, pilot, or dependency.
- P3-J adds a pure Non-Invasive Target Project Orchestration Controller that
  binds exact canonical P3-I `complete` evidence to a caller-supplied redacted
  target capability/component snapshot without receiving a physical root.
- P3-J derives requirement traces, component plans, exact P3-F task lanes and
  waves, capability-preservation checks, independent review requirements, and
  `plan-ready`, `needs-evidence`, `block`, and `orchestration-accepted` states.
- Existing target capabilities default to `preserve`; an explicit change binds
  a P3-H requirement and remains a later independent target transaction.
- P3-I adds a pure Idea-to-Result Session Controller that composes exact
  canonical P3-A, P3-B, P3-C, P3-F, P3-G, and P3-H records into one resumable
  beginner-facing stage, result, and next-step projection.
- P3-I verifies the complete cross-stage digest chain and preserves `auto`,
  `recommend`, `confirm`, `needs-evidence`, `block`, and `complete` semantics;
  only an exact accepted P3-H result completes the session.
- P3-I preserves P3-A recommendations at the intent boundary and reports the
  earliest invalid stage with `planned` phase when a source chain drifts.
- P3-H adds a pure Requirement Trace and Consolidation Controller that binds
  exact completed P3-G lifecycle evidence to deterministic `REQ-*` to P3-A,
  P3-B section, task, artifact, consolidation, phase-evidence, and independent
  review records.
- P3-H requires exact task coverage, explicit write-conflict resolution,
  residual-gap registration, phase isolation, and reviewer independence before
  a combined result can be `ACCEPT`; task-level success alone is not treated as
  compatible delivery evidence.
- P3-G adds a pure Goal-to-Delivery Lifecycle Controller that binds one exact
  P3-F plan to a resumable wave cursor, task evidence, transaction-bound
  decisions and approvals, append-only checkpoint digests, consolidation
  references, ordered phase acceptance, and a compact ordinary-user result.
- P3-G lets routine `AUTO` work advance without repeated owner interruption;
  `RECOMMEND` and `CONFIRM` pause only at their explicit boundaries, while
  failed evidence stops the run and blocks dependents. Runtime, deployment,
  publication, promotion, pilot, and release acceptance remain separate phases.
- P3-F adds a deterministic Autonomous Task Orchestration controller that turns
  exact ready P3-C/P3-B evidence into a recommended task path, dependency-closed
  execution waves, compact next-task output, and one final acceptance summary.
- P3-F binds every task to exact read/write ownership, Gates, acceptance and
  rollback references, executor identity, and the P3-E action policy. It
  automatically serializes overlapping ownership instead of asking ordinary
  users to reconcile routine lane conflicts.
- P3-F evaluates independently reviewed task evidence into `ACCEPT`,
  `INCOMPLETE`, or `BLOCK`; missing evidence and executor self-review cannot be
  reported as final success.
- P3-E adds a bounded Project Materialization Apply Transaction Controller
  after P3-D. It validates the frozen P3-D preview and manifest content,
  fingerprints an explicitly supplied physical root, captures pre-state,
  performs compare-and-swap writes, records redacted result evidence, and
  performs post-hash-guarded rollback.
- P3-E makes routine governance automatic by default: a policy- and
  evidence-bound, bounded, reversible, no-secret, no-network, no-cost,
  no-credential, no-real-data local action is `AUTO`; safe alternatives are
  `RECOMMEND`; only consequential boundaries produce `CONFIRM`; unsafe or
  incomplete facts are `BLOCK`.
- `PROJECT_MATERIALIZATION.md`, the operator guide, and the policy reference
  now define the P3-E classification matrix, transaction-scoped confirmation,
  physical-root fingerprinting, compare-and-swap execution, snapshot evidence,
  and drift-safe rollback contract.
- P3-D adds a deterministic, offline Project Materialization Preview contract
  after P3-C. It binds exact canonical readiness and blueprint evidence to an
  explicit downstream proposal while remaining preview-only and
  `apply_authority=false`.
- The new `PROJECT_MATERIALIZATION.md` reference defines P3-D source binding,
  pending and blocked preview states, proposal completeness, canonical safety,
  downstream authority boundaries, and rollback requirements.
- P3-C adds a deterministic offline Implementation Readiness Resolver between
  P3-B and a later P3-D materialization-preview transaction. It binds the exact
  blueprint, canonical P2 intake, original stack candidates, Domain Pack
  registry and applicability evidence, guided-intake inputs, and explicit
  P3-A-to-P2 evidence relationship.
- P3-C recomputes stack scoring, guided-intake compatibility, applicable Domain
  Packs, professional Gate requirements, and the complete readiness result
  during parse. Its seven closed states end with
  `ready-for-materialization-preview`.
- The new `IMPLEMENTATION_READINESS.md` reference defines canonical source
  binding, first-match state precedence, fail-closed parsing, P3-D handoff,
  bounded rollback, and repository-local Gate requirements for the established
  `2026-08-09` transaction.
- P3-B adds a closed Project Blueprint Generator contract that consumes exact
  canonical P3-A evidence and derives exactly `PROJECT_BRIEF`, `PRODUCT_PLAN`,
  `UX_FLOW`, `ARCHITECTURE`, `STACK_DECISION`, `TASK_GRAPH`, `QUALITY_PLAN`, and
  `DEPLOYMENT_PLAN`.
- P3-B binds the complete P3-A source and SHA-256 digest, recomputes derived
  sections during parse, keeps recommendations as assumptions, records unknown
  stack evidence without inventing technology, and remains
  `ready_for_implementation=false`.
- The new `PROJECT_BLUEPRINT.md` reference defines the canonical JSON contract,
  deterministic Markdown projection, task-graph invariants, plan-only quality
  and deployment states, and later project-materialization boundary.
- P3-A documents a closed one-idea intake and decision-router contract that
  normalizes project type, target platform, user persona, goals, constraints,
  uncertainty, and evidence references without retaining raw prompts, PII,
  credentials, secrets, customer records, or unbounded conversation text.
- Decision routing uses exactly `AUTO`, `RECOMMEND`, and `CONFIRM`. Cost,
  production, privacy, real data, provider/network access, publication,
  deployment, irreversible external action, and materially ambiguous product
  direction always require `CONFIRM`.
- The canonical output separately exposes structured intent, necessary
  questions, recommended plan entries, automatic decisions, recommended
  decisions, confirmation-required decisions, rationale, bounded confidence,
  and evidence references.

### Compatibility

- P3-J does not replace or weaken P3-A through P3-I. Target capability records
  are caller-supplied redacted baselines, separate from P3-B product capability
  codes, and all existing target capabilities remain preserved by default.
- `orchestration-accepted` accepts only a plan and preservation evidence. Any
  capability change, target write, execution, runtime, deployment, publication,
  pilot, or release remains a separate transaction with separate authority.
- P3-I coordinates accepted source contracts without replacing or weakening
  them. P3-D/P3-E materialization, runtime, deployment, publication, promotion,
  pilot, release, and Git remain separate transactions and are never inferred
  from session progress.
- P3-H does not execute work, convert repository evidence into a later phase,
  create conflict or approval records, or widen P3-F/P3-G task authority. It
  exposes ordinary users to the combined result and next evidence only while
  retaining the full trace for review.
- P3-F reuses P3-E `AUTO`, `RECOMMEND`, `CONFIRM`, and `BLOCK` semantics and
  only tightens them for Git mutation and release. It does not weaken P3-C
  readiness, P3-B task topology, or P3-E materialization controls.
- P3-F `auto_authorized_task_ids` means only that a later exact-scope executor
  may proceed without another routine approval interruption. It is not task
  execution or downstream, runtime, deployment, publication, Git, pilot, or
  release authority.
- P3-E does not weaken P3-D preview-only semantics. A logical root locator is
  never a physical root, and automatic authorization never widens the frozen
  manifest or turns a recommendation into a write.
- P3-E does not ask ordinary users to repeat routine safe approvals. It keeps
  user interruption for external, costly, sensitive, public, irreversible, or
  materially ambiguous consequences and records the final evidence result.
- P3-D does not create or modify a downstream project. It performs no Gate,
  dependency, provider/network, runtime, deployment, publication, promotion,
  pilot, release, or Git action; `preview-ready` is not apply authority.
- P3-C does not weaken or rewrite P2, P3-A, or P3-B records. P3-A and P2 IDs
  remain independent and require explicit shared evidence; stack and guided-UX
  decisions are recomputed from source-complete inputs.
- `implementation_authority` is always `not-authorized`. Only the final state
  sets `ready_for_materialization_preview=true`, and P3-B remains
  `ready_for_implementation=false`.
- P3-C is standard-library-only, immutable, in-memory, and side-effect-free. It
  performs no materialization, Gate execution, provider/network call,
  dependency installation, runtime launch, downstream mutation, Git operation,
  deployment, publication, promotion, pilot, or release action.
- P3-B rejects `CONFIRM`, false P3-A readiness, and P2-derived routes that lack
  source-complete canonical P2 evidence. It does not weaken or replace P2 or
  P3-A records.
- P3-B is standard-library-only, immutable, in-memory, and side-effect-free. It
  creates no downstream project and performs no task, Gate, provider, network,
  dependency, deployment, publication, promotion, runtime, pilot, Git, or
  release action.
- P3-A composes above the existing P2-A intake, P2-B routing, P2-C stack,
  P2-D Domain Pack, and P2-E guided-UX contracts without replacing them.
- P3-A is standard-library-only, in-memory, and side-effect-free. It creates no
  approval and performs no filesystem write, provider/network call, dependency
  discovery, Gate selection, runtime launch, publication, deployment, global
  promotion, downstream mutation, or release action.

### Boundaries

- P3-J has no physical-root, source-content, credential, token, account-data,
  machine-identifier, or target-executor input. It performs no filesystem,
  subprocess, network, runtime, deployment, publication, pilot, release, or
  Git action, and its three authority/mutation/execution flags remain false.
- P3-I is an in-memory stage-chain and next-step controller. It performs no
  filesystem mutation, task execution, Gate execution, provider/network call,
  runtime launch, deployment, publication, promotion, pilot, release, or Git
  operation.
- P3-H is an in-memory requirement/evidence reconciliation controller. It
  performs no filesystem mutation, task execution, provider/network call,
  runtime launch, deployment, publication, promotion, pilot, release, or Git
  operation.
- P3-F is an in-memory route and acceptance-evidence controller. It performs no
  filesystem mutation, task execution, provider/network call, runtime launch,
  deployment, publication, promotion, pilot, release, or Git operation.
- A later task executor remains responsible for an exact root, allowed paths,
  pre-state, Gates, evidence capture, rollback, and phase-specific independent
  acceptance. P3-E remains the controller for bounded physical materialization.
- A P3-E controller call still requires an independently supplied physical root
  and frozen content bytes. It does not itself discover a target or prove any
  downstream, runtime, provider/network, deployment, publication, promotion,
  pilot, or release acceptance.
- A downstream P3-D apply remains a separate transaction requiring the exact
  root, manifest, pre-state, approved write set, owner approval, Gates,
  acceptance, and compare-and-swap rollback.
- P3-C readiness authorizes only consideration of a separately governed P3-D
  materialization preview. P3-D requires its own ChangeRecord, changed paths,
  baseline, approval, Gates, rollback, and acceptance; preview is not apply.
- Public publication, global promotion, host/runtime, provider/network,
  downstream pilot, deployment, and release remain separate transactions and
  cannot be inferred from a P3-C state, test, Doctor result, or Gate receipt.
- P3-B output is a plan/evidence bundle, not implementation authority, Gate
  evidence, deployment authority, public acceptance, runtime acceptance,
  downstream-pilot acceptance, or release acceptance.
- P3-A output is evidence for a later P3-B Project Blueprint Generator; it does
  not generate blueprint documents or authorize P3-B apply.
- Repository implementation, public publication, global promotion, host/runtime
  activation, provider/network acceptance, downstream pilot acceptance,
  deployment, and release remain separate facts with separate evidence.

### Verification

- P3-J acceptance requires canonical and tamper tests, complete capability
  preservation coverage, exact component/task partitioning, lane/wave reuse,
  independent-review enforcement, zero-side-effect checks, source compilation,
  full discovery, Doctor, plan-bound Gates, and independent read-only review.
  This entry does not claim those checks have passed.
- P3-I acceptance requires exact stage parsing, cross-stage digest binding,
  automatic progression, recommendation/confirmation preservation,
  needs-evidence and block propagation, canonical parser tamper tests, source
  compilation, Doctor, plan-bound Gates, and independent read-only review.
  This entry does not claim those checks have passed.
- P3-H acceptance requires canonical lifecycle and source-chain binding tests,
  exact task/artifact/consolidation coverage, conflict and residual-gap tests,
  phase-isolation and independent-review checks, parser tamper tests, source
  compilation, Doctor, plan-bound Gates, and independent read-only review. This
  entry does not claim those checks have passed.
- P3-E acceptance requires automatic, recommendation, confirmation, and block
  classification tests; transaction- and root-bound approval tests; canonical
  P3-D source and manifest checks; pre-state and pre-commit CAS drift coverage;
  snapshot integrity; post-state rollback drift; source compilation; Doctor;
  and fresh plan-bound controller Gates. This entry does not claim those Gates
  have passed.
- P3-D acceptance requires canonical round-trip and source-binding tests,
  pending and blocked proposal coverage, path/baseline/secret rejection,
  zero-side-effect evidence, Doctor, independent review, and fresh plan-bound
  `controller-focused`, `controller-compile`, and `controller-full` Gates.
- P3-C acceptance requires canonical source-binding and tamper tests, coverage
  of all seven ordered states, exact stack/Domain Pack/guided-UX recomputation,
  zero-side-effect evidence, Doctor, independent review, and fresh
  `controller-focused`, `controller-compile`, and `controller-full` Gates. This
  changelog entry does not claim those checks have passed.
- P3-B acceptance requires canonical and adversarial source-binding tests,
  eight-section closure, acyclic task-graph checks, zero-side-effect evidence,
  Doctor, and fresh plan-bound focused, compile, and full controller Gates. This
  changelog entry does not claim those checks have passed.
- Acceptance requires the P3-A canonical parser and routing matrix, mandatory
  `CONFIRM` trigger tests, compatibility tests with P2-A through P2-E, Doctor,
  focused controller tests, the full controller Gate set, and source
  compilation. This changelog entry does not claim those Gates have passed.

## 0.3.0 - 2026-08-08

### Added

- Accepted P2-D Domain Packs and P2-E guided-intake source contracts are now
  included in the repository-local installable package.
- The package now carries the canonical Domain Pack catalog, six frozen
  guided-intake examples, and the offline evaluator used for source-bound
  route, stack, question, Gate, and stop verification.

### Changed

- The source CLI, package `VERSION`, and package manifest report `0.3.0`.
- The deterministic builder collects only the approved catalog, P2-E example
  tree, and evaluator script; unrelated workspace fixtures and examples remain
  outside the package.

### Compatibility

- The six-command CLI and the top-level workspace `VERSION=1.0.003` remain
  unchanged at their respective boundaries.
- This is a repository-local package release. It does not publish to GitHub,
  promote global Codex/Claude/Cursor installations, reload a host, claim
  provider/runtime acceptance, or roll out to downstream projects.

### Verification

- P2-E source-bound independent acceptance passed all six route, stack, and
  stop cases, including the P2-C wrong-stack recommendation.
- P2-F requires byte-identical dual builds, exact manifest membership and
  SHA-256 verification, packaged CLI/evaluator smoke, a fresh 0.2.1 rollback
  tree, full Gates, Doctor, independent acceptance, and rollback rehearsal.

## 0.2.1 - 2026-07-23

### Added

- Canonical adapter dialect, Gate-contract, explicit-policy, receipt-state,
  automated-review progress, and evidence-bound regression identity controls.
- Bounded architecture graphs, source/generated cross-surface consistency,
  conservative affected-Gate planning, and Gate execution provenance.
- Plan-bound Gate execution authenticates the selected plan receipt,
  ChangeRecord, policy, architecture graph, consistency manifest, and Gate
  provenance before execution.
- Shared receipt-ledger inventory accepts at most 10,000 receipts, 1 MiB per
  receipt, and 64 MiB aggregate input, and rejects malformed, noncanonical,
  linked, hardlinked, unreadable, unstable, or oversized records.

### Changed

- A pre-existing invalid receipt ledger now fails plan-bound execution before
  any Gate and without a new receipt; ledger drift after execution starts
  retains exit `4` scope-violation and rollback semantics.
- The installable package includes six post-0.2.0 modules and all accepted P0
  and P1 controller and documentation updates.

### Compatibility

- The CLI remains the same six commands: `audit`, `init`, `adopt`,
  `plan-change`, `check`, and `doctor`.
- Legacy phase mode, absent optional architecture/consistency inputs, historical
  canonical receipts, Receipt 1.0, ChangeRecord, and evidence schemas retain
  their prior behavior.
- The workspace top-level `VERSION=1.0.003` remains a separate boundary.
- This stable-package release does not promote Codex, Claude, or Cursor, apply
  downstream Route B, install Grill, operate AniSpeak, change dependencies, or
  change Git state.

### Verification

- P1-E1.1 source acceptance completed with 356 controller tests run, 355
  passed, one existing conditional skip, and an independent `ACCEPT` verdict.
- Release promotion requires byte-identical dual builds, exact manifest
  membership and SHA-256 verification, packaged runtime smoke, stable-tree
  backup verification, and independent acceptance.

## 0.2.0 - 2026-07-17

### Added

- Optional change-scoped feedback loops for agentic coding, developer review,
  and external feedback.
- Deterministic iteration, elapsed-time, cost, failure, and no-progress budgets.
- Immutable feedback-loop sidecars and append-only receipt-chain validation.
- Proposed regression deltas in loop-aware check receipts without direct ledger
  writes.
- Approved recurring-defect persistence at
  `.governance/regressions/<fingerprint>.json` through `plan-change --apply`.
- Read-only doctor diagnostics for malformed, unsupported, missing-link, stale,
  inconsistent, and drifted regression records.

### Changed

- Route B adoption now renders substantive canonical documents and projects
  configured command gates into managed adapters.
- Managed adapter blocks can refresh across canonical policy revisions while
  preserving manual content and rejecting body drift.
- Command gates enforce project-relative working directories and isolated
  runtime environments.
- Governance contract paths and `AGENTS.md` changes require high-risk full
  verification.
- Canonical policy command argv survives receipt projection after per-scalar
  redaction while generic command and capture payloads remain opaque.

### Compatibility

- The CLI remains the same six commands: `audit`, `init`, `adopt`,
  `plan-change`, `check`, and `doctor`.
- Receipt 1.0 and ChangeRecord top-level schemas and golden canonical bytes are
  unchanged.
- Feedback loops and the regression ledger remain opt-in; unconfigured legacy
  projects keep their prior behavior.
- The workspace top-level `VERSION=1.0.003` is a separate workspace boundary
  and is unchanged by this package release.

### Verification

- Historical full-suite failures were reduced from `11 failures / 3 errors` to
  a green baseline before feature completion.
- Slice B source verification completed with `239` controller tests passing.
- Release artifacts require byte-identical dual builds and full manifest
  membership and SHA-256 verification before installation.

## 0.1.9 - 2026-07-16

- Unified bounded snapshot behavior across `init`, `plan-change`, `check`, and
  `doctor` for large repositories.
- Retained deterministic packaging and the repository-local six-command
  governance controller.
