<!-- project-governance:begin -->
<!-- project-governance:metadata policy-version=0.1.0 policy-digest=93e44a674b20aa6cb3fd6b75e70a9b1a663d16e104dfa8c9f98cab92f9e697f7 generator-version=1 scope=. body-digest=6b1a1c702170f463b9b35bc88e382f425266f53d1f7ad34afccb10ce1f910199 -->
Run governance checks from the project root.
Required phases: inspect, validate, verify, report.
Validation command: - `python -X utf8 -m unittest`
Do not install tools globally or claim external protections.
Read only the project rules and references relevant to the requested change.
The controller may format its own results; this adapter does not impose a fixed
response schema on ordinary agent prose.
<!-- project-governance:end -->

## Final Status Snapshot

For every material checkpoint, successful or failed terminal result, `BLOCK`,
or `CONFIRM`, the final user-facing response MUST end with exactly one
`Status Snapshot` section. It is the final section: no trailing classification,
conclusion, or additional next-step text may appear after it.

The snapshot MUST state the current phase, completed work, source-bound total
progress and current-stage progress (or `not-computable` with the missing
source), current delivery and Gate state, next automatic work, at most one
real human gate, blockers and independent-review state, later delivery
boundaries, and the exact Continuation/resume condition. When a program
roadmap is declared, it MUST also state the separate program total and current
program-stage percentages, the immediate program transaction, the following
program stage, and the ordered successor transactions. The immediate program
transaction is the user's next actionable work; a later confirmation boundary
must not be presented as if it were the immediate step. Never infer a
percentage from time, tokens, changed files, receipts, or Gate counts.
The repository controller mechanically enforces this envelope for its own
human-readable non-JSON routes. Arbitrary host or model final prose is not
intercepted by repository code; host/adapter enforcement remains a later
separate lifecycle transaction.
