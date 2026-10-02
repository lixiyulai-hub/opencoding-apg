# Stage26：目标平台适配器契约与 fail-closed 观察

Stage26 为 Windows、macOS、Web 三个规划目标增加统一的目标适配器接口：

- `opencoding.target_adapters:get_target_adapter` 返回对应契约适配器；别名会先规范化为 `windows`、`macos`、`web`。
- `status_report()` 分开报告宿主、目标标签、工具链观察和目标执行状态。
- 工具链版本探测即使为 `observed`，也不会把目标执行升级为 `observed`。浏览器、SDK、Xcode/.NET、打包和部署仍需目标专用执行器。
- 当前契约适配器的 `execute()` 对未观测或错误观察一律 fail-closed，分别返回 `target_execution_unverified` 或 `target_capability_blocked`，不调用 shell、浏览器、SDK、模型、网络或外部服务。
- `LocalAgentAdapter.capabilities()` 和每个执行回执的 `capability_observation.target_adapter` 暴露该边界。

Linux contract tests 对三个目标均得到 `execution.status=unverified`、`observed=false`，且没有目标动作副作用。当前环境的 managed loader 仍是 `null/unobserved`，本地 Python 进程仍是同用户边界且 `sandbox=false`。这些测试是接口/拒绝路径验证，不是 Windows、macOS 或浏览器真实运行时验收。
