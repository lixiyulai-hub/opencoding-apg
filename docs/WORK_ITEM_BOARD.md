# Work Item Board Projection

`project_governance.work_item_board` exposes the existing P3-F task plan and optional P3-G lifecycle as a canonical, read-only board. It does not dispatch work, grant authorization, mutate lifecycle state, or infer external execution.

## Inputs

- An exact canonical `AutonomousTaskPlan` payload is required.
- A canonical `GoalDeliveryLifecycle` payload is optional and must bind the same plan digest.

## Per-item contract

Each `WorkItem` carries the P3-F task ID, wave index, bound action-context source references, dependencies, slice goal (`output_code`), phase/gate integration surfaces, acceptance references, rollback reference, state, and one next-action code.

States are derived only from the source plan/lifecycle:

- `unblocked`: current dependency-closed AUTO task; next action is preparation only.
- `accepted`: P3-G has accepted the task.
- `blocked`: P3-F or P3-G has a blocking result.
- `needs-evidence`: dependencies, a decision, or an approval remain unresolved.

`next_work_item_ids` contains only `unblocked` work items. Rendering recomputes the projection from its P3-F/P3-G inputs and rejects altered board fields.
