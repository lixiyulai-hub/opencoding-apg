# Stage20：工具链探测边界与多项目失败恢复

Stage20 为 capability matrix 增加了显式本地工具链探测。适配器默认不执行探测；调用方选择固定 profile 后，探测器只用 `shell=False` 运行版本命令，不联网、不读取凭据、不运行项目代码。只有 profile 中每条命令都找到并以退出码 0 完成，结果才是 `observed`；报告的范围仍限于 profile 的 runtime。

当前固定 profile：

- `python-cli-runtime`：当前 Python 解释器；只证明 Python runtime，不证明 CLI 打包或框架。
- `node-web-runtime`：Node/npm；只证明 Node/npm runtime，不证明浏览器、bundler、框架或部署。
- `rust-cli-runtime`：rustc/cargo；缺少任一命令则保持 `unverified`。
- `windows-dotnet-runtime`：仅允许 Windows host family；Linux 上即使存在同名命令也不标 observed。

`check_agent_adapter.py --probe-toolchain PROFILE` 会把探测结果附到 capability matrix。没有 profile 时仍保持 `toolchain.status=unverified`。目标标签被识别也不会自动升级工具链状态。

## 两个非模板项目

`scripts/run_stage20_multi_project.py` 运行两个不同的合成项目：

1. `recipe-index-cli` 使用 CLI/Python runtime profile，测试标签分组逻辑；
2. `stock-delta-web` 使用 Web/Node runtime profile，但测试仍明确是 Python unittest，不冒充浏览器执行。

两个项目都真实写入项目文件，先观察故意缺陷导致的测试失败，再以新的授权修复并通过；篡改 root 的授权在写入前被拒绝；事务回滚探针最终 `rolled_back` 且无残留。报告仍只证明这两个离线夹具，不证明通用 Agent 能力、浏览器、Windows、部署、managed loader 或 provider。
