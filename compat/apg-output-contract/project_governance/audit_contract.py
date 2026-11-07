from __future__ import annotations

from collections.abc import Mapping

from .discovery import (
    DISCOVERY_PRUNED_DIRECTORY_NAMES,
    DISCOVERY_PRUNED_DIRECTORY_PREFIXES,
)
from .path_guard import WorkspaceGuard, WorkspaceSnapshot


AUDIT_CONTENT_HASH_LIMIT = 8 * 1024 * 1024
AUDIT_PROOF_MODE = "bounded-content-and-subtree-metadata"
AUDIT_STABLE_PRUNED_DIRS = frozenset({".git"})
AUDIT_PRUNED_DIRS = frozenset(
    DISCOVERY_PRUNED_DIRECTORY_NAMES
    | {
        f"{prefix}*"
        for prefix in DISCOVERY_PRUNED_DIRECTORY_PREFIXES
    }
)


def audit_proof_contract() -> dict[str, object]:
    return {
        "mode": AUDIT_PROOF_MODE,
        "content_hash_limit_bytes": AUDIT_CONTENT_HASH_LIMIT,
        "pruned_directory_names": tuple(sorted(AUDIT_PRUNED_DIRS)),
        "stable_pruned_directory_names": tuple(sorted(AUDIT_STABLE_PRUNED_DIRS)),
    }


def snapshot_for_audit(guard: WorkspaceGuard) -> WorkspaceSnapshot:
    snapshot = guard.snapshot(
        file_content_limit=AUDIT_CONTENT_HASH_LIMIT,
        pruned_dirs=AUDIT_PRUNED_DIRS,
        stable_pruned_dirs=AUDIT_STABLE_PRUNED_DIRS,
    )
    assert isinstance(snapshot, WorkspaceSnapshot)
    return snapshot


def snapshot_for_audit_receipt(
    guard: WorkspaceGuard,
    proof: Mapping[str, object],
) -> WorkspaceSnapshot:
    mode = proof.get("mode")
    if mode is None:
        snapshot = guard.snapshot()
        assert isinstance(snapshot, WorkspaceSnapshot)
        return snapshot

    contract = audit_proof_contract()
    pruned = proof.get("pruned_directory_names")
    stable_pruned = proof.get("stable_pruned_directory_names")
    if isinstance(pruned, str) or not isinstance(pruned, (list, tuple)):
        raise ValueError("audit receipt proof has invalid pruned directories")
    if isinstance(stable_pruned, str) or not isinstance(
        stable_pruned,
        (list, tuple),
    ):
        raise ValueError("audit receipt proof has invalid stable pruned directories")
    if (
        mode != contract["mode"]
        or proof.get("content_hash_limit_bytes")
        != contract["content_hash_limit_bytes"]
        or tuple(pruned) != contract["pruned_directory_names"]
        or tuple(stable_pruned)
        != contract["stable_pruned_directory_names"]
    ):
        raise ValueError("audit receipt proof contract is unsupported")
    return snapshot_for_audit(guard)


__all__ = [
    "AUDIT_CONTENT_HASH_LIMIT",
    "AUDIT_PROOF_MODE",
    "AUDIT_PRUNED_DIRS",
    "AUDIT_STABLE_PRUNED_DIRS",
    "audit_proof_contract",
    "snapshot_for_audit",
    "snapshot_for_audit_receipt",
]
