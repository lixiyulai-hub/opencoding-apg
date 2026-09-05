# MVP Foundation Implementation Slice

This slice implements the offline-first child-parent task loop without providers or real data.

## Included
- Task state machine: assigned → accepted → awaiting-review → completed/returned → repairable.
- Proof metadata submission using local references only.
- Parent approval or return with an explicit reason.
- One why-question answer after approval.
- Append-only reward ledger with deterministic balance.
- Child/parent UI contract for the WeChat mini-program surface.

## Excluded
Authentication, payments, object storage, AI voice, notifications, provider calls, deployment, and publication remain separate transactions.
