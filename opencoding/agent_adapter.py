"""Host-independent structured execution adapter for Agent/skill callers.

The adapter is intentionally small: it translates a reviewed local action into
the existing strict ``Executor`` contract.  It does not provide an AI model,
shell, network, credentials, or a claim that the target project is complete.

``python_module`` and ``node_script`` are bounded subprocess interfaces, not a security sandbox.
The imported module runs with the calling user's operating-system permissions
and can have side effects (including network access initiated by that module).
Callers must use a reviewed root and an explicit authorization record; a
transaction receipt is the only rollback evidence available from this layer.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import platform
import shutil
import sys
import uuid
from typing import Any, Mapping

from .executor import Executor, action_digest
from .host_capabilities import build_capability_matrix, normalize_target_platform
from .target_adapters import target_adapter_status
from .safety import _is_reparse, _reject_linked_ancestors, canonical_json, sha256_bytes

ADAPTER_SCHEMA_VERSION = "1.1"
_UNSPECIFIED_PREIMAGE = object()
_TARGET_CONTRACT_LABELS = frozenset({"windows", "macos", "web"})


def _has_target_contract(value: Any) -> bool:
    return isinstance(value, str) and normalize_target_platform(value)["normalized"] in _TARGET_CONTRACT_LABELS


class AgentAdapterError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _confirmation_binding(*, confirmation_id: str, root: str, action_digest_value: str,
                         targets: list[str], scope: str, expires_at: str) -> str:
    """Bind the caller's confirmation label to the exact local authorization."""
    payload = {
        "confirmation_id": confirmation_id,
        "root": root,
        "action_digest": action_digest_value,
        "targets": list(targets),
        "scope": scope,
        "expires_at": expires_at,
    }
    return sha256_bytes(canonical_json(payload))


