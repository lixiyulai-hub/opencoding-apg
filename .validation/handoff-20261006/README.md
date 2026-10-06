# Exact candidate validation

Product candidate: `537c1506a05beb8daabdaca4e667afb9fbbe8196`.
Product tree: `f617ff1eba53c32ec18011b913e480761f4896f1`.
Baseline: `5d68c2075620df0be005c76f904b256b980d303e`.
Historical source label: `13398c3ab25f98d0a7d08cb782d4554e1e5f14de`.
The original commit object was not recovered. The handoff's complete path set
and 679 SHA256 fingerprints match the restored candidate; all 118 published
file modes/sizes also match. Excluded file modes were not supplied in the
original manifest and are inherited from the exact baseline and original diff.

The historical Windows checklist demanded the unavailable original commit.
This validation uses the real restored commit, complete Git tree and manifest.
It does not fabricate the original commit identity or substitute old CI results.

The workflow checks out tools at the pushed validation commit and product source
at the fixed candidate separately. It records both the GitHub event commit and
actual product commit/tree. The binding SHA256 is:

`7044d9659497cb93c2891bcbacf4ad2362907cf41207019a698af3a85e24288c`

On Windows, use Python 3.11 and 3.12 separately with setuptools>=83 and Rust
stable already prepared. From PowerShell, supply the chosen Python executable:

```powershell
./run_windows_validation.ps1 -Repository <candidate-checkout> `
  -PythonVersion 3.11 -PythonExecutable <python-executable> `
  -BindingSha256 7044d9659497cb93c2891bcbacf4ad2362907cf41207019a698af3a85e24288c `
  -EvidenceDirectory <new-directory-outside-checkout>
```

Repeat with PythonVersion 3.12 and its executable, using a new evidence directory.
The script installs nothing, runs tests offline, stops on failures or skips,
records verbose test IDs, counts, native exit codes and log hashes, and checks
source identity before and after testing. Adapter/full/packaging expected counts
are 19/247/6; Rust expects 4 passing tests. Skipped symlink tests are incomplete
acceptance, even when unittest exits zero. Both Python matrix legs must pass.

The six baseline historical fixture symlinks can be Git-for-Windows regular
link-text files only at the explicitly bound artifacts paths, with exact blob
bytes. Every such representation is reported; no runtime source file is covered
by this allowance. Git object modes remain verified. This says nothing about
Windows symlink protection: the dedicated adapter test must actually execute.
Checkout uses LF bytes (`core.autocrlf=false`) for exact file fingerprints.

The verifier requires clean status and exact paths, commit, tree, modes, blobs,
SHA256 and checkout bytes, including direct checks beyond Git's dirty flags.
It is a consistency check in a trusted local environment, not an OS sandbox or
identity authentication. Review the delivered binding/script hashes before use.
Raw local logs can contain machine paths; redact copies before sharing and retain
original and delivered hashes separately. Hosted CI keeps results in job logs.

Current evidence: source binding and nine negative/positive verifier checks pass
on Linux; 19 restored adapter tests pass. PowerShell and actual Windows execution
remain pending. No unchanged Linux full-suite rerun was performed.

This branch adds validation tooling and a workflow only; it does not change the
restored product source. Pushing `validation/windows-handoff-20261006` will run
this two-leg workflow and the pre-existing five-job workflow. Those older jobs
test the validation branch; only the new workflow explicitly tests the fixed
product candidate above. Creating or pushing this remote branch requires separate
authorization. No PR update, merge, release, deployment or public-path cleanup
is included.
