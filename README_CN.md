# OpenCoding：让 AI Coding 有计划、有边界、可验收

OpenCoding 面向不会写代码、刚开始使用 AI coding 的人。用户只要用大白话说明“想做什么、给谁用、在哪些场景使用”，就能由系统逐步澄清需求、分析方案，并把后续 AI coding 工作整理成可执行、可检查的流程。

**各个 AI agent 是执行引擎；OpenCoding 是项目判断与治理工作流。** 它负责需求澄清、平台与技术决策、项目文档、任务依赖和执行波次、授权边界、结果验证、证据留存、失败恢复和验收。Docker Desktop GUI、MCP 或特定插件不是产品前提。

## 目标工作流

**一句话想法 → 主动追问 → 判断平台和架构 → 生成项目专属文档 → 拆解任务与波次 → 预览并确认范围 → 调度 agent 执行 → 测试与独立审核 → 修复、恢复或 Requeue → 验收交付**。

系统按项目需要判断客户端、后端、服务器、数据库、API、登录、支付、通知、后台和文件存储，并生成各司其职的 `AGENTS.md`、`memory.md`、`PRG.md`、`plan.md` 等文档。密钥、费用、真实数据、外部服务、部署、远程 GitHub 和公开发布等真实外部动作按范围暂停在人工 Gate。

## 当前进度边界

当前代码已有需求会话、技术与能力建议、项目文档和任务图生成、受控本地事务与调度、可选 AI Provider 接口等实现。仓库还保存了一次真实 Provider 评估、合成项目生成，以及候选保存和恢复验证的记录。这些记录只证明对应版本和合成事务的范围；通用 Agent Host 接入、真实用户项目的端到端交付、小白体验和生产部署尚未整体验收。

后续生成级任务、阶段依赖、验收门槛与可复核证据见 [`完整工作流架构与执行路线`](docs/product/AI_CODING_GOVERNANCE_WORKFLOW_V1.md)。本地 API 的已实现入口和限制见 [`Agent 本地使用说明`](docs/product/AGENT_NATIVE_USE.md)。

## 快速查看

```powershell
python -m opencoding --root C:\path\to\existing-project --status --json
```

从源码运行本机工作台：

```powershell
python -m opencoding.workbench --workspace <projects-root> --port 0
```

安装说明见 [`OFFLINE_INSTALLATION.md`](docs/product/OFFLINE_INSTALLATION.md)；这里的文件名指本地安装方式，不限定产品只能离线运行。

## 项目标识

品牌名为 OpenCoding，GitHub 仓库名为 `opencoding-apg`。APG（Adaptive Project Governance）继续作为历史脚本、测试、治理台账和证据格式中的兼容标识。

上游致谢：需求澄清方式参考并致谢 [`mattpocock/skills`](https://github.com/mattpocock/skills) 的 `/grill-me` 与 `/grill-with-docs` 入口；本项目不代表上游作者背书。
