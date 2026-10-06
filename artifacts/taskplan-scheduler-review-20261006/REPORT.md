# 独立审查、期限修复与验证交付

本报告接续 afc1614e 的首次本地交付，替代其“尚未独立审查 / cargo 缺失”状态。
历史证据目录保持原样，不把新测试结果写入旧日志。

## 精确范围

- 原始基线：`5d68c2075620df0be005c76f904b256b980d303e`。
- 独立审查候选：`afc1614e3106b8d72adda35d92f8b70dc570c2a4`。
- 本地分支：`local/taskplan-scheduler-offline`；本次修复在此分支继续。
- 独立审查者：只读子代理 `/root/independent_review`，经用户明确要求启动。
  审查完整 15 文件差异，并向下检查路径、事务和 Scheduler 实现；未修改仓库。
- `reviewed-files.txt` 列出原审查差异；`delivery-files.txt` 列出最终累计交付文件。
  新的精确提交 SHA 由最终交付回复及仓库 HEAD 提供。

## 独立发现及修复

**P1（已修复）：审批期限未绑定调用方确认。** 原 candidate 中 `expires_at` 独立存在于
approval，execute 仅检查其是否在未来。独立探针把期限改为 2999 年，保留原
`authorization_context`，实际执行了 9 个成功文档任务。此问题是授权期限完整性缺口，
不是身份认证绕过；原接口本来就是可信调用方声明。

修复增加 `build_task_plan_confirmation`，把既有 service receipt 与精确 expires_at
包成专用完整上下文。`approve_task_plan` 只复制该上下文，不产生新期限；
`execute_task_plan` 必须收到调用方独立保存的相同完整上下文。旧顶层期限、普通
service 收据、篡改期限、超出一小时的期限及过期上下文均拒绝。旧上下文不能通过
重新 approve 获得续期；必须由调用方重新取得确认。

独立修复探针使用真实墙钟等待，而非作者测试中的模拟 clock，证实：

- 延长期限但保留原外部上下文：`human_confirmation_scope_mismatch`，无文件变更。
- 旧版顶层 expires_at：`approval_fields_invalid`，无文件变更。
- 审批与上下文同时改成 2999 年：`approval_expiry_invalid`，无文件变更。
- 到期后 execute、重新 approve 原确认：均 `approval_expired`，无文件变更。
- 有效原确认仍成功生成 9 个文档任务，因 host_missing 整体 blocked，无源码目录。

另一个独立探针在审批后将 PRG.md 替换为外部哨兵文件的符号链接，执行 blocked，
对应任务失败，外部哨兵保持原内容。源码白名单只映射 document / host_missing，
依赖顺序、幂等、max_attempts=1、失败递归冻结、事务 preimage/hash/path 检查及逐
事务回滚均经下层代码审查。未发现第二项确定缺陷；独立修复复审关闭 P1。

完整上下文是可信调用方的本地授权声明，不是签名或身份认证；调用方不能从待验证的
approval 反取授权上下文。raw Scheduler 仍是单独的受信低层入口，协作锁不隔离
恶意同用户进程。Windows/reparse 行为尚未完成新代码实测。

独立探针脚本、原始输出、原审查记录及修复所审文件哈希保存在本目录的 `independent/` 子目录。
父任务逐一核对这些哈希与提交前工作树一致后才形成交付提交。

## 实际公开入口

入口仅为 `opencoding.taskplan_scheduler` 的 Python API，没有新增 CLI 子命令。

```python
from opencoding.taskplan_scheduler import (
    preview_task_plan, build_task_plan_confirmation,
    approve_task_plan, execute_task_plan,
)

preview = preview_task_plan(root, session_id)
# 调用方先展示 targets、diff、tasks、effects，取得对该范围和期限的确认。
context = build_task_plan_confirmation(
    preview, statement=confirmed_statement, actor=caller_id, expires_in_seconds=300,
)
# 完整 context 由调用方独立保存，不能从待验证 approval 反取。
approval = approve_task_plan(preview, confirmation=context)
result = execute_task_plan(root, approval, authorization_context=context)
```

