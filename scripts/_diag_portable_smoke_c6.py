# -*- coding: utf-8 -*-
"""0.2.6 便携包冒烟:用包内私有运行时导入产品包、启动工作台、核对版本。"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import http.cookiejar
from pathlib import Path

PORTABLE = Path(r"D:\OpenCoding-dev\dist\OpenCoding-0.2.6-portable")
PY = PORTABLE / "runtime-python" / "python.exe"
WS = Path(r"D:\OpenCoding-dev\workspace-portable-smoke")
WS.mkdir(exist_ok=True)

# 1. 包内版本核对
r = subprocess.run([str(PY), "-X", "utf8", "-c",
                    "import opencoding, importlib.metadata as m;"
                    "print(opencoding.__file__);"
                    "print(m.version('opencoding-local-entry'))"],
                   capture_output=True, text=True, encoding="utf-8",
                   env={"PYTHONHOME": str(PORTABLE / "runtime-python"),
                        "PYTHONPATH": str(PORTABLE / "site-packages"),
                        "PATH": str(PORTABLE / "runtime-python") + ";" },
                   timeout=60)
print("1) portable import:", r.stdout.strip().splitlines()[-1] if r.returncode == 0 else r.stderr[-300:])
assert r.returncode == 0 and "0.2.6" in r.stdout

# 2. 启动工作台(后台进程,20 秒后自动退出;stdout 走文件,PIPE 在此环境下读不到启动行——2026-09-28 实测)
code = '''
import threading, time, sys
sys.argv = ["x", "--workspace", r"%s", "--port", "8765"]
from opencoding.workbench import main
t = threading.Timer(20, lambda: (__import__("os")._exit(0)))
t.daemon = True
t.start()
main()
''' % str(WS)
out_file = WS / "_smoke_server_out.txt"
# env 继承系统环境(保留 USERPROFILE/HOMEDRIVE/HOMEPATH,Path.home() 需要),再注入包内运行时
child_env = dict(os.environ)
child_env.update({"PYTHONHOME": str(PORTABLE / "runtime-python"),
                  "PYTHONPATH": str(PORTABLE / "site-packages"),
                  "PATH": str(PORTABLE / "runtime-python") + ";" + os.environ.get("PATH", "")})
proc = subprocess.Popen([str(PY), "-u", "-X", "utf8", "-c", code],
                        env=child_env,
                        stdout=open(out_file, "w", encoding="utf-8"),
                        stderr=subprocess.STDOUT, text=True)
# 3. 轮询输出文件抓入口 URL,再走 HTTP 验证
url = None
for _ in range(40):
    time.sleep(0.5)
    try:
        text = out_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    for line in text.splitlines():
        if "http://127.0.0.1" in line:
            url = line[line.index("http://"):].strip()
            break
    if url:
        break
    if proc.poll() is not None:
        break
print("2) workbench url:", (url or "NOT FOUND")[:60])
assert url, "工作台未打印入口"

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
resp = opener.open(url, timeout=10)
print("3) GET / ->", resp.status, len(resp.read()), "bytes")
resp = opener.open("http://127.0.0.1:8765/api/bootstrap", timeout=10)
boot = json.loads(resp.read().decode("utf-8"))
print("4) bootstrap ok, capability kind:", boot.get("capability", {}).get("kind"))
proc.terminate()
print("PORTABLE SMOKE OK")
