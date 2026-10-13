---
name: opencoding
description: Unified OpenCoding entry for turning a plain-language software idea or existing project into an audit-first, previewable, executable, verified workflow.
metadata:
  version: "0.1.0"
  display_name: "OpenCoding"
---

# OpenCoding

Use this as the product-facing entry for `/opencoding`, `opencoding`, or requests to turn a software idea or existing project into a governed AI-coding workflow.

OpenCoding is the user-facing name. `adaptive-project-governance` (APG) remains the underlying compatibility name. Do not reimplement or rename the APG controller from this entry.

## Routing

1. Establish the authorized project root and read its local rules before substantive work.
2. Run a read-only `audit` first and review its receipt.
3. For a new project, prepare an `init` preview. For an existing, imported, or previously refactored project, prepare an `adopt` preview after the audit.
4. For a requested structural change, use `plan-change` preview; for verification, use `doctor` or the appropriate `check` phase.
5. Obtain explicit authorization for the bounded project write before using `--apply` or making external changes.
6. Preserve user changes, unmanaged files, receipts, rollback evidence, and human Gates.

## Runtime resolution

When this skill is installed from the `opencoding-apg` repository, prefer the repository's own entry point:

```text
python -m opencoding --root <authorized-project> --status --json
```

For audit, adoption, plan-change, and check operations, use the host-local `adaptive-project-governance/scripts/project_governance.py` when that compatibility controller is installed. If neither the repository runtime nor the APG controller is available, report the missing installation instead of inventing a path or claiming that the workflow ran.

Use `--apply` only after the user has authorized the exact project root, bounded paths, approval inputs, and rollback treatment. Do not perform global configuration changes, dependency changes, deployment, credential use, payment, remote Git operations, or publication merely because OpenCoding was invoked.

## Product boundary

OpenCoding turns a plain-language idea into a project-specific plan, documents, task dependencies, execution waves, verification evidence, and recovery guidance. It does not promise universal Agent, Provider, model, deployment, or production-project compatibility. External actions remain human Gates.
