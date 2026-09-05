# APG Gate Failure Matrix

| 场景 | 本地输入 | 预期状态 | blocker code | 外部动作 |
|---|---|---|---|---|
| 完整预览 | `release_approval=true`, `rollback_evidence=true` | `ready-for-preview` | `[]` | 不执行 |
| 缺少审批 | `release_approval` 缺失或非 `true` | `BLOCK` | `missing_release_approval` | 不执行 |
| 缺少回滚证据 | `rollback_evidence` 缺失或非 `true` | `BLOCK` | `missing_rollback_evidence` | 不执行 |
| provider/网络需求 | `provider_required=true` 或 `network_required=true` | `BLOCK` | 对应字段名 | 不执行 |
| 真实数据需求 | `real_data_required=true` | `BLOCK` | `real_data_required` | 不执行 |
| 凭据/运行时/部署/发布/试点需求 | 任一对应字段为 `true` | `BLOCK` | 对应字段名 | 不执行 |

## 通过条件

只有第一行可进入部署预览阶段，且它仍然是 preview：发布、部署、publication 和 provider action 的执行标志必须为 `false`。任何真实环境交易都需要新的、独立的 owner transaction 和后续 Gate。
