# 跨平台 Agent / Skill 兼容性矩阵

| 维度 | 当前实现 | 已验证 | 边界 |
|---|---|---|---|
| OpenCoding 宿主系统 | Python 标准库、显式 root、路径/进程适配 | Linux 云环境真实运行 | Windows/macOS 宿主需各自重跑原生进程/锁测试 |
| 生成项目目标平台 | `windows`、`macos`、`ios`、`android`、`web`、`mini_program`、`cli` 规划枚举 | 计划图与目标栈定向测试 | 目标平台工具链未因宿主不同而自动获得 |
| Agent 工具能力 | `LocalAgentAdapter`：结构化 `write_text` / `python_module` | Linux 实际写文件、运行 Python 模块、非零失败与修复 | 不提供模型；无任意 shell、网络或凭据 |
| Skill 接入 | `skills/opencoding/SKILL.md` + `skill.json` + Python entrypoint | 文件清单、entrypoint、adapter contract 测试 | 具体宿主加载器需按其 skill 目录规范安装 |
| 方案与执行 | service preview/apply + transaction + adapter receipt | 小项目端到端证据 | TaskPlan 不能替代项目代码完成 |
| 外部能力 | payment/notification/deployment 等人审 Gate | Gate 阻断测试 | 不连接真实服务 |

“跨平台”在此表示核心协议与入口不依赖 Windows；它不表示每个宿主已经具备
每种目标平台的原生 SDK 或构建工具。目标平台和 Agent 工具能力必须分别报告。
