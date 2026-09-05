# APG 项目级自适应 Git 检查点 Ledger 契约

确认日期：2026-09-02  
范围：APG-only、中文优先、离线 checkpoint ledger projection。

## 目的

Ledger 将跨 PRG 循环的已验证检查点整理为一个稳定的项目级记录。当前阶段输出目标文件和候选快照，不把数据写入磁盘，也不运行 Git。

未来持久化目标：`.governance/progress/apg-adaptive-git-ledger.json`。

## 记录格式

每条记录包含：`event_id`、`event_type=adaptive-git-preview`，以及 `status`、`success_node`、`checkpoint_id`、`revert_point`、`resume_condition`。

只有 `CHECKPOINT_RECOMMENDED` 能成为新的成功检查点记录：

- 同一 `event_id` 或 `checkpoint_id` 已存在：`REUSE_EXISTING`；
- `FREEZE` 与 `WAIT`：`NO_CHECKPOINT_APPEND`；
- 新的验证成功节点：`APPEND_CANDIDATE`。

## 输出规则

`apg_adaptive_git_ledger_preview.py` 返回：

- `target_path`：未来项目级 ledger 位置；
- `write_action=PREVIEW_ONLY`：本阶段不写入；
- `append_candidate`、`deduplicated`、`last_checkpoint`；
- 完整 `snapshot`、`snapshot_digest`、`replay_digest`。

Controller 将输入中的 `checkpoint_ledger_events` 与本轮 `ledger_event` 交给该适配器，并把结果放在 `checkpoint_ledger` 字段。

## 离线边界

本契约不会改动 `.governance/progress`、Git 仓库、分支、提交、远端、网络、Provider、Host、runtime、部署或发布。所有 external actions 均为 `false`。
