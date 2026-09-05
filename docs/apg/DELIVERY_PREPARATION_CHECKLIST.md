# Delivery Preparation Checklist

Status: prepared; no external delivery action executed.

## Completed evidence
- [x] Offline child-parent task state machine implemented.
- [x] Proof metadata, parent review, why-answer, repair path, and append-only ledger verified.
- [x] Child payment controls hidden; external effects disabled in offline slice.
- [x] Rust and Python test suites pass.
- [x] APG doctor, audit, and full check pass.
- [x] Baseline, receipts, and rollback evidence retained.

## Required before integration
- [ ] Freeze WeChat mini-program account type and login model.
- [ ] Approve parent/guardian role matrix and deletion/export authority.
- [ ] Approve child-proof retention, deletion, withdrawal, and access rules.
- [ ] Define production object-storage adapter and retention enforcement.
- [ ] Define AI gateway policy, allowlist prompts, human escalation, and provider selection.
- [ ] Define notification policy and failure handling.
- [ ] Add integration tests against local fakes for storage, auth, and AI gateway.

## Required before deployment
- [ ] Create deployment target and environment inventory.
- [ ] Add secret/credential provisioning through an approved channel.
- [ ] Run child-safety, privacy, security, accessibility, and performance review.
- [ ] Run migration, backup, restore, and rollback rehearsal.
- [ ] Verify observability: audit events, error rates, latency, deletion jobs, and alert ownership.
- [ ] Obtain independent review acceptance.

## Required before publication
- [ ] Complete WeChat review materials and privacy disclosures.
- [ ] Confirm real-audience pilot scope, consent, support, and incident response.
- [ ] Obtain explicit owner release approval.

## Explicit exclusions in this transaction
No WeChat connection, provider call, real child data, credential creation, deployment, publication, pilot, release, or Git mutation was performed.

## Continuation condition
Resume with a new approved transaction only after integration prerequisites are evidence-bound. Release remains a separate human-gated transaction.
