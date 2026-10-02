# OpenCoding Stage21 delivery

Stage21 完成跨平台能力契约和边界回归：

- `opencoding.capability_contract` 生成可重现的 host/target/toolchain/action contract；Python 与 Node/npm runtime 在固定 profile 中观察到，Windows .NET 在 Linux 上保持 `unverified`，所有 target execution 仍为 false。
- 工具链观察带有固定 provenance、目标、命令状态、无 shell/无网络约束；适配器在消费授权前校验观察，错误记录不会写文件或创建 claim。
- 三个不同项目完成真实生成、故意失败、重新授权修复、通过测试、越权授权前置拒绝和事务回滚无残留。

Stage21 只在 Linux 离线环境完成。managed loader、model、provider、external services、浏览器、Windows 和部署未观测。未添加 APG/Rust/frontend/cargo 资产，阶段 ZIP 仅内部保存。
