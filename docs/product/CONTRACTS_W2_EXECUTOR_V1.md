# OpenCoding W2-B 持久调度与本地执行器契约 V1

日期：2026-09-06。实现目标为 Python 3.11+；本契约 schema_version 固定
为 "1.0"。这是 W2-B 的受控本地执行契约，不授权 Host、Provider、网络、
凭据、真实生产数据、部署或发布。

## 目标与边界

W2-B 交付一个可恢复的本地任务循环：

- 用 SQLite 保存任务、依赖、状态迁移、attempt 和 run receipt；
- 只接受经过结构校验的本地动作，不拼接 shell，不使用 `shell=True`；
- 支持允许动作的真实本地运行、取消、超时、崩溃恢复、失败冻结、
  限次重试和幂等 Requeue；
- 对每次运行记录完整输入摘要、action 摘要、attempt、退出码、超时/取消
  标志、脱敏输出摘要和产物哈希；
- 所有路径都相对于调用方显式提供的 root，运行 cwd 固定为该 root；
- 本地合成 fixture 可以真实运行并写入 fixture 目录，但不得启动外部服务
  或提交真实任务。

本契约不把应用级路径检查称为操作系统沙箱。对拥有本机权限的其他进程的
恶意 TOCTOU 隔离仍是明确限制。

## 代码所有权

本轮 Terra High worker 只拥有以下路径：

- `opencoding/executor.py`
- `opencoding/scheduler.py`
- `opencoding/scheduler_migrations.py`
- `tests/test_product_executor.py`
- `tests/test_product_scheduler.py`

协调者拥有本契约、治理证据和后续共享入口集成。worker 不得修改
`opencoding/__init__.py`、`opencoding/service.py`、现有 W1/W2 Entry 文件、
共享契约、README、AGENTS 或其他 worker 的路径。

## 结构化 Action

Action 是严格 JSON 对象，不允许未知字段、shell 字段、任意 command 字符串
或 `execute` 字段。允许的最小动作类型如下：

```json
{
  "type": "write_text",
  "path": "artifacts/fixture/result.txt",
  "content": "合成结果"
}
```

```json
{
  "type": "python_module",
  "module": "fixture_task",
  "args": ["--value", "ok"]
}
```

`write_text` 只允许 root 内的新建/更新普通文件；`python_module` 只能通过
`[sys.executable, "-m", module, *args]`、`shell=False`、明确 root cwd 执行。
模块名和参数必须通过严格类型与长度校验。不得通过参数传递秘密或把秘密写入
数据库、receipt、异常或输出摘要。

Executor 接收一个已核验的 ActionContext，至少绑定：

- `schema_version`
- 显式 `root`
- `action_digest`
- 精确 targets
- `external=false`
- `cost_limit=0`
- 合成/本地 `data_scope`
- `irreversible=false`

执行结果必须是可 JSON 序列化对象，至少包含 `run_id`、`status`、`exit_code`、
`timed_out`、`cancelled`、`duration_ms`、`input_sha256`、`action_digest`、
脱敏且有界的 stdout/stderr 摘要和产物哈希。模拟结果不得标记
`live_verified`。

## Task 与 Scheduler

Task 至少包含以下字段：

- `task_id`：稳定 ASCII 标识；
- `input`：可 JSON 序列化的完整任务输入；
- `action`：上述结构化 Action；
- `depends_on`：稳定、无重复的 task id 列表；
- `max_attempts`：正整数，默认 2；
- `timeout_seconds`：正数且有上限；
- `idempotency_key`：用于重复 enqueue/requeue 的稳定键。

SQLite 数据库固定在
`.opencoding/scheduler/state.sqlite3`。数据库必须使用显式、可核验的迁移
版本，不得静默改变旧表结构。任务状态至少包括：

`queued`、`running`、`succeeded`、`failed`、`frozen`、`cancelled`、
`timed_out`。

基本状态规则：

1. 依赖未成功时，任务不能被派发；
2. 首次不可恢复失败会冻结该任务的依赖后继，并保留失败 receipt；
3. `requeue` 必须绑定 task_id、idempotency_key 和当前状态，重复调用返回
   同一逻辑结果，不得制造重复任务或绕过 `max_attempts`；
4. attempt 达到 `max_attempts` 后不得继续重试；
5. 发现进程遗留的 `running` 任务时，打开 scheduler 必须把它们转为可审计
   的恢复状态，重新排队或冻结，不能伪造成功；
6. cancel 和 timeout 必须记录最终状态与原因，并使后续状态迁移幂等；
7. 任务、依赖和 run receipt 的更新必须在 SQLite 事务中完成。

Worker 可以提供类作为内部协调器，但公开业务结果必须是 dict/list/string/bool/
null 等可 JSON 序列化值。推荐的受控函数边界为：

- `enqueue(root, task) -> dict`
- `run_next(root, ...) -> dict`
- `cancel(root, task_id) -> dict`
- `requeue(root, task_id, idempotency_key) -> dict`
- `recover(root) -> dict`
- `get_task(root, task_id) -> dict`
- `list_runs(root, task_id=None) -> list`

函数名可以按既有代码风格调整，但语义、根目录显式性和状态规则不得削弱。

## Receipt 与验证

每次实际 run 必须绑定唯一 `run_id`，并记录：

- `task_id`、`attempt`、`input_sha256`、`action_digest`；
- started/finished 时间、状态、exit code、timeout、cancel；
- 脱敏且有界的 stdout/stderr 摘要；
- 产物相对路径及精确 SHA256；
- 崩溃恢复或取消原因；
- receipt 自身摘要。

测试必须覆盖：

- 允许动作真实本地执行和禁止 shell/未知动作；
- SQLite 初始迁移与重复打开；
- 依赖排序和失败后冻结；
- 取消、超时和真实非零退出码；
- 崩溃恢复；
- 限次重试和重复 Requeue 幂等性；
- 输入/action/receipt 字段完整性、脱敏和产物哈希；
- 显式 root、路径穿越、链接/不安全目标和无外部动作。

所有测试使用合成 fixture、临时显式 root 和本地 Python 标准库，不联网、
不安装依赖、不启动外部服务。

## 回滚与恢复

本轮源码交付在独立 `w2-executor` worktree 中完成。协调者只从当前集成
HEAD 创建该 worktree，并保存 exact preimage、absent manifest、worker commit、
测试输出、diff 范围和 postimage 哈希。

失败时立即冻结候选实现，保留所有 receipt、数据库 fixture 和日志；不得
reset、clean、删除历史证据或盲目恢复。集成前必须独立复核代码所有权、测试、
哈希、diff 和 `services/domain/target/` 保留状态。
