# OpenCoding Stage23 delivery

Stage23 完成验收状态的恢复与阻断一致性：

- 新增 `opencoding.acceptance_state`，用报告摘要和 status digest 固化 `observed/unverified/blocked`；重启和重复 resume 保持状态不漂移，报告变化会被拒绝。
- `require_observed` 在适配器授权前执行。blocked provider/model 与 unverified Windows execution 均验证了无 claim、无目标文件；observed 本地动作正常执行。
- 三项目矩阵继续通过真实失败观察、重新授权修复、越权拒绝和事务回滚；skill verifier 与 144 项核心回归保持通过。

本阶段仍是 Linux-first 离线验证。Windows、目标执行、managed loader、model/provider、外部服务和部署未验证或被 Gate 阻断；未添加 APG/Rust/frontend/cargo 资产，阶段 ZIP 仅内部保存。
