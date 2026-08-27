# Canonical Policy Reference

Version 1 uses `.governance/policy.toml` as the single semantic authority. Adapters are projections. UTF-8 TOML is fixed-schema and explicit-versioned; unknown major versions, unknown top-level fields, malformed values, and unsupported enum values fail closed before writes.

## Storage contract

`.governance/project.toml` is the profile; `.governance/policy.toml` is policy; `.governance/baseline.json` is accepted debt and measurements; `.governance/changes/<change-id>.json` is change control; `.governance/receipts/<timestamp>-<command>.json` is evidence; optional `.governance/architecture.graph.json` is reviewed topology evidence; optional `.governance/consistency.manifest.json` is reviewed artifact-relationship evidence; `.governance/current-state.md` is replaceable generated state.

Receipt 1.x uses exactly `schema_version`, `command`, `policy_digest`, `target_fingerprint`, `actor`, `timestamp_utc`, `authorized_scope`, `inputs`, `outputs`, `findings`, `checks`, `approvals`, `classification`, and `evidence_refs`. Findings and checks also use closed fields. Persisted receipt JSON is UTF-8 canonical JSON with one trailing newline; duplicate keys, unknown or missing fields, wrong types, unknown commands, non-UTC timestamps, and non-canonical bytes fail closed. Canonical historical receipts never expire and remain append-only evidence. CLI adoption and Doctor use the same semantic parser; Doctor additionally requires persisted history to be byte-canonical. Operation-specific freshness may require a new audit before adoption, but that does not invalidate or remove older evidence. Doctor reports receipt totals, canonical/invalid counts, and oldest/latest references without deleting or rewriting history.

The optional current-state projection is TOML-compatible Markdown and has one closed shape:

```toml
# Project Governance Current State
schema_version = "1.0"
source_receipt = ".governance/receipts/RECEIPT.json"
source_receipt_sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

[[files]]
path = ".governance/project.toml"
sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

[[files]]
path = ".governance/policy.toml"
sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
```

Paths are normalized project-relative governance evidence paths; hashes are lowercase SHA-256. The projection is bounded to 128 entries and Doctor will not hash a listed file larger than 8 MiB. Every existing project, policy, and baseline authority file must be represented. The source must be a canonical receipt under `.governance/receipts/`. Absence is valid, while stale receipt selection, missing links, malformed projection data, or digest drift warns because the projection is not an authority. Invalid underlying receipt evidence remains a deterministic Doctor failure. The controller does not generate `current-state.md` in Version 1.

Optional feedback-loop state uses
`.governance/changes/<change-id>.feedback-loop.json` for immutable loop
configuration and `.governance/regressions/<fingerprint>.json` for durable
recurring-defect records. A Ledger `1.0` record uses exactly
`extension_schema_version`, `fingerprint`, `defect_class`, `symptom_code`,
`first_seen_receipt_ref`, `last_seen_receipt_ref`, `recurrence_count`,
`permanent_assets`, `owner`, `status`, and `next_gate`. Ledger `1.1` adds
`gate_contract_digest` and `evidence_digest`; its `fingerprint` is the exact
source `candidate_id`. Permanent asset types are `test`, `eval`, `static_rule`,
`runtime_probe`, and `incident_fingerprint`. Status is `open` or `closed`; closed
records require at least one existing contained asset and `next_gate = "closed"`.
Raw feedback, prompts, logs, free-form summaries, personal data, query-bearing
URLs, absolute paths, traversal, and unknown fields are outside the schema.

### Feedback-loop progress evidence

`check --loop-run LOOP_RUN` is opt-in. The loop-run object has required fields
`change_id`, `loop_id`, `cost_units`, and `input_evidence_refs`; optional fields
are `previous_receipt_ref` and `progress_evidence`. The first run omits
`previous_receipt_ref`; a later run names the exact current receipt-chain tip.
If `progress_evidence` is present, all four of its fields are required and its
shape is closed:

```json
{
  "change_id": "change-001",
  "loop_id": "agentic-main",
  "cost_units": 1,
  "input_evidence_refs": ["spec.md"],
  "progress_evidence": {
    "metrics": [
      {"id": "quality.score", "value": 1.0}
    ],
    "acceptance": [
      {"id": "review", "status": "pass"}
    ],
    "incidents": ["incident.closed"],
    "owner_decision_code": "continue"
  }
}
```

`metrics[].id`, `acceptance[].id`, and every incident are bounded code IDs, not
free text. IDs must be unique within their arrays. Metric values are bounded
finite integers or floats; booleans, strings, NaN, and infinities are invalid.
Integral floats and signed zero are canonicalized before hashing, so `1` and
`1.0` are equivalent and `0` and `-0.0` are equivalent. Acceptance status is
exactly one of `pass`, `fail`, `inconclusive`, or `not_applicable`.
`owner_decision_code` is exactly one of `none`, `continue`, `revise`, `defer`, or
`stop`. Unknown fields, missing nested fields, duplicate IDs, unsupported status
or owner codes, and free-form text are rejected while preparing the loop run,
before any Gate process starts.

The progress fingerprint is computed from the loop type, canonical receipt
checks, normalized metrics and acceptance records, sorted incident IDs, and the
closed owner decision code. On chain load, every matching receipt must first pass
the canonical receipt loader. The controller then reads the feedback-loop input
from that receipt, treats an absent historical `progress_evidence` field as empty
metrics/acceptance/incidents plus owner code `none`, recomputes the fingerprint
from the receipt's own `checks` and `inputs`, and compares it with the stored
decision output. A present `progress_evidence` object never receives this legacy
relaxation: all four closed fields remain required. Fingerprint mismatch, a broken
link, multiple roots or tips, branching, or a cycle invalidates the chain.

`owner_decision_code` is evidence, not authority. It does not approve adoption,
change application, regression persistence, or another protected action, and it
does not change or override the computed loop `stop_state` or `next_gate`. The
controller persists only the normalized closed evidence in the canonical check
receipt; it does not persist raw feedback, logs, prompts, free-form summaries,
PII, credentials, tokens, or secrets. Progress evidence also does not change the
regression candidate or ledger identity. Proposed symptom codes continue to come
from failed or inconclusive Gate checks. Legacy Ledger `1.0` identity remains the
normalized symptom fingerprint; Ledger `1.1` identity is the separately defined
candidate identity below. Neither identity includes P0-E progress evidence.

### Regression candidate and ledger identity

A new feedback-loop check receipt keeps the legacy `symptom_codes` array and
emits this closed proposal shape alongside it:

```json
{
  "candidate_schema_version": "2.0",
  "status": "candidate",
  "symptom_codes": ["gate.unit.fail"],
  "candidates": [
    {
      "candidate_schema_version": "2.0",
      "candidate_id": "70fa95a8895226cc1aa555c63553109d1f61e8915ca76945548fdf535d350e64",
      "symptom_code": "gate.unit.fail",
      "gate_contract": {
        "schema": "regression-gate-contract-v1",
        "gate_id": "unit",
        "phase": "fast",
        "kind": "command",
        "required": true,
        "timeout_seconds": 60,
        "warning_exit_codes": [],
        "command_arg_count": 3,
        "option_keys": ["cwd"]
      },
      "gate_contract_digest": "b86001761d3f4662157748774e3dc4195d61e7a48c9cc630e47be32498f34a7b",
      "evidence_digest": "8aab5e01f91790683b71fced2b2b984e4eeab77da22fca635ca22367de8b61b8"
    }
  ]
}
```

The example evidence digest uses the safe locator `tests/test_unit.py`. The
proposal and every candidate are closed records: missing fields, unknown
fields, duplicate candidate IDs, symptom/candidate disagreement, or digest drift
fail validation. `status` is `candidate` when candidates exist and `none` when
both `symptom_codes` and `candidates` are empty. `candidate_id` is the canonical
SHA-256 identity over the normalized symptom/defect class, safe Gate contract
digest, and receipt-safe evidence digest.

Besides its fixed schema tag, the safe Gate contract projection contains only
`gate_id`, `phase`, `kind`, `required`, `timeout_seconds`, sorted unique
`warning_exit_codes`, `command_arg_count`, and sorted unique `option_keys`. It
does not copy command or argv values, option values, environment-variable names
or values, working-directory values, or other runtime configuration. Sensitive
Gate IDs or kinds are redacted in the projection before hashing. Each new check
receipt also stores an independent closed `regression_gate_contracts` snapshot.
The source receipt is valid only when every failed or inconclusive check has
exactly one candidate and each candidate's visible contract and digest match
that snapshot; a self-consistent candidate cannot substitute a forged contract.
Both generator and parser enforce at most 64 candidates and 256 Gate contract
snapshots before Receipt persistence.

The receipt-safe evidence digest covers Gate ID, phase, result status, and a
sorted unique array of normalized evidence locators. A locator is bounded UTF-8,
project-relative, traversal-free, may be a single path segment, supports common
filename punctuation and spaces, and may contain only one constrained code
locator after `:`. A general whitespace-bearing locator requires path syntax
such as a separator or filename suffix; trusted scope `changed_paths` may also
identify a bare directory containing spaces. Backslashes normalize to `/`, and
`.` normalizes to `project.root`. URLs, queries, fragments, absolute or drive
paths, duplicates, control characters, and whitespace-bearing non-path prose
are rejected. A reference that the receipt
privacy boundary redacts is replaced before hashing by a deterministic
count-based locator such as `redacted/evidence-001.ref`; the original reference
is neither hashed nor persisted. Candidate evidence never includes
`CheckResult.message`, stdout, stderr, logs, prompts, raw feedback, PII,
credentials, tokens, secrets, or file content. If a Gate supplies no evidence
references, the canonical evidence locator array is empty. P0-E metrics,
acceptance, incidents, owner decisions, and the progress fingerprint are
excluded from candidate identity.

