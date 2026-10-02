# -*- coding: utf-8 -*-
"""真实 AI 链验证:真实网关评估→确认→权威采用→记录 ai_binding(不执行生成派发)。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opencoding import advisor, service
from opencoding.aiconfig import load_config
from opencoding.aiadapter import adapter_from_config

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from tests.test_product_service import _complete

project = Path(r"D:\OpenCoding-dev\workspace\real-ai-verify-c5")
if not (project / ".opencoding").exists():
    project.mkdir(parents=True, exist_ok=True)
    _complete(project, "验证:工房工具借还登记,本机使用,数据落盘")

sessions = service.list_sessions(project)
items = sessions.get("sessions") if isinstance(sessions, dict) else sessions
sid = items[0]["id"] if isinstance(items[0], dict) else str(items[0])

config, err = load_config()
assert config, "AI 配置缺失:" + str(err)
adapter = adapter_from_config(config)
# 诊断专用放宽:评估 prompt 大,glm-5.3 思考+生成可能超过产品默认 420s 截止(2026-09-28 实测)。
# 产品默认护栏不动,仅验证脚本放宽整体截止;idle 90s 空闲兜底仍生效。
adapter.deadline_seconds = 900.0
print("provider:", adapter.provider, "model:", adapter.model,
      "deadline:", adapter.deadline_seconds, flush=True)

t0 = time.time()
record = advisor.run_ai_evaluation(project, sid, adapter, run_id="real-c5")
print("eval %0.1fs id=%s choice=%s contract_steps=%d files=%d" % (
    time.time() - t0, record["evaluation_id"], record["structured"]["choice"],
    len((record["structured"].get("implementation_contract") or {}).get("steps") or []),
    len((record["structured"].get("implementation_contract") or {}).get("files") or [])), flush=True)

current = service.session_view(project, sid)["session"]["revision"]
confirmed = advisor.confirm_evaluation(project, sid, record["evaluation_id"],
                                       expected_revision=current, accepted=True)
print("confirmed:", confirmed["status"], flush=True)

adopted = advisor.adopt_confirmed_evaluation(project, sid)
b = adopted["adoption"]["ai_binding"]
print("adopt binding:", json.dumps({k: b[k] for k in ("evaluation_id", "ai_choice", "rule_choice_at_adopt", "source")},
                                   ensure_ascii=False), flush=True)
print("contract_sha256:", b["contract_sha256"][:16] + "…")
print("REAL AI CHAIN OK")
