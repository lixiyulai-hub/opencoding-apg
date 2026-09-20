# 离线安装

OpenCoding 的运行时没有第三方依赖。离线使用时，先在一台具备构建条件的机器上生成本地 wheel，再把该 wheel 复制到目标机器安装。

## 安装本地 wheel

在目标机器上创建或选择 Python 3.11 及以上版本的虚拟环境，然后仅从本地 wheel 安装：

```powershell
python -m pip install --no-index --no-deps .\opencoding_local_entry-0.1.0-py3-none-any.whl
```

安装后可使用模块入口或命令入口。`--status` 是只读查询；尚未初始化时会返回 `not_initialized`，不会创建项目状态。

```powershell
python -m opencoding --root C:\path\to\local-project --status --json
opencoding --root C:\path\to\local-project --status --json
```

## 已安装包中的 agent 使用

已安装的 wheel 提供 `python -m opencoding`、`opencoding` 和 Python 模块 API；它不是 HTTP 服务、MCP server、浏览器工作台或特定 Host 适配器。所有调用都必须给出已有本地项目的绝对 `--root` 或 `root`，并且不会连接 Provider、网络、凭据或外部服务。

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

该包不连接 Host、Provider、网络、凭据或真实外部服务。生成文档仍需在本地交互流程中确认精确写入范围，生成的本地事务可通过 `--rollback` 回滚。
