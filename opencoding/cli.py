"""Chinese-first, offline OpenCoding entry point."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

from . import grants, uxtext
from .safety import sanitize_text
from .service import (
    adopt_evaluation_plan,
    ServiceError,
    apply_approved,
    approve_preview,
    as_json,
    cancel_autonomous_run,
    create_session,
    evaluate_session,
    execution_status,
    preview_session,
    rollback,
    rollback_autonomous_run,
    session_view,
    submit_answer,
)


_TASK_STATE_LABELS = {
    "queued": "排队中",
    "pending": "等待中",
    "running": "运行中",
    "succeeded": "已成功",
    "failed": "失败",
    "frozen": "已冻结",
    "cancelled": "已取消",
    "rolled_back": "已回滚",
    "partial_failure": "部分失败",
    "blocked": "已阻塞",
    "timed_out": "已超时",
}
_RUN_STATE_LABELS = {
    "running": "运行中",
    "succeeded": "已成功",
    "failed": "失败",
    "cancelled": "已取消",
    "rolled_back": "已回滚",
    "timed_out": "已超时",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m opencoding",
        description="OpenCoding 中文入口：离线问答、方案预览、精确本地文档确认与回滚。",
    )
    parser.add_argument("--root", required=True, help="已有本地项目根目录（绝对路径）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", metavar="SESSION_ID", help="恢复指定会话")
    mode.add_argument("--list", action="store_true", help="列出本地会话")
    mode.add_argument("--preview", metavar="SESSION_ID", help="只读显示指定会话的方案、文件范围和差异")
    mode.add_argument("--evaluate", metavar="SESSION_ID", help="只读评估已有答案和事实，返回待采用任务图")
    mode.add_argument("--adopt-plan", metavar="SESSION_ID", help="显式采用并持久化已校验的评估任务图")
    mode.add_argument("--change", nargs=3, metavar=("SESSION_ID", "QUESTION_ID", "ANSWER"), help="修改已有会话答案并显示新方案差异")
    mode.add_argument("--rollback", metavar="TRANSACTION_ID", help="回滚指定的本地文档事务")
    mode.add_argument("--status", action="store_true", help="只读显示本地任务与运行状态")
    mode.add_argument("--autorun", metavar="SCENARIO", help="自主工作模式：在有限批次授权内连续执行（当前支持 lendreg 合成借还示例）")
    mode.add_argument("--cancel-run", metavar="RUN_ID", help="取消指定自主运行")
    mode.add_argument("--query-run", metavar="RUN_ID", help="只核验本地已保存响应；不新增请求")
    mode.add_argument("--rollback-run", metavar="RUN_ID", help="按事务记录回滚自主运行产物，可用 --task-id 限定任务")
    parser.add_argument("--task-id", help="仅在 --status 时查看指定任务")
    parser.add_argument("--reason", help="取消自主运行的原因")
    parser.add_argument("--mock-ai", action="store_true", help="自主模式专用：使用明确标记的模拟 AI（非真实接通）")
    parser.add_argument("--run-id", help="自主模式专用：指定运行编号以便断点接续")
    parser.add_argument("--grant-ttl", type=int, default=8, help="自主模式专用：批次有效期小时数（默认 8，不自动续长）")
    parser.add_argument("--plan-digest", help="自主模式专用：绑定已明确采用的评估计划摘要")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出服务状态")
    return parser


def _delivery_exit_code(summary: Mapping[str, Any]) -> int:
    """退出码以“本批是否可交付”为准（S05/Q11）。

    冻结、取消、最终回归（含依赖已回归任务）任一存在即返回 1；
    旧版不含 deliverable 字段的汇总退化为按冻结/取消判定，不再把回归当作成功。
    """

    if "deliverable" in summary:
        return 0 if summary["deliverable"] else 1
    regressed_states = [
        value for value in dict(summary.get("final_states") or {}).values() if str(value).startswith("regressed")
    ]
    if regressed_states:
        return 1
    return 0 if (summary.get("frozen", 0) == 0 and summary.get("cancelled", 0) == 0) else 1


def _autorun_mode(args: argparse.Namespace) -> int:
    if args.autorun != "lendreg":
        print("错误：不支持的自主场景：" + str(args.autorun) + "；当前仅支持 lendreg（合成借还示例）。", file=sys.stderr)
        return 2
    if args.grant_ttl < 1:
        print("错误：批次有效期必须至少 1 小时；不会自动续长。", file=sys.stderr)
        return 2
    from . import autorun as autorun_module
    from .aiadapter import AIAdapter, AIRequestError
    from .safety import sanitize_text

    if args.mock_ai:
        adapter = MockAdapterLike()
        adapter_note = "模拟 AI（明确标记，非真实接通）"
    else:
        base_url = os.environ.get("OPENCODING_AI_BASE_URL", "")
        api_key = os.environ.get("OPENCODING_AI_API_KEY", "")
        model = os.environ.get("OPENCODING_AI_MODEL", "")
        missing = [name for name, value in (("OPENCODING_AI_BASE_URL", base_url), ("OPENCODING_AI_API_KEY", api_key), ("OPENCODING_AI_MODEL", model)) if not value]
        if missing:
            print(uxtext.render_pause(
                "真实 AI 调用缺少必要配置：" + "、".join(missing) + "。",
                "先设置上述环境变量指向已授权服务；或用 --mock-ai 运行明确标记的模拟链路。",
                "是否允许按当前配置继续。",
            ), file=sys.stderr)
            return 2
        try:
            adapter = AIAdapter(base_url=base_url, api_key=api_key, model=model, provider=os.environ.get("OPENCODING_AI_PROVIDER", "unknown"))
        except AIRequestError as exc:
            print("错误：" + sanitize_text(str(exc)), file=sys.stderr)
            return 2
        adapter_note = "真实服务（" + adapter.provider + " / " + adapter.model + "）"
    # F05/R05：接续运行不得重新签发授权；同一 run-id 只绑定其账本登记的那一批。
    # S03/Q17：先区分“没有运行 / 已绑定 / 已存在但未绑定 / 账本损坏”，后两类拒绝换发新授权，也不要求改名绕过。
    existing_id: str | None = None
    if args.run_id:
        status = autorun_module.run_binding_status(args.root, args.run_id)
        state = str(status.get("state"))
        if state == "bound":
            existing_id = status["grant_id"]
        elif state in {"unbound", "corrupt"}:
            ledger_path = str(Path(args.root).resolve() / ".opencoding" / "autoruns" / (args.run_id + ".json"))
            detail = "账本存在但未登记批次授权" if state == "unbound" else "账本不可读（" + str(status.get("reason")) + "）"
            print("错误：运行 " + args.run_id + " 已存在但绑定不可用：" + detail + "。\n"
                  + "不会自动换发新授权，也不会要求你改名绕过。\n"
                  + "核实路径：" + ledger_path + "\n"
                  + "处置方式：确认该运行的状态与未提交内容后，修复账本；或显式新开批次（更换 --run-id）并重新确认范围与预算。", file=sys.stderr)
            return 2
        elif state != "no_run":
            print("错误：运行 " + args.run_id + " 的绑定状态无法判定（" + state + "）；不会自动换发新授权。", file=sys.stderr)
            return 2
    grant = None
    if existing_id is not None:
        try:
            grant = grants.load_grant(args.root, existing_id)
        except grants.GrantError as exc:
            print("错误：运行 " + args.run_id + " 绑定的批次授权无法读取（" + sanitize_text(exc.code)
                  + "）；不会自动换发新授权。请显式新开批次（更换 --run-id）。", file=sys.stderr)
            return 2
        valid, reason = grants.grant_valid(grant)
        if not valid:
            print("错误：运行 " + args.run_id + " 绑定的批次授权已失效（" + reason
                  + "）；不会自动换发新授权。请显式新开批次（更换 --run-id）后重新确认范围与预算。", file=sys.stderr)
            return 2
    if grant is None:
        try:
            grant = grants.issue_batch_grant(
                args.root,
                goal="自主模式：合成单机借还登记（lendreg 场景）",
                allowed_paths=["lendreg", "tests", "scripts", "data", "RECOVERY.md"],
                action_kinds=["local_write", "local_run", "ai_request"],
                issued_by="用户运行自主模式命令并确认本批范围与预算（命令行确认事件）",
                ttl_seconds=args.grant_ttl * 3600,
            )
        except grants.GrantError as exc:
            print("错误：批次授权登记失败：" + sanitize_text(str(exc)), file=sys.stderr)
            return 2
        print(uxtext.render_progress(
            "新批次授权已登记，准备连续推进 " + adapter_note + "。",
            "用户已通过自主模式命令确认本批范围；程序逐步核对凭据。",
            "批次编号 " + grant["grant_id"] + "。",
            "按依赖顺序执行十项借还任务。",
            "暂无",
        ))
        print("本批预算：AI 请求 ≤ " + str(grant["budget"]["max_ai_requests"])
              + "；自动修复 ≤ " + str(grant["budget"]["max_repair_rounds"])
              + "；并发 = " + str(grant["concurrency"]) + "；有效期 " + str(args.grant_ttl) + " 小时，不自动续长。")
    else:
        print(uxtext.render_progress(
            "接续既有批次授权，准备继续推进 " + adapter_note + "。",
            "断点接续沿用运行登记的原授权：不重新签发、不重置预算。",
            "批次编号 " + grant["grant_id"] + "。",
            "未完成任务按账本继续；已成功任务按血缘核对。",
            "暂无",
        ))
        print("沿用批次预算：AI 请求 ≤ " + str(grant["budget"]["max_ai_requests"])
              + "；自动修复 ≤ " + str(grant["budget"]["max_repair_rounds"])
              + "；并发 = " + str(grant["concurrency"]) + "；有效期至 " + str(grant["expires_at"]) + "，不自动续长。")
    try:
        summary = autorun_module.run_batch(
            args.root, autorun_module.LENDREG_SCENARIO,
            grant_id=grant["grant_id"], adapter=adapter,
            run_id=args.run_id,
            # N01-a：仅内置 MockAdapter（固定剧本、非未知模型输出）允许走受信任合成通道；
            # 真实 AIAdapter 路径不传该参数，无实际受限执行环境即冻结。
            trusted_fixture=("tests-reviewed-synthetic-v1" if args.mock_ai else None),
            adopted_plan_digest=args.plan_digest,
            progress=print,
        )
    except autorun_module.AutorunError as exc:
        print("错误：自主批次未启动：" + sanitize_text(str(exc)), file=sys.stderr)
        return 2
    print("批次结果：成功 " + str(summary["succeeded"]) + "/" + str(summary["total"])
          + "；冻结 " + str(summary["frozen"]) + "；账本 " + summary["ledger_path"])
    blockers = [str(item) for item in (summary.get("delivery_blockers") or [])]
    final_states: dict[str, str] = dict(summary.get("final_states") or {})
    regressed = sorted(name for name, value in final_states.items() if str(value).startswith("regressed"))
    if regressed:
        print("不可按成功使用：以下任务曾成功但最终验收未通过或依赖已回归任务：" + "、".join(regressed))
    if blockers:
        print("交付阻塞：" + "；".join(blockers))
    # S05/Q11：批次是否可交付以最终状态为准，不再只看冻结数；回归不得以成功结果离开。
    deliverable = bool(summary.get("deliverable", (summary["frozen"] == 0 and summary["cancelled"] == 0)))
    if not deliverable:
        print("本批不可直接交付：请先处理上述阻塞并复核，不要按成功使用本批结果。")
    stats = uxtext.interruption_stats(args.root)
    print("中断统计：总次数 " + str(stats["total"])
          + "；不必要重复询问 " + str(stats["unnecessary_questions"])
          + "；必要新增授权 " + str(stats["necessary_new_authorizations"])
          + "；工具/系统强制确认 " + str(stats["tool_forced_confirmations"]) + "。")
    return _delivery_exit_code(summary)


class MockAdapterLike:
    """CLI 自主模式的离线模拟适配器，输出中明确标记为非真实。"""

    def __init__(self) -> None:
        from .aiadapter import MockAdapter

        self._inner = MockAdapter()

    @property
    def real(self) -> bool:
        return False

    def complete(self, *args, **kwargs):
        return self._inner.complete(*args, **kwargs)


def _print_frontier(view: dict[str, Any]) -> None:
    print(f"当前会话：{view['session']['id']}，版本：{view['session']['revision']}")
    print("Host 未接通；本入口不会调用 Host、Provider、网络、凭据或真实外部服务。")
    if view["frontier"]:
        print("本轮需要确认的问题：")
        for item in view["frontier"]:
            print(f"- {item['id']}：{item['question']}（{item['why']}）")


def _show_plan(view: dict[str, Any]) -> None:
    recommendation = view["recommendation"]
    print(f"方案状态：{recommendation['status']}")
    print(f"平台建议：{recommendation['platforms']['primary']}；技术建议：{recommendation['stack']['client']['technology']}")
    print("能力需求：" + "、".join(f"{item['id']}={item['need']}" for item in recommendation["capabilities"]))
    if recommendation["unresolved"]:
        print("仍待确认：" + "；".join(recommendation["unresolved"]))
    print("外部能力仍未接通。")
    if "diff" in view:
        print("本地文件差异：")
        print(view["diff"] or "（无差异）")


def _status_text(value: Any, *, limit: int = 96) -> str:
    """Keep human status output single-line and deliberately bounded."""

    if value is None:
        return "无"
    text = "".join(character for character in str(value) if not (ord(character) < 32 or 127 <= ord(character) <= 159))
    text = sanitize_text(text).strip()
    if not text:
        return "（已省略）"
    return text if len(text) <= limit else text[:limit] + "..."


def _state_label(value: Any, labels: dict[str, str]) -> str:
    return labels.get(value, "未知状态")


def _show_execution_status(snapshot: dict[str, Any]) -> None:
    status = snapshot.get("status")
    if status == "not_initialized":
        print("执行状态：尚未初始化；未创建任务状态。")
        return
    if status == "not_found":
        print("执行状态：未找到指定任务。")
        return
    if status != "ready":
        print("执行状态：状态信息不可识别。")
        return

    tasks = snapshot.get("tasks")
    runs = snapshot.get("runs")
    tasks = tasks if isinstance(tasks, list) else []
    runs = runs if isinstance(runs, list) else []
    print(f"执行状态：就绪。任务：{len(tasks)}；运行：{len(runs)}。")
    _show_status_tasks(tasks)
    _show_status_runs(runs)
    events = snapshot.get("events")
    if isinstance(events, list) and events:
        print("自主执行记录：")
        for event in events[-20:]:
            item = event if isinstance(event, dict) else {}
            print(
                "- "
                + "；".join(
                    (
                        "运行=" + _status_text(item.get("run_id")),
                        "任务=" + _status_text(item.get("task_id")),
                        "请求=" + _status_text(item.get("request_id")),
                        "阶段=" + _status_text(item.get("stage") or item.get("event_type")),
                        "验收=" + _status_text(item.get("verification_ok")),
                    )
                )
            )


def _show_status_tasks(tasks: list[Any]) -> None:
    if not tasks:
        return
    print("任务摘要：")
    for item in tasks[:20]:
        task = item if isinstance(item, dict) else {}
        print(
            "- "
            + "；".join(
                (
                    "类型=" + _status_text(task.get("kind") or "scheduler"),
                    "任务=" + _status_text(task.get("task_id")),
                    "状态=" + _state_label(task.get("state"), _TASK_STATE_LABELS),
                    "尝试=" + _status_text(task.get("attempt")) + "/" + _status_text(task.get("max_attempts")),
                    "最近运行=" + _status_text(task.get("last_run_id")),
                )
            )
        )
    if len(tasks) > 20:
        print(f"其余 {len(tasks) - 20} 条任务未展开。")


def _show_status_runs(runs: list[Any]) -> None:
    if not runs:
        return
    print("运行摘要：")
    for item in runs[:20]:
        run = item if isinstance(item, dict) else {}
        print(
            "- "
            + "；".join(
                (
                    "类型=" + _status_text(run.get("kind") or "scheduler"),
                    "运行=" + _status_text(run.get("run_id")),
                    "任务=" + _status_text(run.get("task_id")),
                    "状态=" + _state_label(run.get("status"), _RUN_STATE_LABELS),
                    "尝试=" + _status_text(run.get("attempt")),
                    "开始=" + _status_text(run.get("started_at")),
                    "结束=" + _status_text(run.get("finished_at")),
                    "退出码=" + _status_text(run.get("exit_code")),
                )
            )
        )
    if len(runs) > 20:
        print(f"其余 {len(runs) - 20} 条运行未展开。")


def _print_status_error(code: str) -> None:
    print(as_json({"error": {"code": code}}), file=sys.stderr)


def _show_status_error(code: str) -> None:
    print(uxtext.error_message(code), file=sys.stderr)


def _note_pause(
    root: str,
    *,
    category: str,
    action: str,
    reason: str,
    reason_code: str = "",
    source_layer: str | None = None,
    authorization: str | None = None,
    auto_path: str | None = None,
    new_risk: str | None = None,
) -> None:
    """在真实暂停点登记中断来源；登记失败不影响已经给出的中文提示。"""

    try:
        uxtext.record_interruption(
            root,
            category=category,
            action=action,
            reason=reason,
            reason_code=reason_code,
            source_layer=source_layer,
            authorization=authorization,
            auto_path=auto_path,
            new_risk=new_risk,
        )
    except (OSError, ValueError, TypeError):
        pass


def _prompt(input_fn: Callable[[str], str], message: str) -> str:
    try:
        return input_fn(message)
    except EOFError:
        raise


def _wizard(root: str, resume: str | None, input_fn: Callable[[str], str]) -> int:
    if resume:
        view = session_view(root, resume)
    else:
        try:
            goal = _prompt(input_fn, "先说说你想解决的事情：").strip()
        except EOFError:
            print("已暂停；没有创建会话，也没有写入任何文件。")
            _note_pause(root, category="user_pause", action="创建会话", reason="输入流结束，未说明目标。", source_layer="tool", auto_path="重新运行并输入目标后继续。")
            return 0
        if not goal:
            print("已取消；没有写入业务文件。")
            _note_pause(root, category="user_pause", action="创建会话", reason="目标为空，未创建会话。", auto_path="未写入任何业务文件。")
            return 0
        view = create_session(root, goal)
        print(f"已创建会话 {view['session']['id']}，可以随时退出后用 --resume 恢复。")

    while view["frontier"]:
        _print_frontier(view)
        question = view["frontier"][0]
        try:
            answer = _prompt(input_fn, f"{question['question']}\n回答（输入“取消”退出）：").strip()
        except EOFError:
            print("已暂停；会话已保存，可用 --resume 恢复。")
            _note_pause(root, category="user_pause", action="回答问题", reason="输入流结束，用户暂停。", source_layer="tool", auto_path="会话已保存，可用 --resume 恢复。")
            return 0
        if answer in {"取消", "退出", "q", "quit"}:
            print("已暂停；会话已保存，可用 --resume 恢复。")
            _note_pause(root, category="user_pause", action="回答问题", reason="用户主动暂停。", auto_path="会话已保存，可用 --resume 恢复。")
            return 0
        if not answer:
            print("回答不能为空，本轮仍需确认。")
            _note_pause(root, category="repeat_question", action="回答问题", reason="用户提交了空回答。", auto_path="继续等待当前问题的有效回答。")
            continue
        result = submit_answer(root, view["session"]["id"], view["session"]["revision"], question["id"], answer)
        if result.get("status") in {"busy", "stale"}:
            print("会话版本或写入锁发生变化，已拒绝本次回答；请重新查看会话。")
            _note_pause(root, category="system_error", action="回答问题", reason="会话版本或写入锁发生变化。", reason_code=result.get("reason_codes", ["unknown"])[0], auto_path="重新查看会话后再回答。")
            return 2
        view = result
        requirement = view["session"]["requirements"].get(question["id"], {})
        if requirement.get("kind") in {"unknown", "ambiguous", "conflict"}:
            print("这个回答还不能形成明确判断，请在当前问题上补充或修改。")

    preview = preview_session(root, view["session"]["id"])
    _show_plan(preview)
    print("精确本地写入范围：" + "、".join(preview["targets"]))
    print("将生成文档并保留可回滚 receipt；不会执行源码任务或外部服务。")
    try:
        confirmation = _prompt(input_fn, "确认目标、方案和以上精确范围并生成文档？请输入“确认”或“拒绝”：").strip()
    except EOFError:
        print("已暂停；尚未审批或写入，可用 --resume 恢复。")
        _note_pause(root, category="user_pause", action="确认生成文档", reason="输入流结束，尚未审批。", source_layer="tool", auto_path="可用 --resume 恢复后重新确认。")
        return 0
    if confirmation not in {"确认", "同意", "yes", "y"}:
        print("已拒绝；未写入业务文件。")
        _note_pause(root, category="user_rejected", action="确认生成文档", reason="用户拒绝写入。", authorization="未签发任何批准。", auto_path="未写入任何业务文件。")
        return 0
    approval = approve_preview(preview)
    result = apply_approved(root, approval)
    if result.get("status") != "applied":
        print("应用未完成：" + as_json(result))
        _note_pause(root, category="system_error", action="应用已批准文档", reason="应用未完成：" + str(result.get("status")), auto_path="保留现场，人工核对后可重试。")
        return 2
    transaction = result["transaction"]
    print("文档已生成：" + "、".join(transaction.get("changed_paths", [])))
    print(f"事务：{transaction.get('transaction_id')}；可用 --rollback {transaction.get('transaction_id')} 回滚。")
    return 0


def main(argv: list[str] | None = None, *, input_fn: Callable[[str], str] = input) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.task_id is not None and not (args.status or args.rollback_run):
        parser.error("--task-id 只能与 --status 或 --rollback-run 一起使用")
    if args.reason is not None and not args.cancel_run:
        parser.error("--reason 只能与 --cancel-run 一起使用")
    if (args.mock_ai or args.run_id is not None or args.grant_ttl != 8 or args.plan_digest is not None) and not args.autorun:
        parser.error("--mock-ai / --run-id / --grant-ttl / --plan-digest 只能与 --autorun 一起使用")
    try:
        if args.autorun:
            return _autorun_mode(args)
        if args.cancel_run:
            result = cancel_autonomous_run(
                args.root, args.cancel_run, reason=args.reason or "用户通过中文命令取消",
            )
            if args.json:
                print(as_json(result))
            else:
                print("自主运行已取消；不会自动恢复。")
            return 0
        if args.query_run:
            from .service import query_autonomous_run

            result = query_autonomous_run(args.root, args.query_run)
            if args.json:
                print(as_json(result))
            else:
                print("只读核验状态：" + str(result.get("status")))
                if result.get("status") == "not_supported":
                    print("当前适配器不支持按原请求编号查询；未新增请求、未改变预算。")
            return 0 if result.get("status") not in {"not_supported", "invalid"} else 2
        if args.rollback_run:
            result = rollback_autonomous_run(args.root, args.rollback_run, args.task_id)
            if args.json:
                print(as_json(result))
            else:
                print("自主任务回滚状态：" + str(result.get("status")))
                print(as_json(result))
            return 0 if result.get("status") == "rolled_back" else 2
        if args.status:
            try:
                result = execution_status(args.root, args.task_id)
            except ServiceError as exc:
                if args.json:
                    _print_status_error(exc.code)
                else:
                    _show_status_error(exc.code)
                return 2
            except (OSError, ValueError, TypeError):
                if args.json:
                    _print_status_error("status_read_failed")
                else:
                    _show_status_error("status_read_failed")
                return 2
            if args.json:
                print(as_json(result))
            else:
                _show_execution_status(result)
            return 0
        if args.evaluate:
            result = evaluate_session(args.root, args.evaluate)
            if args.json:
                print(as_json(result))
            else:
                decision = result["decision_record"]
                plan = result["task_plan"]
                print(uxtext.render_progress(
                    "已依据当前答案与事实生成离线任务图；当前仍是只读预览，未激活执行。",
                    "首选平台：" + str(decision["chosen"]["platform"])
                    + "；方案摘要：" + result["adopted_plan_digest"],
                    "已校验 " + str(len(plan["tasks"])) + " 项任务、"
                    + str(len(plan["waves"])) + " 个波次。",
                    "下一步依计划核对任务验收与独立激活边界。",
                    "；".join(decision["user_decisions_required"]) or "暂无",
                ))
                print("备选：" + as_json(decision["alternatives"]))
            return 0
        if args.adopt_plan:
            result = adopt_evaluation_plan(args.root, args.adopt_plan)
            if args.json:
                print(as_json(result))
            else:
                adoption = result["adoption"]
                print("评估任务图已明确采用并持久化。")
                print("会话=" + str(adoption["session_id"]) + "；版本=" + str(adoption["revision"]))
                print("计划摘要=" + str(adoption["plan_digest"]))
                print("验证结果=" + ("通过" if adoption["validated"] else "未通过"))
            return 0
        if args.rollback:
            result = rollback(args.root, args.rollback)
            if args.json:
                print(as_json(result))
            else:
                print("回滚状态：" + str(result.get("status")))
                print(as_json(result))
            return 0 if result.get("status") in {"rolled_back", "partial_failure"} else 2
        if args.list:
            from .service import list_sessions

            result = list_sessions(args.root)
            if args.json:
                print(as_json(result))
            else:
                print("\n".join(f"{item['id']} revision={item['revision']} state={item['state']} {item['goal']}" for item in result) or "暂无本地会话。")
            return 0
        if args.preview:
            result = preview_session(args.root, args.preview)
            if args.json:
                print(as_json(result))
            else:
                print("只读预览（未写入）：")
                _show_plan(result)
                print("精确本地写入范围：" + "、".join(result["targets"]))
            return 0
        if args.change:
            session_id, question_id, answer = args.change
            before = session_view(args.root, session_id)
            result = submit_answer(args.root, session_id, before["session"]["revision"], question_id, answer)
            if result.get("status") in {"busy", "stale"}:
                print("修改被拒绝：会话版本或写入锁已变化。", file=sys.stderr)
                return 2
            after = preview_session(args.root, session_id)
            if args.json:
                print(as_json(after))
            else:
                print("答案已修改；旧审批失效。更新后的方案与差异：")
                _show_plan(after)
            return 0
        return _wizard(args.root, args.resume, input_fn)
    except ServiceError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


__all__ = ["main"]
