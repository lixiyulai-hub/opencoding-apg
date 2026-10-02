# -*- coding: utf-8 -*-
"""本地重放审计方原版探针(读取实际新源码)——证据输出到本轮 evidence 目录。
探针文件原样使用审计包 reviewer/,不改写;仅在本 runner 中重定向 OUT 路径。"""
import sys, json, traceback
from pathlib import Path

SRC = r"E:/儿童知行星球/.tmp/opencoding-product-foundation-20260905/integration"
REVIEWER = r"C:/Users/Administrator/AppData/Local/Temp/c6fix5-audit-r1/reviewer"
sys.path.insert(0, SRC)
sys.path.insert(0, REVIEWER)

OUT_DIR = Path(sys.argv[1])
OUT_DIR.mkdir(parents=True, exist_ok=True)
import independent_probes as ip
ip.OUT = OUT_DIR

FUNCS = [ip.p01_service_positive, ip.q02_confirm_types, ip.q03_scope_budget,
         ip.p04_revocation_during_confirmation, ip.q05_late_official_revoke,
         ip.q06_two_confirmations_last_write, ip.q07_confirmation_write_fails,
         ip.p05_real_listener, ip.p06_listener_unknown, ip.p07_unknown_entitlement,
         ip.p08_parent_absent, ip.p09_noncanonical_root]

result = {"environment": {"python": sys.version}, "observations": {}}
for fn in FUNCS:
    try:
        value = fn()
    except BaseException as exc:
        value = {"probe_error": type(exc).__name__, "detail": str(exc),
                 "traceback": traceback.format_exc()}
    result["observations"][fn.__name__] = value
    brief = json.dumps(value, ensure_ascii=False)
    print(fn.__name__, brief[:220], flush=True)
(OUT_DIR / "INDEPENDENT_OBSERVATIONS.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print("PROBES_DONE")
