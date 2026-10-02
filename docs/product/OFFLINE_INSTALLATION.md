# 离线安装

OpenCoding 的运行时没有第三方依赖。离线使用时，先在一台具备构建条件的机器上生成本地 wheel，再把该 wheel 复制到目标机器安装。

## 安装本地 wheel

在目标机器上创建或选择 Python 3.11 及以上版本的虚拟环境，然后仅从本地 wheel 安装：

```powershell
python -m pip install --no-index --no-deps "./opencoding_local_entry-0.2.7-py3-none-any.whl"
```

上例版本对应当前 `pyproject.toml`；请使用构建命令实际输出的完整 wheel 文件名，文件不在当前目录时填写它的路径。不要直接把 `*.whl` 交给 pip 作为跨 shell 通用用法。

安装后可使用模块入口或命令入口。`--status` 是只读查询；尚未初始化时会返回 `not_initialized`，不会创建项目状态。

```powershell
python -m opencoding --root C:\path\to\local-project --status --json
opencoding --root C:\path\to\local-project --status --json
```

## 已安装包中的 agent 使用

已安装的 wheel 提供 `python -m opencoding`、`opencoding`、`opencoding-project` 和 Python 模块 API。本节验证的会话、文档及只读状态路径使用已有本地项目的绝对 `--root` 或 `root`，不发起 Provider 或外部服务调用。包内也有其他模式及适配器；本节不宣称它们已接通或始终离线。

先用帮助和只读状态确认入口及本地状态：

```powershell
python -m opencoding --help
python -m opencoding --root C:\path\to\authorized-project --status --json
```

`--status --json` 的成功状态为 `not_initialized`、`not_found` 或 `ready`；状态查询错误以 `{"error":{"code":"..."}}` 输出到标准错误。不要把这个稳定的 status JSON 约定扩展为交互式向导或所有 CLI 错误的通用协议。没有 `--create`、`--apply`、`--requeue` 或其他未在 `--help` 中出现的开关。

交互式 CLI 会在开始时创建或恢复会话；创建/回答会写本地会话，只有在操作者确认精确 preview 范围后才会尝试生成本地文档。`--preview` 和 `--status` 是查看入口；`--rollback TRANSACTION_ID` 是本地回滚操作。

已安装包中可直接调用的会话/文档 API 是：`create_session(root, goal)`、`session_view(root, session_id, *, include_preview=False)`、`list_sessions(root)`、`submit_answer(root, session_id, expected_revision, question_id, answer)`、`preview_session(root, session_id)`、`approve_preview(preview, *, expires_in_seconds=300)`、`apply_approved(root, approval)` 和 `rollback(root, transaction_id)`。创建会话和提交回答会在文档 apply 前写入本地会话；`preview_session` 是查看精确 targets 和 diff 的零写入步骤；`approve_preview` 只校验完整 preview 并在内存中生成到期的本地 approval；只有调用方已记录用户对该 exact reviewed root、targets 和 diff 的授权后，才应调用 `apply_approved`。不能自行构造 approval，也不能把其中的 `approved` 字段当成外部授权。对 `busy` 或 `stale` 结果，停止当前流程、重新读取并重新 preview，不得继续 apply。

已安装包也导出 `opencoding.scheduler.read_snapshot(root, task_id=None)`、`recover(root)` 和 `requeue(root, task_id, idempotency_key)`。只有 `read_snapshot` 以及 CLI 的 `--status` 是零写入状态查看；它们不会初始化、迁移或恢复 scheduler。`recover` 和 `requeue` 都会构造 `Scheduler`，而构造 scheduler 会初始化/迁移本地状态并执行恢复；它们是需要授权本地范围的写入操作。`get_task` 与 `list_runs` 也构造 scheduler，因此不能仅因最后查询而标记为零写入。`requeue` 需要匹配 idempotency key，可能因状态或 attempt 限制被拒绝；`recover` 仅恢复遗留运行记录，二者都不会单独执行任务，也不会把 TaskPlan 变成真实执行。

应先检查每次返回的 `status`，并把 `ServiceError.code`、`ValueError` 或 `SchedulerSnapshotError` 作为调用方处理的本地失败，而不是解析人类可读错误文本。不要把 `--status --json` 的稳定状态错误格式扩展为交互式向导或所有 CLI 模式的统一 JSON 协议。源码仓库中的更完整说明位于 `docs/product/AGENT_NATIVE_USE.md`，但该源文件不包含在当前 wheel；本节已列出安装后所需的调用顺序和限制。

## 安装产物与源码入口的边界

