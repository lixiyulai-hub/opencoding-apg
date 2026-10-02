# OpenCoding Stage22 delivery

Stage22 完成失败可观测性与用户验收边界：

- 新增 `opencoding.acceptance_report` 与 `scripts/report_acceptance.py`，输出 `observed/unverified/blocked` 三态、可读原因、错误码映射和人工 Gate 请求。
- `LocalAgentAdapter` 在写入前校验工具链观察的 schema、provenance、目标、命令状态和无网络/无 shell 约束；错误记录不会消费授权或创建文件。
- Stage21 三项目矩阵重跑，失败观察、重新授权修复、越权拒绝、事务回滚和 skill verifier 均保留。

本阶段仍只在 Linux 离线运行。Python/Node runtime 的 observed 仅限 profile scope；Windows、完整目标执行、浏览器、managed loader、model/provider、外部服务和部署没有验收。未加入 APG/Rust/frontend/cargo 资产，阶段 ZIP 仅内部保存。
