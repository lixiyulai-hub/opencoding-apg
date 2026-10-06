# 受限离线调度本地交付

## 精确基线与旧分支核对

- 远端 PR #1：open、draft，head=work，精确提交
  `5d68c2075620df0be005c76f904b256b980d303e`。
- 初始环境虽名为 work，实际 HEAD 是旧 main
  `91a20392a856ff4717d230467d32fc154bcb82b1`；未在此旧基线上开发。
- 从核实后的远端 work 创建本地 `local/taskplan-scheduler-offline`。
- checkpoint/stage28-20261002 可读，精确提交
  `9c05a5a3ef3552271b07197f6392fa7c33cb6b36`。
  work 相对 checkpoint ahead=8、behind=26，共同祖先为上述 main。
- 只读检查双方 AGENTS.md，以及 checkpoint 的 `.agents/skills/opencoding/SKILL.md`。
  当前 work 没有该 .agents skill 目录；未安装旧分支 skill 或运行其 Host/安装流程。
- 基线 CI run 37286657441 的 5 jobs 均 success；这不是新代码的 Windows 测试证据。

## 实现差异

新增 `opencoding/taskplan_scheduler.py`：只读预览、精确调用方确认/到期审批、
当前会话与计划复核、稳定幂等键、按依赖入队/执行、已完成输出哈希复核。
未归属任务的已预览上下文文档成为显式前置节点，避免漏写或隐式副作用。

Executor 仅增加 Markdown `document` 事务动作和无执行内容的 `host_missing` 动作；
Scheduler 仅使用共同 targets 计算函数以支持多文档动作。文档事务沿用现有
apply_changes / rollback_changes，不重建回滚系统。每个节点最多一次尝试；
失败/缺 Host 冻结传递后继，部分失败保留证据，不盲目重试。

任务 evidence 绑定 Scheduler receipt、文件哈希和 transaction_id；失败事务 JSON
同样保留在 stdout_summary。implement_feature / verify_feature 未接执行者，
不会生成假源码或假测试成功。integration_design 保持 offline_design、not_activated。

旧 checkpoint 的 product_loop 已有输出证据、缺执行者阻断和未知终态停止语义，
本批对齐这些契约并移植相应回归场景到 work 的严格确认 API；没有整合其大型
product_loop/adoption/Agent/Host 依赖。具体源函数和测试对照见
`docs/product/TASKPLAN_SCHEDULER.md`。旧分支整体迁移仍未完成，也未被本批覆盖。

## 实际验证

| 范围 | 结果 | 证据 |
| --- | --- | --- |
| 新增适配测试 | 15 passed | adapter.log |
| 关联回归 | 93 passed | targeted.log |
| 独立 packaging | 6 passed | packaging.log |
| 未修改基线全量 | 228 tests，14 failures，5 errors | baseline.log |
| 修改后全量 | 243 tests，14 failures，5 errors | full.log |
| compileall / diff whitespace | exit 0 | verification.json |

全量失败/错误案例逐项相同，新增案例集合为空。现有已初始化 Scheduler 快照
需要 Windows 锁原语，Linux 对应测试与 CLI 状态测试失败；另有 cargo 缺失。
没有跳过或改写这些测试以制造全绿。新代码的 Windows 执行尚未验证。

一次初始关联命令误带入 checkpoint 才有的 test_product_transactions_w2，导致
ImportError；该诊断保留在 initial-targeted-command-error.log。纠正模块列表后
93 项实际存在的 work 回归全部通过。verification.json 保存命令、退出状态、
源文件及原始日志 SHA-256。日志中的临时目录是合成测试工作区，不含个人示例。

## 交付边界与接续

仅本地实现、验证及证据；无 push、merge、release。未执行此前被拦截的公开路径
清理或重试其动作。没有安装全局工具，没有连接运行期网络、支付、凭据或部署。
部分失败不会自动整体回滚，须按返回的文档事务 ID 选择逆序 rollback 并检查状态；
回滚/用户修改后的成功输出会阻止旧审批重放。

作者已完成自查；没有独立审查者结论。接续条件：在本地分支精确交付上复核差异；
需要 Windows 验证时在具备 Windows/cargo 的环境运行同一测试入口。
本批不需要新的用户授权 Gate；远端推送、合并或发布仍需另行明确授权。

## Status Snapshot

阶段：report。本批受限实现及本地验证完成；交付为本地分支和上述证据。
总进度/阶段百分比：not-computable，缺少双方认可的总路线图和阶段分母。
Gate：本地任务已授权，当前无人类 Gate；后续远端交付边界保持关闭。
下一自动工作：向父任务交付精确分支/基线/日志；不执行远端变更。
阻塞：Linux Windows-snapshot 限制、cargo 缺失；独立审查未进行。
Continuation：父任务在本地交付上继续复核，Windows 环境具备后补测；
只有获得明确的新授权才可执行 push/merge/release。公开路径清理继续单独待处理。
