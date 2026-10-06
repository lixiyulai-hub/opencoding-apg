# TaskPlan 内存证据投影 API

`opencoding.taskplan_evidence.project_taskplan_evidence` 检查调用方已持有的
原预览、Scheduler 快照、运行收据及捕获字节之间的内部一致性。它是纯内存
API，没有 CLI，也不读取项目、数据库、事务目录或当前文件。

本版本没有 `observed` 路径。所有 `document` / `offline_design` 任务始终为
`unverified`，并包含 `strict_transaction_validator_unavailable`。现有
`transactions._load_receipt_manifest` 将严格事务结构、preimage 库存、安全路径
与磁盘读取交织在一起；在不修改核心的本次范围内，无法完整复用为纯验证器。
不能把部分检查、匹配的 SHA256 或调用方的肯定性标记当成完整事务验证。

## 调用契约

```python
from opencoding.taskplan_evidence import project_taskplan_evidence

report = project_taskplan_evidence(
    expected_root=retained_root,
    expected_preview_digest=retained_preview_digest,
    original_preview=retained_approval_preview,
    scheduler_snapshot=captured_scheduler_snapshot,
    transaction_evidence=captured_transactions,
    file_evidence=captured_files,
)
```

六个参数都是必填的关键字参数。`expected_root` 和
`expected_preview_digest` 必须来自调用方此前独立保留的执行上下文。
`original_preview` 是当时的 `approval.preview`；执行后重新调用
`preview_task_plan` 会因文件 before hash、diff 等改变而得到不同预览，不能
用于重建原始凭据。函数不自动补齐预期摘要，也无法认证调用方是否真正独立
保存了这些值。摘要提供完整性绑定，不是签名或人类授权证明。

根目录仅按绝对路径字符串做词法检查与精确匹配，不调用 `resolve`、`stat`
或 `exists`，也不归一化为另一条等价路径。输入仅支持普通内置
`dict` / `list` 和有限 JSON 值；`bytes` 仅允许出现在下述捕获字段。
不支持自定义 `Mapping`、迭代器或其他带行为对象。调用期间由调用方负责
输入稳定性；函数不提供跨线程快照或操作系统原子性。

六个输入共用资源预算：最多 100,000 个节点、16 MiB 字符串 UTF-8 与 bytes
负载；嵌套深度最多 64，单个容器最多 4,096 项，原 TaskPlan 最多 256 个任务。
整数须处于有符号 64 位范围，浮点数须有限。超限明确拒绝，不截断后继续认定
匹配。资源检查是应用层输入约束，不是操作系统隔离或并发内存保证。

### 原预览和 Scheduler 快照

原预览使用现有 `preview_task_plan` 的完整结构。函数核对独立预期摘要、
嵌套服务与文件计划摘要、文档 UTF-8 内容哈希及任务派生映射。它只复用已有
纯计算 helper，不调用当前预览、执行、审批、恢复、锁或 Scheduler 构造入口。

Scheduler 输入是完整快照的内存值；只包含部分任务的筛选结果不能替代原图。
任务定义必须与原映射一致，运行须由 `last_run_id` 唯一选中，并绑定同一
任务、attempt、input/action digest 和收据自摘要。运行与收据的共同字段
必须一致。`started_at` / `finished_at` 位于 run 中，现有 receipt 不含这两个字段。
原状态被保留，不从依赖关系生成新的 `frozen` 或成功状态。
若未就绪原计划却出现执行尝试，或缺失 Host 的父任务伪称成功后其后继也
声称成功，报告会指出不一致并停止对应字节匹配资格。负向诊断不修改原状态，
也不把仅缺少事务捕获、或统一严格验证缺口传播为已发生的执行失败。

本 API 不调用 `execution_status`，也不绕过其平台限制读取 SQLite。
获取快照与捕获其他证据由独立的后续采集层负责。纯内存测试不证明这些采集
步骤已经实现或在 Windows 上通过。

### 事务捕获

`transaction_evidence` 是以 transaction ID 为键的普通字典，每项精确包含：

```python
{
    "root": retained_root,
    "transaction_id": transaction_id,
    "raw_files": {"receipt.json": receipt_bytes},
    "acquisition_error": None,
}
```

外层键必须等于 `transaction_id`，`root` 必须与独立预期根精确匹配。
`raw_files` 的值为 `bytes`，键为描述性相对名，绝不转成磁盘读取路径。
事务 ID 由匹配的 Scheduler artifacts 关联；不会跟随 stdout 中的
`receipt_path`。本版本不解析 raw receipt、manifest、events 或 preimage，
不判定 applied/rollback 终态，也不宣称已验证事件链、preimage 库存或文件身份。
损坏、缺失或看似完整的事务字节都不能产生 `observed`。

### 文件捕获

`file_evidence` 以原计划规范相对路径为键，每项精确包含：

```python
{
    "root": retained_root,
    "path": relative_target,
    "content_bytes": captured_bytes,  # bytes 或 None
    "acquisition_error": None,
}
```

外层键必须等于 `path`，路径须属于原计划目标，`root` 必须与独立预期根精确匹配。
无捕获错误时，只对输入 bytes 重算 SHA256。匹配只表示这些输入字节与原计划
预期一致，不证明字节来自当前磁盘、安全普通文件或同一文件对象。采集错误
仅作为负向信息，输出固定原因码，不回显原错误内容。不接受 `verified`、
`current`、`trusted_validated` 等额外字段作为安全或成功的捷径。

## 返回语义和隐私

`binding.status == "matched"` 只表示原预览与独立预期及纯派生一致，不表示
Scheduler、事务或当前磁盘真实可信。原绑定拒绝时不投影可信任务；快照或
收据错误另列原因，不能因原绑定匹配而消失。`byte_match` 也不改变任务的
`unverified` 状态。

`host_missing` 来自原图中的对应任务，不从空图臆造。存在实现/验证任务时，
它们仍被 Host 缺失阻断；不把历史 `succeeded`、`failed`、`frozen` 或文件
存在推断成真实实现/测试完成。真实 Host、managed loader 和外部服务均未验证。
输出保持 `activation_status="not_activated"` 和 `live_verified=False`，没有
整图成功、实时就绪或里程碑百分比。

输出不包含文档正文、原始捕获 bytes、preimage、stdout/stderr 或捕获错误文本。
公开根标识使用摘要，避免回显私有绝对根目录。任务 ID、允许的相对输出路径
与摘要用于对照输入；它们也可能构成项目元数据，调用方仍负责交付范围。

## 验证与后续边界

本模块的反例验收应覆盖绑定篡改、运行/收据交叉归属、状态与 artifacts 矛盾、
输入字节漂移、恶意捕获包装、资源限制、确定性、输入保护和禁止副作用入口。
测试命令：

```text
python -B -X utf8 -m unittest tests.test_taskplan_evidence -v
python -B -X utf8 -m unittest tests.test_taskplan_scheduler -v
python -B -X utf8 -m unittest tests.test_product_packaging -v
```

运行结果必须绑定实际提交和平台，不能借用其他分支的 Windows 通过结果。
新模块的 Windows 验证、已安装 API 调用、真实只读采集和完整事务观测均需各自
证据。未来若要提供 `observed`，应独立抽取并复用完整严格事务纯验证器，定义
可信采集契约，再验证路径/身份/链接、读取期间变化、回滚与事件链。
两次相同捕获也不等于全局原子快照或持续当前真实性。