Regression Update `1.0` remains supported with `source_receipt_ref`,
`symptom_code`, owner/status/next-gate fields, and permanent assets; it creates or
updates a Ledger `1.0` record under the legacy symptom fingerprint. Regression
Update `1.1` replaces `symptom_code` with the exact `candidate_id` selected from
the canonical source check receipt; it creates or updates a Ledger `1.1` record
whose `fingerprint` and filename are that candidate ID and whose Gate/evidence
digests match the candidate. A `1.0` update cannot modify a `1.1` record and a
`1.1` update cannot modify a `1.0` record. Historical records and receipts are
accepted in place and are never automatically migrated, rewritten, or renamed.

Doctor is read-only for both ledger versions. It verifies canonical record bytes,
the filename against the stored fingerprint, canonical source receipts, the
uniquely selected source candidate, candidate/record symptom agreement, and the
Gate contract against the source receipt's independent snapshot, plus evidence
digests recomputed from source receipt checks and safe locators. It reports
malformed, noncanonical, filename, source-candidate, Gate digest, evidence
digest, missing receipt/asset, stale evidence, and status drift; it never repairs
or migrates a record.

## Canonical TOML

```toml
schema_version = "1.0"
policy_version = "0.1.0"
level = "G1"
reasons = ["evidence:bounded-start"]
required_documents = ["AGENTS.md", "PROJECT_BRIEF.md", "ARCHITECTURE.md", "QUALITY_GATES.md", "docs/decisions/"]
adapters = []
non_baselinable_rules = ["secret.private-key", "scope.unauthorized-write", "data.integrity-corruption"]

[[gates]]
gate_id = "unit"
phase = "fast"
required = true
command = ["python", "-m", "unittest"]
timeout_seconds = 60
warning_exit_codes = []
kind = "command"
options = { cwd = "." }
```

Top-level fields are exactly `schema_version`, `policy_version`, `level`, `reasons`, `required_documents`, `adapters`, `gates`, and `non_baselinable_rules`, and every field is present in canonical Version 1 TOML. `schema_version` is `1.0`; `policy_version` is a revision string, with the Version 1 initializer default `0.1.0`; `level` is one of `G1`, `G2`, `G3`, `G4`, with evidence-based `G1` as the starting default. To represent no configured Gate in a G1 policy, use `gates = []` instead of an array-of-tables block. `required_documents` includes at least the audit-selected documents. `adapters` is a string array and has no separate enum; it may name configured projections such as `codex`, `claude-code`, `cursor`, `git`, and `github`, and only configured adapters are generated or verified. `non_baselinable_rules` lists rule IDs that cannot become accepted debt. `reasons` records evidence-explained selection and escalation.

`init` and `adopt` accept this canonical TOML through `--policy-file`. The controller loads it with the same fixed-schema loader used for `.governance/policy.toml`, then parses all Gate mappings through the canonical Gate contract. The effective floor is the maximum of the audit-selected level and any canonical embedded-policy level, while the document floor is their union. A supplied policy cannot lower either floor. `G2`, `G3`, and `G4` require at least one valid Gate before preview or apply can construct a transaction. Owner-supplied policies and non-G1 embedded policies also require at least one command Gate when projecting adapters; built-in-only policies are rejected rather than receiving an invented command. G1 may continue to use generated initialization policy or an older canonical embedded policy from an audit receipt.

The profile fields are `project_id`, `root`, `project_types`, `lifecycle`, `public_surfaces`, `data_risk`, `user_exposure`, `release_model`, `test_burden`, `operational_dependencies`, `owners`, and `evidence_refs`. Allowed profile values are project types `web`, `desktop`, `app`, `API/backend`, `automation`, `creative/content`, `hybrid`; lifecycle `active development`, `maintenance`, `migration`, `release preparation`, `archived`; data risk `none`, `internal`, `sensitive`, `regulated`, `unknown`; exposure `internal`, `limited external`, `public`, `safety/mission critical`; release model `manual`, `continuous`, `staged`, `scheduled`, `unknown`; and test burden `low`, `moderate`, `high`, `unknown`.

## Gates

Gate mappings are extensible within supported TOML scalar, array, and mapping values. The canonical gate model fields are `gate_id` (required stable string), `phase` (required string: `fast`, `full`, or `release`), `command` (string array, default `[]`), `timeout_seconds` (non-negative integer, default `60`), `required` (boolean, default `true`), `warning_exit_codes` (integer array, default `[]`), `kind` (string, default `command`), and `options` (mapping, default `{}`). The controller does not invent project commands.

Command Gates start from a fixed minimal environment rather than the ambient process environment. The inherited allowlist is `SystemRoot`, `WINDIR`, `PATH`, `TEMP`, `TMP`, `PATHEXT`, `USERPROFILE`, `APPDATA`, and `LOCALAPPDATA`; each key is included only when present in the controller environment. The three Windows user-location keys support standard-library home resolution and Python user-site discovery without synthesizing machine-specific paths. Arbitrary ambient variables remain excluded, and configured Gate environment values cannot override protected runtime-control variables.

Init/adopt receipts record the canonical policy digest, sorted Gate IDs, and a safe argv summary containing only Gate ID, phase, kind, and argument count. They also record planned documents, effective adapters, and whether policy input was `explicit`, `generated`, or `embedded`; command argument values are not copied into the receipt summary. An empty policy adapter array uses the Version 1 Codex projection default so init/adopt and Doctor evaluate the same adapter set.

Supported gate types are scope/authorized-write, schema, adapter drift, baseline ratchet, secret patterns, required evidence, forbidden path/import, lint/type/compile, unit, bug-reproduction, integration, contract, build/package, deployment, dependency, license, security, migration, reliability, provenance, reproducibility, telemetry, staged rollout, rollback readiness, approval, and numeric performance budgets. Performance gates name metric, workload, environment, variance policy, comparator, threshold, and tolerance. Metric commands emit `{"value": number, "unit": string}`. No arbitrary budget is invented without a measured baseline.

## Gate execution provenance

For an adopted target, canonical CLI `check` parses
`.governance/policy.toml`, serializes that parsed authority canonically, and
stores its SHA-256 in the receipt `policy_digest`. The authority is revalidated
against the execution snapshot before any Gate starts. A missing policy or a
policy path containing a symlink, reparse point, root-external final handle, or
change-during-read fails closed. A semantic mutation fails closed; a later target mutation remains subject to the
normal post-execution scope comparison. Both normal and scope-violation CLI
receipts retain the same bound policy digest and Gate execution evidence.

Phase remains the default execution selector. `fast` runs fast Gates, `full`
runs the cumulative fast and full set, and `release` runs the cumulative fast,
full, and release set in canonical Gate order. An adopted project may instead
select one authenticated P1-D plan through `--plan-receipt`. This option is
mutually exclusive with explicit `--phase`, accepts no raw planned-Gate list,
and cannot be combined with feedback-loop input.

The check receipt `outputs` contains a `gate_execution_evidence` object produced
and explicitly parsed under a closed schema `1.0`. Its exact outer fields are
`schema_version`, `selection_mode`, `phase`, `policy_sha256`, `performed`,
`entry_count`, and `entries`. `selection_mode` is `phase` or `plan`;
`policy_sha256` carries the canonical policy binding when one is required and
equals the canonical CLI receipt's top-level `policy_digest`.
`performed` and `entry_count` distinguish an evaluated selection from its
ordered evidence entries. One projection contains at most 256 entries; the
entire selected Gate set is rejected before execution when that bound is
exceeded.

Entries correspond one-for-one with receipt checks. Each has exactly these 18
fields:

- `check_index`, `gate_id`, `phase`, `kind`, `required`, and `status`;
- `reason_code`, `process_exit_code`, `gate_contract_sha256`, and `duration_ms`;
- `stdout_capture_sha256`, `stdout_captured_bytes`,
  `stdout_observed_bytes`, and `stdout_truncated`; and
- `stderr_capture_sha256`, `stderr_captured_bytes`,
  `stderr_observed_bytes`, and `stderr_truncated`.

`gate_contract_sha256` is computed from the full canonical Gate mapping,
including command, timeout, warning exits, and options as well as identity,
phase, kind, and required semantics. The six reason codes are exactly
`process_exited`, `process_timed_out`, `process_spawn_failed`,
`command_context_invalid`, `command_missing`, and `builtin_evaluated`.

Capture digests cover only a bounded, redacted capture's character-class shape;
they intentionally do not identify exact literal content. Raw argv,
commands, stdout, stderr, environment names or values, option values, working
directory values, file contents, and secrets are excluded from persistence.
The full Gate contract digest remains an exact equality verifier for canonical
policy semantics. Policy is public governance authority and must not contain
credentials or low-entropy secrets; known credential-bearing arguments, fields,
and literal forms are rejected before any selected Gate runs.
When no process starts, the process exit code and capture digests are nullable
and byte counts remain zero; truncation must agree with observed versus captured
bytes. Reason codes replace raw exception or process text.

This is a closed producer/parser contract for the P1-E0 projection. The generic
Receipt 1.x loader still validates the outer Receipt schema and treats `outputs`
as a JSON mapping; it does not, by itself, claim to enforce this nested evidence
schema. The public `run_gate` API continues to return `CheckResult`, and the
top-level CheckResult and Receipt schemas do not change. Direct `run_check` API
callers may omit policy binding for legacy unmanaged or internal use. That
compatibility path is not valid evidence for an adopted canonical CLI check,
which always requires the canonical policy binding and fails closed on missing
or mutated authority.

## Optional architecture graph and impact evidence

