# P3-D Project Materialization Preview

P3-D is the deterministic, offline contract after P3-C. It turns an exact
canonical `ImplementationReadiness` record and a complete downstream proposal
into a reviewable materialization preview. It does not materialize a project.

## Required Bindings

The preview binds the complete canonical P3-C result, its SHA-256 digest, the
embedded P3-B blueprint digest, P3-C evidence, and a caller-supplied policy
digest. The P3-C result must be reparsed and recomputed exactly. Only
`ready-for-materialization-preview` with
`ready_for_materialization_preview=true` can proceed beyond `BLOCK`.

The downstream proposal must name a bounded root locator, manifest identifier,
manifest entries, expected pre-state for every proposed path, approval state,
configured Gates, acceptance references, rollback identifier, and policy
digest. P3-D never discovers those facts from the filesystem or prose.

`downstream_root` is a stable logical code, not a physical filesystem path,
URL, or provider locator. Every `baseline_entries.expected_sha256` value is a
compare-and-swap pre-state: a SHA-256 digest means the named path must have
those exact bytes, while `null` means the named path must be absent. `null`
never means unknown, unchecked, or unrestricted. A later authorized apply must
verify these assertions against the separately approved physical root.

## Preview States

| State | Meaning |
| --- | --- |
| `block` | P3-C is not ready or a source record is malformed, drifted, or unsafe. |
| `pending-user-input` | A downstream root, manifest, baseline, approval, Gate, acceptance, or rollback input is incomplete. |
| `preview-ready` | The source and proposal are complete enough for owner review only. |

Every output sets `preview_only=true` and `apply_authority=false`. Even a
`preview-ready` result cannot write a downstream root, run a Gate, install a
dependency, launch a runtime, call a provider or network, deploy, publish,
promote, pilot, release, merge, or push.

## Canonical and Safety Rules

The normative form is closed canonical UTF-8 JSON with sorted keys, compact
separators, NFC strings, deterministic tuples, and one trailing LF. It rejects
unknown fields, duplicate keys or identifiers, non-canonical bytes, digest
drift, unsafe locators, traversal, root escape, duplicate or mismatched paths,
secret-shaped values, and unbounded payloads.

P3-D performs no filesystem discovery or write, subprocess execution,
dependency discovery or installation, approval creation, Gate selection or
execution, provider/network call, runtime launch, deployment, publication,
promotion, downstream mutation, Git operation, pilot, or release action.

## Later Apply Boundary

A later downstream transaction must bind an exact physical root, frozen
manifest content, pre-state hashes or absence, approved write paths, applicable
Gates, acceptance criteria, and compare-and-swap rollback. P3-E classifies the
transaction before it asks a person to intervene.

## P3-E Apply Transaction Controller

P3-E is the repository-local controller for a later, explicitly supplied
physical root. It consumes an exact canonical P3-D preview plus the matching
manifest bytes. The P3-D logical `downstream_root` remains only an identifier;
the physical root is a separate runtime input and is fingerprinted without
being rendered in the canonical result.

The ordinary path is automatic. A user should receive the recommended path and
the final acceptance result, while the controller performs routine scope,
digest, policy, pre-state, post-state, rollback, and evidence checks itself.
The controller never turns an absent fact into an automatic success.

| Classification | Behavior |
| --- | --- |
| `AUTO` | Proceed only for a bounded, reversible, no-secret, no-network, no-cost, no-credential, no-real-data local transaction whose policy digest and evidence references match P3-D. |
| `RECOMMEND` | Return a safe recommendation and make no write. |
| `CONFIRM` | Pause only for provider or network access, cost or quota, credentials, real or production data, public delivery, runtime launch, deployment, irreversible change, security or privacy posture change, or materially ambiguous direction. One owner approval or one exact expiring authorization session must bind the transaction ID, P3-D digest, physical-root fingerprint, allowed paths, policy, and complete reason set. |
| `BLOCK` | Stop on incomplete evidence, unbounded scope, secret-shaped content, unsafe path or root, policy or preview drift, a compare-and-swap mismatch, snapshot tampering, or rollback drift. |

`AUTO` is not a broad permission. It is available only after the frozen P3-D
preview, policy digest, evidence references, supplied content hashes, physical
root, and full pre-state agree. It never widens the manifest or converts a
logical root locator into a filesystem location.

### Transaction Sequence

1. Parse and recompute the exact canonical P3-D preview.
2. Verify the caller-supplied content bytes match every frozen manifest hash
   and no additional path is present.
3. Classify the action. `AUTO` needs no owner interruption; `RECOMMEND` writes
   nothing; `CONFIRM` checks a fresh transaction-bound owner approval or an
   exact authorization session already approved for this child transaction.
4. Resolve a regular absolute physical root, reject links, reparse points,
   traversal, and root escape, then read every compare-and-swap pre-state.
5. On execution, reread pre-state, retain a separate snapshot with hashes,
   write only the declared files, and verify every post-write hash.
6. Roll back only while every live post-state hash still matches. Restore
   captured bytes or delete only paths that were absent before the transaction.

The P3-E controller is standard-library-only. It does not discover a target,
generate a product plan, select a provider, contact a network, install a
dependency, launch a runtime, deploy, publish, promote, pilot, release, or
perform a Git operation on its own. Repository-local P3-E tests, Doctor, and
Gate results do not prove downstream, runtime, provider/network, deployment,
publication, promotion, pilot, or release acceptance.

Rollback for the APG implementation transaction is limited to its declared
repository paths. Downstream rollback remains a separate, owner-authorized
transaction and must preserve unrelated dirty state and historical evidence.

### Delegated authorization session

One canonical `AuthorizationSession` may cover several pre-bound P3-E child
transactions after one real owner Gate. It is not a broad standing permission:
its owner, explicitly listed transaction IDs, policy digest, one P3-D preview
digest, one physical-root preimage, one exact manifest scope, one complete
consequential reason set, evidence refs, issuance time, and UTC expiry are all
checked before the plan becomes `ready`. Every child must match that exact
source, root, scope, and reason set; a session does not combine alternative
sources or pair an arbitrary transaction ID with a different source. A
lifecycle-bound P3-G session is never valid for P3-E. A new path, root, source
digest, risk reason, provider/network, Git, runtime, deployment, publication,
pilot, release, or other delivery boundary is not session reuse; it is a new
`CONFIRM` or `BLOCK` decision.
