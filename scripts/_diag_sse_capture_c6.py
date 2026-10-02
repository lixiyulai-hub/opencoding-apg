# -*- coding: utf-8 -*-
"""C6 根因取证(最后一次):抓真实 SSE 原始帧,判断模型为何返回空 content。

只保存:帧数、字节数、首尾片段(脱敏)、reasoning_content / finish_reason 计数。
不保存完整流。产品代码不改。
"""
import json
import os
import sys
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from opencoding import aiconfig, generic_run, service  # noqa: E402
from opencoding import aiadapter  # noqa: E402
from opencoding.aiadapter import adapter_from_config  # noqa: E402

BASE = Path(r"D:\OpenCoding-dev\workspace")
PROJECT = BASE / "real-c6-points"
MAX_TOKENS_PROBE = int(os.environ.get("C6_MAX_TOKENS", "16384"))

CHUNKS: list[bytes] = []
_real_urlopen = urllib.request.urlopen


class _Tee:
    def __init__(self, raw):
        self._raw = raw
        self.fp = raw.fp

    def read1(self, n):
        c = self._raw.read1(n)
        if c:
            CHUNKS.append(c)
        return c

    def read(self, n=-1):
        c = self._raw.read(n)
        if c:
            CHUNKS.append(c)
        return c

    def __enter__(self):
        self._raw.__enter__()
        return self

    def __exit__(self, *exc):
        return self._raw.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._raw, name)


def _patched_urlopen(req, *a, **kw):
    resp = _real_urlopen(req, *a, **kw)
    return _Tee(resp)


def _patched_request_init(self, url, data=None, headers=None, *args, **kwargs):
    if isinstance(data, (bytes, bytearray)) and b'"messages"' in data:
        try:
            obj = json.loads(data.decode("utf-8"))
            if MAX_TOKENS_PROBE:
                obj["max_tokens"] = MAX_TOKENS_PROBE
            CHUNKS.append(b"__REQUEST__" + json.dumps(
                {"model": obj.get("model"), "response_format": obj.get("response_format"),
                 "stream": obj.get("stream"),
                 "messages_bytes": len(json.dumps(obj.get("messages"), ensure_ascii=False)),
                 "max_tokens": obj.get("max_tokens")}, ensure_ascii=False).encode("utf-8"))
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except (ValueError, UnicodeDecodeError):
            pass
    _real_init(self, url, data=data, headers=headers or {}, *args, **kwargs)


_real_init = urllib.request.Request.__init__
urllib.request.Request.__init__ = _patched_request_init
urllib.request.urlopen = _patched_urlopen


def _redact(t: str) -> str:
    for tok in ("x-wb-webapp-access-key", "access_key", "publishable_key", "Bearer "):
        t = t.replace(tok, "<redacted>")
    return t


def main() -> int:
    config, err = aiconfig.load_config()
    if not config:
        print("AI 配置缺失:", err)
        return 2
    adapter = adapter_from_config(config)
    adapter.deadline_seconds = 900.0
    sessions = service.list_sessions(PROJECT)
    items = sessions.get("sessions") if isinstance(sessions, dict) else sessions
    sid = items[0]["id"] if isinstance(items[0], dict) else str(items[0])

    contract = None
    ev_dir = PROJECT / ".opencoding" / "evaluations"
    for path in sorted(ev_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("session_id") == sid:
            c = (data.get("structured") or {}).get("implementation_contract")
            if isinstance(c, dict) and c.get("files"):
                contract = c
    report: dict = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "session_id": sid,
                    "max_tokens_probe": MAX_TOKENS_PROBE}
    if not contract:
        report["error"] = "契约缺失"
        print(json.dumps(report, ensure_ascii=False))
        return 3

    run_id = "gen-" + ("%012x" % (int(time.time()) % (16 ** 12)))
    report["run_id"] = run_id
    # C6-04:诊断脚本**不得改名/移除运行锁**。锁占用与否只读探查;
    # 是否接管交给产品自身的 stale 判定(心跳过期 + owner 进程确认死亡),
    # 绕过锁等于绕过并发保护,取证脚本无此权限。
    lock_path = PROJECT / ".opencoding" / "generic_runs" / ".active-lock"
    if lock_path.exists():
        try:
            info = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        report["lock_blocked"] = {
            "lock": str(lock_path),
            "owner_run_id": info.get("run_id"),
            "owner_pid": info.get("pid"),
            "note": "存在活跃运行锁,诊断脚本不改锁;"
                    "若属上次残留,由产品 stale 判定接管或由操作者按流程处置",
        }
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 5
    receipt = None
    try:
        receipt = generic_run.run_generic_app(
            PROJECT, "小店会员积分登记:网页后台、多店员共用、需要账号", adapter,
            contract=generic_run._validate_contract(contract), session_id=sid,
            run_id=run_id, max_repair_rounds=0, acknowledged_unknown=True)
    except BaseException as exc:  # noqa: BLE001
        report["exception"] = type(exc).__name__ + ":" + str(exc)[:200]
        traceback.print_exc()

    text = b"".join(CHUNKS).decode("utf-8", errors="replace")
    report["sse"] = {
        "chunks": len(CHUNKS),
        "bytes": len(text.encode("utf-8")),
        "count_reasoning_content": text.count("reasoning_content"),
        "count_finish_reason": text.count("finish_reason"),
        "finish_reasons": sorted(set(
            seg.split('"', 2)[1] for seg in text.split('"finish_reason":"')[1:]
            if '"' in seg))[:10],
        "count_data_lines": text.count("data:"),
        "count_done": text.count("[DONE]"),
        "head_1200": _redact(text[:1200]),
        "tail_1200": _redact(text[-1200:]),
    }
    report["status"] = (receipt or {}).get("status")
    out = BASE / ("c6-sse-capture-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("REPORT", out)
    print(json.dumps({k: v for k, v in report.items() if k != "sse"}, ensure_ascii=False, indent=1))
    print("sse:", json.dumps({k: v for k, v in report["sse"].items()
                              if k not in ("head_1200", "tail_1200")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
