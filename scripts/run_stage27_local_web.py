#!/usr/bin/env python3
"""Run a real local Web/Node project through the adopted product loop."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from opencoding.acceptance_report import build_acceptance_report
from opencoding.agent_tasks import LocalAgentTaskExecutor, preview_agent_tasks
from opencoding.intake import QUESTION_DEFINITIONS
from opencoding.product_evidence import verify_product_evidence
from opencoding.product_loop import rollback_product_run, resume_product_run, start_product_run
from opencoding.service import (
    adopt_evaluation_plan,
    apply_approved,
    approve_preview,
    create_session,
    evaluate_session,
    preview_session,
    submit_answer,
)


def _actions(source: str, test: str, *, repair: bool) -> dict[str, list[dict[str, object]]]:
    verify = [
        {"type": "write_text", "path": "tests/features/test_scenario-1.test.ts", "content": test},
        {"type": "node_script", "path": "tests/features/test_scenario-1.test.ts", "args": ["--experimental-strip-types"]},
    ]
    if repair:
        verify.insert(0, {"type": "write_text", "path": "src/features/scenario-1.ts", "content": source})
        return {"verify-scenario-1": verify}
    return {
        "implement-scenario-1": [{"type": "write_text", "path": "src/features/scenario-1.ts", "content": source}],
        "verify-scenario-1": verify,
    }


WRONG_SOURCE = """import { createServer } from 'node:http';

export function renderPage(records, today, intervalDays = 7) {
  const overdue = records.filter((item) => (Date.parse(today) - Date.parse(item.lastWatered)) / 86400000 > intervalDays);
  return `<!doctype html><html><body><h1>Plant Watering</h1><p>Due: ${overdue.length}</p></body></html>`;
}

export function createPlantServer(records) {
  return createServer((_request, response) => {
    response.writeHead(200, {'content-type': 'text/html; charset=utf-8'});
    response.end(renderPage(records, '2026-10-01'));
  });
}
"""

FIXED_SOURCE = WRONG_SOURCE.replace(
    ") / 86400000 > intervalDays",
    ") / 86400000 >= intervalDays",
).replace(
    "response.writeHead(200, {'content-type': 'text/html; charset=utf-8'});\n    response.end(renderPage(records, '2026-10-01'));",
    "if (_request.url === '/missing') { response.writeHead(404); response.end('not found'); return; }\n    response.writeHead(200, {'content-type': 'text/html; charset=utf-8'});\n    response.end(renderPage(records, '2026-10-01'));",
)

TEST_SOURCE = """import { createPlantServer, renderPage } from '../../src/features/scenario-1.ts';

const records = [{name: '绿萝', lastWatered: '2026-09-24'}];
const page = renderPage(records, '2026-10-01');
if (!page.includes('Due: 1')) throw new Error('page did not show the overdue plant');

const server = createPlantServer(records);
server.listen(0, '127.0.0.1', async () => {
  const port = server.address().port;
  const ok = await fetch(`http://127.0.0.1:${port}/`);
  if (ok.status !== 200 || !(await ok.text()).includes('Due: 1')) throw new Error('HTTP success path failed');
  const missing = await fetch(`http://127.0.0.1:${port}/missing`);
  if (missing.status !== 404) throw new Error('HTTP error path failed');
  console.log('LOCAL_HTTP_STATUS=200');
  console.log('LOCAL_HTTP_404=404');
  console.log('OPENCODING_TESTS_RUN=2');
  server.close(() => process.exit(0));
});
"""


def run(root: Path) -> dict[str, object]:
    if root.exists() and any(root.iterdir()):
        raise SystemExit("--root must be new or empty")
    root.mkdir(parents=True, exist_ok=True)
    goal = "我想要一个离线网页植物浇水追踪器，记录植物和最近浇水日期，找出逾期植物。"
    view = create_session(root, goal)
    answers = {
        "audience": "家庭成员", "outcome": "记录植物和最近浇水日期，找出逾期植物。", "platform": "网页",
        "data_persistence": "不需要", "cross_device": "不需要", "file_storage": "不需要", "external_data": "不需要",
        "admin_access": "不需要", "account_access": "不需要", "notifications": "不需要", "payments": "不需要", "multi_user": "不需要",
    }
    for question in QUESTION_DEFINITIONS:
        view = submit_answer(root, view["session"]["id"], view["session"]["revision"], question["id"], answers[question["id"]])
    session_id = view["session"]["id"]
    evaluation = evaluate_session(root, session_id)
    adopt_evaluation_plan(root, session_id)
    apply_approved(root, approve_preview(preview_session(root, session_id)))

    first_preview = preview_agent_tasks(root, session_id, _actions(WRONG_SOURCE, TEST_SOURCE, repair=False))
    first_executor = LocalAgentTaskExecutor(
        root, first_preview, expected_digest=first_preview["preview_digest"],
        confirmation_id="user-authorized-stage27-failure", confirmed=True, synthetic=False,
    )
    failed = start_product_run(root, session_id, human_confirmed=True, run_id="product-d4e5f6a7b8c9", project_executor=first_executor)

    second_preview = preview_agent_tasks(root, session_id, _actions(FIXED_SOURCE, TEST_SOURCE, repair=True))
    second_executor = LocalAgentTaskExecutor(
        root, second_preview, expected_digest=second_preview["preview_digest"],
        confirmation_id="user-authorized-stage27-repair", confirmed=True, synthetic=False,
    )
    repaired = resume_product_run(root, failed["run_id"], human_confirmed=True, project_executor=second_executor)
    evidence = verify_product_evidence(root, repaired["run_id"], repaired["record_digest"])
    report = build_acceptance_report(run_root=root, run_id=repaired["run_id"], expected_run_digest=repaired["record_digest"])
    rolled = rollback_product_run(root, repaired["run_id"], reason="user-authorized local web acceptance rollback")
    residual = [str(path.relative_to(root)) for base in (root / "src", root / "tests") if base.exists() for path in base.rglob("*.ts")]
    result = {
        "schema": "opencoding-stage27-local-web-v1",
        "goal": goal,
        "answers_source": "offline fixture; not evidence of a live interview",
        "user_authorization": True,
        "per_action_human_approval": False,
        "synthetic_confirmation": False,
        "host": "linux",
        "target_platform": "web",
        "node_http_executed": True,
        "browser_executed": False,
        "managed_loader": None,
        "model_used": False,
        "external_actions": False,
        "plan": {"platform": evaluation["recommendation"]["platforms"], "client": evaluation["recommendation"]["stack"]["client"]},
        "first_run": {"status": failed["status"], "failure": failed.get("failure")},
        "repaired_run": {"status": repaired["status"], "run_id": repaired["run_id"], "record_digest": repaired["record_digest"]},
        "acceptance_checks": {item["id"]: item["status"] for item in report["checks"] if item["id"] in {"local_structured_actions", "skill_project_discovery", "web_target_execution"}},
        "evidence": evidence,
        "rollback": {"status": rolled["status"], "file_rollbacks": rolled.get("file_rollbacks", []), "residual_project_ts": residual},
    }
    (root / "evidence").mkdir(exist_ok=True)
    (root / "evidence/STAGE27_LOCAL_WEB.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    if not args.root.is_absolute():
        parser.error("--root must be absolute")
    print(json.dumps(run(args.root), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