`.governance/architecture.graph.json` is an optional, canonical UTF-8 JSON
artifact. Its schema is closed at every level; unknown fields, missing fields,
wrong types, unsupported versions or enums, duplicate IDs, duplicate ownership,
unsafe paths, dangling edge endpoints, and bound exhaustion fail validation. The
top-level shape is:

```json
{
  "schema_version": "1.0",
  "nodes": [
    {
      "node_id": "core",
      "kind": "library",
      "path_prefixes": ["src/core"],
      "owner": "platform",
      "gate_ids": ["unit.core"]
    },
    {
      "node_id": "api",
      "kind": "service",
      "path_prefixes": ["src/api"],
      "owner": "backend",
      "gate_ids": ["contract.api"]
    }
  ],
  "edges": [
    {
      "dependent": "api",
      "dependency": "core",
      "kind": "depends_on"
    }
  ],
  "always_gate_ids": ["secret.scan"]
}
```

Top-level fields are exactly `schema_version`, `nodes`, `edges`, and
`always_gate_ids`; `schema_version` is `1.0`. Each node has exactly `node_id`,
`kind`, `path_prefixes`, `owner`, and `gate_ids`. Node kinds are
`application`, `service`, `library`, `package`, `module`, `component`, `data`,
`infrastructure`, `test`, and `tooling`. Stable IDs use lowercase
`[a-z0-9][a-z0-9._-]{0,127}` syntax. Each edge has exactly `dependent`,
`dependency`, and `kind`; P1-A supports only `kind = "depends_on"`, meaning the
dependent uses the dependency. Every endpoint must name a declared node.

The canonical file is at most 1 MiB and contains at most 256 nodes and 1,024
edges. Each node has at most 32 path prefixes and 32 Gate IDs, and
`always_gate_ids` has at most 32 entries. Arrays and stable identifiers are
duplicate-free. Path prefixes are normalized project-relative POSIX prefixes;
absolute paths, drive paths, traversal, query strings, and fragments are invalid,
and `.` alone represents the project root.

One exact path prefix cannot be owned by multiple nodes. Hierarchical overlap is
valid and uses deterministic longest segment-prefix ownership: a changed path
under `src/api` belongs to that node rather than a broader `src` node. Impact
begins with every directly owned changed path and then follows the directed
transitive dependent closure in reverse dependency direction. For the example,
a `core` change affects both `core` and dependent `api`. Traversal is sorted,
bounded, deterministic, and cycle-safe; detected cycles remain visible and force
conservative fallback.

When a graph exists, plan-change keeps every legacy `impact` key and adds one
nested `architecture_graph` projection with exactly these evidence fields:

- `graph_sha256`, `node_count`, and `edge_count`
- `direct_node_ids` and transitive `affected_node_ids`
- `candidate_gate_ids`, including affected-node and `always_gate_ids` candidates
- `unmapped_paths` and `ambiguous_paths`
- `cycle_detected`, `cycle_count`, and `unknown_gate_ids`
- `traversal_exhausted` and `fallback_full`

`fallback_full` is `true` for any unmapped or ambiguous changed path, unknown
configured Gate ID, graph cycle, or traversal-bound exhaustion. This prevents a
false empty-success projection, but it is still evidence only. P1-A plan-change
does not select, skip, execute, reorder, or waive Gates, and `candidate_gate_ids`
does not alter `check`. Plan-change does not write or repair the graph and does
not infer architecture from imports, packages, source text, Git history, or
runtime traces. It produces no downstream graph, project file, or diagram.

Graph absence preserves exact legacy behavior: plan-change emits the same legacy
impact structure with no `architecture_graph` member, and Doctor adds no graph
warning or failure. For a present graph, Doctor is read-only. Canonical valid
graphs report digest and node/edge counts as pass evidence; malformed schemas,
dangling references, unsafe paths, and exhausted bounds fail; cycles are visible
warnings. Doctor never creates, normalizes, migrates, or repairs the graph.

## Optional consistency manifest and impact evidence

`.governance/consistency.manifest.json` is an optional, canonical UTF-8 JSON
artifact. It declares reviewed relationships among existing project artifacts;
the controller does not discover relationships from filenames, imports, build
configuration, Git history, or file contents. Its schema is closed at every
level. The top-level shape is:

```json
{
  "schema_version": "1.0",
  "relationships": [
    {
      "relationship_id": "api-client-generated",
      "kind": "source_generated",
      "comparison": "exact_bytes",
      "source_path": "contracts/client.txt",
      "generated_paths": ["src/client.txt"]
    },
    {
      "relationship_id": "shared-config-surfaces",
      "kind": "cross_surface",
      "comparison": "exact_bytes",
      "paths": ["app/config.json", "worker/config.json"]
    }
  ]
}
```

Top-level fields are exactly `schema_version` and `relationships`;
`schema_version` is `1.0`. Every relationship has exactly
`relationship_id`, `kind`, and `comparison` plus its kind-specific members. A
`source_generated` relationship also has exactly `source_path` and
`generated_paths`. A `cross_surface` relationship also has exactly `paths` and
is symmetric: no member is designated as authoritative. Stable relationship IDs
use the controller's bounded code-token syntax. Version 1 supports only
`comparison = "exact_bytes"`; it does not normalize text, line endings, JSON,
TOML, generated headers, or semantic content.

Each relationship contains at least two and at most 16 total members; the
`source_path` counts toward that total. The canonical manifest is at most 1 MiB
and contains at most 128 relationships, 512 globally unique member paths, and
256 Unicode characters per path. Each member file is at most 8 MiB, and one
evaluation reads at most 64 MiB of aggregate declared member bytes. Duplicate
relationship IDs, duplicate or aliased member ownership, bound exhaustion,
unknown or missing fields, wrong types, unsupported kinds or comparisons, and
non-canonical bytes fail closed.

Member paths are normalized project-relative POSIX file paths. They must resolve
to normal regular files inside the project root. Absolute or drive paths,
traversal, query strings, fragments, directory members, symlinks or paths through
symlinked ancestors, aliases to the same resolved file, governance-evidence
paths, missing members, and unsafe or oversized files fail validation. The
manifest itself is declarative evidence, not authority to create a missing
member.

`exact_bytes` compares each declared member without transformation. A
`source_generated` relationship matches only when every generated member is
byte-identical to its source. A `cross_surface` relationship matches only when
all members are byte-identical. A present valid manifest with all relationships
matching is a Doctor pass; malformed, unsafe, missing, oversized, or drifted
relationships are deterministic failures. Doctor remains read-only and never
creates, rewrites, normalizes, repairs, or migrates a manifest or member.

When the manifest exists, plan-change preserves every legacy `impact` key and
adds nested `impact.consistency_manifest` evidence. It reports the manifest
    digest, affected relationship IDs and member paths, omitted counterpart members,
    and each affected relationship's current pass, drift, or missing status. This
    projection does not append counterpart members to
`changed_paths`, widen authorized scope, or select, skip, execute, reorder, or
waive any Gate. Manifest absence preserves exact legacy behavior: Doctor adds no
manifest diagnostic and plan-change adds no `consistency_manifest` impact
member.

Exact-byte agreement proves only the configured comparison at evaluation time.
It is not evidence that a generator is correct, that the declared source is
authentic or authoritative, or that a build is reproducible. Those properties
require separate project-native Gates and evidence.

## Conservative affected-Gate plan evidence

When and only when a valid architecture graph exists, plan-change appends
`impact.affected_gate_plan`. The projection is closed schema `1.0`, deterministic,
bounded, and execution-free. A graph-present plan-change receipt also binds the
canonical policy SHA-256 in the receipt envelope. The nested projection contains
exactly:

- `schema_version` and `mode` (`affected`, `fallback_full`, or `inconclusive`)
- `policy_sha256`, `architecture_graph_sha256`, and nullable
  `consistency_manifest_sha256`
- `required_phase`, monotonic `effective_phase`, and `execution_performed`
- `changed_paths`, `derived_consistency_paths`, and their bounded
  `planning_paths` union
- `direct_node_ids`, `affected_node_ids`, `candidate_gate_ids`,
  `eligible_policy_gate_ids`, and `eligible_candidate_gate_ids`
- `planned_gate_ids`, `omitted_gate_ids`, `unassigned_gate_ids`, and
  `unsafe_gate_ids`
- `nonpassing_consistency_relationship_ids`, `fallback_reason_codes`, and
  `fallback_full`

The planner reprojects the graph over `planning_paths`. A consistency relationship
touched by the request contributes all declared endpoints to the planning-only
derived set; it does not change the ChangeRecord, approval scope, authorized
writes, evidence references, or sibling `architecture_graph` projection.

Effective phase is the maximum of the risk-required phase and every known
candidate Gate phase. Any conservative fallback raises it to at least `full`;
release risk and release candidates remain `release`. Eligible policy Gates are
the cumulative `fast`, `fast + full`, or `fast + full + release` set for that
effective phase. `affected` recommends only eligible graph candidates.
`fallback_full` recommends the full cumulative eligible set.

Stable fallback reasons cover unmapped or ambiguous paths, cycles, unknown Gate
references, traversal exhaustion, empty candidates, unassigned eligible policy
Gates, non-passing consistency relationships, and bounded projection exhaustion.
Missing policy authority, no eligible policy Gate, unsafe Gate identity, or an
incomplete bounded Gate projection is `inconclusive` with no claimed plan. Gate
IDs that would be redacted by the receipt contract are represented only by a
stable redacted digest label.

