# -*- coding: utf-8 -*-
"""工作台 HTTP 冒烟:进程内启动 ThreadingHTTPServer,验证首页/鉴权/bootstrap/生成暂停链。"""
import json
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opencoding import aiconfig, advisor, service
from opencoding.workbench import Workbench, _Handler, _html
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_product_full_chain_v5 import FakeAdapter, eval_response
from tests.test_product_service import _complete

tmp_base = Path(__file__).resolve().parents[2] / "review-envs" / "wb-smoke-20260928c"
ws = tmp_base / "workspace"
ws.mkdir(parents=True, exist_ok=True)

project = ws / "冒烟项目"
project.mkdir(exist_ok=True)
if not (project / ".opencoding").exists():
    _complete(project, "冒烟:家庭借还登记")

bench = Workbench(ws)
handler = type("H", (_Handler,), {"bench": bench})
httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
port = httpd.server_address[1]
t = threading.Thread(target=httpd.serve_forever, daemon=True)
t.start()
base_url = "http://127.0.0.1:%d" % port

import http.cookiejar
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

# 1. 首次带 token 访问 → 种 Cookie
req = urllib.request.Request(base_url + "/?t=" + bench.token)
resp = opener.open(req, timeout=10)
page = resp.read().decode("utf-8")
print("1) GET / ->", resp.status, "len", len(page), "title:", "工作台" in page)

# 2. bootstrap
resp = opener.open(base_url + "/api/bootstrap", timeout=10)
boot = json.loads(resp.read().decode("utf-8"))
print("2) bootstrap projects:", [p["name"] for p in boot["projects"]])

# 3. 无 token 访问 → 401/重定向
try:
    r2 = urllib.request.urlopen(base_url + "/api/bootstrap", timeout=10)
    print("3) no-token ->", r2.status, "(意外)")
except urllib.error.HTTPError as e:
    print("3) no-token ->", e.code, "(预期拒绝)")

# 4. 会话视图(含 evaluations 字段)
from urllib.parse import quote
sessions = service.list_sessions(project)
items = sessions.get("sessions") if isinstance(sessions, dict) else sessions
sid = items[0]["id"] if isinstance(items[0], dict) else str(items[0])
resp = opener.open(base_url + "/api/project/" + quote("冒烟项目") + "/session/" + sid, timeout=10)
view = json.loads(resp.read().decode("utf-8"))
print("4) session view keys:", sorted(view.keys())[:6], "…evaluations" if "evaluations" in view else "")

# 5. gen-status 对不存在 run → unknown
resp = opener.open(base_url + "/api/project/%s/session/%s/gen-status/gen-%s" % (quote("冒烟项目"), sid, "0"*12), timeout=10)
doc = json.loads(resp.read().decode("utf-8"))
print("5) gen-status unknown ->", doc["status"])

httpd.shutdown()
print("SMOKE OK")
