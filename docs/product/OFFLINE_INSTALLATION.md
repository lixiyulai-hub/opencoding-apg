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

交互式 CLI 会在开始时创建或恢复会话；创建/回答会写本地会话，只有在操作者确认精确 preview 范围后才会尝试生成本地文档。`--preview`、`--task-preview` 和 `--status` 是查看入口；`--rollback TRANSACTION_ID` 是本地回滚操作。各模式互斥，`--task-id` 仍仅可与 `--status` 搭配。

已安装包中可直接调用的会话/文档 API 是：`create_session(root, goal)`、`session_view(root, session_id, *, include_preview=False)`、`list_sessions(root)`、`submit_answer(root, session_id, expected_revision, question_id, answer)`、`preview_session(root, session_id)`、`build_caller_confirmation(preview, *, statement, actor=...)`、`approve_preview(preview, *, confirmation, expires_in_seconds=300)`、`apply_approved(root, approval, authorization_context=...)` 和 `rollback(root, transaction_id)`。创建会话和提交回答会在文档 apply 前写入本地会话；`preview_session` 是查看精确 targets 和 diff 的零写入步骤；调用方必须先让人确认该 exact reviewed root、session revision、targets 和 diff，再生成 caller-issued receipt。`approve_preview` 会把该 receipt 绑定到到期的本地 approval；`apply_approved` 必须收到同一份 `authorization_context`，否则拒绝。不能自行构造或编辑 approval，也不能把其中的 `approved` 字段当成外部授权。对 `busy` 或 `stale` 结果，停止当前流程、重新读取并重新 preview，不得继续 apply。

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

该包不连接 Host、Provider、网络、凭据或真实外部服务。服务端、数据库、身份/KYC、存储、外部数据、支付、通知、物流、鉴定和风控集成只生成带人工 Gate、未启用状态和回滚说明的离线方案；不会创建账号或写入密钥。生成文档仍需在本地交互流程中确认精确写入范围，生成的本地事务可通过 `--rollback` 回滚。

## 受限任务图只读预览与调度 API

对已有项目和会话，可用只读 CLI 查看 TaskPlan：

```powershell
python -m opencoding --root C:\path\to\authorized-project --task-preview SESSION_ID
opencoding --root C:\path\to\authorized-project --task-preview SESSION_ID --json
```

中文输出包括 root、会话 ID/revision、方案状态、精确 targets、diff、任务 ID、依赖和
分类。`ready` 仅表示方案状态，不代表执行成功；`document` 是预计本地文档任务，
`offline_design` 尚未激活，`host_missing` 表示缺少实现/验证执行器。
预览不查询实际运行或冻结状态；不能将方案中的能力缺口报告为已运行或已 `frozen`。
`effects` 是另行确认并执行 API 后可能发生的效果，不是本次已经产生的效果。

`--json` 原样序列化 `preview_task_plan(root, session_id)` 的完整返回值。
退出 `0` 表示读取成功，即使方案仍有未解决问题；读取失败退出 `2`，JSON 模式下
标准错误输出 `{"error":{"code":"..."}}`。参数错误使用 CLI 参数解析器的错误输出。
这不改变 `--status --json` 协议，也不建立适用于所有 CLI 模式的统一错误协议。

本模式不会创建、初始化、迁移、恢复或修复会话/Scheduler，不审批、执行、派发、
重排、回滚或进入向导；向标准输入发送“确认”也不会执行。root/session 不存在、
会话 ID 非法、模式冲突、坏数据或读取锁失败时，不创建路径、不自动修复。
新增 CLI 的 Windows 验证仍待执行，以上 Windows 命令示例不是测试通过记录。

安装包也提供 `opencoding.taskplan_scheduler.preview_task_plan(root, session_id)`、
`build_task_plan_confirmation(preview, *, statement, actor="human-caller", expires_in_seconds=300)`、
`approve_task_plan(preview, *, confirmation)` 和
`execute_task_plan(root, approval, *, authorization_context)`。预览、确认构造和审批均零写入；调用方展示
完整调度预览的 targets、diff、tasks、effects 后，使用
`build_task_plan_confirmation(preview, statement=..., actor=..., expires_in_seconds=300)`
记录该范围及期限的确认。将完整上下文独立保存，传入审批及执行；不要从待验证审批反取
授权上下文。approve 不重置期限，过期必须重新取得确认。普通 service 收据未绑定调度
期限，不能替代调度确认。确认、审批和执行仍仅提供 Python API；`--task-preview`
仅提供上述零写入预览，没有 CLI 审批、执行或自动派发入口。

执行仅按依赖生成同源 Markdown 文档，每个节点复用现有可回滚事务，并写入 Scheduler
任务与证据。integration_design 保持未激活；implement_feature/verify_feature 没有
真实执行器，明确 host_missing，失败传递后继 frozen。整个图被阻断时不返回成功。
相同审批重放不新增已完成文档事务；成功输出被修改或回滚后会阻止重放，会话变化返回
stale。会话锁元数据可能刷新，执行 API 不是只读查询。每个节点最多一次尝试，部分
失败保留现场和回滚引用；从 run 的 artifacts 或 stdout_summary 文档事务 JSON 取得
transaction_id，通过既有 rollback 按逆序撤销并检查返回状态。源码完整说明为
`docs/product/TASKPLAN_SCHEDULER.md`（不随 wheel 打包）。
