# OpenCoding Stage11 delivery

Stage11 从 Stage10 单包恢复，验证 Linux 离线 skill/Agent 主链：归档恢复、项目 skill 发现、临时 Codex home 安装、入口加载、真实文件生成、测试失败修复、通过和事务回滚。

- Stage10 ZIP SHA256 `a4b680a5936db596a6b469bc56b11e4c6da135b8fbd442c3d8683fc21e0189a2`、872714 bytes；恢复 verifier 与 skill verifier 通过。
- `format_valid=true`、`resource_discovered=true`、`host_adapter_loaded=true`，声明入口被实际执行；`codex_managed_loader_observed=null`。
- Linux core focused 125/125；E2E 使用 synthetic input/confirmation，model=false、external_actions=false，真实写文件、故意失败→修复→通过、回滚 residual=[]，授权绑定完整。
- 初始 runner 根目录不匹配时 fail-closed，修正为显式 `source/` 根后通过；该边界保留为证据，不计入成功。
- 未使用 provider、密钥、网络、部署、远程 Git、合并、Release，也未补造历史 receipt、时间线、用户确认或旧 APG fixture 结果。
