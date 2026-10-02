---
name: opencoding
description: Cross-platform Agent AI coding planning and controlled local execution through OpenCoding.
---

# OpenCoding skill entry

For a project-level installation, copy this directory to
`.agents/skills/opencoding/` and run
`python scripts/verify_codex_skill.py --root /absolute/project --exercise`.
That check distinguishes a valid resource and local entrypoint import from an
actual host load; it cannot manufacture host-load evidence.

For a Codex-compatible local host, install the resource into an explicit
`$CODEX_HOME/skills/opencoding` with
`python scripts/install_codex_skill.py --project-root /absolute/project
--codex-home /absolute/private-codex-home --load`. The command copies only the
skill resource and reports managed-host observation separately.

Use this skill when an Agent needs to turn a plain-language coding idea into a
reviewable plan and, after explicit local confirmation, execute structured local
actions. The skill is host-independent: the host OS is an execution fact, while
the generated project's target platform is a separate input.

## Boundaries

- Start with an explicit absolute project root and a user-provided or clearly
  synthetic fixture goal.
- Clarify the goal and required answers before evaluating a plan.
- Treat `preview_session` as read-only and call `approve_preview` only for the
  exact reviewed root, targets, and diff.
- Use `LocalAgentAdapter` for the supported local actions (`write_text` and
  `python_module`). It uses `shell=False`, a bounded timeout, strict paths,
  zero external cost, and a structured receipt. `python_module` is still
  arbitrary importable Python running as the caller's same OS user; it is not a
  security sandbox and the module can initiate side effects such as network
  access. Do not describe the subprocess boundary as isolation.
- Pass an explicit `authorization` object with `approved=true`; synthetic
  fixture authorization must be labelled as synthetic and must not be reported
  as a real user confirmation.
- The adapter's `confirmation_binding` is a local integrity digest over the
  confirmation label, root, action, targets, scope, and expiry. It rejects
  cross-project or tampered records and consumes each binding once in the
  local project. It is not a signature or identity proof; a new human gate is
  still required for a new confirmation.
- Keep provider/model, network, credentials, payment, notification, deployment,
  remote Git, and publication behind a separate human Gate.
- A TaskPlan or adapter receipt proves only the recorded local action. It does
  not prove that a project is complete or that a model was used.
- Adapter execution has no automatic rollback. Use the transaction layer and
  retain its receipt when a file change needs rollback; receipts are bound to
  the canonical execution root, and a moved/copied tree is blocked. Arbitrary
  Python effects cannot be assumed reversible.

## Host, target, and agent capability

Call `LocalAgentAdapter.capabilities(target_platform=...)` and report all three
dimensions separately:

1. `host`: the OS and Python runtime executing OpenCoding;
2. `target_platform`: the project platform chosen during planning;
3. `actions`/`model`: the tools this Agent adapter can actually execute.

The adapter reports `model.available=false`; this package does not silently
pretend to have a real model integration. A host can plan a Windows, web, iOS,
Android, macOS, mini-program, or CLI target without requiring that host OS to
match, but target-specific toolchains still need their own verified adapter.

The normalized `capability_matrix` is the portable contract for these facts. It
reports `host.family`, the requested/normalized target, structured local action
effects, model/external/sandbox/process boundaries, path and timeout guards, and
`toolchain.status=unverified`. A recognized target label is planning input, not
evidence that its compiler, browser, SDK, or deployment path is available.

Run the minimal local check without project writes:

```bash
python scripts/check_agent_adapter.py --project-root /absolute/project \
  --target-platform cli
```

To exercise one disposable, synthetic `write_text` action, opt in explicitly:

```bash
python scripts/check_agent_adapter.py --project-root /absolute/project \
  --target-platform web --probe-local-write --confirm-synthetic
```

The probe uses a temporary unlinked root and reports the binding and file result;
it does not prove a managed host load, target toolchain, model, network safety,
or sandbox. `python_module` remains arbitrary same-user Python with uncontrolled
side effects.

Every completed `LocalAgentAdapter.execute` result also includes
`capability_observation`. It records the host facts, requested target label,
target-toolchain status, action type, model/external/sandbox boundary, and
managed-loader observation alongside the action receipt. This is an
observability record, not a target execution claim; target labels remain
planning inputs while `toolchain.status=unverified`.

For a repeatable non-template offline check, run:

```bash
python scripts/run_stage19_non_template.py \
  --output /absolute/project/evidence/STAGE19_NON_TEMPLATE_E2E.json
```

The check builds a small synthetic CLI budget ledger, writes its project files
through the adapter, observes a real failing `unittest`, authorizes a repair,
re-runs the test, and exercises a transaction rollback probe. Its report is
limited to that fixture; it does not prove universal Agent capability, a CLI
toolchain, a managed loader, or a real user confirmation.

## Explicit toolchain probe

The adapter does not probe tools implicitly. Use the fixed no-shell profiles
only when a bounded local check is useful:

