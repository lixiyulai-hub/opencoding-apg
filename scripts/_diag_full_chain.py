# -*- coding: utf-8 -*-
"""诊断正向链 worker 为什么走 write_worker_outcome。"""
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opencoding import aiconfig, advisor, service
from opencoding.generic_run import load_receipt, run_generic_app, write_worker_outcome
from opencoding.workbench import Workbench

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_product_full_chain_v5 import FakeAdapter, GOOD_CONTRACT, eval_response, impl_response
from tests.test_product_service import _complete

tmp = tempfile.TemporaryDirectory()
base = Path(tmp.name)
workspace = base / "workspace"
workspace.mkdir()
project = workspace / "p"
project.mkdir()
view = _complete(project)
sid = view["session"]["id"]
bench = Workbench(workspace)

eval_adapter = FakeAdapter([eval_response()])
record = advisor.run_ai_evaluation(project, sid, eval_adapter, run_id="t")
current = service.session_view(project, sid)["session"]["revision"]
advisor.confirm_evaluation(project, sid, record["evaluation_id"], expected_revision=current, accepted=True)
adopted = advisor.adopt_confirmed_evaluation(project, sid)
print("ai_binding:", json.dumps(adopted["adoption"]["ai_binding"], ensure_ascii=False)[:200])

appdata = base / "appdata"
with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(appdata)}):
    aiconfig.save_config({"provider": "openai_compatible", "base_url": "https://x/v1",
                          "api_key": "k", "model": "m"})
    from opencoding.aiadapter import AIRequestError
    with mock.patch("opencoding.aiadapter.adapter_from_config",
                    return_value=FakeAdapter([impl_response()])):
        try:
            start = bench.api("POST", "/api/project/p/session/%s/generate" % sid, {}, {})
            print("start:", json.dumps(start, ensure_ascii=False)[:200])
        except Exception:
            traceback.print_exc()
            sys.exit(1)

time.sleep(3)
doc = load_receipt(project, start["run_id"])
print("receipt status:", doc.get("status") if doc else None)
if doc:
    print(json.dumps(doc, ensure_ascii=False, indent=1)[:1500])
tmp.cleanup()