root 必须是已经授权且存在的绝对项目路径，session_id 来自已有澄清会话；
示例中的 confirmed_statement / caller_id 为真实调用方记录，不默认制造人工确认。
前三步不写盘；execute 会使用既有会话锁及 Scheduler/文档事务存储。
实现/验证任务仍 host_missing，integration_design 仍未激活。部分失败保留现场和
rollback_ref；继续使用 `service.rollback(root, transaction_id)`，不另建回滚机制。

## 官方 cargo 工具链

环境没有预装 cargo/rustc/rustup。根据 Rust 官方文档，下载官方 rustup-init 及同源
SHA-256 校验文件，`sha256sum -c` 通过，然后在临时目录安装 stable minimal profile，
使用 `--no-modify-path`。未全局安装、未修改 shell 配置、未使用不明来源软件。

- rustc `1.99.0 (b940084d7 2026-09-28)`。
- cargo `1.99.0 (5f94df478 2026-08-27)`。
- `CARGO_HOME`、`RUSTUP_HOME`、`CARGO_TARGET_DIR` 均为任务专用临时目录。
- 执行测试时 `CARGO_NET_OFFLINE=true`；domain crate 没有第三方依赖。
- `cargo test --offline --manifest-path services/domain/Cargo.toml`：4 passed。

来源：[Rust 安装](https://rust-lang.org/tools/install/)；
[官方自定义安装目录说明](https://rust-lang.github.io/rustup/installation/index.html)。
原始安装输出、校验文件和 cargo 输出均在本目录。

## 测试原始摘要

| 日志 | 原始摘要 | 退出状态 |
| --- | --- | --- |
| adapter.log | Ran 19 tests in 3.165s / OK | 0 |
| targeted.log | Ran 97 tests in 6.037s / OK | 0 |
| packaging.log | Ran 6 tests in 3.082s / OK | 0 |
| cargo.log | 4 passed; 0 failed; 0 ignored | 0 |
| baseline-cargo.log | Ran 228 tests in 11.700s / FAILED (failures=14, errors=4) | 1 |
| full-cargo.log | Ran 247 tests in 14.677s / FAILED (failures=14, errors=4) | 1 |

19 项新适配测试包含在 97 项关联回归及 247 项全量内，不把重叠测试累计成独立覆盖数。
`verification.json` 记录完整命令、环境和日志哈希。代码/测试/文档的 whitespace
检查及 compileall 通过；全 staged whitespace 检查仅指出 cargo 原始输出末尾空行和
rustup 官方安装输出行末空格，保留原始日志而未改写。

`baseline-failure-comparison.json` 提供逐项对照：原交付无 cargo 的 19 个错误/失败
标题完全一致；本次同一官方工具链下，基线与修复候选的 18 个错误/失败标题及顺序
完全一致，新增集合为空。cargo 缺失的那一个 error 已消除。仍有 Linux 不支持的
Windows Scheduler 快照相关测试/CLI 状态测试；没有改写或跳过以制造通过。
**本批不标整体通过，新代码 Windows 验证仍待补。**

## 边界与接续

只有本地提交和补丁，不 push/merge/release；未触碰被拒的公开路径清理。
未接入运行期网络、Host、支付、真实凭据或部署。工具链下载仅用于明确要求的测试
环境修复。旧 checkpoint 分叉未整体合并，其他历史功能仍独立待整合。

## Status Snapshot

阶段：report。独立审查、P1 修复及独立复审、官方 cargo 补齐和本地回归完成。
总进度/阶段进度：not-computable，缺少认可的项目路线图分母。
交付：本地修复分支和原始证据；当前无人类 Gate，后续远端交付边界保持关闭。
下一自动工作：父任务核对精确 HEAD 和证据；不自动发布。
阻塞：新代码 Windows 验证待补；独立复审无未解决确定缺陷。
Continuation：以最终回复中的精确新 HEAD 继续，在 Windows 环境补跑同一测试入口；
只有新增明确授权后才执行 push/merge/release。公开路径清理继续独立待处理。
