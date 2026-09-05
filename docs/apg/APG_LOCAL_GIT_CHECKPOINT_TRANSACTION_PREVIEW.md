# APG Local Git Checkpoint Transaction Preview (2026-09-03)

Scope: APG-only offline preview. Current state is not-a-local-git-repository. This preview does not initialize .git, create commits, branches, tags, remotes, or perform external actions.

Read-only preconditions: git-safety classification=warn; read_only_proof.passed=true; changed_paths=[]; project_git.kind=absent; status_code=128; .git/HEAD=false; .git/config=false; project-local identity=false.

Bound hashes: target_fingerprint=69d56f2e1d89802c22028f2918cb1d9f060c9d14c9ad49589c474738b7622536; ledger_sha256=4409c75327f2db0a1f48e03edef7b048ea0af02c7b70fbffe33c4bbc639b6b8c; idempotency_marker_sha256=1ac767c6d80a74d9777e5d052d26dc04a0d67382f17e9afd2a1ac528c19e6f94.

Future bounded write paths (none written now): .git/HEAD, .git/index, .git/objects/, .git/refs/heads/apg-local-checkpoint, .git/logs/HEAD, .git/logs/refs/heads/apg-local-checkpoint, .git/COMMIT_EDITMSG. Fixed branch: apg-local-checkpoint.

Gate-G1 is the only consequential gate: before any future local Git mutation, owner confirms fixed paths, fixed branch, preimage manifest, commit content, and rollback snapshot. Gate-G1 is not consumed by this preview.

State: git_action=PREVIEW_ONLY; execution_performed=false; external_actions all false; resume_condition=local-git-preflight-pass-and-owner-gate-g1-confirmed; next_action=record-checkpoint-preview.

Evidence: RESULT.json, MODIFIED_FILE, DIFF_FILE, VERIFICATION.txt, ROLLBACK.sh. Rollback removes only this transaction document and evidence directory; it never touches .git, Ledger, receipts, changes, or snapshots.
