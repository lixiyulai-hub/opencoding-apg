# APG：让小白轻松面对 AI Coding

Adaptive Project Governance（APG）不是儿童产品源码，而是一个面向普通用户和初学者的 **idea-to-result 项目治理与 AI coding 协调器**。用户只需用自然语言提出 Coding 项目需求，APG 会主动整理、追问、规划、拆分并编排后续工作。

## 核心体验

- **Grill Me 自然语言澄清**：主动追问目标、用户、平台、约束、风险和验收，不要求小白先懂项目管理。
- **自动生成项目 Markdown 文献**：按项目类型建立 `PROJECT_BRIEF.md`、`PRODUCT_PLAN.md`、`UX_FLOW.md`、`ARCHITECTURE.md`、`STACK_DECISION.md`、`TASK_GRAPH.md`、`QUALITY_PLAN.md`、`DEPLOYMENT_PLAN.md`，以及按需生成的 PRG plan、memory、agents、design 等文档。
- **需求拆分与执行编排**：将需求拆成依赖、任务波次、Gate、证据和回滚步骤，安全的例行工作自动推进，关键外部动作保留明确 Gate。
- **最终落地导向**：从一句想法开始，经过澄清、文档化、拆分、验证和复核，形成可执行的交付路径。

## 可视化流程

![APG idea-to-result 治理流程图](docs/diagrams/APG_GOVERNANCE_RELEASE_FLOW.svg)

- [查看可编辑 draw.io 图稿](docs/diagrams/APG_GOVERNANCE_RELEASE_FLOW.drawio)
- [查看完整中文说明](docs/README.md)

## 本仓库的验证范围

本仓库同时用于验证 APG 的治理、Gate、证据、回滚、部署预览和发布编排能力。仓库中的验证 Release 只代表 APG 项目验证结果，不代表任何下游产品已经实现、部署或上线。

## English (secondary)

APG is a beginner-friendly idea-to-result coordinator for AI coding: it Grill Me-clarifies natural-language requests, creates project-specific Markdown plans and memory/agent/design artifacts, decomposes requirements, orchestrates execution waves, and preserves Gates, evidence, and rollback.

## 下一阶段正式目标

已确认 APG 下一阶段聚焦 **初心者交互体验与执行器连接**：

- 让小白用中文自然语言开始项目；
- 用 Grill Me 问清真正关键的需求；
- 自动形成项目专属 Markdown 资料；
- 拆分需求并安排执行波次；
- 定义与 AI coding 执行器的适配器契约；
- 先离线模拟和契约测试，再考虑受控本地执行；
- 外部 Host、Provider、凭据、网络、运行时、Git、部署和发布继续保持独立 Gate。

这项确认是 APG 的正式产品方向，不等于已经连接或调用任何执行器。
