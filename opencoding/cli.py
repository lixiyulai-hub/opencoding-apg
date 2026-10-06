"""Chinese-first, offline OpenCoding entry point."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Callable

from .safety import sanitize_text
from .service import (
    ServiceError,
    apply_approved,
    approve_preview,
    as_json,
    build_caller_confirmation,
    create_session,
    execution_status,
    preview_session,
    rollback,
    session_view,
    submit_answer,
)
from .taskplan_scheduler import preview_task_plan


_TASK_STATE_LABELS = {
    "queued": "排队中",
    "running": "运行中",
    "succeeded": "已成功",
    "failed": "失败",
    "frozen": "已冻结",
    "cancelled": "已取消",
    "timed_out": "已超时",
}
_RUN_STATE_LABELS = {
    "running": "运行中",
    "succeeded": "已成功",
    "failed": "失败",
    "cancelled": "已取消",
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
    mode.add_argument("--task-preview", metavar="SESSION_ID", help="只读显示指定会话的 TaskPlan 任务、依赖和预计效果；不执行")
    mode.add_argument("--change", nargs=3, metavar=("SESSION_ID", "QUESTION_ID", "ANSWER"), help="修改已有会话答案并显示新方案差异")
    mode.add_argument("--rollback", metavar="TRANSACTION_ID", help="回滚指定的本地文档事务")
    mode.add_argument("--status", action="store_true", help="只读显示本地任务与运行状态")
    parser.add_argument("--task-id", help="仅在 --status 时查看指定任务")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出服务状态")
    return parser


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


def _show_task_preview(preview: dict[str, Any]) -> None:
    print("TaskPlan 只读预览（未执行、未写入）：")
    print(f"项目根目录：{preview['root']}")
    print(f"当前会话：{preview['session']['id']}；版本：{preview['session']['revision']}")
    print(f"方案状态：{preview['status']}（仅表示方案状态，不表示任务运行成功）")
    print("Host 未接通；以下任务均未由本次预览执行，未查询实际运行或冻结状态。")
    unresolved = preview["service_preview"]["task_plan"]["unresolved"]
    if unresolved:
        print("仍待确认：" + "；".join(unresolved))
    print("精确目标范围：")
    for target in preview["targets"]:
        print("- " + target)
    print("本地文件差异：")
    print(preview["diff"] or "（无差异）")
    print("任务与依赖（预计映射，均未激活）：")
    classifications = {
        "document": "document（本地文档，未激活）",
        "offline_design": "offline_design（仅离线设计，未激活外部能力）",
        "host_missing": "host_missing（缺少 Host 执行器，无法执行实现或验证）",
    }
    for task in preview["tasks"]:
        print(f"- 任务 ID：{task['task_id']}；方案任务 ID：{task['input']['plan_task_id']}")
        print("  依赖：" + ("、".join(task["depends_on"]) or "无"))
        print("  分类：" + classifications[task["input"]["classification"]])
    print("预计效果（仅在另外确认并调用执行 API 后可能发生，本次未发生）：")
    print("- 本地 Scheduler 初始化、入队与回执；会话协作写锁；按文档事务回滚。")
    print("- 不接通或激活外部能力。")
    print("预览结束：未审批、未执行、未创建或恢复 Scheduler；输入“确认”也不会执行。")


def _task_preview(root: str, session_id: str, *, json_output: bool) -> int:
    try:
        result = preview_task_plan(root, session_id)
    except (OSError, ValueError, TypeError) as exc:
        code = exc.code if isinstance(exc, ServiceError) else "task_preview_read_failed"
        if json_output:
            print(as_json({"error": {"code": code}}), file=sys.stderr)
        else:
            print(f"无法读取 TaskPlan 预览（{code}）；未执行、未写入，请检查根目录及会话。", file=sys.stderr)
        return 2
    if json_output:
        print(as_json(result))
    else:
        _show_task_preview(result)
    return 0


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
    messages = {
        "invalid_root": "无法读取执行状态：项目根目录无效。",
        "execution_status_invalid_task_id": "无法读取执行状态：任务编号无效。",
        "execution_status_database_busy": "读取执行状态失败：本地状态正在被占用，请稍后重试。",
        "execution_status_unsafe_journal_state": "无法安全读取执行状态：本地状态正在更新。",
        "execution_status_unsupported_platform": "当前平台不支持只读执行状态查询。",
        "execution_status_unsupported_schema": "无法读取执行状态：本地状态版本不受支持。",
        "execution_status_database_unavailable": "无法读取执行状态：本地状态不可用。",
    }
    print(messages.get(code, "读取执行状态失败，请检查本地状态后重试。"), file=sys.stderr)


def _prompt(input_fn: Callable[[str], str], message: str) -> str:
    try:
        return input_fn(message)
    except EOFError:
        raise


def _wizard(root: str, resume: str | None, input_fn: Callable[[str], str]) -> int:
    if resume:
        view = session_view(root, resume)
    else:
        goal = _prompt(input_fn, "先说说你想解决的事情：").strip()
        if not goal:
            print("已取消；没有写入业务文件。")
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
            return 0
        if answer in {"取消", "退出", "q", "quit"}:
            print("已暂停；会话已保存，可用 --resume 恢复。")
            return 0
        if not answer:
            print("回答不能为空，本轮仍需确认。")
            continue
        result = submit_answer(root, view["session"]["id"], view["session"]["revision"], question["id"], answer)
        if result.get("status") in {"busy", "stale"}:
            print("会话版本或写入锁发生变化，已拒绝本次回答；请重新查看会话。")
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
        return 0
    if confirmation not in {"确认", "同意", "yes", "y"}:
        print("已拒绝；未写入业务文件。")
        return 0
    caller_receipt = build_caller_confirmation(
        preview,
        statement="用户确认以上方案、精确范围和差异，只生成本地文档。",
        actor="interactive-cli",
    )
    approval = approve_preview(preview, confirmation=caller_receipt)
    result = apply_approved(root, approval, authorization_context=caller_receipt)
    if result.get("status") != "applied":
        print("应用未完成：" + as_json(result))
        return 2
    transaction = result["transaction"]
    print("文档已生成：" + "、".join(transaction.get("changed_paths", [])))
    print(f"事务：{transaction.get('transaction_id')}；可用 --rollback {transaction.get('transaction_id')} 回滚。")
    return 0


def main(argv: list[str] | None = None, *, input_fn: Callable[[str], str] = input) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.task_id is not None and not args.status:
        parser.error("--task-id 只能与 --status 一起使用")
    try:
        if args.task_preview is not None:
            return _task_preview(args.root, args.task_preview, json_output=args.json)
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