```bash
python scripts/check_agent_adapter.py --project-root /absolute/project \
  --target-platform cli --probe-toolchain python-cli-runtime
python scripts/check_agent_adapter.py --project-root /absolute/project \
  --target-platform web --probe-toolchain node-web-runtime
```

`observed` requires every fixed version command in the selected profile to be
found and exit successfully. The result is scoped to that runtime (for
example, Node/npm runtime only); it does not prove a browser, framework, SDK,
packaging, deployment, or a complete target application. A Windows profile on
a non-Windows host remains `unverified` even if an unrelated command exists.

To repeat two distinct synthetic projects and recovery paths, run
`python scripts/run_stage20_multi_project.py --output
/absolute/project/evidence/STAGE20_MULTI_PROJECT_E2E.json`. The report keeps
CLI and Web target labels separate from the Python test runner and records
authorization rejection, test repair, and clean transaction rollback for each.

For a consolidated cross-platform contract, run:

```bash
python scripts/report_capability_contract.py \
  --output /absolute/project/evidence/STAGE21_CAPABILITY_CONTRACT.json
```

The contract reports Python and Node/npm runtime observations separately from
target execution. On a Linux host the Windows profile remains `unverified`;
the contract never infers Windows or browser support from Linux. It also keeps
model/provider/external actions and managed-loader observation explicit.

Stage21's three-project regression is available with
`python scripts/run_stage21_project_matrix.py --output
/absolute/project/evidence/STAGE21_PROJECT_MATRIX_E2E.json`. It repeats two
earlier domains and adds a meeting-reminder project, with the same failure,
repair, authorization-rejection and rollback checks.

## Acceptance status and human gates

Generate the user-readable acceptance report with:

```bash
python scripts/report_acceptance.py \
  --json-output /absolute/project/evidence/STAGE22_ACCEPTANCE_REPORT.json \
  --markdown-output /absolute/project/evidence/STAGE22_ACCEPTANCE_REPORT.md
```

Every check is explicitly `observed`, `unverified`, or `blocked`. Observed
means only the stated local evidence passed; unverified means more environment
evidence is needed; blocked means a human gate is required. The report carries
the same error codes and readable reasons emitted by the adapter, including
toolchain-observation rejection, expired/replayed authorization and external
action rejection. Synthetic confirmations are always recorded as blocked for
real-user acceptance.

Persist and resume the acceptance state when a run can be interrupted:

```python
from opencoding.acceptance_state import initialize_acceptance_state, resume_acceptance_state, require_observed
```

The state binds the report digest and every check status. On restart or repeat
resume, a changed report or status digest is rejected. `require_observed` must
run before creating an adapter authorization: `blocked` and `unverified`
raise without consuming a claim or writing a destination file.

Stage23's regression command is:

```bash
python scripts/run_stage23_state_recovery.py \
  --output /absolute/project/evidence/STAGE23_STATE_RECOVERY.json
```

It covers restart, two resumes, blocked provider/model, unverified Windows
execution, one observed local action and the three-project failure/repair/
rollback matrix.

## Python entry points

```python
from opencoding.agent_adapter import LocalAgentAdapter
from opencoding.service import create_session, submit_answer, preview_session, approve_preview, apply_approved
```

Use the normal service flow to create and answer a session, inspect the preview,
and apply only after explicit confirmation. Then use the adapter for a bounded
local action and retain its structured result. For rollback, use the transaction
receipt returned by the service/transaction layer; adapter execution is local
and does not imply remote rollback.

## CLI

```bash
python -m opencoding --root /abs/project --help
```

The CLI is one host interface to the same API. Other Agents may call the Python
API or wrap this skill entry without assuming Windows or a particular Agent
host. Real model/provider integration is intentionally a separate adapter and
approval task.

## Reusable install/discover/run contract

Run `python scripts/run_skill_contract.py --project-root /absolute/opencoding-root
--codex-home /absolute/private-codex-home --target-platform cli` from the same
source version as the runner. This installs skill resources to the named home;
it does not execute project actions. Source, home and execution roots must be
separate, unlinked absolute directories. Installation is a local write.

For a reviewed fixture, pass `--run-actions --confirm-synthetic
--execution-root /absolute/disposable-root --action-file /absolute/actions.json
--expected-action-file-sha256 <reviewed-bytes-sha256>
--expected-source-sha256 <reviewed-source-fingerprint>`. Review both the action
bytes and executable source fingerprint before execution. A missing
confirmation, changed file/source, invalid action/path or overlapping root is
rejected before destination writes.
The marker is synthetic evidence, not a real user confirmation. The two SHA
values bind the reviewed fixture and source bytes, but do not authenticate a
human or sandbox Python.

`--repeat 1..10` reruns the reviewed bytes using fresh invocation/run IDs.
Repeat is intentional re-execution, not deduplication or arbitrary-module
idempotency. Any failed action stops later actions/repeats with exit code 1;
already completed writes remain. Inspect the JSON report before continuing.
Fixes require a newly reviewed action file and its SHA. Optional
`--rollback-probe-path relative/path` creates and rolls back a new probe only.

