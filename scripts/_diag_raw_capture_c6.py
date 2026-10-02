# -*- coding: utf-8 -*-
"""C6 根因取证:真实派发一次 points 契约,抓取模型原始响应(脱敏摘要)。

只做一次真实请求;产品代码不改。原始响应只落:长度/SHA256/首尾片段/括号平衡,
不落完整正文(可能含模型自由文本),并做敏感串脱敏。
"""
import hashlib
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from opencoding import aiconfig, generic_run, service  # noqa: E402
from opencoding import aiadapter  # noqa: E402
from opencoding.aiadapter import adapter_from_config  # noqa: E402

BASE = Path(r"D:\OpenCoding-dev\workspace")
PROJECT = BASE / "real-c6-points"

CAPTURED: list[str] = []
_orig = aiadapter.WorkBuddyGatewayAdapter._extract_json_text


def _spy(content: str):
    CAPTURED.append(content)
    return _orig(content)


aiadapter.WorkBuddyGatewayAdapter._extract_json_text = staticmethod(_spy)

# ---- 假设验证:网关未显式给 max_tokens,默认上限可能截断长输出 -----------------
# 只在本诊断进程内给请求体补 max_tokens,不改产品代码。
MAX_TOKENS_PROBE = int(__import__("os").environ.get("C6_MAX_TOKENS", "0"))
if MAX_TOKENS_PROBE:
    import urllib.request as _u

    _RealRequest = _u.Request

    class _PatchedRequest(_RealRequest):  # type: ignore[misc]
        def __init__(self, url, data=None, headers=None, *args, **kwargs):
            if isinstance(data, (bytes, bytearray)) and b'"messages"' in data:
                try:
                    obj = json.loads(data.decode("utf-8"))
                    obj["max_tokens"] = MAX_TOKENS_PROBE
                    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    pass
            super().__init__(url, data=data, headers=headers or {}, *args, **kwargs)

    aiadapter.urllib.request.Request = _PatchedRequest  # type: ignore[attr-defined]
    print("max_tokens probe =", MAX_TOKENS_PROBE, flush=True)


def _redact(text: str) -> str:
    out = text
    for token in ("x-wb-webapp-access-key", "publishable_key", "access_key", "api_key",
                  "Authorization", "Bearer "):
        out = out.replace(token, "<redacted>")
    return out


def _summary(raw: str) -> dict:
    b = raw.encode("utf-8")
    balance = 0
    for ch in raw:
        if ch == "{":
            balance += 1
        elif ch == "}":
            balance -= 1
    return {
        "bytes": len(b),
        "sha256": hashlib.sha256(b).hexdigest(),
        "starts_with_brace": raw.strip().startswith("{"),
        "ends_with_brace": raw.strip().endswith("}"),
        "brace_balance": balance,
        "head_1500": _redact(raw[:1500]),
        "tail_1500": _redact(raw[-1500:]),
        "line_count": raw.count("\n") + 1,
    }


def main() -> int:
    config, err = aiconfig.load_config()
    if not config:
        print("AI 配置缺失:" + str(err))
        return 2
    adapter = adapter_from_config(config)
    adapter.deadline_seconds = 900.0

    sessions = service.list_sessions(PROJECT)
    items = sessions.get("sessions") if isinstance(sessions, dict) else sessions
    sid = items[0]["id"] if isinstance(items[0], dict) else str(items[0])

    view = service.session_view(PROJECT, sid)
    contract = None
    # 契约来自上一次真实评估:从已采用记录里取
    adoption = service.current_plan_source(PROJECT, sid) if hasattr(service, "current_plan_source") else None
    decision = view.get("decision") or {}
    contract = (decision.get("implementation_contract")
                or (view.get("session") or {}).get("implementation_contract"))
    report: dict = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "session_id": sid,
                    "model": adapter.model, "provider": adapter.provider}
    if not contract:
        contract = _load_contract_from_adoption(PROJECT, sid)
    if not contract or not contract.get("files"):
        report["error"] = "未取到已确认契约,不派发"
        out = BASE / ("c6-raw-capture-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print("REPORT", out)
        print(json.dumps(report, ensure_ascii=False))
        return 3

    run_id = "gen-" + ("%012x" % (int(time.time()) % (16 ** 12)))
    # C6-04:诊断脚本**不自行扩额**。额度不足或账本不可读时如实落账并停止,
    # 扩额必须由操作者按产品流程给予新的明确授权,不在取证脚本里静默续费。
    state = generic_run.task_budget_state(PROJECT, generic_run.TASK_ID, sid)
    report["budget_before"] = state
    if int(state.get("remaining") or 0) < 1 or state.get("ledger_unreadable"):
        report["budget_blocked"] = (
            "累计额度不足或账本不可读,诊断脚本不自行扩额;"
            "如需继续取证,请由操作者按产品流程给予新的明确授权后再运行")
        out = BASE / ("c6-raw-capture-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print("REPORT", out)
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 4
    receipt = None
    # 前一运行已在回执中明确失败(no received material / 无候选目录 / 业务区零写入),
    # 故此处显式确认接受其不确定性;此判断与依据一并写入报告,不留暗账。
    ACK_NOTE = ("已核实:run gen-00006aba588c 两次尝试均为 response_not_json,"
                "回执无 received_material、无候选目录、项目内除 .opencoding 运行状态外无任何业务文件,"
                "因此其效果并非未知;显式确认以便继续取证。")
    report["acknowledged_unknown"] = ACK_NOTE
    try:
        receipt = generic_run.run_generic_app(
            PROJECT, "小店会员积分登记:网页后台、多店员共用、需要账号", adapter,
            contract=generic_run._validate_contract(contract),
            session_id=sid, run_id=run_id, max_repair_rounds=0,
            acknowledged_unknown=True)
    except BaseException as exc:  # noqa: BLE001 - 宿主删除闸门会抛 SystemExit,也要落证据
        report["exception"] = type(exc).__name__ + ":" + str(exc)[:300]
        traceback.print_exc()
    report["run_id"] = run_id
    report["status"] = (receipt or {}).get("status")
    report["ai_requests_dispatched"] = (receipt or {}).get("ai_requests_dispatched")
    report["failure"] = ((receipt or {}).get("failure") or "")[:200]
    report["raw_captures"] = [_summary(r) for r in CAPTURED]
    out = BASE / ("c6-raw-capture-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("REPORT", out)
    print(json.dumps({k: v for k, v in report.items() if k != "raw_captures"},
                     ensure_ascii=False, indent=1))
    for i, s in enumerate(report["raw_captures"]):
        print("--- capture", i, {k: s[k] for k in ("bytes", "sha256", "starts_with_brace",
                                                   "ends_with_brace", "brace_balance", "line_count")})
    return 0


def _load_contract_from_adoption(project: Path, sid: str) -> dict | None:
    """从已确认的评估记录里取契约(不再新增一次评估请求)。"""
    ev_dir = project / ".opencoding" / "evaluations"
    newest = None
    for path in sorted(ev_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("session_id") != sid:
            continue
        struct = data.get("structured") or {}
        contract = struct.get("implementation_contract")
        if isinstance(contract, dict) and contract.get("files"):
            newest = contract
    return newest


if __name__ == "__main__":
    raise SystemExit(main())