| 入口 | 当前 wheel 边界 |
| --- | --- |
| `python -m opencoding --help` / `opencoding --help` | 列出原有会话、状态、计划和自主模式；本文只验收本地会话/文档 API。 |
| `python -m opencoding project --help` / `opencoding-project --help` | 可查看 Stage28 子命令；`init/plan/apply-docs/status` 可运行。`project status` 读取项目入口状态，与旧 `--status` 的 scheduler/autorun 快照是不同协议。 |
| `project preview/run`、`preview_agent_tasks`、`LocalAgentTaskExecutor` | 需要随源码 checkout 提供的 `.agents/skills/opencoding`。当前 wheel/sdist 没有这份资源，安装包不能据此声称完成结构化任务闭环。需要该路径时，从完整 checkout 使用 `python -m opencoding project ...`。 |
| 文档与 skill 安装脚本 | 本说明嵌入 wheel 的包元数据；`QUICKSTART_CN.md` 只在 sdist 中。`AGENT_NATIVE_USE.md`、`scripts/`、两份 skill 资源不在 wheel/sdist 中。 |

`python -m opencoding.project_entry --help` 没有模块级 main 调用，可能只退出 0，不能作为入口有效的证据。使用上表 CLI 路由。`project` 错误 JSON 和退出码也不等同于旧 `--status --json` 的约定；所有路径都必须检查结构化状态，不能仅凭退出码 0 声称任务成功。

## 可调用的会话/文档示例

以下函数用于调用方已有授权范围内的会话和文档工作；加载代码只定义函数，不自动创建项目或写入文档。调用方给出已有绝对目录、需求及 `answer_for(question)`。回答未收齐时返回调用方，不把未知回答填成否；`busy/stale` 后用 `session_view` 重读同一会话再协调，不重新创建会话冒充续跑。

```python
from opencoding.service import (
    apply_approved, approve_preview, create_session, preview_session, submit_answer,
)


def collect_preview(root, goal, answer_for, *, max_answers=24):
    view = create_session(root, goal)
    session_id = view["session"]["id"]
    for _ in range(max_answers):
        if not view["frontier"]:
            break
        question = view["frontier"][0]
        answer = answer_for(question)
        if answer is None:
            return {"status": "needs_answers", "session_id": session_id, "question": question}
        outcome = submit_answer(root, session_id, view["session"]["revision"], question["id"], answer)
        if outcome.get("status") in {"busy", "stale"}:
            return {"status": "conflict", "session_id": session_id, "result": outcome}
        view = outcome
    if view["frontier"]:
        return {"status": "needs_answers", "session_id": session_id, "questions": view["frontier"]}
    preview = preview_session(root, session_id)
    if preview["status"] != "ready":
        return {"status": "needs_clarification", "session_id": session_id, "preview": preview}
    return {"status": "ready_for_authorization", "preview": preview}


def apply_after_exact_user_authorization(prepared):
    if prepared["status"] != "ready_for_authorization":
        return prepared
    # Only call after existing user authority covers this exact root, targets and diff.
    preview = prepared["preview"]
    return apply_approved(preview["root"], approve_preview(preview))
```

`collect_preview` 会保存会话/回答，但不会应用业务文档。先查看返回的 `preview["targets"]` 和 `preview["diff"]`；已有授权覆盖该精确范围后，调用 `apply_after_exact_user_authorization`。它仍可能返回 `stale/busy` 或事务失败，不能自动重用审批。仅在 `status == "applied"` 时，从 `result["transaction"]["transaction_id"]` 取真实事务 ID；需要回滚时调用 `opencoding.service.rollback(root, transaction_id)`，并检查返回状态及回执。

## 在开发机生成分发包

源代码构建使用已安装的 `setuptools>=83`。在离线环境中，构建机必须预先具备满足该版本要求的构建后端；构建过程不会下载依赖。本文档也是分发包的专用说明文件，不会因此把其他 `docs` 目录或根目录 README 一并加入分发包。

可以通过 Python 的构建后端接口在本地输出目录中先生成 source distribution（sdist）和 wheel：

```powershell
python -c "import pathlib, setuptools.build_meta as backend; out=pathlib.Path('dist'); out.mkdir(exist_ok=True); print(backend.build_sdist(str(out))); print(backend.build_wheel(str(out)))"
```

发布前可在独立目录中解开本地 sdist，再使用同一台构建机的本地后端从该源归档重建 wheel；目标机器只安装这个本地重建的 wheel。构建机不应依赖网络下载构建后端。

开发时也可以直接从源代码运行：

```powershell
python -m opencoding --root C:\path\to\local-project --status --json
```

本文验证的文档路径不激活 Host、Provider 或外部服务。已有本地任务授权覆盖精确写入范围时可由调用方继续；授权对象不能代替用户授权。文档事务可通过 `--rollback` 回滚。Python/Node 执行动作是同用户子进程，其网络和任意副作用不由本地适配器隔离或自动回滚。
