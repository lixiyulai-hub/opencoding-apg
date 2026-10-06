# 受限 TaskPlan → Scheduler 离线适配

入口为 `opencoding.taskplan_scheduler` 的 `preview_task_plan`、`approve_task_plan`、
`execute_task_plan`，以及专用 `build_task_plan_confirmation` 确认构造器。它增量使用当前会话/TaskPlan、Scheduler 和文档事务层。
不要求安装 Host，不接入任意 Python/Node 动作或真实业务执行器。

## 只读 CLI 预览

使用已有项目根目录和已有会话 ID 查看任务图；该命令与 `--preview`、`--status`、
`--resume` 等其他模式互斥，`--task-id` 仍仅适用于 `--status`：

```powershell
python -m opencoding --root C:\path\to\authorized-project --task-preview SESSION_ID
python -m opencoding --root C:\path\to\authorized-project --task-preview SESSION_ID --json
```

中文输出显示 root、会话 ID 与 revision、方案状态、精确 targets、diff、任务 ID、
依赖和分类。`ready` 仅表示方案就绪；`document` 是预计本地文档任务，
`offline_design` 是未激活的外部能力设计，`host_missing` 表示缺少实现/验证执行器。
预览不查询实际运行或冻结状态，不能据此报告任务已经成功或 `frozen`。
`effects` 描述另行确认并调用执行 API 后可能发生的效果，不是本次预览产生的效果。

`--json` 直接序列化同一 `preview_task_plan(root, session_id)` 返回值，包括完整
`service_preview`、`tasks`、`effects` 和摘要；不改变既有 `--status --json` 协议。
成功读取预览退出 `0`，不代表方案已 ready 或任务已执行；读取失败退出 `2`。
JSON 模式的读取失败以 `{"error":{"code":"..."}}` 输出到标准错误，参数错误仍由
CLI 参数解析器报告，不能将其视为所有模式通用的 JSON 协议。

本模式不创建、初始化、迁移、恢复或修复会话/Scheduler，不审批、执行、派发、
重排或回滚，也不进入交互式向导；标准输入中的“确认”不会触发执行。
root/session 不存在、会话 ID 非法、模式冲突、坏数据或读取锁失败时，均不会
创建缺失路径或自动修复状态。TaskPlan 的确认、审批和执行仍仅由 Python API 显式调用。

## Python API 使用顺序

```python
from opencoding.taskplan_scheduler import (
    preview_task_plan, build_task_plan_confirmation, approve_task_plan, execute_task_plan,
)

# root 是调用方已授权、已存在的绝对项目路径；session_id 来自当前会话。
preview = preview_task_plan(root, session_id)
# 向操作者展示 targets、diff、tasks、effects 和 host_missing 节点。
# 只有操作者确认该预览及 300 秒期限后，调用方才执行下面的确认步骤。
# 本示例中的 statement 必须由调用方真实授权记录提供。
caller_receipt = build_task_plan_confirmation(
    preview, statement=confirmed_statement, actor=caller_id, expires_in_seconds=300,
)
# 将完整 caller_receipt 独立保存，执行时不要从待验证 approval 反取授权。
approval = approve_task_plan(preview, confirmation=caller_receipt)
result = execute_task_plan(root, approval, authorization_context=caller_receipt)
```

预览和审批零写入，不构造 Scheduler。审批必须来自完整、ready、无 unresolved 的
当前预览；绑定根目录、会话版本、任务映射、依赖、文件计划、targets、diff 和到期时间。
收据沿用 service 的调用方声明契约，是本地完整性检查，不是身份认证或操作系统隔离。
专用确认包装既有 service 收据和精确 expires_at；审批仅复制该期限，不重新续期。
执行必须收到调用方独立保存的完整确认上下文，修改审批内期限会被拒绝。
过期必须重新取得调用方确认，不能通过重新 approve 旧收据续期。
普通 service 收据不包含调度期限，因此不能替代调度确认。
上述确认、审批和执行是 Python API 入口；`--task-preview` 只提供零写入预览，
不提供 CLI 审批、执行或自动派发入口。

执行会取得现有 session 写锁，复核同源会话/方案，初始化 Scheduler 并持久化任务及
receipt。仅派发本图中的任务，不执行数据库中其他 queued 任务。

