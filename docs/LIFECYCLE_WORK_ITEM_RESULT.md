# P3-G Next-Unblocked Work Item Result

`lifecycle_work_item_result` is the Ralph-inspired bridge from the P3-G
goal-delivery lifecycle to the read-only Work Item Board. It gives an ordinary
user the exact next-unblocked work item IDs without exposing the lifecycle's
operator trace or granting execution authority.

## Source Binding

`build_lifecycle_work_item_result` accepts canonical P3-G lifecycle bytes. It
rebuilds the Work Item Board from the lifecycle's exact P3-F plan and lifecycle
bytes, binds both SHA-256 digests, and copies only the board's
`next_work_item_ids`. Rendering and parsing recompute the same projection, so a
changed lifecycle, board digest, result code, phase, or work item ID is
rejected.

The compact `lifecycle_work_item_user_result` exposes the existing P3-G
`status`, `result`, `next_step`, and `phase` plus `next_work_items`. The latter
is a canonical comma-separated list, or `none` when no AUTO work item is
unblocked. `present_lifecycle_work_item_result` adds that compact result to the
ordinary user presentation envelope.

## Authority Boundary

This projection is read-only. `execution_performed` is structurally fixed to
`false`; it does not dispatch a task, run a Gate, mutate a repository, or
broaden approval. `RECOMMEND`, `CONFIRM`, blocked, and dependency-pending
states can therefore produce `next_work_items=none` even when P3-G itself has a
different next interaction.
