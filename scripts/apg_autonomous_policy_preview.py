#!/usr/bin/env python3
"""Offline APG autonomous PRG policy simulator.

The simulator models the beginner path without invoking a host, provider,
network, runtime, deployment target, Git, or credentials.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

CONSEQUENTIAL_CODES = (
    "secret",
    "money",
    "network",
    "deployment-choice",
    "real-data",
    "git-release",
    "irreversible-external-action",
)

TERMS = {
    "secret": ("secret", "password", "token", "密钥", "密码", "令牌"),
    "money": ("money", "payment", "charge", "付费", "付款", "金钱"),
    "network": ("network", "internet", "api", "联网", "网络", "接口"),
    "deployment-choice": ("deploy", "deployment", "hosting", "部署", "上线", "托管"),
    "real-data": ("real data", "production data", "真实数据", "生产数据"),
    "git-release": ("git push", "release", "publish", "发布", "推送"),
    "irreversible-external-action": ("delete", "send", "不可逆", "删除", "发送"),
}

_SECRET_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;]+|"
    r"\b(?:sk|ghp)_[A-Za-z0-9_-]{8,}\b"
)


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def redact(text: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", text)


def consequential_reasons(request: str) -> tuple[str, ...]:
    folded = request.casefold()
    reasons = [code for code in CONSEQUENTIAL_CODES if any(term.casefold() in folded for term in TERMS[code])]
    return tuple(reasons)


def _beginner_message(route: str, reasons: tuple[str, ...]) -> str:
    if route == "auto":
        return "已自动整理计划并继续执行，不需要你理解技术细节。"
    return "这一步会产生外部影响，系统只在这里请你做一次选择。"


def simulate(request: str, *, failure: str | None = None) -> dict[str, Any]:
    if not isinstance(request, str) or not request.strip():
        raise ValueError("request must be non-empty text")
    safe_request = redact(request.strip())
    reasons = consequential_reasons(safe_request)
    route = "consequential-gate" if reasons else "auto"
    states = ["INSPECT", "PROGRESS", "PLAN", "DISPATCH", "VALIDATE", "REPORT", "REQUEUE"]
    resume = "resume.after-bounded-loop-continues-without-stop-condition"
    if failure:
        failure_code = failure.replace(" ", "-").casefold()
        states = ["INSPECT", "PROGRESS", "PLAN", "DISPATCH", "VALIDATE", "FREEZE"]
        resume = "resume.after-" + failure_code + "-is-resolved"
    return {
        "schema_version": "1.0",
        "request": safe_request,
        "route": route,
        "human_gate": bool(reasons),
        "gate_reasons": list(reasons),
        "default_policy": "auto-select-safe-defaults",
        "beginner_message": _beginner_message(route, reasons),
        "loop_states": states,
        "dispatch_permitted": not bool(reasons) and failure is None,
        "execution_performed": False,
        "resume_condition": resume,
        "external_actions": {
            "host": False,
            "provider": False,
            "network": False,
            "runtime": False,
            "deployment": False,
            "git": False,
            "publication": False,
        },
    }


def replay_digest(request: str, *, failure: str | None = None) -> str:
    return hashlib.sha256(canonical_bytes(simulate(request, failure=failure))).hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: apg_autonomous_policy_preview.py <request> [--failure CODE]", file=sys.stderr)
        return 2
    failure = None
    if "--failure" in args:
        index = args.index("--failure")
        if index + 1 >= len(args):
            return 2
        failure = args[index + 1]
        del args[index:index + 2]
    result = simulate(" ".join(args), failure=failure)
    sys.stdout.buffer.write(canonical_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