| TaskPlan 动作 | 适配行为 |
| --- | --- |
| review_requirements / render_document / define_schema / define_interface / define_access / security_review / plan_delivery | `document`：写入同源生成的 Markdown，通过现有文档事务校验 preimage、记录证据 |
| integration_design | `offline_design`：同一文档事务；始终 `not_activated`，保留原 activation_gate |
| implement_feature / verify_feature | `host_missing`：没有可执行内容，不生成源码、不运行测试、不伪造成功 |

没有 TaskPlan owner 的已预览上下文文档（例如 memory.md）成为显式
`adapter-context-documents` 前置节点。其他节点保持原依赖顺序和多输出事务边界。
`security_review` 成功仅表示已生成受限文档，不表示业务代码或真实安全审计已完成。
`delivery-plan` 依赖实现/验证时会被冻结，不越过 host_missing 写成完成。

## 幂等、失败与回滚

稳定调度 ID 绑定原 service digest 和源 task ID；重放同一审批使用原任务与 runs。
成功节点不会重复写入；会话锁元数据可以刷新，执行 API 不承诺零写入。
继续前逐一检查成功输出的实际哈希，用户修改或回滚后返回
`completed_output_drift`，不会自动重写。会话/方案变动返回 `stale`。

每个文档节点最多一个 attempt。事务失败、缺 Host、取消或未知完成状态不会盲目重试；
现有 Scheduler 将失败的传递后继冻结。未执行的 verify 节点可同时具有
`state=frozen` 和 `input.classification=host_missing`，没有成功或测试通过证据。
独立且就绪的文档分支仍可以完成；整个图只要有阻断，结果就是 `blocked`。

成功 run 的 artifacts 包含文件哈希和 transaction_id，Scheduler receipt 保留摘要。
`stdout_summary` 保存文档事务 JSON，包括部分失败时的 transaction_id、receipt_path、
rollback_ref 和 uncertain_paths。部分失败保留现场，之前成功节点不自动回滚。
使用现有 `service.rollback(root, transaction_id)` 按逆执行顺序撤销所选文档事务，
检查实际返回状态；用户修改/身份漂移会阻止破坏性回滚。回滚后保留 Scheduler 历史。
丢失终态的运行必须先检查文档事务证据，再使用既有恢复流程；不自动重新执行。

## 与 stage28 的对齐范围

只读核对 `checkpoint/stage28-20261002` 的
`9c05a5a3ef3552271b07197f6392fa7c33cb6b36`：

- `opencoding/product_loop.py` 的 `_file_evidence`、`_execute_task`、`_continue`
  已有输出哈希、未提供执行器阻断、依赖推进和未知终态停止契约。本适配沿用这些语义，
  复用 work 的 Scheduler 存储与事务实现，不另建 product-loop ledger。
- `tests/test_product_loop.py` 的输出证据/外部边界案例，以及
  `tests/test_agent_product_loop_bridge.py` 的缺执行器、失败和回滚案例，
  在新 `tests/test_taskplan_scheduler.py` 中按 work 的确认收据契约适配验证。
- 不整合旧分支的 adoption、facts、grants、autorun、通用 Agent/Python/Node bridge、
  Host、provider 或产品功能；旧分支 POSIX snapshot 锁实现也不在本批移植。
- work 的确认收据绑定较旧分支更严格，保留当前实现。旧分支整体迁移仍是单独事务，
  本批不宣称其所有成果已合入或所有历史测试已通过。

## 验证

```bash
python -X utf8 -m unittest tests.test_taskplan_cli -v
python -X utf8 -m unittest tests.test_taskplan_scheduler -v
python -X utf8 -m unittest
python -X utf8 -m unittest tests.test_product_packaging
```

新增 CLI 的 Windows 验证仍待执行；此处的命令和使用示例不是平台测试通过证据。
本适配的文档事务测试可在 Linux 执行。work 既有已初始化 Scheduler 零写入快照依赖
Windows 锁原语，Linux 会返回 `unsupported_platform`；未初始化状态查询仍可零写入。
完整测试还要求 cargo。不得把这些环境限制下的全量测试称为全绿。
