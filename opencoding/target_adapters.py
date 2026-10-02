"""Fail-closed target execution adapter contracts.

The local structured adapter can write reviewed files and run Python tests on
the host.  It cannot claim that a Windows, macOS, or web target was executed.
This module gives those planning labels a stable adapter interface and a
machine-readable status report for a future target-specific implementation.

``status_report`` may include a separately verified toolchain observation, but
that observation never promotes target execution to ``observed``.  ``execute``
therefore fails closed for the contract adapters below; no target command,
browser, SDK, deployment, model, or network is invoked.
"""

from __future__ import annotations

from typing import Any, Mapping

from .host_capabilities import build_capability_matrix, normalize_target_platform


TARGET_ADAPTER_SCHEMA_VERSION = "opencoding-target-adapter-v1"


class TargetAdapterError(ValueError):
    """Structured error from a target adapter contract."""

    def __init__(self, code: str, message: str, *, report: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.report = dict(report or {})


_TARGET_SPECS: dict[str, dict[str, Any]] = {
    "windows": {
        "adapter_id": "target-windows-contract",
        "toolchain_profile": "windows-dotnet-runtime",
        "scope": "Windows/.NET runtime contract only; SDK, GUI, signing, packaging and execution are unverified",
        "host_families": {"windows"},
    },
    "macos": {
        "adapter_id": "target-macos-contract",
        "toolchain_profile": "macos-swift-runtime",
        "scope": "macOS/Swift runtime contract only; Xcode, signing, packaging and execution are unverified",
        "host_families": {"macos"},
    },
    "web": {
        "adapter_id": "target-web-contract",
        "toolchain_profile": "node-web-runtime",
        "scope": "Node/npm runtime contract only; browser, framework, bundler, and deployment are unverified",
        "host_families": {"linux", "macos", "windows", "posix-other"},
    },
}


def _host_override(host_family: str | None) -> tuple[str | None, str | None]:
    """Map deterministic contract-test host families to matrix facts."""
    if host_family is None:
        return None, None
    values = {
        "linux": ("posix", "linux"),
        "macos": ("posix", "darwin"),
        "windows": ("nt", "win32"),
        "posix-other": ("posix", "freebsd"),
    }
    if host_family not in values:
        raise TargetAdapterError("host_family_invalid", "未知宿主系统族")
    return values[host_family]


class TargetAdapter:
    """Interface implemented by a target-specific execution provider later."""

    target_platform: str
    adapter_id: str

    def status_report(
        self,
        *,
        toolchain_observation: dict[str, Any] | None = None,
        host_family: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def execute(
        self,
        action: Mapping[str, Any],
        *,
        toolchain_observation: dict[str, Any] | None = None,
        host_family: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError


class ContractTargetAdapter(TargetAdapter):
    """A declarative adapter that refuses target execution until implemented."""

    def __init__(self, target_platform: str):
        target = normalize_target_platform(target_platform)
        if target["normalized"] not in _TARGET_SPECS:
            raise TargetAdapterError("target_unsupported", "当前只提供 Windows、macOS 和 Web 目标契约")
        self.target_platform = target["normalized"]
        self.adapter_id = _TARGET_SPECS[self.target_platform]["adapter_id"]
        self._spec = _TARGET_SPECS[self.target_platform]

    def status_report(
        self,
        *,
        toolchain_observation: dict[str, Any] | None = None,
        host_family: str | None = None,
    ) -> dict[str, Any]:
        host_os, host_platform = _host_override(host_family)
        try:
            matrix = build_capability_matrix(
                target_platform=self.target_platform,
                host_os=host_os,
                host_platform=host_platform,
                toolchain_observation=toolchain_observation,
            )
        except (TypeError, ValueError) as error:
            # Preserve host/target facts while making malformed evidence a
            # blocked state.  No target action may be attempted afterwards.
            matrix = build_capability_matrix(
                target_platform=self.target_platform,
                host_os=host_os,
                host_platform=host_platform,
            )
            return self._report(
                matrix,
                status="blocked",
                execution_status="blocked",
                reason="工具链观察记录无效，拒绝目标执行：" + str(error),
                error_code="toolchain_observation_invalid",
            )

        host = matrix["host"]["family"]
        if host not in self._spec["host_families"]:
            reason = f"宿主 {host} 与目标 {self.target_platform} 不匹配，未提供跨宿主执行器"
        elif self.target_platform == "web":
            reason = "Node/npm 运行时观察不等于浏览器执行；未提供浏览器/框架执行器"
        else:
            reason = "目标工具链观察不等于目标执行；未提供该目标的 SDK/执行器"
        return self._report(
            matrix,
            status="unverified",
            execution_status="unverified",
            reason=reason,
        )

    def _report(
        self,
        matrix: dict[str, Any],
        *,
        status: str,
        execution_status: str,
        reason: str,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        execution = {
            "status": execution_status,
            "observed": False,
            "attempted": False,
            "reason": reason,
        }
        report: dict[str, Any] = {
            "schema_version": TARGET_ADAPTER_SCHEMA_VERSION,
            "adapter_id": self.adapter_id,
            "target": matrix["target"],
            "host": matrix["host"],
            "status": status,
            "execution": execution,
            "toolchain": matrix["toolchain"],
            "target_toolchain_verified": matrix["compatibility"]["target_toolchain_verified"],
            "model": matrix["model"],
            "external": matrix["external"],
            "sandbox": matrix["sandbox"],
            "process_boundary": matrix["process_boundary"],
            "managed_loader": matrix["managed_loader"],
            "scope": self._spec["scope"],
            "contract_only": True,
        }
        if error_code:
            report["error"] = {"code": error_code, "message": reason}
        return report

    def execute(
        self,
        action: Mapping[str, Any],
        *,
        toolchain_observation: dict[str, Any] | None = None,
        host_family: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(action, Mapping):
            raise TargetAdapterError("action_invalid", "目标动作必须是对象")
        report = self.status_report(toolchain_observation=toolchain_observation, host_family=host_family)
        if report["status"] == "blocked":
            raise TargetAdapterError("target_capability_blocked", report["execution"]["reason"], report=report)
        # The contract adapter deliberately has no target side effects.  A
        # future Windows/macOS/browser adapter must provide its own observed
        # execution proof before this branch can be enabled.
        raise TargetAdapterError("target_execution_unverified", report["execution"]["reason"], report=report)


class WindowsTargetAdapter(ContractTargetAdapter):
    def __init__(self):
        super().__init__("windows")


class MacOSTargetAdapter(ContractTargetAdapter):
    def __init__(self):
        super().__init__("macos")


class WebTargetAdapter(ContractTargetAdapter):
    def __init__(self):
        super().__init__("web")


_ADAPTER_TYPES = {
    "windows": WindowsTargetAdapter,
    "macos": MacOSTargetAdapter,
    "web": WebTargetAdapter,
}


def get_target_adapter(target_platform: str) -> TargetAdapter:
    target = normalize_target_platform(target_platform)
    adapter_type = _ADAPTER_TYPES.get(target["normalized"])
    if adapter_type is None:
        raise TargetAdapterError("target_unsupported", "当前只提供 Windows、macOS 和 Web 目标契约")
    return adapter_type()


def target_adapter_status(
    target_platform: str | None,
    *,
    toolchain_observation: dict[str, Any] | None = None,
    host_family: str | None = None,
) -> dict[str, Any]:
    """Return a status report for one target label without executing it."""
    if target_platform is None:
        return {
            "schema_version": TARGET_ADAPTER_SCHEMA_VERSION,
            "status": "unverified",
            "execution": {"status": "unverified", "observed": False, "attempted": False,
                           "reason": "未指定目标平台"},
            "contract_only": True,
        }
    return get_target_adapter(target_platform).status_report(
        toolchain_observation=toolchain_observation,
        host_family=host_family,
    )


__all__ = [
    "TARGET_ADAPTER_SCHEMA_VERSION",
    "TargetAdapterError",
    "TargetAdapter",
    "ContractTargetAdapter",
    "WindowsTargetAdapter",
    "MacOSTargetAdapter",
    "WebTargetAdapter",
    "get_target_adapter",
    "target_adapter_status",
]
