"""Chinese-first, offline OpenCoding entry point."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Callable

from .service import (
    ServiceError,
    apply_approved,
    approve_preview,
    as_json,
    create_session,
    preview_session,
    rollback,
    session_view,
    submit_answer,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m opencoding",
        description="OpenCoding 中文入口：离线问答、方案预览、精确本地文档确认与回滚。",
    )
    parser.add_argument("--root", required=True, help="已有本地项目根目录（绝对路径）")
    parser.add_argument("--resume", metavar="SESSION_ID", help="恢复指定会话")
    parser.add_argument("--list", action="store_true", help="列出本地会话")
    parser.add_argument("--preview", metavar="SESSION_ID", help="只读显示指定会话的方案、文件范围和差异")
    parser.add_argument("--change", nargs=3, metavar=("SESSION_ID", "QUESTION_ID", "ANSWER"), help="修改已有会话答案并显示新方案差异")
    parser.add_argument("--rollback", metavar="TRANSACTION_ID", help="回滚指定的本地文档事务")
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
    approval = approve_preview(preview)
    result = apply_approved(root, approval)
    if result.get("status") != "applied":
        print("应用未完成：" + as_json(result))
        return 2
    transaction = result["transaction"]
    print("文档已生成：" + "、".join(transaction.get("changed_paths", [])))
    print(f"事务：{transaction.get('transaction_id')}；可用 --rollback {transaction.get('transaction_id')} 回滚。")
    return 0


def main(argv: list[str] | None = None, *, input_fn: Callable[[str], str] = input) -> int:
    args = _parser().parse_args(argv)
    try:
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