An exact or ancestor-path change to `.governance/policy.toml`,
`.governance/architecture.graph.json`, or
`.governance/consistency.manifest.json` is always `inconclusive` with an
`authority.*_changed` reason and no planned Gate IDs. A pre-change authority
cannot prove a post-change graph, relationship, or release-phase Gate set. Apply
the separately approved authority change, then run plan-change again against the
new canonical state. The public planner rejects an empty changed-path sequence.
Plan-change compares each request path with its guard-resolved root-relative
identity using NFC plus casefold. A different portable identity is an alias and
is rejected rather than silently rewriting the approved path. Existing paths
are also compared with the policy, graph, and manifest by filesystem identity so
hardlink aliases are rejected. These additional alias checks apply only when the
architecture graph exists; graph-absent validation remains byte-compatible with
pre-P1-D behavior.

The projection never includes raw argv, Gate options or environment values, file
contents, or secret values. Plan-change itself does not invoke Gate orchestration
or alter Doctor, feedback-loop progress, regression identity, or the
Receipt/ChangeRecord schemas. Its output can be consumed only by the explicit
plan-bound contract below. Graph absence preserves the exact pre-P1-D impact
shape and empty plan-change receipt policy digest.

## Plan-bound Gate execution

The plan-bound input is one project-relative
`.governance/receipts/<timestamp>-plan-change-<change-id>-<digest>.json` reference
inside an adopted target. The path must be a regular, non-linked, non-hardlinked
file with stable identity during read. Canonical receipt bytes, the filename
digest, plan-change command and schema, closed outputs, ChangeRecord bytes,
change ID, evidence references, authorized scope, approval references, risk,
classification, required phase, and current policy digest must agree. High and
critical plan receipts require recorded owner approval. The current architecture
graph is mandatory; the optional consistency manifest is included when present.

The controller loads current policy Gates, graph, and manifest, evaluates
consistency relationships, recomputes the complete closed P1-D projection, and
requires canonical equality with the receipt. Missing, stale, incomplete,
aliased, unsafe, or changed authority fails before execution. The selection is
read and compared twice before Gate orchestration. The controller then requires
a complete canonical receipt-ledger inventory before any Gate: no more than
10,000 entries, 1 MiB per receipt, or 64 MiB of aggregate input bytes. Each
entry must be one contained regular file, without a link, reparse point, or
hardlink, with stable reads and canonical Receipt 1.x bytes. Invalid, unreadable,
unsafe, or exhausted ledger input fails with exit `2`, executes no Gate, and
persists no check receipt. `affected` executes exactly `planned_gate_ids`;
`fallback_full` executes the complete cumulative eligible set; and
`inconclusive` executes nothing and returns exit `3`. Execution retains
canonical phase/Gate order and one CheckResult plus one P1-E0 provenance entry
for every executed Gate.

Receipt age alone never invalidates ledger evidence. Historical receipts are
checked against their persisted canonical Receipt contract, not current policy,
approval, or ChangeRecord requirements. ChangeRecord and current-authority
revalidation applies only to the selected plan-change receipt.

Plan-bound receipts add `outputs.plan_bound_execution`, closed schema `1.0`,
with exactly `schema_version`, `selection_mode`, `plan_receipt_ref`,
`plan_receipt_sha256`, `plan_receipt_digest`, `change_id`,
`change_record_sha256`, `mode`, `effective_phase`, `policy_sha256`,
`architecture_graph_sha256`, nullable `consistency_manifest_sha256`,
`planned_gate_ids`, `executed_gate_ids`, `omitted_gate_ids`,
`fallback_reason_codes`, `execution_performed`, and `authority_status`.
Executed IDs must equal the planned set for executable modes. Inconclusive plans
have neither planned nor executed IDs; fallback-full has no omitted eligible
Gates. Omitted IDs mean only that those Gates did not execute and must never be
projected as pass, waived, or not applicable.

After Gate orchestration, policy and the complete plan authority are loaded and
recomputed again. The complete ledger is also re-inventoried; an invalid ledger
or a fingerprint different from the retained pre-execution inventory is a scope
violation independent of whether selected plan authority changed. Workspace
scope comparison detects other concurrent project changes. Any plan,
ChangeRecord, receipt, policy, graph, manifest, or workspace drift returns exit
`4`, sets `authority_status = "changed"` only when the selected plan authority
changed, and does not persist a successful check receipt. The bounded inventory
guard is not a lock, lease, or atomic-replacement protocol; those controls remain
P1-E2 work. The legacy phase API and CLI defaults, direct `run_check`
compatibility path, feedback-loop phase behavior, public CheckResult, and
top-level Receipt schema remain unchanged and do not gain the plan-bound ledger
precondition.

## Levels, escalation, and risk

- `G1`: limited exposure/dependencies; defaults, ownership, discovery, fast checks, secret checks, receipts, basic rollback.
- `G2`: meaningful contracts, multiple modules, integrations, or automation; contract tests, dependency-aware selection, full gates, relevant budgets, release ownership, impact.
- `G3`: multiple public surfaces, substantial dependencies, operational/data risk, or expensive regression burden; periodic regression, staged rollout, telemetry, recovery, architecture fitness, stronger dependency review.
- `G4`: regulated data, safety/mission-critical exposure, irreversible integrity risk, or severe outage consequences; named approval, independent verification where configured, fail-closed critical controls, rollback rehearsal, retained evidence.

Escalation is monotonic and evidence-explained; it never deletes controls. File count and lines of code are supporting signals only. Unknown critical facts create evidence tasks, and inability to evaluate a configured G4 control blocks the affected operation. De-escalation requires changed evidence, explicit approval, and a receipt.

Risk classes are `routine` (isolated/reversible), `moderate` (cross-module, dependency, or user-visible), `high` (public API, auth, persistence, payment, global state, deployment, core build, sensitive data, or performance budget), and `critical` (regulated, safety/mission-critical, irreversible integrity, or severe recovery risk). High/critical apply requires configured approval. Exceptions require owner, reason, evidence, and expiry; review cannot waive a required deterministic gate without a recorded exception.

## Baselines and non-baselinable rules

Baseline entries require stable fingerprint, severity, acceptance timestamp, actor, owner, review date, source audit receipt, and rule version. Comparison returns existing, new, worsened, and resolved findings. New or worsened findings block the ratchet. Baselines cannot waive secret leakage, unauthorized writes, data-integrity corruption, or any `non_baselinable_rules` entry, including configured critical-control failures. Expired or duplicate entries are invalid. Tightening or removing a baseline requires an approved change and passing evidence.

## Five states, exits, adapters, rollback

States are `pass`, `warn`, `fail`, `inconclusive`, and `not-applicable`; inconclusive never passes. Stable exits are `0` pass, `1` deterministic failure, `2` invalid invocation/schema, `3` inconclusive required evidence, `4` authorization/scope violation, and `5` partial-write recovery required.

Managed projections may include Codex `AGENTS.md`, Claude Code `CLAUDE.md`, Cursor scoped `.cursor/rules/`, Git fast-check hooks, and GitHub integration/release checks. Each records policy version, source digest, generator version, and scope. `doctor` detects missing, stale, manually edited, duplicated, or divergent required adapters. Every write records inputs, outputs, approver, receipt, and rollback path. Preview does not apply; apply is bounded and approved; partial writes require recovery or rollback evidence and exit `5`.

## Profile-driven conditional controls

Profiles are the evidence record used to choose the governance level and conditional documentation set. The selected policy may require baseline documents such as `AGENTS.md`, `PROJECT_BRIEF.md`, `ARCHITECTURE.md`, `QUALITY_GATES.md`, and `docs/decisions/`, plus documents appropriate to the observed project type, lifecycle, public exposure, data risk, release model, and test burden. A conditional document is required only when the profile and selected level call for it; an adapter is generated or verified only when named in `adapters`.

## Legacy ratchet boundary

Legacy acceptance is an audit-first ratchet, not a waiver. A reproducible existing failure may be retained only with the required fingerprint, owner, review date, source receipt, and expiry/review discipline; new or worsened findings block. Non-baselinable failures must be fixed, explicitly handled by the applicable control, or stopped for recovery. They cannot be converted into accepted debt by an approval string or by a preview.

Version 1 does not activate any global or user-wide adapter, Git configuration, launcher setting, daemon, or background process. Such activation is outside this repository-scoped controller and requires separate authorization. The controller also does not promise zero bugs; it provides evidence-backed regression controls and explicit recovery boundaries.

## P2-A intake extension contract

The `project_governance.intake` module is a repository-local, standard-library
contract layered beside (and not substituted for) the Receipt, ChangeRecord,
policy, baseline, and storage schemas. Its only persistence surface is the
caller-owned bytes passed to `parse_intake` or returned by
`render_intake`; the module itself has no target, network, dependency, or
approval side effect.

### Closed shape

The extension version is exactly `1.0`. The top-level field set is exactly:

```text
decisions, evidence, extension_schema_version, intake_id,
need_evidence_level, project_mode, project_mode_evidence_refs, purpose,
remediation_level, slice_complexity, stack_fitness, stop_state, user_context
```

`user_context` has exactly `audience_mode`, `domain_experience`,
`project_relationship`, and `technical_experience`. A decision has exactly
`confidence`, `decision_id`, `disposition`, `evidence_refs`,
`recommendation_code`, `resolution_state`, `topic_code`, and
`user_impact_code`. An evidence reference has exactly `kind`, `locator`, and
`reference_id`. Every object is closed; unknown fields and duplicate JSON keys
are deterministic schema failures.

The allowed values are:

| Field | Closed values |
|---|---|
| `project_mode` | `new`, `existing`, `ambiguous` |
| `purpose` | `personal-learning`, `real-audience` |
| `disposition` | `D`, `V`, `B` |
| `resolution_state` | `open`, `ai-resolved`, `user-confirmed` |
| `remediation_level` | `L0`, `L1`, `L2`, `L3`, `L4` |
| `stack_fitness` | `S0`, `S1`, `S2`, `S3`, `S4` |
| `need_evidence_level` | `T0`, `T1`, `T2` |
| `stop_state` | `continue`, `ready-for-preview`, `owner-gate` |

