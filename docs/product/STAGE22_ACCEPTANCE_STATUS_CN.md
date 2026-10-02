# Stage22：失败可观测性与人工 Gate 验收报告

Stage22 新增 `opencoding-acceptance-report-v1`。它把能力契约、工具链探测和适配器错误统一成三态：

- `observed`：指定范围内的本地证据已经成功；
- `unverified`：环境或目标执行证据不足，不能外推；
- `blocked`：动作需要人工 Gate，当前流程应停止。

报告中的每条检查都有可读原因；`blocked` 条目必须带 `human_gate.required=true` 和下一步请求。当前 provider/model、外部服务、部署和真实用户确认均是 blocked；managed loader、目标执行和 Windows 仍是 unverified。Python 与 Node/npm 只有固定 runtime profile 的观测，不升级成完整 CLI/Web 产品验收。

适配器错误码和用户提示保持同一份映射。工具链观察 schema/provenance/目标/命令证据不一致时，在消费授权和写入前返回 `toolchain_observation_invalid`；外部动作、过期授权、重放授权和绑定漂移继续 fail closed，并保留零副作用证据。

运行 `scripts/report_acceptance.py` 会同时生成 JSON 和 Markdown。Stage21 的三个非模板项目再次纳入 Stage22 回归，保持真实失败、修复、越权拒绝、回滚和 skill verifier 证据。
