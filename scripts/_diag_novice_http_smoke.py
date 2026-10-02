# -*- coding: utf-8 -*-
"""新手(小白)使用验收:真起 HTTP 服务,按页面按钮的真实顺序走一遍。

不做模型调用:只验收"从零到能看见计划"的本地流程与中文提示是否可用,
以及未确认评估时点生成是不是被清楚拦住(而不是静默失败或假成功)。
"""
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from opencoding import workbench  # noqa: E402
from opencoding.intake import QUESTION_DEFINITIONS  # noqa: E402

workspace = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"D:\OpenCoding-dev\workspace\novice-smoke-c6")
workspace.mkdir(parents=True, exist_ok=True)
bench = workbench.Workbench(workspace)
handler = type("_BenchHandler", (workbench._Handler,), {"bench": bench})
httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"
token = bench.token
print("server:", base, "token:", token[:8] + "…", flush=True)

steps: list[dict] = []


def call(method, path, body=None, label=""):
    opener = urllib.request.build_opener()
    opener.addheaders = [("Cookie", "oc_token=" + token)]
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(base + urllib.parse.quote(path), data=data, method=method)
    req.add_header("Content-Type", "application/json")
    started = time.time()
    try:
        with opener.open(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            status = resp.status
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode("utf-8") or "{}")
        status = exc.code
    except Exception as exc:  # noqa: BLE001
        payload = {"error": type(exc).__name__ + ":" + str(exc)[:200]}
        status = 0
    record = {"step": label or path, "method": method, "path": path,
              "http": status, "ms": int((time.time() - started) * 1000)}
    if status >= 400:
        record["code"] = payload.get("code")
        record["message"] = str(payload.get("message", ""))[:200]
    steps.append(record)
    return status, payload


opener = urllib.request.build_opener()
opener.addheaders = [("Cookie", "oc_token=" + token)]
page = opener.open(base + "/", timeout=30).read().decode("utf-8")
steps.append({"step": "打开页面(带 Cookie 的首页 HTML)", "method": "GET", "path": "/",
              "http": 200, "ms": 0, "html_bytes": len(page),
              "has_goal_input": ("想做什么" in page) or ("目标" in page),
              "has_generate_button": "生成" in page,
              "has_resume_hint": ("继续" in page) or ("resume" in page)})
status, _ = call("GET", "/api/ai/status", label="查看 AI 接入状态")

project = "新手验收-借还登记"
call("POST", "/api/project", {"name": project}, label="新建项目")
status, created = call("POST", f"/api/project/{project}/session",
     {"goal": "登记邻居借走的工具并确认归还"}, label="新建会话(一句话目标)")
print("create-session:", status, json.dumps(created, ensure_ascii=False)[:400])
_, listing = call("GET", f"/api/project/{project}", label="读取会话列表")
sessions = listing if isinstance(listing, list) else (listing.get("sessions") or [])
sid = sessions[0]["id"]
steps[-1]["session_id"] = sid

_, view = call("GET", f"/api/project/{project}/session/{sid}", label="读取问答页")
revision = view["session"]["revision"] if "session" in view else view["revision"]
for question in QUESTION_DEFINITIONS:
    answer = {"audience": "社区居民", "outcome": "登记借用并确认归还",
              "platform": "网页"}.get(question["id"], "不需要")
    _, result = call("POST", f"/api/project/{project}/session/{sid}/answer",
                     {"revision": revision, "question_id": question["id"], "answer": answer},
                     label="回答:" + str(question.get("short") or question["id"]))
    revision = result["session"]["revision"] if isinstance(result, dict) and "session" in result else revision
steps.append({"step": "问答完成", "revision": revision})

call("GET", f"/api/project/{project}/session/{sid}/preview", label="只读预览(不写入)")
call("GET", f"/api/project/{project}/session/{sid}", label="再次读取(方案/计划)")
# 未确认评估就点生成:必须被清楚拦住
call("POST", f"/api/project/{project}/session/{sid}/generate", {}, label="未确认评估点生成(应被拦)")
# 没有可接续候选时,resume 必须给出可理解的错误
call("POST", f"/api/project/{project}/session/{sid}/resume",
     {"run_id": "gen-000000000000"}, label="无候选时点接续(应报错)")

httpd.shutdown()
report = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "workspace": str(workspace),
          "steps": steps,
          "blocked_generation": next((s for s in steps if s["step"].startswith("未确认")) , None),
          "resume_error": next((s for s in steps if s["step"].startswith("无候选")), None)}
out = workspace / ("novice-http-smoke-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print("REPORT:", out)
for item in steps:
    print(" ", item.get("http"), item["step"], item.get("code") or "", item.get("message", "")[:80])
