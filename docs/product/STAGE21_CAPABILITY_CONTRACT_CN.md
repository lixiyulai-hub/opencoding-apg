# Stage21：跨平台能力契约与边界回归

Stage21 将 Stage20 的运行时探测和适配器事实汇总为 `opencoding-capability-contract-v1`。契约包含实际宿主、目标标签、固定 profile 的工具链状态、目标执行状态、动作边界、模型/外部动作、sandbox、进程边界和 managed loader 状态。

在当前 Linux 环境，Python CLI runtime 与 Node/npm Web runtime 的固定版本命令均成功，因此只在各自 scope 内标记 `observed`。Windows .NET profile 要求 Windows host family，当前保持 `unverified`。所有目标的 `target_execution_verified` 都是 false；Web 的 Node/npm 观察不等于浏览器或前端应用运行。

Stage21 还把工具链观察校验前置到授权消费和文件写入之前。缺少 probe provenance、目标不匹配、命令未通过或声称外部动作的观察会 fail closed，并且不会创建本地 authorization claim。

## 三项目回归

`run_stage21_project_matrix.py` 复跑 recipe index CLI、stock delta Web，并增加 meeting reminder CLI。三项目均由结构化适配器写入文件，故意缺陷导致真实 unittest 失败，重新授权修复后通过；root 篡改的授权在写入前拒绝；事务回滚均无残留。报告仍是合成离线夹具证据，不是通用 Agent、Windows、浏览器、部署或真实用户确认。
