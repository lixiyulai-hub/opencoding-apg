# Vertical Slice Invariant Evaluator

The evaluator converts a canonical `WorkItemBoard` into an explicit, source-bound assessment for each P3-F/P3-G task. It is a pure projection: it never executes a task, issues a decision, opens an authorization session, or updates lifecycle state.

## Required invariants

Every assessment checks that the work item has:

1. source references;
2. a phase plus at least one test or gate integration surface;
3. dependency closure;
4. acceptance references;
5. a rollback reference; and
6. independent-review evidence before an item may be reported `accepted`.

A P3-G accepted task must bind independent, accepting review evidence. A blocked P3-F/P3-G work item is reported `blocked`, including dependent propagation already derived by P3-G. Planned work without sufficient completed evidence is reported `needs-evidence`; the evaluator does not turn planned work into successful execution.

## Canonical boundary

`render_vertical_slice_evaluation` recomputes every assessment from the embedded Work Item Board. `parse_vertical_slice_evaluation` rejects altered fields, non-canonical JSON, unknown fields, and source digest drift.
