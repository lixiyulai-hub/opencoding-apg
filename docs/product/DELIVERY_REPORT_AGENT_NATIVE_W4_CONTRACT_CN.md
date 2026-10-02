# W4 Host/connector 离线 contract 阶段报告

更新日期：2026-10-02。事务：`agent-native-w4-offline-contract`。本阶段只实现本地可验证的 contract、权限边界和合成夹具，不连接真实 Host、Provider、网络、凭据或外部服务。

## 缺口审计

现有 `CodexSkillHost` 能在项目、显式 Codex home 或安装包资源中发现并加载 `LocalAgentAdapter`，并准确报告 `codex_managed_loader_observed=null`。它没有可观察的托管 Host 回执，也没有统一表达服务器、数据库、API、登录、支付、通知、后台、存储和模型/provider 的外部权限状态。M09/M10 的“真实适配器”和 live verification 仍是后续人工 Gate。

## 最小离线骨架

新增 `opencoding.host_connector`：

- `OfflineHostContract.inspect()` 只观察本地 adapter 和资源来源，返回 managed loader 未观测、`provider_used=false`、`transport_implemented=false`、`credentials_read=false`，不会写项目或 Codex home。
- `check_permission()` 将 `skill_discover/skill_load` 标为只读可用，将 reviewed local write/test 标为仍需既有本地授权，把 provider/network/credentials/remote Git/deployment/publication 统一标为 `blocked_human_gate`。查询结果不等同授权，不能直接传给 `LocalAgentAdapter`。
- `ConnectorContract` 和 `OfflineConnectorRegistry` 仅保存固定、有限、无动态导入的声明；覆盖 M10 八类能力和 provider。外部 connector request 始终 `blocked_human_gate`，无 transport、无凭据、无执行。
- `FixtureConnector` 只接受有界 JSON 的内存响应，要求显式 `synthetic=true` 和当前 preview digest；返回 `simulated`、`live_verified=false`、`external_executed=false`。fixture 不是服务成功、不是模型调用、不是人工确认。

## 测试与证据

`tests/test_host_connector_contract.py` 的 11 项夹具覆盖：本地 Host 观察、权限未授权写入拒绝、六类外部边界 Gate、九类 connector 声明与操作、无 transport/process 调用哨兵、fixture 漂移/重放/失败/敏感数据/大小限制、部分 Codex home 资源 fail-closed 和回调后无文件写入。

测试证明的是离线 contract 行为。它不证明真实 provider、真实 connector 服务、托管 Host 自动加载、网络安全、沙箱或目标平台执行。W4 改动后的全量原始计数为 `Ran 675 tests in 160.575s`、`OK (skipped=9)`：675 总数、666 通过、9 跳过、0 失败、0 错误，返回码 0；日志位于 `/workspace/stage28-validation-20261002/w4-offline-full/`。

## 风险、回滚和 Gate

contract schema 与真实服务协议尚未绑定，不能拿作 API 兼容承诺。回滚使用 `git revert` 本阶段提交；不会触碰用户项目或外部服务。唯一人工 Gate 是未来要启用具体 Host/connector 时，确认精确 provider、账号/凭据、目标、数据、费用、网络动作和 action digest；在该 Gate 前，所有外部状态保持 blocked/unverified。