class LocalAgentAdapter:
    """Execute structured local actions under an explicit root.

    File transactions can be rolled back by the transaction layer; an
    arbitrary ``python_module`` action is not intrinsically reversible.
    """

    adapter_id = "local-structured-python"

    def __init__(self, root: str | os.PathLike[str] | Path):
        path = Path(root)
        if not path.is_absolute():
            raise AgentAdapterError("root_invalid", "adapter root must be absolute")
        # Hand the original path to Executor.  Resolving first would turn a
        # symlink alias into an apparently safe root and bypass its linked
        # ancestor checks.
        try:
            self.executor = Executor(path)
        except (OSError, ValueError) as exc:
            raise AgentAdapterError("root_invalid", "adapter root must be an existing unlinked directory") from exc
        self.root = self.executor.root

    def authorization_for(
        self,
        action: Mapping[str, Any],
        *,
        targets: list[str] | None = None,
        scope: str = "synthetic-local-confirmation",
        confirmation_id: str | None = None,
        ttl_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """Build a short-lived authorization bound to one exact local action.

        The confirmation identifier is content-bound to the root, action,
        targets, scope, and expiry.  This does not turn synthetic confirmation
        into a real user confirmation. Each resulting binding is consumed once
        by ``execute`` through a local atomic claim.
        """
        action_value = dict(action)
        digest = action_digest(action_value)
        expected_targets = [action_value["path"]] if action_value.get("type") in {"write_text", "node_script"} else []
        if targets is None:
            targets = expected_targets
        if targets != expected_targets:
            raise AgentAdapterError("targets_mismatch", "targets 必须与结构化动作精确一致")
        if not isinstance(scope, str) or not scope.strip():
            raise AgentAdapterError("scope_invalid", "授权 scope 必须是非空字符串")
        if not isinstance(ttl_seconds, (int, float)) or isinstance(ttl_seconds, bool) or not 0 < ttl_seconds <= 3600:
            raise AgentAdapterError("expiry_invalid", "授权有效期必须在 1 秒到 1 小时之间")
        expires = datetime.now(timezone.utc) + timedelta(seconds=float(ttl_seconds))
        confirmation_id = confirmation_id or f"confirm-{uuid.uuid4().hex}"
        return {
            "approved": True,
            "external": False,
            "cost_limit": 0,
            "scope": scope,
            "confirmation_id": confirmation_id,
            "root": str(self.root),
            "action_digest": digest,
            "targets": list(targets),
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
            "confirmation_binding": _confirmation_binding(
                confirmation_id=confirmation_id,
                root=str(self.root),
                action_digest_value=digest,
                targets=list(targets),
                scope=scope,
                expires_at=expires.isoformat().replace("+00:00", "Z"),
            ),
        }

    def capabilities(
        self,
        *,
        target_platform: str | None = None,
        toolchain_observation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Separate host facts from the requested project target platform."""
        matrix = build_capability_matrix(
            target_platform=target_platform,
            toolchain_observation=toolchain_observation,
        )
        target_adapter = target_adapter_status(
            target_platform,
            toolchain_observation=toolchain_observation,
        ) if _has_target_contract(target_platform) else None
        return {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "adapter_id": self.adapter_id,
            "host": {"os": os.name, "platform": sys.platform, "python": platform.python_version()},
            "target_platform": target_platform,
            "actions": ["write_text", "python_module", "node_script"],
            "tools": {"python": shutil.which(sys.executable) or sys.executable},
            "external": False,
            "sandbox": False,
            "process_boundary": "same-user-subprocess",
            "model": {"available": False, "reason": "本适配器不提供模型；由调用方另行接入并确认。"},
            "capability_matrix": matrix,
            "target_adapter": target_adapter,
        }

    def _claim_confirmation(self, binding: str, *, confirmation_id: str, scope: str, expires_at: str) -> None:
        """Atomically consume one local confirmation binding.

        This is a same-user local replay policy, not a signature or a host-wide
        identity service. A receipt rollback never removes this claim.
        """
        metadata = self.root / ".opencoding"
        claims = metadata / "authorizations"
        _reject_linked_ancestors(metadata)
        for directory in (metadata, claims):
            if directory.exists() or directory.is_symlink():
                if directory.is_symlink() or _is_reparse(directory) or not directory.is_dir():
                    raise AgentAdapterError("authorization_claim_unsafe", "授权消费目录不是安全目录")
            else:
                try:
                    directory.mkdir()
                except FileExistsError:
                    # A same-root caller may have created it between the
                    # existence check and mkdir; revalidate below.
                    pass
        _reject_linked_ancestors(claims)
        target = claims / f"{binding}.json"
        payload = canonical_json({
            "schema": "opencoding-authorization-claim-v1",
            "confirmation_binding": binding,
            "confirmation_id": confirmation_id,
            "root": str(self.root),
            "scope": scope,
            "expires_at": expires_at,
        }) + b"\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(target, flags, 0o600)
        except FileExistsError as exc:
            raise AgentAdapterError("authorization_replayed", "该本地确认已消费，必须重新确认") from exc
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                target.unlink()
            except OSError:
                pass
            raise

    def execute(
        self,
        action: Mapping[str, Any],
        *,
        authorization: Mapping[str, Any],
        targets: list[str] | None = None,
        data_scope: str = "synthetic-local",
        run_id: str | None = None,
        timeout_seconds: float = 30.0,
        target_platform: str | None = None,
        toolchain_observation: dict[str, Any] | None = None,
        transactional_write: bool = False,
        expected_before_sha256: Any = _UNSPECIFIED_PREIMAGE,
    ) -> dict[str, Any]:
        if not isinstance(action, Mapping):
            raise AgentAdapterError("action_invalid", "结构化动作必须是对象")
        action_value = dict(action)
        if action_value.get("type") == "write_text" and not isinstance(action_value.get("path"), str):
            raise AgentAdapterError("action_invalid", "write_text 必须包含字符串 path")
        try:
            digest = action_digest(action_value)
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentAdapterError("action_invalid", "结构化动作未通过本地适配器校验") from exc
        expected_targets = [action_value["path"]] if action_value.get("type") in {"write_text", "node_script"} else []
        if targets is None:
            targets = expected_targets
        if targets != expected_targets:
            raise AgentAdapterError("targets_mismatch", "targets 必须与结构化动作精确一致")
        if not isinstance(authorization, Mapping) or type(authorization.get("approved")) is not bool or not authorization["approved"]:
            raise AgentAdapterError("authorization_required", "本地执行必须绑定明确的 approved=true 授权记录")
        if authorization.get("external") is not False or authorization.get("cost_limit", 0) != 0:
            raise AgentAdapterError("external_action_rejected", "适配器只接受无外部影响、零费用动作")
        if not isinstance(authorization.get("confirmation_id"), str) or not authorization["confirmation_id"].strip():
            raise AgentAdapterError("authorization_binding_invalid", "授权必须有 confirmation_id")
        if authorization.get("root") != str(self.root):
            raise AgentAdapterError("authorization_binding_invalid", "授权 root 与适配器 root 不一致")
        if authorization.get("action_digest") != digest:
            raise AgentAdapterError("authorization_binding_invalid", "授权 action_digest 与动作不一致")
        if authorization.get("targets") != list(targets):
            raise AgentAdapterError("authorization_binding_invalid", "授权 targets 与动作不一致")
        scope = authorization.get("scope")
        if not isinstance(scope, str) or not scope.strip():
            raise AgentAdapterError("authorization_binding_invalid", "授权 scope 无效")
        expires_at = authorization.get("expires_at")
        if not isinstance(expires_at, str):
            raise AgentAdapterError("authorization_expired", "授权必须包含带时区的 expires_at")
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AgentAdapterError("authorization_expired", "授权 expires_at 无效") from exc
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise AgentAdapterError("authorization_expired", "授权已过期")
        expected_binding = _confirmation_binding(
            confirmation_id=authorization["confirmation_id"],
            root=str(self.root),
            action_digest_value=digest,
            targets=list(targets),
            scope=scope,
            expires_at=expires_at,
        )
        if authorization.get("confirmation_binding") != expected_binding:
            raise AgentAdapterError("authorization_binding_invalid", "confirmation_id 与授权内容未绑定")
        if not isinstance(data_scope, str) or data_scope not in {"synthetic", "synthetic-local", "synthetic-local-documents", "local"}:
            raise AgentAdapterError("data_scope_invalid", "数据范围必须是合成或本地范围")
        try:
            matrix = build_capability_matrix(
                target_platform=target_platform,
                toolchain_observation=toolchain_observation,
            )
        except (TypeError, ValueError) as exc:
            raise AgentAdapterError("toolchain_observation_invalid", "工具链观察记录未通过来源、目标或命令证据校验") from exc
        if type(transactional_write) is not bool:
            raise AgentAdapterError("action_invalid", "transactional_write must be boolean")
        file_plan = None
        if transactional_write:
            if action_value["type"] != "write_text":
                raise AgentAdapterError("action_invalid", "only write_text supports file transactions")
            from .transactions import preview_changes
            file_plan = preview_changes(self.root, {action_value["path"]: action_value["content"]})
            if expected_before_sha256 is not _UNSPECIFIED_PREIMAGE and file_plan["entries"][0]["before_sha256"] != expected_before_sha256:
                raise AgentAdapterError("preview_drifted", "文件前像与确认预览不一致")
        self._claim_confirmation(
            expected_binding,
            confirmation_id=authorization["confirmation_id"],
            scope=scope,
            expires_at=expires_at,
        )
        try:
            context = {
                "schema_version": "1.0",
                "root": str(self.root),
                "action_digest": digest,
                "targets": list(targets),
                "external": False,
                "cost_limit": 0,
                "data_scope": data_scope,
                "irreversible": False,
            }
            if file_plan is None:
                result = self.executor.execute(action_value, context, run_id=run_id, timeout_seconds=timeout_seconds)
            else:
                from .transactions import apply_changes
                transaction = apply_changes(self.root, file_plan, approved_digest=file_plan["plan_digest"])
                passed = transaction.get("status") == "applied"
                result = {
                    "schema_version": "1.0", "run_id": run_id or "run-" + uuid.uuid4().hex,
                    "status": "succeeded" if passed else "failed", "exit_code": 0 if passed else None,
                    "action_digest": digest, "transaction": transaction,
                    "artifacts": [{"path": action_value["path"], "sha256": file_plan["entries"][0]["after_sha256"]}] if passed else [],
                    "stdout_summary": "", "stderr_summary": "", "live_verified": False,
                }
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentAdapterError("action_invalid", "结构化动作未通过本地适配器校验") from exc
        result["adapter_id"] = self.adapter_id
        result["authorization_scope"] = scope
        result["external"] = False
        result["sandbox"] = False
        result["process_boundary"] = "same-user-subprocess"
        result["capability_observation"] = {
            "schema_version": "opencoding-execution-observation-v1",
            "action_type": action_value["type"],
            "host": matrix["host"],
            "target": matrix["target"],
            "toolchain": matrix["toolchain"],
            "target_toolchain_verified": matrix["compatibility"]["target_toolchain_verified"],
            "model": matrix["model"],
            "external": matrix["external"],
            "sandbox": matrix["sandbox"],
            "process_boundary": matrix["process_boundary"],
            "managed_loader": matrix["managed_loader"],
            "boundary": "adapter-observed-result; target-label-is-not-target-execution",
            "target_adapter": target_adapter_status(
                target_platform,
                toolchain_observation=toolchain_observation,
            ) if _has_target_contract(target_platform) else None,
        }
        result["authorization_binding"] = {
            "confirmation_id": authorization["confirmation_id"],
            "root": str(self.root),
            "action_digest": digest,
            "targets": list(targets),
            "scope": scope,
            "expires_at": expires_at,
            "confirmation_binding": expected_binding,
            "replay_policy": "one_time_local",
        }
        return result

__all__ = ["ADAPTER_SCHEMA_VERSION", "AgentAdapterError", "LocalAgentAdapter"]