The context vocabularies are also closed and expose `unknown` where a
self-declared value is not known. No raw identity, account, credential,
customer, or free-form conversation field is available.

### Bounds and evidence rules

Stable IDs and codes are lowercase ASCII tokens of at most 80 characters;
locators are at most 240 characters. A record is at most 64 KiB, with no more
than 32 decisions, 64 evidence references, or 16 references on one decision or
project-mode field. IDs and arrays use deterministic sorted order. A locator is
either a stable token or a contained project-relative POSIX path. Absolute
paths, traversal segments, empty path segments, backslashes, query-bearing
URLs, control characters, secret/token patterns, and non-NFC scalars are
rejected.

Evidence kinds are intentionally separate: `hypothesis` is an unverified
working claim; `public-research` is externally observable research;
`real-user-evidence` is evidence from actual users; `technical-viability` is
prototype, benchmark, or runtime feasibility evidence; `user-confirmation` is
a declared preference or answer; and `project-evidence` is a contained fact
read from the existing project. T0 is personal-learning evidence, T1 is a
real-audience hypothesis supported by public research, and T2 requires
real-user evidence. None of these labels substitutes for domain acceptance.

### Decision budgets and stop invariants

`B` decisions are the human-decision count. The maximum is three for
`slice_complexity = simple` and five for `complex`. AI-resolved `D` and `V`
items are counted by `ai_resolved_count` and do not consume the human budget.
`B` cannot be `ai-resolved`. `ready-for-preview` requires no open decision;
`owner-gate` requires an open `B`; and `continue` requires open `D`/`V` items
with no open `B`. Contradictory terminal states fail before rendering.

### Canonical and authority boundaries

Parsing accepts only bounded UTF-8 JSON bytes with one trailing newline and
requires byte equality with the sorted-key, compact rendering. Rendering is
deterministic and returns UTF-8 bytes. Frozen dataclasses and tuple fields make
post-parse mutation observable as an error. This contract is evidence and
routing input only: it neither creates approvals nor authorizes APG apply,
Gate selection, intake materialization, agent questioning, research execution,
technical candidate scoring, document projection, release packaging, global
installation, promotion, host reload, external projects, dependencies, or Git
operations. Those surfaces require later scoped Change IDs and their own
previews, approvals, receipts, Gates, acceptance, and rollback.

## P3-A user-intent and decision-router contract

P3-A is a closed, standard-library-only extension layered above the P2-A through
P2-E contracts. `build_user_intent` accepts one trimmed NFC UTF-8 idea of at most
4 KiB and performs conservative deterministic keyword extraction. It is not a
model-inference API and does not prove complete understanding of arbitrary
natural language. Unmatched or conflicting concepts remain explicit
uncertainty. `parse_user_intent` and `render_user_intent` define the separate
canonical-byte contract for normalized intent evidence. P3-A does not retain the
raw prompt, persist a conversation, or mutate the P2 records from which a caller
may derive evidence.

### Structured intent

The structured intent projection contains exactly the project type, target
platform, user persona, goals, constraints, uncertainties, and stable evidence
references required for later planning. The implementation may expose these as
closed nested records, but it must not expose an unbounded free-form extension
map or a field for raw prompts, names, email addresses, account identifiers,
credentials, tokens, secrets, customer identifiers, customer records, or other
PII. Unknown fields, duplicate JSON keys, duplicate stable IDs, unsupported
closed values, non-NFC text, control characters, secret-shaped scalars, unsafe
locators, and non-canonical bytes are schema failures.

### Decision dispositions

The disposition vocabulary is exactly `AUTO`, `RECOMMEND`, and `CONFIRM`.

- `AUTO` records a reversible bounded default that APG can decide from available
  evidence. It must include a stable decision ID, rationale, bounded confidence,
  and evidence references. It never authorizes execution.
- `RECOMMEND` records a preferred default, the reason for it, bounded confidence,
  and the material consequence of overriding it. It remains a recommendation,
  not implicit owner consent.
- `CONFIRM` records an owner-only decision. The router may recommend an option,
  but dependent planning remains owner-gated until explicit confirmation is
  supplied through a separately authoritative caller workflow.

The router must emit `CONFIRM` for any decision involving cost or paid service,
production use, privacy or personal data, real customer or production data,
provider or network access, public publication, deployment, irreversible
external action, or materially ambiguous product direction. A trigger cannot be
downgraded to `AUTO` or `RECOMMEND` because confidence is high. Missing answers
are not consent.

`route_user_intent` computes every disposition from the validated source
record. Parsed route bytes must exactly match a fresh recomputation from their
embedded `structured_intent`; moving a `CONFIRM` item into `AUTO`, adding a
caller-authored decision, or changing a derived plan entry fails closed. Optional
P2 intake and stack evidence can escalate an in-memory route, but a serialized
P2-derived route is rejected unless a later schema carries enough canonical P2
source context to recompute it. P3-A does not treat an unbound P2 projection as
verified evidence.

### Closed route output

The route output separates these field groups:

```text
structured_intent
necessary_questions
recommended_plan
automatic_decisions
recommended_decisions
confirmation_required_decisions
rationale
confidence
evidence_refs
```

Decision groups are disjoint and collectively contain every routed decision.
Necessary questions are limited to unresolved facts that can materially change
scope, product direction, risk, delivery, or acceptance. Each question carries
a stable topic, a recommended answer when one is safe, rationale, impact, and
evidence references. `recommended_plan` is evidence for a later generator; it
is not a project blueprint and cannot contain executable commands, environment
values, provider requests, approval strings, or side-effect instructions.

### Bounds and canonical form

- The complete canonical input or output is at most 64 KiB.
- IDs and codes are lowercase ASCII tokens of at most 80 characters.
- Evidence locators are at most 240 characters and must be stable tokens or
  contained project-relative POSIX paths.
- One intent has at most 16 goals, 16 constraints, and 16 uncertainties.
- One route has at most 16 necessary questions and 16 recommended-plan entries.
- All three decision groups together contain at most 32 decisions.
- A route has at most 64 evidence references and one item may cite at most 16.
- Arrays use deterministic order; confidence uses a closed bounded vocabulary.
- Canonical rendering is deterministic UTF-8 JSON with one trailing newline.

Absolute paths, traversal segments, backslashes in locators, query-bearing URLs,
invalid UTF-8, non-canonical key or array order, unsupported values, and bound
exhaustion fail before an intent or route is returned.

### Evidence, retention, and authority

P3-A is side-effect-free. Importing, parsing, classifying, routing, and rendering
perform no filesystem writes, network calls, provider calls, dependency
discovery, approval creation, Gate selection, runtime launch, publication,
deployment, global promotion, downstream mutation, or release action. The
module neither stores caller-owned bytes nor writes P3-A evidence into receipts.
Only normalized bounded fields may be returned to a caller that separately
decides whether and where they may be retained.

P3-A output is input evidence for P3-B only. A Project Blueprint Generator,
including `PROJECT_BRIEF`, `PRODUCT_PLAN`, `UX_FLOW`, `ARCHITECTURE`,
`STACK_DECISION`, `TASK_GRAPH`, `QUALITY_PLAN`, and `DEPLOYMENT_PLAN`, requires
an independent P3-B ChangeRecord. P3-A approval and Gates do not authorize P3-B
or establish public publication, global promotion, host/runtime, provider or
network, downstream pilot, deployment, or release acceptance. Each status
requires its own bounded transaction and evidence.

## P3-B project-blueprint contract

P3-B is a closed, standard-library-only projection over one exact canonical
P3-A `IntentDecisionResult`. Generation MUST first render the P3-A source,
embed its complete mapping, and record the SHA-256 digest of those canonical
bytes. Parsing MUST parse that embedded P3-A mapping, recompute the complete
blueprint, and reject any caller-authored or edited derived section.

### Source and readiness requirements

Generation MUST reject unless `type(source) is IntentDecisionResult`,
`ready_for_blueprint is True`, and `confirmation_required_decisions` is empty.
There is no force, approval, confirmation, or missing-answer override parameter.
A P2-derived route that P3-A cannot canonically serialize and recompute MUST be
rejected; P3-B MUST NOT strip P2 decisions, infer consent, or reconstruct absent
P2 intake, stack, owner-gate, or evidence context.

P3-A readiness means only that no P3-A `CONFIRM` item remains. It does not prove
complete requirements, supported stack evidence, implementation readiness,
deployment authority, or external acceptance. P3-B v1 therefore records
`ready_for_implementation=false`. `RECOMMEND` decisions remain assumptions, and
unknown facts remain explicit unresolved or needs-evidence states.

### Closed output

The canonical output contains exactly these sections in this order:

1. `PROJECT_BRIEF`
2. `PRODUCT_PLAN`
3. `UX_FLOW`
4. `ARCHITECTURE`
5. `STACK_DECISION`
6. `TASK_GRAPH`
7. `QUALITY_PLAN`
8. `DEPLOYMENT_PLAN`

The stack section MUST NOT invent or select a framework, provider, database,
package, version, or candidate without source-complete canonical evidence. The
task graph is a bounded deterministic acyclic plan and MUST NOT execute a task.
The quality plan records required evidence with execution state `not-run`; it
is not Gate evidence. The deployment plan records prerequisites, artifacts,
rollback, and verification requirements with authority `not-authorized`; it is
not a deployment, publication, endpoint, or runtime record.

### Canonical, retention, and side-effect rules

The normative form is canonical UTF-8 JSON with sorted keys, compact separators,
NFC strings, and one trailing newline. Records use closed enums, exact types,
immutable tuples, unique IDs, deterministic order, bounded codes, and bounded
evidence references. Duplicate keys, unknown fields, unsupported values,
non-canonical bytes, unsafe locators, secret-shaped values, and bound exhaustion
fail closed. Deterministic Markdown is non-normative and is never parsed as a
source record.

