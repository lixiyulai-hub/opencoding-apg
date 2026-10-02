# -*- coding: utf-8 -*-
"""CP5 后续轮:多项目真实 AI 链(评估→确认→采用→派发→受限后端实况→接续入口)。

只做真实网关调用与真实状态记录;不伪造任何"已执行/已交付"结论:
- 无合格执行资格(受限后端未验证)时,派发会以 blocked_execution 终态化并保留候选;
- 该状态下接续入口必须可用(零新 AI 请求),这是"环境好了继续原任务"的真实路径。
"""
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from opencoding import aiconfig, advisor, generic_run, service  # noqa: E402
from opencoding.aiadapter import adapter_from_config  # noqa: E402
from tests.test_product_service import _complete  # noqa: E402

BASE = Path(r"D:\OpenCoding-dev\workspace")
BASE.mkdir(parents=True, exist_ok=True)

CASES = [
    ("real-c6-lending", "社区工具借还登记:单机使用、数据落盘", "命令行"),
    ("real-c6-points", "小店会员积分登记:网页后台、多店员共用、需要账号", "网页"),
]

report: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "cases": []}

config, err = aiconfig.load_config()
if not config:
    print("AI 配置缺失:" + str(err))
    raise SystemExit(2)
adapter = adapter_from_config(config)
# 诊断专用放宽整体截止(glm-5.3 评估实测约 400s);产品默认护栏不动。
adapter.deadline_seconds = 900.0
print("provider:", adapter.provider, "model:", adapter.model,
      "deadline:", adapter.deadline_seconds, flush=True)

for name, goal, platform in CASES:
    entry: dict = {"project": name, "goal": goal, "answers_platform": platform}
    try:
        project = BASE / name
        if not (project / ".opencoding").exists():
            project.mkdir(parents=True, exist_ok=True)
            _complete(project, goal, platform)
        sessions = service.list_sessions(project)
        items = sessions.get("sessions") if isinstance(sessions, dict) else sessions
        sid = items[0]["id"] if isinstance(items[0], dict) else str(items[0])
        entry["session_id"] = sid

        t0 = time.time()
        record = advisor.run_ai_evaluation(project, sid, adapter, run_id="real-c6")
        struct = record.get("structured") or {}
        contract = struct.get("implementation_contract") or {}
        entry["evaluation"] = {
            "evaluation_id": record["evaluation_id"],
            "seconds": round(time.time() - t0, 1),
            "choice": struct.get("choice"),
            "contract_steps": len(contract.get("steps") or []),
            "contract_files": len(contract.get("files") or []),
            "real": bool(record.get("real")),
        }
        print("[%s] eval %ss choice=%s steps=%d" % (
            name, entry["evaluation"]["seconds"], struct.get("choice"),
            entry["evaluation"]["contract_steps"]), flush=True)

        revision = service.session_view(project, sid)["session"]["revision"]
        advisor.confirm_evaluation(project, sid, record["evaluation_id"],
                                   expected_revision=revision, accepted=True)
        adopted = advisor.adopt_confirmed_evaluation(project, sid)["adoption"]
        binding = adopted.get("ai_binding") or {}
        entry["adoption"] = {
            "ai_choice": binding.get("ai_choice"),
            "rule_choice": binding.get("rule_choice_at_adopt"),
            "plan_changed_by_ai": binding.get("plan_changed_by_ai"),
            "ai_plan_digest": (binding.get("ai_plan_digest") or "")[:16],
            "rule_plan_digest": (binding.get("rule_plan_digest") or "")[:16],
            "plan_source": (adopted.get("plan_source") or {}).get("kind"),
        }
        print("[%s] adopt plan_changed_by_ai=%s (%s->%s)" % (
            name, binding.get("plan_changed_by_ai"), binding.get("rule_choice_at_adopt"),
            binding.get("ai_choice")), flush=True)

        view = service.session_view(project, sid)
        entry["view_primary"] = (view["recommendation"]["platforms"] or {}).get("primary")

        if not contract.get("files") or not contract.get("steps"):
            entry["dispatch"] = {"skipped": "模型未返回可用契约;不派发"}
            report["cases"].append(entry)
            continue
        run_id = "gen-" + ("%012x" % (int(time.time()) % (16 ** 12)))
        receipt = generic_run.run_generic_app(
            project, goal, adapter,
            contract=generic_run._validate_contract(contract),
            session_id=sid,
            adoption_binding={"session_id": sid,
                              "evaluation_id": record["evaluation_id"],
                              "plan_digest": adopted["plan_digest"],
                              "contract_sha256": binding.get("contract_sha256")},
            run_id=run_id, max_repair_rounds=1)
        entry["dispatch"] = {
            "run_id": run_id,
            "status": receipt.get("status"),
            "failure": (receipt.get("failure") or "")[:200],
            "effect": (receipt.get("current_request") or {}).get("effect"),
            "ai_requests_dispatched": receipt.get("ai_requests_dispatched"),
            "capability": (receipt.get("execution_capability") or {}).get("kind"),
            "task_budget": receipt.get("task_budget"),
        }
        print("[%s] dispatch status=%s capability=%s" % (
            name, receipt.get("status"),
            (receipt.get("execution_capability") or {}).get("kind")), flush=True)
        pending = generic_run.resumable_candidate(project, sid)
        entry["resume"] = None if pending is None else {
            "run_id": pending["run_id"], "resumable": pending["resumable"],
            "files": pending["files"], "status": pending["status"]}
        print("[%s] resume_entry=%s" % (name, (pending or {}).get("resumable")), flush=True)
    except Exception as exc:  # noqa: BLE001 - 诊断脚本要留下真实失败
        entry["error"] = type(exc).__name__ + ":" + str(exc)[:300]
        traceback.print_exc()
    report["cases"].append(entry)

report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
out = BASE / ("c6-multiproject-real-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print("REPORT:", out)
print(json.dumps(report, ensure_ascii=False, indent=1)[:4000])
