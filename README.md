# OpenCoding

OpenCoding helps people who are new to coding use AI coding agents to turn an idea into a planned, governed, checked, and deliverable software project.

## 中文介绍

你只要用大白话说明“想做什么、给谁用、在哪些场景使用”，OpenCoding 就会追问关键问题，判断适合的平台和技术方案，按项目实际需要生成文档和任务计划，再编排 AI coding agent 执行、验证、修复与交付。

**Agent 是执行引擎；OpenCoding 负责需求判断、方案决策、任务编排、授权边界、过程证据和验收。** 用户不需要先学会编程，也不需要把 Docker Desktop GUI、MCP 或某个特定插件作为产品前提。实际可用的 agent、模型和外部服务仍取决于对应配置与接入验收。

### 完整工作流

**想法 → 需求澄清 → 平台与架构判断 → 项目专属文档 → 任务依赖与执行波次 → 预览与范围确认 → agent 执行 → 测试与独立审核 → 修复或恢复 → 验收交付**。

OpenCoding 会根据项目需要生成 `AGENTS.md`、`memory.md`、`PRG.md`、`plan.md` 等不同职责的文档，并判断是否需要客户端、后端、服务器、数据库、API、登录、支付、通知、后台和文件存储。它为任务保留验证、证据、失败冻结和回滚线索；涉及凭据、费用、真实数据、外部服务、部署、远程 GitHub 或公开发布时，按具体动作暂停在人工 Gate。

### 当前实现边界

仓库已有需求会话、方案与能力判断、文档和任务图生成、本地事务与调度、AI Provider 接口，以及合成项目上的生成和恢复证据。这些证据证明了对应代码路径和那次合成事务，不代表通用 agent Host 接入、真实用户项目端到端执行、所有 Provider、生产部署或小白用户验收已经完成。

生成级后续阶段、依赖关系、验收标准和当前证据见 [`AI Coding 治理工作流架构与执行路线`](docs/product/AI_CODING_GOVERNANCE_WORKFLOW_V1.md)。Agent 本地 API 的现有限制见 [`AGENT_NATIVE_USE.md`](docs/product/AGENT_NATIVE_USE.md)。

### 开始使用

```powershell
Set-Location <your-project-root>
python -m opencoding --root C:\path\to\existing-project --status --json
```

需要交互式本地工作台时，可运行 `python -m opencoding.workbench --workspace <projects-root> --port 0`。源码测试：

```powershell
python -X utf8 -m unittest
```

本地安装说明见 [`OFFLINE_INSTALLATION.md`](docs/product/OFFLINE_INSTALLATION.md)。这里的离线安装说明不表示 OpenCoding 产品只支持离线治理。

### 仓库入口

- [`README_CN.md`](README_CN.md)：中文快速介绍。
- [`docs/product/`](docs/product/)：产品契约、架构、使用边界与执行路线。
- [`docs/apg/`](docs/apg/)：历史 APG 治理契约、Gate、验收和回滚参考。
- `opencoding/` 与 `tests/`：当前实现和自动化测试。

## English

OpenCoding is a governance workflow for people who are new to coding and want to build software with AI coding agents. It clarifies an idea, recommends a project shape, creates project-specific instructions and a dependency-aware task plan, then coordinates agent execution, verification, recovery, and delivery.

**Agents are the execution engines. OpenCoding provides project judgment, orchestration, authorization boundaries, evidence, and acceptance.** Docker Desktop GUI, MCP, and a particular plugin are not product prerequisites. The availability of a specific agent, model, or external service depends on its integration and verification.

The repository contains requirement sessions, recommendations, project-document and task-plan generation, local transactions and scheduling, optional AI provider interfaces, and evidence from a synthetic generation and recovery run. That evidence is scoped to the recorded run; it does not establish universal agent-host support, end-to-end acceptance on real user projects, production deployment, or beginner usability acceptance.

See [`docs/product/AI_CODING_GOVERNANCE_WORKFLOW_V1.md`](docs/product/AI_CODING_GOVERNANCE_WORKFLOW_V1.md) for the staged delivery architecture and acceptance criteria, and [`docs/product/AGENT_NATIVE_USE.md`](docs/product/AGENT_NATIVE_USE.md) for current local API boundaries.

## Name and compatibility

OpenCoding is the product name. `opencoding-apg` is the repository slug. `Adaptive Project Governance`, `APG`, and `adaptive-project-governance` remain technical compatibility identifiers for historical scripts, tests, ledgers, receipts, snapshots, and change IDs.

## Upstream attribution

The Grill Me style of requirement clarification is inspired by and attributed to [`mattpocock/skills`](https://github.com/mattpocock/skills), including its `/grill-me` and `/grill-with-docs` entry points. This project does not imply endorsement by the upstream authors.