No blueprint field stores a raw idea, prompt, transcript, model message, PII,
credential, customer record, secret, or unbounded free-form text. Importing,
generating, parsing, and rendering perform no project materialization,
filesystem discovery or write, subprocess execution, provider/network call,
dependency discovery or installation, approval creation, Gate selection or
execution, deployment, publication, global promotion, host/runtime launch,
downstream mutation, Git operation, receipt write, or release action.

P3-B acceptance remains repository-local static acceptance. Public publication,
global promotion, host/runtime activation, provider/network acceptance,
downstream pilot acceptance, deployment, and release require independent
transactions and MUST NOT be inferred from P3-B bytes, tests, Doctor, or Gates.

## P3-C implementation-readiness contract

P3-C is a closed, deterministic, standard-library-only resolver after P3-B and
before a later P3-D materialization-preview transaction. It MUST NOT modify the
source blueprint, materialize project files, or grant implementation authority.
Its established ChangeRecord remains
`adaptive-project-governance-p3-c-implementation-readiness-resolver-v1-20260809`;
this contract does not create or authorize another transaction.

### Required canonical bindings

The resolver MUST bind the exact canonical P3-B `ProjectBlueprint` and digest,
including its embedded P3-A source. It MUST separately bind a canonical P2
`ProjectIntake`, the original P2-C `StackCandidate` records, the complete P2-D
`DomainPackRegistry` and `DomainApplicabilityEvidence`, and the source inputs
needed for P2-E guided-intake recomputation.

P3-A and P2 record IDs are independent. Their relationship MUST be established
by explicit shared evidence references in a closed P3-A-to-P2 binding; equal or
similar IDs, codes, labels, or prose MUST NOT establish identity. Architecture
compatibility likewise requires explicit evidence and MUST NOT be inferred by
string similarity.

Stack candidates MUST preserve every original dimension assessment and evidence
reference required to rerun `score_stack_candidates`. Domain Pack mappings MUST
preserve applicability rules, test profiles, performance profiles, and
professional Gate requirements. A rendered guided-intake view is derived and
MUST NOT substitute for its source-complete inputs.

### Recomputed output and state precedence

Generation MUST derive all output fields from validated sources. Parsing MUST
reparse every embedded record, verify every canonical digest, rerun stack
scoring, recompute guided-intake compatibility, recompute Domain Pack
applicability and applicable pack IDs, derive the complete professional Gate
set, and require exact equality with a fresh readiness result.

The readiness state is the first applicable member of this exact ordered set:

1. `source-binding-required`
2. `owner-confirmation-required`
3. `intake-evidence-required`
4. `stack-evidence-required`
5. `stack-correction-required`
6. `domain-evidence-required`
7. `ready-for-materialization-preview`

Only `ready-for-materialization-preview` may set
`ready_for_materialization_preview=true`; every earlier state MUST set it to
false. `implementation_authority` MUST equal `not-authorized` in every state.
P3-C MUST NOT alter, reinterpret, or replace P3-B's
`ready_for_implementation=false` field.

`source-binding-required` covers absent or insufficient canonical source and
cross-stage identity evidence. `owner-confirmation-required` preserves open
owner-bound decisions. `intake-evidence-required` preserves evidence debt from
the intake contract. `stack-evidence-required` means scoring cannot establish a
supported candidate. `stack-correction-required` means recomputed scoring or
guided compatibility contradicts the proposed architecture or candidate.
`domain-evidence-required` covers unresolved applicability, missing applicable
pack evidence, or incomplete professional Gate derivation. The final state
authorizes only a request for a later bounded preview.

### Canonical, failure, and authority rules

The normative representation is closed canonical UTF-8 JSON with sorted keys,
compact separators, NFC strings, deterministic arrays, and one trailing LF.
Embedded records, source digests, and derived fields are bounded. Duplicate
keys or IDs, unknown fields, unsupported values, unsafe locators, secret-shaped
values, non-canonical bytes, digest mismatch, recomputation mismatch, and bound
exhaustion fail closed. There is no force, owner-approval, fallback-success, or
caller-authored readiness override. Evidence insufficiency produces the first
applicable closed state; malformed or tampered source produces no accepted
result.

Resolving, parsing, and rendering perform no filesystem discovery or write,
subprocess execution, dependency discovery or installation, Gate selection or
execution, approval creation, provider/network call, project materialization,
host/runtime launch, downstream mutation, Git operation, receipt write,
deployment, public publication, global promotion, pilot, or release action.

P3-D requires an independent ChangeRecord and MUST bind the exact canonical
P3-C result and blueprint digest, downstream root, changed paths, dirty-baseline
treatment, approval, Gates, rollback, and acceptance before preview. Preview is
not apply. P3-C repository acceptance requires `controller-focused`,
`controller-compile`, and `controller-full`, Doctor, and independent review;
those results remain repository-local and do not authorize any downstream or
external action. Rollback is path-bounded, restores captured pre-transaction
bytes, removes only new P3-C files, and preserves historical evidence and the
existing dirty baseline.

## P3-D project-materialization-preview contract

P3-D is a closed, deterministic, standard-library-only preview contract after
P3-C. It MUST bind an exact canonical `ImplementationReadiness` record, its
SHA-256 digest, the embedded P3-B blueprint digest, source evidence, and an
explicit policy digest. It MUST reparse the embedded P3-C record and reject a
digest or recomputation mismatch. A non-ready P3-C state MUST yield `block`.

The proposal MUST explicitly provide a downstream root locator, manifest ID and
entries, one pre-state expectation for each proposed path, approval state,
configured Gates, acceptance references, rollback identifier, and policy
digest. P3-D MUST NOT discover a root, infer a manifest, manufacture baseline
evidence, infer owner consent, or elevate an approval state into write
authority. Missing proposal facts yield `pending-user-input`; a complete source
and proposal may yield `preview-ready` for review only.

The root locator MUST be a stable logical code, not a filesystem path, URL,
provider locator, or runtime endpoint. Each baseline entry is a mandatory
compare-and-swap assertion: its SHA-256 requires exact existing bytes and
`null` requires the named path to be absent. `null` MUST NOT be interpreted as
an unknown, unchecked, or unrestricted baseline.

Every P3-D output MUST set `preview_only=true` and `apply_authority=false`.
Canonical output uses closed UTF-8 JSON with sorted keys, compact separators,
NFC strings, deterministic arrays, and one trailing LF. Duplicate keys or IDs,
unknown fields, unsupported values, non-canonical bytes, traversal, root
escape, unsafe locators, path or baseline mismatch, secret-shaped values,
digest drift, and bound exhaustion fail closed.

Building, rendering, and parsing MUST perform no filesystem discovery or write,
subprocess execution, dependency discovery or installation, approval creation,
Gate selection or execution, provider/network call, project materialization,
host/runtime launch, deployment, publication, promotion, downstream mutation,
Git operation, pilot, or release action. A future downstream apply requires an
independent ChangeRecord with the exact physical root, manifest, pre-state,
approved write scope, fresh owner approval, Gates, acceptance, and
compare-and-swap rollback. Repository-local checks do not establish downstream
or external acceptance.

## P3-E project-materialization-apply transaction contract

P3-E is the bounded transaction controller after P3-D. It MUST accept only an
exact canonical P3-D `preview-ready` record with `preview_only=true` and
`apply_authority=false`. It MUST re-render the preview and bind its SHA-256,
embedded P3-C/P3-B evidence, and policy digest before it classifies or writes
anything. The P3-D logical root code MUST NOT be treated as a filesystem path,
URL, provider locator, or runtime endpoint.

The controller MUST classify every request as exactly one of `AUTO`,
`RECOMMEND`, `CONFIRM`, or `BLOCK`.

- `AUTO` MAY proceed without a human interruption only when all facts are
  evidence-bound: exact P3-D preview and policy digest, non-empty canonical
  evidence references, bounded scope, reversibility, no secret-shaped values,
  no network or provider access, no cost or quota, no credentials, no real or
  production data, no runtime launch, no deployment, no public delivery, no
  security or privacy posture change, and no materially ambiguous direction.
- `RECOMMEND` MUST perform no write and MUST NOT be treated as authorization.
- `CONFIRM` MUST be used for provider or network access, cost or quota,
  credentials, real or production data, public delivery, runtime launch,
  deployment, irreversible change, security or privacy posture change, or
  materially ambiguous direction. A valid confirmation MUST be fresh and bind
  an owner actor, transaction ID, P3-D digest, physical-root fingerprint, and
  every allowed path.
- `BLOCK` MUST be returned for missing action context, policy drift, unbounded
  scope, missing reversibility evidence, secret-shaped content, non-ready or
  tampered preview, content or manifest mismatch, unsafe physical root, link or
  reparse traversal, root escape, baseline mismatch, snapshot integrity
  failure, post-state drift, or any unknown condition.

Before an `AUTO` or confirmed execution, P3-E MUST receive a caller-supplied
absolute regular physical root. It MUST reject relative roots, links, reparse
points, and root changes. It MUST verify that supplied bytes exactly match each
P3-D manifest hash and that no path is added or omitted. Every P3-D baseline
entry is a mandatory compare-and-swap condition: a digest requires exact bytes
and `null` requires absence. The controller MUST capture a separate pre-state
snapshot and its hashes before it replaces any target path.

Execution MUST recheck pre-state immediately before committing staged files,
write only the frozen manifest paths, and verify every post-state hash. A
canonical evidence result MUST omit the physical root path while retaining its
fingerprint, transaction ID, P3-D digest, authorization classification,
pre-state, post-state, snapshot reference, rollback ID, and `BLOCK` reasons.
Rollback MUST recheck every post-state hash and snapshot hash before restoring
bytes or deleting previously absent paths. Drift MUST produce `BLOCK`; it MUST
NOT overwrite unknown user changes.

