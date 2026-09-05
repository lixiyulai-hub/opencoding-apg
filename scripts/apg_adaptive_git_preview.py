#!/usr/bin/env python3
"""APG active adaptive Git checkpoint advisor (offline only)."""
from __future__ import annotations
import argparse, hashlib, json, sys
from typing import Any, Mapping

STAGES=("intake-ready","plan-ready","implementation-slice","validation-passed","release-candidate")
SUCCESS_ORDER={stage:i for i,stage in enumerate(STAGES)}
FAILURE_CODES={"missing-evidence":"证据不完整","tests-failed":"测试未通过","scope-drift":"工作范围发生变化","unknown-state":"状态无法确认"}

def canonical_bytes(value:Any)->bytes:
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)+"\n").encode()

def digest(value:Any)->str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()

def _verified_stages(state:Mapping[str,Any])->list[str]:
    raw=state.get("verified_stages",())
    if isinstance(raw,(str,bytes)) or not isinstance(raw,(list,tuple)): return []
    return [x for x in STAGES if x in raw]

def detect_stage(state:Mapping[str,Any])->str|None:
    if not isinstance(state,Mapping): return None
    explicit=state.get("success_node")
    if explicit in STAGES: return str(explicit)
    for stage in reversed(STAGES):
        if state.get(stage) is True: return stage
    return None

def simulate(state:Mapping[str,Any], *, failure:str|None=None, repeated:bool=False)->dict[str,Any]:
    if not isinstance(state,Mapping): raise ValueError("state must be an object")
    verified=_verified_stages(state)
    latest=verified[-1] if verified else None
    stage=detect_stage(state)
    failure_code=(failure or "").strip().casefold().replace(" ","-")
    if failure_code and failure_code not in FAILURE_CODES: failure_code="unknown-state"
    evidence_complete=state.get("evidence_complete",True) is True
    tests_passed=state.get("tests_passed",True) is True
    scope_clean=state.get("scope_clean",True) is True
    blocked_reason=failure_code or ("missing-evidence" if not evidence_complete else "tests-failed" if not tests_passed else "scope-drift" if not scope_clean else "")
    if blocked_reason:
        resume=f"resume.after-{blocked_reason}-is-resolved"
        return {
            "schema_version":"1.0","mode":"offline-simulation","git_action":"PREVIEW_ONLY",
            "status":"FREEZE","success_node":stage,"checkpoint":None,
            "latest_verified_checkpoint":latest,"revert_point":latest,
            "revert_reason":("先回到最近一次已验证检查点，再修复："+FAILURE_CODES[blocked_reason]) if latest else ("先补齐证据后再创建检查点。"),
            "reason":"当前状态未满足成功节点条件。","resume_condition":resume,
            "duplicate_suppressed":False,"execution_performed":False,
            "external_actions":{"git":False,"network":False,"remote":False},
        }
    if stage is None:
        return {"schema_version":"1.0","mode":"offline-simulation","git_action":"PREVIEW_ONLY","status":"WAIT","success_node":None,"checkpoint":None,"latest_verified_checkpoint":latest,"revert_point":latest,"revert_reason":"等待一个可验证成功节点。","reason":"尚未发现可保存的成功节点。","resume_condition":"resume.after-success-node-is-verified","duplicate_suppressed":False,"execution_performed":False,"external_actions":{"git":False,"network":False,"remote":False}}
    if repeated or stage in verified:
        checkpoint_id=f"apg-{stage}-{digest({'stage':stage,'verified':verified})[:12]}"
        return {"schema_version":"1.0","mode":"offline-simulation","git_action":"PREVIEW_ONLY","status":"ALREADY_RECOMMENDED","success_node":stage,"checkpoint":{"checkpoint_id":checkpoint_id,"action":"reuse-existing-checkpoint","save":"沿用该成功节点已有的 Git 检查点","trigger_evidence":[f"success-node:{stage}","evidence-complete","tests-passed","scope-clean"]},"latest_verified_checkpoint":stage,"revert_point":stage,"revert_reason":"重复建议被抑制，继续使用原检查点。","reason":"这个成功节点已经有检查点建议。","resume_condition":"resume.after-next-new-success-node","duplicate_suppressed":True,"execution_performed":False,"external_actions":{"git":False,"network":False,"remote":False}}
    checkpoint_id=f"apg-{stage}-{digest({'stage':stage,'verified':verified})[:12]}"
    return {"schema_version":"1.0","mode":"offline-simulation","git_action":"PREVIEW_ONLY","status":"CHECKPOINT_RECOMMENDED","success_node":stage,"checkpoint":{"checkpoint_id":checkpoint_id,"action":"create-checkpoint-preview","save":"保存当前已验证的项目文件、知识包、任务图和证据摘要","trigger_evidence":[f"success-node:{stage}","evidence-complete","tests-passed","scope-clean"]},"latest_verified_checkpoint":stage,"revert_point":latest,"revert_reason":"如后续失败，退回最近一次已验证检查点。" if latest else "这是第一个已验证检查点。","reason":"这个小目标已经验证通过，现在保存回退点最稳妥。","resume_condition":"resume.after-checkpoint-preview-is-recorded","duplicate_suppressed":False,"execution_performed":False,"external_actions":{"git":False,"network":False,"remote":False}}

def replay_digest(state:Mapping[str,Any], *, failure:str|None=None, repeated:bool=False)->str:
    return hashlib.sha256(canonical_bytes(simulate(state,failure=failure,repeated=repeated))).hexdigest()

def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description="APG offline adaptive Git checkpoint preview")
    p.add_argument("stage",choices=STAGES+('none',))
    p.add_argument("--verified",default="",help="comma-separated verified stages")
    p.add_argument("--failure",default=None)
    p.add_argument("--repeated",action="store_true")
    a=p.parse_args(argv)
    state={"success_node":None if a.stage=='none' else a.stage,"verified_stages":[x for x in a.verified.split(',') if x]}
    sys.stdout.buffer.write(canonical_bytes(simulate(state,failure=a.failure,repeated=a.repeated)))
    return 0
if __name__=='__main__': raise SystemExit(main())
