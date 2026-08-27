# AI-Native SDLC V1 Slice A

Slice A is a pure local preview facade.  It composes a canonical P3-F plan,
optional P3-G lifecycle, Work Item Board, vertical-slice evaluation, and a
closed feedback-loop stop state.  It returns least-privilege task packs plus
one of `auto-continue`, `frozen`, or `human-gate`.

It does not execute tasks, write receipts, call a provider, access a network,
mutate Git, launch a runtime, deploy, publish, pilot, or release.

## API

```python
from project_governance.ai_native_sdlc import build_ai_native_sdlc_preview

preview = build_ai_native_sdlc_preview(plan_payload, lifecycle_payload)
```

The preview canonicalizes and rechecks all derived fields when rendered or
parsed.  A P3 route returns `human-gate`; P2 returns `frozen` pending
plan-bound validation; vertical-slice evidence gaps and non-continuing
feedback-loop stop states also freeze.  `execution_performed` is always false.

## Scope

This capability implements the Slice A section of
`AI_NATIVE_SDLC_V1_DESIGN_PREVIEW.md`.  Later persistence, CLI, external
adapter, runtime, deployment, and release work remain separate transactions.
