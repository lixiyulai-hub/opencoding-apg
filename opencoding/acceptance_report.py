"""User-readable acceptance status and human-gate explanations."""

from __future__ import annotations

from typing import Any

from .capability_contract import build_capability_contract


ACCEPTANCE_REPORT_SCHEMA_VERSION = "opencoding-acceptance-report-v1"
ERROR_USER_MESSAGES = {
    "toolchain_observation_invalid": "工具链观察记录无效或来源、目标、命令证据不一致；已在写入前停止。",
    "authorization_binding_invalid": "授权没有绑定当前项目、动作、目标或确认内容；已在写入前停止。",
    "external_action_rejected": "动作要求外部影响或费用；当前本地适配器拒绝执行。",
    "authorization_required": "缺少明确的本地确认授权；不会执行文件动作。",
    "authorization_expired": "本地授权已过期；需要重新确认。",
    "authorization_replayed": "本地确认已消费过；需要新的确认，旧回执不会被重放。",
}


def _check(identifier: str, status: str, reason: str, *, gate: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {"id": identifier, "status": status, "reason": reason}
    if gate is not None:
        result["human_gate"] = gate
    return result


def build_acceptance_report(contract: dict[str, Any] | None = None, *, run_root=None, run_id: str | None = None, expected_run_digest: str | None = None) -> dict[str, Any]:
    contract = contract or build_capability_contract()
    evidence = None
    reason = "No current product-run evidence was supplied; historical tests do not establish this project."
    if run_root is not None and run_id is not None and expected_run_digest is not None:
        from .product_evidence import verify_product_evidence
        try:
            evidence = verify_product_evidence(run_root, run_id, expected_run_digest)
            reason = "Current run receipt, plan, skill/source hashes, file bytes and non-empty successful Python tests verified. Same-user integrity only."
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            reason = "Current evidence rejected: " + str(error)[:240]
    state = "observed" if evidence is not None else "unverified"
    checks: list[dict[str, Any]] = [
        _check("local_structured_actions", state, reason),
        _check("skill_project_discovery", state, reason + " Managed host loading remains separate."),
    ]
    for target, details in contract["targets"].items():
        checks.append(_check(
            f"{target}_toolchain",
            details["toolchain_status"],
            details["toolchain"].get("scope") or details["toolchain"].get("reason") or "Toolchain profile result.",
        ))
        checks.append(_check(f"{target}_target_execution", details["target_execution_status"], details["target_execution_reason"]))
    checks.extend([
        _check("managed_loader", "unverified", "No managed host skill-loader response was observable in this environment."),
        _check("provider_or_model", "blocked", "No provider or model call was requested or configured.", gate={
            "required": True,
            "request": "Before any provider call, explicitly approve endpoint, model, data scope, maximum calls and spend cap.",
        }),
        _check("external_services", "blocked", "Network services, credentials, payment, notification and deployment paths are outside this offline acceptance run.", gate={
            "required": True,
            "request": "A human must explicitly approve the specific external service and its data/side-effect scope.",
        }),
        _check("real_user_confirmation", "blocked", "Synthetic confirmations in fixtures are not real user approval.", gate={
            "required": True,
            "request": "A real user must review the exact preview, root, targets and actions before product execution.",
        }),
    ])
    return {
        "schema_version": ACCEPTANCE_REPORT_SCHEMA_VERSION,
        "status_vocabulary": ["observed", "unverified", "blocked"],
        "checks": checks,
        "error_messages": ERROR_USER_MESSAGES,
        "human_gate_summary": "Blocked checks require explicit approval; unverified checks require additional environment evidence; observed checks are limited to their stated scope.",
        "contract": contract,
        "run_evidence": evidence,
    }


def render_acceptance_markdown(report: dict[str, Any]) -> str:
    lines = ["# OpenCoding acceptance status", "", report["human_gate_summary"], "", "| Check | Status | Reason |", "|---|---|---|"]
    for check in report["checks"]:
        reason = check["reason"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{check['id']}` | **{check['status']}** | {reason} |")
    lines.extend(["", "## Human Gates", ""])
    for check in report["checks"]:
        gate = check.get("human_gate")
        if gate:
            lines.append(f"- `{check['id']}`: {gate['request']}")
    lines.extend(["", "## Error messages", ""])
    for code, message in sorted(report["error_messages"].items()):
        lines.append(f"- `{code}` — {message}")
    return "\n".join(lines) + "\n"


__all__ = ["ACCEPTANCE_REPORT_SCHEMA_VERSION", "ERROR_USER_MESSAGES", "build_acceptance_report", "render_acceptance_markdown"]
