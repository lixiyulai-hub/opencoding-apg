from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
try:
    from scripts.apg_adaptive_git_ledger_persistence_preview import TARGET_PATH as PERSISTENCE_TARGET_PATH, build_plan as build_persistence_plan, persist as persist_ledger
except ModuleNotFoundError:
    from apg_adaptive_git_ledger_persistence_preview import TARGET_PATH as PERSISTENCE_TARGET_PATH, build_plan as build_persistence_plan, persist as persist_ledger

DEPLOYMENT_REQUIRED = (
    "scripts/apg_deployment_preview.py",
    "tests/test_apg_release_orchestration.py",
    "docs/apg/APG_DEPLOYMENT_PREVIEW.md",
    "docs/apg/APG_GATE_FAILURE_MATRIX.md",
    "artifacts/apg-deployment-preview/MODIFIED_FILE",
    "artifacts/apg-deployment-preview/DIFF_FILE",
    "artifacts/apg-deployment-preview/ROLLBACK.sh",
)
AUTONOMOUS_REQUIRED = (
    "docs/apg/APG_AUTONOMOUS_PRG_POLICY.md",
    "docs/apg/APG_AUTO_PLAN_LOOP_HARNESS.md",
    "scripts/apg_autonomous_policy_preview.py",
    "tests/test_apg_autonomous_policy.py",
    "artifacts/apg-autonomous-policy/RESULT.json",
    "artifacts/apg-autonomous-policy/MODIFIED_FILE",
    "artifacts/apg-autonomous-policy/DIFF_FILE",
    "artifacts/apg-autonomous-policy/ROLLBACK.sh",
)
ADAPTIVE_GIT_REQUIRED = (
    "docs/apg/APG_ADAPTIVE_GIT_CHECKPOINT_CONTRACT.md",
    "docs/apg/APG_GIT_CHECKPOINT_POLICY.md",
    "scripts/apg_adaptive_git_preview.py",
    "tests/test_apg_adaptive_git.py",
    "artifacts/apg-adaptive-git-checkpoint-offline/RESULT.json",
    "artifacts/apg-adaptive-git-checkpoint-offline/MODIFIED_FILE",
    "artifacts/apg-adaptive-git-checkpoint-offline/DIFF_FILE",
    "artifacts/apg-adaptive-git-checkpoint-offline/ROLLBACK.sh",
)
ADAPTIVE_GIT_CONTROLLER_REQUIRED = (
    "docs/apg/APG_ADAPTIVE_GIT_CONTROLLER_CONTRACT.md",
    "scripts/apg_adaptive_git_controller.py",
    "tests/test_apg_adaptive_git_controller.py",
    "artifacts/apg-adaptive-git-controller-connection/MODIFIED_FILE",
    "artifacts/apg-adaptive-git-controller-connection/DIFF_FILE",
    "artifacts/apg-adaptive-git-controller-connection/ROLLBACK.sh",
)
ADAPTIVE_GIT_LEDGER_REQUIRED = (
    "docs/apg/APG_ADAPTIVE_GIT_LEDGER_CONTRACT.md",
    "scripts/apg_adaptive_git_ledger_preview.py",
    "tests/test_apg_adaptive_git_ledger.py",
    "artifacts/apg-adaptive-git-ledger-replay/MODIFIED_FILE",
    "artifacts/apg-adaptive-git-ledger-replay/DIFF_FILE",
    "artifacts/apg-adaptive-git-ledger-replay/ROLLBACK.sh",
)
ADAPTIVE_GIT_LEDGER_PERSISTENCE_REQUIRED = (
    "docs/apg/APG_ADAPTIVE_GIT_LEDGER_PERSISTENCE_CONTRACT.md",
    "docs/apg/APG_ADAPTIVE_GIT_LEDGER_PERSISTENCE_PLAN.md",
    "scripts/apg_adaptive_git_ledger_persistence_preview.py",
    "tests/test_apg_adaptive_git_ledger_persistence.py",
    "artifacts/apg-adaptive-git-ledger-persistence-preview/RESULT.json",
    "artifacts/apg-adaptive-git-ledger-persistence-preview/MODIFIED_FILE",
    "artifacts/apg-adaptive-git-ledger-persistence-preview/DIFF_FILE",
    "artifacts/apg-adaptive-git-ledger-persistence-preview/VERIFICATION.txt",
    "artifacts/apg-adaptive-git-ledger-persistence-preview/ROLLBACK.sh",
)
BEGINNER_EXECUTOR_REQUIRED = (
    "docs/apg/APG_BEGINNER_EXECUTOR_OFFLINE_CONTRACT.md",
    "docs/apg/APG_GRILL_ME_UPSTREAM_ATTRIBUTION.md",
    "scripts/apg_beginner_executor_preview.py",
    "tests/test_apg_beginner_executor.py",
    "artifacts/apg-beginner-executor-offline/RESULT.json",
    "artifacts/apg-beginner-executor-offline/MODIFIED_FILE",
    "artifacts/apg-beginner-executor-offline/DIFF_FILE",
    "artifacts/apg-beginner-executor-offline/ROLLBACK.sh",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _deployment_preview(root: Path, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    process = subprocess.run([sys.executable, str(root / "scripts/apg_deployment_preview.py")], cwd=root, input=json.dumps(payload, ensure_ascii=False), text=True, capture_output=True, encoding="utf-8", errors="replace")
    try:
        return process.returncode, json.loads(process.stdout)
    except json.JSONDecodeError:
        return process.returncode, {"status": "BLOCK", "blocker_codes": ["invalid_preview_output"]}


def _policy_preview(root: Path, args: list[str]) -> tuple[int, dict[str, Any]]:
    process = subprocess.run([sys.executable, str(root / "scripts/apg_autonomous_policy_preview.py"), *args], cwd=root, text=True, capture_output=True, encoding="utf-8", errors="replace")
    try:
        return process.returncode, json.loads(process.stdout)
    except json.JSONDecodeError:
        return process.returncode, {"status": "BLOCK", "blocker_codes": ["invalid_preview_output"]}


def _beginner_preview(root: Path, args: list[str]) -> tuple[int, dict[str, Any]]:
    process = subprocess.run([sys.executable, str(root / "scripts/apg_beginner_executor_preview.py"), *args], cwd=root, text=True, capture_output=True, encoding="utf-8", errors="replace")
    try:
        return process.returncode, json.loads(process.stdout)
    except json.JSONDecodeError:
        return process.returncode, {"status": "BLOCK", "blocker_codes": ["invalid_preview_output"]}


def _require(root: Path, required: tuple[str, ...], blockers: list[str], present: dict[str, bool], hashes: dict[str, str]) -> None:
    for relative in required:
        path = root / relative
        present[relative] = path.is_file()
        if not path.is_file():
            blockers.append(f"missing_artifact:{relative}")
        else:
            hashes[relative] = _sha256(path)


def run_review(root: Path) -> dict[str, Any]:
    root = Path(root).resolve()
    blockers: list[str] = []
    present: dict[str, bool] = {}
    hashes: dict[str, str] = {}
    _require(root, DEPLOYMENT_REQUIRED + AUTONOMOUS_REQUIRED + BEGINNER_EXECUTOR_REQUIRED + ADAPTIVE_GIT_REQUIRED + ADAPTIVE_GIT_CONTROLLER_REQUIRED + ADAPTIVE_GIT_LEDGER_REQUIRED + ADAPTIVE_GIT_LEDGER_PERSISTENCE_REQUIRED, blockers, present, hashes)
    original = root / "PROJECT_BRIEF.md"
    modified = root / "artifacts/apg-deployment-preview/MODIFIED_FILE"
    if original.is_file() and modified.is_file():
        if _sha256(original) == _sha256(modified):
            blockers.append("modified_fixture_not_changed")
        if "deployment-preview test artifact" not in modified.read_text(encoding="utf-8"):
            blockers.append("modified_fixture_marker_missing")
    ready_code, ready = _deployment_preview(root, {"release_approval": True, "rollback_evidence": True})
    blocked_code, blocked = _deployment_preview(root, {"rollback_evidence": True})
    if ready_code != 0 or ready.get("status") != "ready-for-preview":
        blockers.append("preview_ready_assertion_failed")
    if blocked_code != 3 or blocked.get("blocker_codes") != ["missing_release_approval"]:
        blockers.append("preview_block_assertion_failed")
    if any(ready.get(field) is not False for field in ("release_action_executed", "publication_action_executed", "deployment_action_executed")) or ready.get("external_actions") != []:
        blockers.append("external_action_assertion_failed")
    auto_code, auto = _policy_preview(root, ["我想做一个离线学习工具"])
    gate_code, gate = _policy_preview(root, ["请联网并使用密钥部署到线上并付款"])
    freeze_code, freeze = _policy_preview(root, ["继续自动检查", "--failure", "missing evidence"])
    if auto_code != 0 or auto.get("route") != "auto" or auto.get("human_gate") is not False or auto.get("dispatch_permitted") is not True:
        blockers.append("autonomous_auto_path_assertion_failed")
    if gate_code != 0 or gate.get("route") != "consequential-gate" or gate.get("human_gate") is not True or gate.get("dispatch_permitted") is not False:
        blockers.append("autonomous_single_gate_assertion_failed")
    if freeze_code != 0 or freeze.get("loop_states", [None])[-1] != "FREEZE" or freeze.get("resume_condition") != "resume.after-missing-evidence-is-resolved":
        blockers.append("autonomous_freeze_assertion_failed")
    if any(any(item.get("external_actions", {}).values()) for item in (auto, gate, freeze)):
        blockers.append("autonomous_external_action_assertion_failed")
    beginner_code, beginner = _beginner_preview(root, ["我想做一个中文学习打卡工具"])
    beginner_gate_code, beginner_gate = _beginner_preview(root, ["请联网并用密钥部署到 GitHub"])
    beginner_freeze_code, beginner_freeze = _beginner_preview(root, ["继续整理", "--failure", "missing evidence"])
    pack = beginner.get("knowledge_pack", {})
    required_sections = ("项目目标", "初心者澄清", "任务编排", "Gate", "证据", "回滚", "Requeue")
    if beginner_code != 0 or beginner.get("route") != "auto" or len(beginner.get("grill_me", {}).get("questions", [])) != 3 or any(section not in pack.get("content", "") for section in required_sections):
        blockers.append("beginner_knowledge_pack_assertion_failed")
    if beginner_gate_code != 0 or beginner_gate.get("route") != "consequential-gate" or not beginner_gate.get("human_gate") or beginner_gate.get("adapter", {}).get("dispatch_permitted") is not False:
        blockers.append("beginner_single_gate_assertion_failed")
    if beginner_freeze_code != 0 or beginner_freeze.get("terminal_state") != "FREEZE" or beginner_freeze.get("resume_condition") != "resume.after-missing-evidence-is-resolved":
        blockers.append("beginner_freeze_assertion_failed")
    if any(beginner.get("external_actions", {}).values()) or any(beginner_gate.get("external_actions", {}).values()) or any(beginner_freeze.get("external_actions", {}).values()):
        blockers.append("beginner_external_action_assertion_failed")
    adaptive_code, adaptive, adaptive_freeze = 1, {}, {}
    git_script = root / "scripts/apg_adaptive_git_preview.py"
    if git_script.is_file():
        process = subprocess.run([sys.executable, str(git_script), "validation-passed", "--verified", "plan-ready"], cwd=root, text=True, capture_output=True, encoding="utf-8", errors="replace")
        try: adaptive=json.loads(process.stdout); adaptive_code=process.returncode
        except json.JSONDecodeError: adaptive_code=process.returncode; adaptive={}
        freeze_process = subprocess.run([sys.executable, str(git_script), "implementation-slice", "--verified", "plan-ready", "--failure", "tests-failed"], cwd=root, text=True, capture_output=True, encoding="utf-8", errors="replace")
        try: adaptive_freeze=json.loads(freeze_process.stdout)
        except json.JSONDecodeError: adaptive_freeze={}
        if adaptive_code != 0 or adaptive.get("status") != "CHECKPOINT_RECOMMENDED" or adaptive.get("git_action") != "PREVIEW_ONLY" or adaptive.get("revert_point") != "plan-ready": blockers.append("adaptive_git_checkpoint_assertion_failed")
        if freeze_process.returncode != 0 or adaptive_freeze.get("status") != "FREEZE" or adaptive_freeze.get("revert_point") != "plan-ready": blockers.append("adaptive_git_freeze_assertion_failed")
        if any(adaptive.get("external_actions", {}).values()) or any(adaptive_freeze.get("external_actions", {}).values()): blockers.append("adaptive_git_external_action_assertion_failed")
    controller_script = root / "scripts/apg_adaptive_git_controller.py"
    controller_code, controller = 1, {}
    controller_freeze = {}
    if controller_script.is_file():
        controller_payload = json.dumps({"success_node":"validation-passed","verified_stages":["intake-ready","plan-ready"],"evidence_complete":True,"tests_passed":True,"scope_clean":True}, ensure_ascii=False)
        controller_process = subprocess.run([sys.executable, str(controller_script), "--state", controller_payload], cwd=root, text=True, capture_output=True, encoding="utf-8", errors="replace")
        try: controller=json.loads(controller_process.stdout); controller_code=controller_process.returncode
        except json.JSONDecodeError: controller_code=controller_process.returncode; controller={}
        freeze_payload = json.dumps({"success_node":"implementation-slice","verified_stages":["plan-ready"],"failure":"tests-failed"}, ensure_ascii=False)
        controller_freeze_process = subprocess.run([sys.executable, str(controller_script), "--state", freeze_payload], cwd=root, text=True, capture_output=True, encoding="utf-8", errors="replace")
        try: controller_freeze=json.loads(controller_freeze_process.stdout)
        except json.JSONDecodeError: controller_freeze={}
        if controller_code != 0 or controller.get("git_checkpoint", {}).get("status") != "CHECKPOINT_RECOMMENDED" or controller.get("next_action", {}).get("id") != "record-checkpoint-preview" or controller.get("checkpoint_ledger", {}).get("status") != "APPEND_CANDIDATE" or controller.get("checkpoint_ledger", {}).get("write_action") != "PREVIEW_ONLY": blockers.append("adaptive_git_controller_assertion_failed")
        if controller_freeze_process.returncode != 0 or controller_freeze.get("git_checkpoint", {}).get("status") != "FREEZE" or controller_freeze.get("git_checkpoint", {}).get("revert_point") != "plan-ready": blockers.append("adaptive_git_controller_freeze_assertion_failed")
        if any(controller.get("external_actions", {}).values()) or any(controller_freeze.get("external_actions", {}).values()) or any(controller.get("checkpoint_ledger", {}).get("external_actions", {}).values()) or any(controller_freeze.get("checkpoint_ledger", {}).get("external_actions", {}).values()): blockers.append("adaptive_git_controller_external_action_assertion_failed")
    attribution = root / "docs/apg/APG_GRILL_ME_UPSTREAM_ATTRIBUTION.md"
    if attribution.is_file() and "https://github.com/mattpocock/skills" not in attribution.read_text(encoding="utf-8"):
        blockers.append("grill_me_attribution_missing")
    # Persistence contract smoke checks run only against a disposable directory.
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            snap = {"schema_version":"1.0", "ledger_id":"apg-adaptive-git-ledger-v1", "target_path":PERSISTENCE_TARGET_PATH, "records":[]}
            plan = build_persistence_plan(Path(td), snap)
            applied = persist_ledger(Path(td), plan, apply=True)
            replayed = persist_ledger(Path(td), plan, apply=True)
            if applied.get("status") != "PERSISTED" or replayed.get("status") != "ALREADY_PERSISTED":
                blockers.append("persistence_idempotency_assertion_failed")
            if any(applied.get("external_actions", {}).values()) is not True:
                blockers.append("persistence_write_observation_missing")
    except Exception:
        blockers.append("persistence_smoke_failed")
    return {
        "schema_version": "1.0", "status": "PASS" if not blockers else "BLOCK", "blocker_codes": blockers,
        "review_scope": "offline-apg-deployment-preview-autonomous-prg-beginner-executor-adaptive-git-and-ledger",
        "external_actions_executed": False, "provider_network_credentials_used": False, "real_data_used": False,
        "present_artifacts": present, "artifact_sha256": hashes,
        "preview_cases": {
            "complete": {"exit_status": ready_code, "status": ready.get("status"), "blocker_codes": ready.get("blocker_codes", [])},
            "missing_approval": {"exit_status": blocked_code, "status": blocked.get("status"), "blocker_codes": blocked.get("blocker_codes", [])},
            "autonomous_auto": {"exit_status": auto_code, "route": auto.get("route"), "human_gate": auto.get("human_gate")},
            "autonomous_gate": {"exit_status": gate_code, "route": gate.get("route"), "human_gate": gate.get("human_gate")},
            "autonomous_freeze": {"exit_status": freeze_code, "terminal_state": freeze.get("loop_states", [None])[-1]},
            "beginner_auto": {"exit_status": beginner_code, "route": beginner.get("route"), "knowledge_pack_sha256": pack.get("sha256")},
            "beginner_gate": {"exit_status": beginner_gate_code, "route": beginner_gate.get("route"), "human_gate": beginner_gate.get("human_gate")},
            "beginner_freeze": {"exit_status": beginner_freeze_code, "terminal_state": beginner_freeze.get("terminal_state")},
            "adaptive_git_checkpoint": {"exit_status": adaptive_code, "status": adaptive.get("status"), "revert_point": adaptive.get("revert_point")},
            "adaptive_git_freeze": {"status": adaptive_freeze.get("status"), "revert_point": adaptive_freeze.get("revert_point")},
            "adaptive_git_controller": {"exit_status": controller_code, "status": controller.get("git_checkpoint", {}).get("status"), "next_action": controller.get("next_action", {}).get("id"), "ledger_status": controller.get("checkpoint_ledger", {}).get("status")},
            "adaptive_git_controller_freeze": {"status": controller_freeze.get("git_checkpoint", {}).get("status"), "revert_point": controller_freeze.get("git_checkpoint", {}).get("revert_point")},
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline APG independent review")
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    result = run_review(Path(args.root))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())

# append persistence review support