P3-E does not select a target, discover a physical root, create an approval,
infer external facts, install dependencies, call a provider or network, launch
a runtime, deploy, publish, promote, pilot, release, or run Git. It provides
the evidence-bound local controller that a later authorized orchestration layer
may call. Repository-local validation remains distinct from downstream,
runtime, provider/network, deployment, publication, promotion, pilot, and
release acceptance.

## P3-F autonomous-task-orchestration contract

P3-F MUST accept only exact canonical P3-C readiness bytes whose recomputed
state is `ready-for-materialization-preview`, whose readiness flag is true, and
whose blocker set is empty. It MUST bind the complete readiness digest and the
exact embedded P3-B task graph. A non-ready, non-canonical, tampered, or
source-incomplete record MUST fail closed.

Every P3-B task MUST have exactly one closed execution context. The context
MUST bind its task ID, executor ID, contained canonical read and write scopes,
declared Gate IDs, acceptance references, rollback reference, one exact P3-E
`ActionContext`, and explicit Git-operation and release flags. Context task IDs
MUST exactly equal the task graph IDs; omissions, additions, duplicates,
traversal, absolute paths, aliases, unsupported values, and policy-digest
disagreement MUST fail closed. Planning evidence MUST be contained in the P3-C
evidence set, and the union of declared Gates MUST cover every required P3-C
professional Gate.

P3-F MUST retain deterministic P3-B topological order and MUST place every task
after all dependencies. Tasks MAY share a wave only when their scopes are safe
to run together. A write path that overlaps another task's read or write path
MUST be serialized into a later wave and recorded as
`ownership-overlap-serialized`; it MUST NOT become a user-managed conflict or
silent parallel write.

P3-F MUST classify every task using P3-E `assess_action` and MAY only tighten
that result. `AUTO` is permitted only for complete, bounded, reversible,
no-secret, no-network, no-cost, no-credential, no-real-data local work.
`RECOMMEND` grants no write authority. `CONFIRM` is required for every P3-E
consequential trigger and additionally for Git mutation or release. `BLOCK` is
required for unsafe, incomplete, unbound, drifted, or policy-inconsistent
facts. P3-F MUST NOT downgrade a `CONFIRM` or `BLOCK` classification.

An autonomous plan MUST expose deterministic routes and waves, the complete
recommended task path, the next material task IDs, class-specific task sets,
self-check codes, blocker codes, one compact user summary code, and
`execution_performed=false`. An `AUTO` task ID means only that a later executor
bound to the exact context need not interrupt the owner for routine approval.
It MUST NOT be represented as implementation, filesystem, provider, network,
runtime, deployment, publication, Git, pilot, or release authority.

Final evaluation MUST treat absent task evidence as `INCOMPLETE`, never success.
An accepted task MUST bind its declared executor, output references, Gate
references, acceptance references, and rollback reference; it MUST have a
reviewer identity distinct from the executor and review verdict `ACCEPT`. Every
dependency MUST already be accepted. A missing dependency keeps dependent work
`INCOMPLETE`; a blocked dependency MUST block its dependents. `RECOMMEND`
additionally requires decision evidence, and `CONFIRM` additionally requires
authorization evidence. Any failed task, blocking review, binding drift,
unsafe plan, or inconsistent evidence MUST produce `BLOCK`. Only a complete
independently accepted task set MAY produce final `ACCEPT`.

Canonical plan JSON MUST be closed, bounded UTF-8 with deterministic ordering,
one trailing LF, duplicate-key rejection, unknown-field rejection, and full
derived-field recomputation during parse. P3-F planning, parsing, rendering,
and evaluation MUST be standard-library-only and MUST perform no filesystem
mutation, subprocess, dependency, provider/network, credential, runtime,
deployment, publication, promotion, pilot, release, or Git action.

## P3-G goal-to-delivery-lifecycle contract

P3-G MUST bind one lifecycle run to one exact canonical P3-F plan digest and a
caller-supplied lifecycle run ID. It MUST derive the current dependency-closed
wave, task cursor, next action, state, and compact user result from the plan and
the append-only checkpoint history. Derived fields MUST be recomputed during
render and parse.

The lifecycle state vocabulary MUST be exactly `AUTO`, `RECOMMEND`, `CONFIRM`,
`BLOCK`, and `COMPLETE`. `AUTO` MAY advance only a bounded reversible task whose
P3-F context remains valid. `RECOMMEND` MUST pause until a decision binds the
run ID, plan ID, plan digest, task ID, and exact task scope. `CONFIRM` MUST pause
until an approval binds those same fields plus a caller-supplied transaction ID.
Missing identifiers MUST remain `PENDING_USER_INPUT`; the controller MUST NOT
invent identifiers or treat silence as consent.

Task evidence MUST be accepted only for the current dependency-closed wave and
only once. Executor, artifact/output, Gate, acceptance, rollback, reviewer, and
task-bound consolidation references MUST remain bounded and stable. A failed
task result, blocking review, binding mismatch, or blocked dependency MUST stop
the run and block its dependents. Exact checkpoint replay MAY be idempotent only
when its event digest, sequence, and previous checkpoint digest are identical;
changed replay, sequence gaps, duplicates, tampered digests, stale plan
bindings, unknown fields, or duplicate JSON keys MUST be rejected.

Lifecycle phases MUST remain isolated and ordered:
`planned`, `repository-validated`, `runtime-verified`,
`deployment-verified`, `publication-verified`, `pilot-accepted`, and
`release-accepted`. Repository-local evidence MUST NOT infer any later phase.
Each post-plan phase MUST use explicit phase-scoped acceptance evidence whose
domain exactly matches the phase and MUST advance one phase at a time.

P3-G MUST be standard-library-only, canonical bounded UTF-8 JSON, and pure in
memory. It MUST perform no filesystem mutation, subprocess, dependency,
provider/network, credential, runtime, deployment, publication, promotion,
pilot, release, or Git action. The ordinary-user result MUST expose only a
concise status, result, next step, and phase; complete governance trace remains
in the operator record.

## P3-H requirement-trace-consolidation contract

P3-H MUST accept only exact canonical P3-G lifecycle bytes. It MUST reparse and
rerender that lifecycle, bind its SHA-256 digest, lifecycle run ID, P3-F plan ID
and digest, checkpoint-chain tip, current lifecycle phase, exact P3-A intent
decision digest, and exact P3-B blueprint digest. The P3-A/P3-B bindings MUST
be recomputed through the embedded P3-F/P3-C source chain; caller-supplied
source summaries, reconstructed blueprint prose, or a task self-report MUST
NOT substitute for canonical source evidence.

Each `REQ-<three-digit>` trace MUST bind authoritative `P3B:<section>`
references, P3-F/P3-G task IDs and their dependencies, exact output artifact
references, exact task consolidation references, conflict-resolution IDs,
residual-gap IDs, next-evidence references, and evidence scoped exactly to the
current P3-G phase. Task claims MUST form one exact partition of the completed
P3-G task set: unknown tasks, missing coverage, duplicate claims, output or
consolidation mismatch, a non-complete lifecycle, or an absent checkpoint tip
MUST return `BLOCK`.

P3-H MUST compare P3-F write-path ownership. Every overlapping pair MUST have
one bounded conflict record whose exact task pair and shared paths match the
P3-F route, whose evidence references are present, and whose state is
`resolved`. A passed task MUST NOT imply that parallel outputs are compatible.
An open residual gap MUST return `needs-evidence`; a blocking or unresolved gap
MUST return `BLOCK`; a closed gap remains traceable. P3-H MUST NOT silently
remove gaps or create conflict, gap, artifact, consolidation, decision,
approval, phase, or evidence identifiers.

`ACCEPT` MUST require a completed lifecycle, exact task/artifact/consolidation
bindings, no unresolved write conflict, no open or blocking residual gap, a
post-`planned` lifecycle phase with matching P3-G phase-acceptance evidence,
and a review verdict `ACCEPT` from an identity different from the consolidator.
Missing review or planned-only evidence is `needs-evidence`. A blocked or
self-review, phase mismatch, drift, tampering, unknown task, overlap, missing
conflict resolution, or blocking gap is `BLOCK`. P3-H MUST NOT infer runtime,
deployment, publication, pilot, or release acceptance from earlier-phase
repository evidence.

P3-H records MUST be immutable, closed, bounded canonical UTF-8 JSON with
sorted unique arrays, deterministic object order, duplicate-key rejection,
unknown-field rejection, unsupported-constant rejection, and full
derived-state recomputation during parse and render. It MUST be
standard-library-only and in memory, and MUST perform no filesystem mutation,
subprocess, dependency, provider/network, credential, runtime, deployment,
publication, promotion, pilot, release, or Git action. Its ordinary-user
projection MUST expose only status, result, next step, and phase.

## P3-I idea-to-result-session contract

P3-I MUST accept at least one exact canonical P3-A intent-decision payload and
MAY accept the exact canonical P3-B, P3-C, P3-F, P3-G, and P3-H successor
payloads. Every supplied payload MUST be parsed through its existing canonical
stage parser. The session MUST retain deterministic SHA-256 evidence for each
supplied stage and MUST verify the complete source chain: P3-B to P3-A, P3-C to
P3-B, P3-F to P3-C, P3-G to the P3-F plan ID and digest, and P3-H to the P3-G
lifecycle run, plan, and digest.