Use `python scripts/recover_skill_transaction.py --execution-root /absolute/root
--transaction-id <receipt-id>` to inspect an existing transaction; `--rollback`
explicitly requests rollback. It only handles receipt-covered file changes.
It does not resume a partly executed arbitrary Python module or undo its effects.

## Product-loop Agent bridge (local, explicit)

For an adopted plan with `implement_feature` and `verify_feature` nodes, a
Codex Agent may prepare an exact action map and review it before dispatch:

```python
from opencoding.agent_tasks import LocalAgentTaskExecutor, preview_agent_tasks
from opencoding.product_loop import start_product_run, resume_product_run

preview = preview_agent_tasks(project_root, session_id, actions_by_task)
executor = LocalAgentTaskExecutor(
    project_root, preview,
    expected_digest=preview["preview_digest"],
    confirmation_id="synthetic-or-caller-confirmed-id",
    confirmed=True,
    synthetic=True,
)
run = start_product_run(
    project_root, session_id, human_confirmed=True,
    project_executor=executor,
)
```

`actions_by_task` is reviewed input, not generated by this bridge. Every action
must match the adopted task's outputs, and verification must run the exact
non-empty Python `unittest discover` command for its test output. The bridge
records the preview digest, skill/source hashes, authorization bindings and
receipts in the product ledger. Resume requires the same plan/root and a
freshly constructed executor when a failed action needs repair; consumed
bindings are never replayed. `transactional_write=True` covers each reviewed
file write. `rollback_product_run` can remove receipt-covered files that did
not exist at the first preview and still match the latest receipt; existing
files or identity drift stop rollback. It never claims arbitrary Python side
effects are reversible. Without an explicit
executor, the product loop remains `blocked_capability`.

For evidence-bound acceptance, pass the current run root/id/digest to
`scripts/report_acceptance.py` with `--run-root`, `--run-id`, and
`--expected-run-digest`. Without all three, local action and project skill
checks remain `unverified`; historical test logs cannot promote them.

## Target adapter contracts (Stage26)

Use `opencoding.target_adapters:get_target_adapter` for Windows, macOS, and Web
planning labels. The returned adapter is a contract boundary, not a target
runtime. `status_report()` keeps `execution.status=unverified` and
`execution.observed=false` until a target-specific executor supplies evidence;
a Node/npm or .NET/Swift version probe alone cannot promote that status.
`execute()` fails closed with `target_execution_unverified` (or
`target_capability_blocked` for malformed observations), and does not invoke a
browser, SDK, shell, deployment, model, network, or external service. The
`target_adapter` field in `LocalAgentAdapter.capabilities()` and
`capability_observation` exposes this report. A managed loader remains
`null/unobserved`, and the local Python action remains same-user with
`sandbox=false`.

`node_script` is available only for a reviewed local Web action. It invokes an
existing `node` executable with `shell=False` and a reviewed `.js`, `.mjs`,
`.cjs`, or `.ts` file under the project root. TypeScript tests must explicitly
pass `--experimental-strip-types`. The action is same-user Node code with
uncontrolled side effects and no automatic rollback; a local HTTP server must
bind only to a loopback address and shut itself down in the test. A Node/npm
runtime observation does not prove browser, framework, SDK, or deployment
execution.

## Default project entry (host Agent)

The normal host entry is `python -m opencoding project`. The host Agent keeps
open-ended understanding and writes the reviewed answer/action files; the
program owns session state, planning, document transactions, task receipts,
tests, resume and rollback. A new project can be resumed in a fresh process:

```bash
python -m opencoding project init --root /abs/new-project \
  --idea "用大白话写下项目想法"
python -m opencoding project plan --root /abs/new-project \
  --answers /abs/answers.json --answers-origin user-conversation
python -m opencoding project apply-docs --root /abs/new-project \
  --expected-digest <service_digest> --authorized-local \
  --authorization-id <caller-confirmation>
python -m opencoding project preview --root /abs/new-project \
  --actions /abs/reviewed-actions.json
python -m opencoding project run --root /abs/new-project \
  --actions /abs/reviewed-actions.json --expected-digest <preview_digest> \
  --authorized-local --authorization-id <caller-confirmation>
python -m opencoding project status --root /abs/new-project
```

`answers.json` must contain every clarification answer; the host Agent must not
silently fill missing answers. `reviewed-actions.json` is also host-Agent input:
the program validates its targets against the adopted plan, binds each action
to the root/digest/expiry/one-time local claim, and fails closed without the
required digest or authorization. `--answers-origin` and `--synthetic` are
recorded so a fixture is never reported as a live user interview. The program
does not contain a general model or open-ended code generator; when a host Agent
is unavailable, implementation tasks remain `blocked_capability`.

`status` is read-only and can be run after a restart. `run` uses a new preview
for repair after a failed test; successful outputs can be run by the user, and
`rollback` removes only receipt-covered files that were absent before the run.
Python/Node actions remain same-user subprocesses with `sandbox=false`; arbitrary
side effects are not automatically reversible. Provider, credentials, payment,
notifications, deployment, remote Git and publishing remain human gates.