The accepted session state vocabulary MUST be exactly `auto`, `recommend`,
`confirm`, `needs-evidence`, `block`, and `complete`. The first unresolved or
invalid stage MUST control the result. A ready earlier stage MAY route
automatically to construction of the next canonical local stage. A source
recommendation MUST remain `recommend`; a consequential source boundary MUST
remain `confirm`; missing evidence MUST remain `needs-evidence`; source drift,
invalid ordering, unsafe authority, or a blocking stage MUST remain `block`.
Only an exact P3-H `accept` record MAY produce session `complete`.

P3-I MUST NOT invent or reuse approval, decision, requirement, task, artifact,
evidence, phase, runtime, deployment, publication, promotion, pilot, release,
or external identifiers. Silence and a previous unrelated approval MUST NOT be
treated as consent. P3-D/P3-E materialization remains an independent exact-root
transaction and is not performed or authorized by the P3-I stage chain.

The session phase MUST remain `planned` until supplied canonical P3-G or P3-H
evidence advances it. Repository validation MUST NOT infer runtime acceptance;
runtime MUST NOT infer deployment; and no earlier phase may infer publication,
promotion, pilot, or release acceptance. Each later boundary retains separate
authority, evidence, rollback, and independent acceptance.

P3-I records MUST be immutable, bounded, closed canonical UTF-8 JSON with
canonical stage order, deterministic object order, one trailing LF,
duplicate-key rejection, unknown-field rejection, unsupported-constant
rejection, and full derived-state recomputation during render and parse. P3-I
MUST be standard-library-only and in memory. It MUST perform no filesystem
mutation, subprocess, dependency, provider/network, credential, Gate, runtime,
deployment, publication, promotion, pilot, release, or Git action. The
ordinary-user projection MUST expose only status, result, next step, stage, and
phase; `execution_performed` MUST remain false.

## P3-J non-invasive-target-project-orchestration contract

P3-J MUST accept only exact canonical P3-I bytes whose state and current stage
are `complete`, whose P3-G lifecycle is complete, and whose P3-H consolidation
is `accept`. It MUST reparse P3-I and recompute the P3-I, P3-H, P3-G, and P3-F
bindings. A caller MUST NOT substitute a source summary or override a nested
plan, lifecycle, or consolidation.

The target snapshot MUST use one stable logical `target_id` and unique,
completely represented capability and component baseline records. It MUST NOT
contain or require a physical path, URL, raw project content, credentials,
tokens, account data, machine identifiers, provider responses, or production
data. Existing capabilities default to `preserve`; a proposed change MUST bind
one P3-H `REQ-*` reference and remain a separate downstream transaction.

Component task claims MUST exactly partition the complete P3-F/P3-H task set.
Derived lanes MUST reuse P3-F waves, dependencies, read/write paths, Gate IDs,
acceptance references, and rollback references. Missing, duplicate, unknown, or
drifted claims are `block`; missing preservation evidence is `needs-evidence`.

The state vocabulary MUST be exactly `plan-ready`, `needs-evidence`, `block`,
and `orchestration-accepted`. The latter requires an `accept` review whose
reviewer differs from the P3-J orchestrator. Review MUST NOT override source,
task, or capability drift.

P3-J records MUST be immutable, bounded, closed canonical UTF-8 JSON with
deterministic order, one trailing LF, duplicate-key rejection, unknown-field
rejection, unsupported-constant rejection, and full derived-state
recomputation. It MUST be standard-library-only and in memory and MUST perform
no filesystem, subprocess, dependency, network/provider, credential, runtime,
deployment, publication, promotion, pilot, release, or Git action.
`execution_authority`, `target_mutation_performed`, and
`execution_performed` MUST remain false. `orchestration-accepted` MUST NOT be
represented as implementation, runtime, deployment, publication, pilot, release,
or product acceptance.

## P4-1 host-integration acceptance contract

P4-1 MUST remain a repository planning contract until a later owner-approved
host transaction binds one exact host product and version, installation class,
installed-root locator, current process and window identity where applicable,
one exact APG source manifest, and one exact destination manifest. The contract
MUST NOT select a host or authorize installation, promotion, reload,
invocation, provider/network access, target execution, runtime, deployment,
publication, pilot, release, or Git action.

Host integration MUST use separate ordered evidence for host selection,
installed bytes, reload, invocation, optional provider/network use, cleanup,
rollback, and independent acceptance. Disk presence MUST NOT imply reload;
reload MUST NOT imply invocation; invocation MUST NOT imply target-project,
runtime, deployment, publication, promotion, pilot, release, or product
acceptance.

Installed-byte evidence MUST bind the approved source artifact and manifest to
every transaction-owned destination path, including expected pre-state and
post-state SHA-256 values, destination ownership, missing or extra path
treatment, and complete byte comparison. Source drift, destination drift,
aliasing, missing ownership, or an unapproved path MUST be `BLOCK`.

Reload evidence MUST use current pre-action process and window identity, an
exact approved reload method, action timestamps, post-action identity and byte
binding, relevant child-process or listener ownership, and cleanup state. An
absent or ambiguous identity, incomplete action, timeout, unexpected ownership,
post-state drift, or cleanup failure MUST be `BLOCK` and MUST cause no wider
process action.

Invocation evidence MUST bind the exact reloaded host session, approved APG
entry point, installed manifest digest, bounded non-secret input, expected
observable result, timestamps, redacted output evidence, installed-source
binding, and cleanup. Discovery, menu presence, command listing, self-report,
or provider success alone MUST NOT count as invocation acceptance.

Provider and network use MUST default to false. When required, it MUST use a
separate owner confirmation that binds provider class, network scope, cost or
quota, request and retry bounds, timeout, evidence, and cleanup. Secret values,
tokens, account data, callback payloads, machine identifiers, and production
data MUST NOT be persisted in governance evidence.

The transaction MUST use first-failure-stop and retain failed or inconclusive
evidence. Timeout MUST NOT count as pass. Rollback MUST be compare-and-swap
bounded to captured preimages and paths proven absent before the transaction;
live post-state drift MUST be `BLOCK`. Rollback MUST NOT change unrelated host
data, accounts, tokens, network or security posture, machine identity, the APG
repository baseline, or any target project.

Host acceptance MUST be exactly `ACCEPT` or `BLOCK` from a reviewer distinct
from the executor. `ACCEPT` is scoped only to the exact approved APG build and
host transaction. `CursorVIP_Dev` remains an excluded historical test carrier
and MUST NOT be treated as a P4 host, target, pilot, or dependency.

## P6-A continuity, progress, and loop-harness contract

P6-A adds independent projection records. It MUST NOT change the closed
schemas or authority semantics of P3-F, P3-G, P3-H, P3-I, P3-J, historical
receipts, or feedback-loop decisions. The projection derives a Status Snapshot
only from explicitly declared, bounded, source-bound inputs.

A `ProgressDefinition` MUST be a canonical JSON object at a contained,
configured project-relative path. It MUST declare a schema version, a stable
definition ID, the exact lifecycle source path and run ID, plan ID and SHA-256,
one target delivery phase, declared out-of-scope boundaries, and a non-empty
canonical list of work packages. Every work package MUST have a stable ID,
positive integer weight, source-bound task or evidence identifiers, and one
delivery stage. IDs and source bindings MUST be unique. Definitions with a
missing, drifted, malformed, or incomplete source are not progress authority.

A `ProgressSnapshot` MUST be pure, recomputable, bounded, and canonical. It
MUST report scope status, source binding, separate execution and independently
verified totals, current-stage totals, task counts, delivery state, Gate
health, stable reasons, no more than five ordered next actions, remaining human
gate, independent-review state, and later phase boundaries. Its percentages use
integer basis points. Progress is `not-computable` when the definition is
absent, invalid, or has no valid denominator; a controller MUST NOT infer a
percentage from elapsed time, tokens, file count, receipt count, Gate count,
or unplanned delivery phases. Blocked work remains in the denominator and
retains any already achieved work state.

The canonical `progress` command is read-only. It MUST emit a receipt whose
read-only proof has no changed paths and either canonical JSON or the fixed
human Status Snapshot. Absence of the optional definition is a valid diagnostic
result, not a Doctor failure. Doctor MUST validate a present definition's
contained lifecycle source, run ID, plan ID, plan digest, and computable task
scope when those source fields are declared, but MUST not generate, repair,
normalize, or update it.

The continuous-delivery harness MUST only plan: `INSPECT -> PROGRESS ->
PLAN_GATE -> DISPATCH -> VALIDATE -> INDEPENDENT_VERIFY -> REPORT -> REQUEUE`.
It MAY recommend `HUMAN_GATE`, `FREEZE`, or `COMPLETE` as the primary state.
`DISPATCH`, `VALIDATE`, `INDEPENDENT_VERIFY`, and `REQUEUE` require preexisting
transaction authority and remain non-executing recommendations. A planner MUST
freeze dispatch on `BLOCK`, scope drift, missing evidence, no progress, budget
exhaustion, failure threshold, or other unsafe loop state. `RECOMMEND` and
`CONFIRM` MAY pause only for the actual decision or transaction gate; routine
planning and reporting do not create approval churn.

Every material checkpoint, successful or failed terminal result, `BLOCK`, or
`CONFIRM` MUST end with exactly one Status Snapshot. It is the final section of
the response; no conclusion, classification, or extra next-step prose may
follow it. It MUST render a Status Snapshot with completed work, measured
progress or its precise absence,
current-stage state, next automatic work, at most one human gate, blocking and
review state, and separately listed unperformed delivery boundaries. A status
projection is not execution, target acceptance, runtime acceptance,
deployment, publication, pilot, release, host action, provider/network use,
or global promotion.

P6-B's APG self-roadmap is one explicit consumer of this contract. Its active
definition may measure only the declared current repository work packages up
to `repository-validated`; it cannot infer historical project completion or
represent host, runtime, deployment, publication, pilot, or release work as
part of that denominator.
