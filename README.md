# OpenCoding（APG）

## 中文介绍

OpenCoding 是一个面向不会写代码、刚开始使用 AI coding 的人的 **项目治理与执行准备助手**。你只要用大白话说清楚想做什么、给谁用，它就会先把需求问清楚，再直接给出适合的项目方案。

### 它能帮你做什么？

- 用自然语言描述想法，不必先会写专业需求文档；
- 通过 **Grill Me** 用大白话追问目标、用户、边界、验收条件和风险；
- 主动判断项目适合 Windows、Mac、iPhone、安卓、网页等哪些平台；
- 不要求小白选择前端技术，系统直接给出客户端、后端和整体技术方案；
- 主动判断是否需要服务器、数据库、登录、API、支付、消息通知、后台管理和文件存储；
- 按项目需要生成 `AGENTS.md`、`memory.md`、`PRG.md`、`plan.md` 及其他 Markdown 文档；
- 自动整理知识包、任务清单、依赖顺序和执行波次；
- 把大目标拆成小步骤，并为每一步安排检查、证据和回滚；
- 先做离线 preview，再决定是否 apply；
- 对失败任务进行记录、重试和 **Requeue**，避免“失败后没人知道下一步”；
- 对密钥、金钱、网络、部署、远程 GitHub 发布等外部影响保留人工 Gate。

### 适合谁？

- **不会写代码的小白**：先把想法说清楚，再按步骤开始；
- **刚开始用 AI coding 的人**：知道 AI 每一步做什么、如何检查；
- **学生、独立开发者和产品人员**：把目标、任务和验收标准放在同一条证据链上；
- **已有项目的维护者**：在保留旧 receipts、snapshots 和无关改动的前提下，进行可追踪的增量变更。

### 如何运作？

1. **输入想法**：用一句话描述目标和希望解决的问题；
2. **澄清需求**：Grill Me 追问关键缺口，形成边界和验收条件；
3. **生成计划**：输出知识包、任务图、依赖关系、风险和回滚路径；
4. **Preview**：列出精确路径、变更字段、Gate、哈希、receipt 和 rollback；
5. **执行**：普通离线任务可按计划自动推进；涉及外部影响的动作停在人工 Gate；
6. **验证与反馈**：运行测试、doctor、audit 和 independent review，记录结果；
7. **完成或 Requeue**：通过则生成证据并结束；失败则冻结相关任务，修正输入后重新排队。

### 执行方式与 Gate

| 模式 | 做什么 | 是否触及外部系统 |
| --- | --- | --- |
| **Offline preview** | 只计算计划、范围、哈希、证据和回滚 | 否 |
| **Bounded apply** | 只在授权路径内写入本地文件，并保留 preimage | 否 |
| **Human Gate** | 由人确认密钥、金钱、网络、部署、远程 Git、发布或不可逆动作 | 可能 |

普通 APG 流程不反复要求人工批准；只有外部影响才需要 Gate。本仓库当前保持 **APG-only、离线、无真实 Git 写入**。

### 反馈、失败与 Requeue

- **反馈入口**：先查看本次 transaction 的 receipt、`VERIFICATION.txt` 和测试输出；
- **可重试问题**：补充需求、修正路径或更新任务输入后，重新生成 bounded preview；
- **失败冻结**：失败任务不会静默跳过，系统保留失败状态和下一 Gate；
- **Requeue**：确认修正后的输入后，把任务放回队列，从最近的有效 checkpoint 继续；
- **升级人工**：若问题涉及凭据、远程服务、费用、部署或发布，转到对应 Human Gate。

### 如何验收？

一次变更至少应满足：

- 变更路径与计划完全一致，没有越界写入；
- preview 与 apply 分离，且保留原始哈希和 rollback 证据；
- `python -X utf8 -m unittest discover -s tests -p 'test_*.py'` 通过；
- doctor、audit 的只读证明和 independent review 结果可追溯；
- receipt、snapshot、ledger（如适用）可重放、幂等且未覆盖历史记录；
- 失败时能冻结、回滚或 Requeue，成功时能说明结果、状态和下一步。

### 快速开始

```powershell
# 1. 进入项目根目录
Set-Location <your-project-root>

# 2. 阅读入口文档
Get-Content .\README.md
Get-ChildItem .\docs\apg

# 3. 运行离线测试
python -X utf8 -m unittest discover -s tests -p 'test_*.py'
```

然后从 `docs/apg/` 选择对应主题：自动 PRG、Executor Adapter、Adaptive Git checkpoint、Controller Bridge、Ledger persistence、Gate 与发布边界。

### 项目目录与入口

- `README.md`：中文优先的完整总览，并附英文版本；
- `README_CN.md`：中文入口，适合第一次了解项目；
- `docs/apg/`：APG 契约、流程、Gate、验收和回滚文档；
- `docs/diagrams/`：OpenCoding 双语流程图及可编辑 draw.io 源文件；
- `scripts/`：离线 preview、Controller、Ledger 相关脚本；
- `tests/`：离线单元测试；
- `.governance/receipts/`：不可覆盖的事务证据；
- `.governance/progress/`：进度定义和 Ledger 投影；
- `artifacts/`：每次受控变更的 preimage、diff、验证和回滚材料。

### 适用范围与明确不做什么

**适用**：需求澄清、AI coding 任务拆解、离线执行准备、证据链、回滚和人工 Gate 编排。

**不适用**：本仓库不实现任何特定下游产品；不连接 Provider、Host、凭据、网络或部署环境；不把离线验证结果写成产品已经发布。

### 与大型热门 GitHub 项目的 README 对照

热门项目通常会同时提供：一句话定位、功能列表、快速开始、文档入口、目录结构、贡献方式、测试/CI、版本或发布说明、许可证、社区反馈和限制说明。OpenCoding 已补齐其中与 APG-only 治理最相关的部分：定位、初学者场景、运作方式、执行模式、反馈/Requeue、验收、快速开始、目录入口、适用边界和双语说明；贡献、许可证和社区渠道会在项目进入相应阶段后单独补充，不在本次离线变更中虚构。

## English

OpenCoding is a **beginner-oriented AI coding governance and execution-preparation framework**. It does not pretend to build a product from an unclear idea; it turns that idea into work that is understandable, executable, verifiable, and reversible.

### What can it do?

- accept a rough idea in natural language;
- use **Grill Me** to clarify goals, users, boundaries, acceptance criteria, and risks;
- recommend target platforms and a front-end/back-end approach without asking beginners to choose frameworks;
- identify likely needs for servers, databases, APIs, login, payments, notifications, admin tools, and file storage;
- generate project-specific Markdown such as `AGENTS.md`, `memory.md`, `PRG.md`, and `plan.md` when needed;
- produce project notes, knowledge packs, task lists, dependencies, and execution context;
- split a large goal into small steps with checks, evidence, and rollback;
- run an offline preview before any apply step;
- record failures, retries, and **Requeue** instead of silently skipping work;
- stop at a human Gate before secrets, money, network, deployment, remote GitHub, or release actions.

### Who is it for?

- **People who cannot code yet** and need a clear first path;
- **AI-coding beginners** who want to understand and check each step;
- **Students, indie builders, and product teams** that need traceable tasks and acceptance criteria;
- **Project maintainers** who need additive, auditable changes without losing existing receipts, snapshots, or unrelated work.

### How does it operate?

1. describe the idea;
2. clarify missing requirements with Grill Me;
3. generate a knowledge pack, task graph, dependencies, risks, and rollback path;
4. preview exact paths, fields, Gates, hashes, receipts, and rollback;
5. automatically advance routine offline work while stopping at human Gates for external impact;
6. run tests, doctor, audit, and independent review;
7. complete with evidence, or freeze the failed task and Requeue it after the input is corrected.

### Execution modes and Gates

| Mode | Purpose | External systems |
| --- | --- | --- |
| **Offline preview** | Calculate scope, hashes, evidence, and rollback only | No |
| **Bounded apply** | Write only inside authorized local paths and retain preimages | No |
| **Human Gate** | Confirm secrets, money, network, deployment, remote Git, release, or irreversible work | Maybe |

Routine APG work proceeds without repeated manual approval. This repository currently remains **APG-only, offline, and free of real Git writes**.

### Feedback, failure, and Requeue

Inspect the transaction receipt, `VERIFICATION.txt`, and test output first. Correct the requirement, path, or task input, generate a new bounded preview, and Requeue from the latest valid checkpoint. Failed work is frozen and recorded; external-impact issues are routed to the appropriate Human Gate.

### Verification and acceptance

A change is accepted when its paths match the plan, preview and apply remain separate, preimages and rollback evidence are retained, the offline unittest command passes, doctor/audit/independent-review evidence is traceable, receipts and ledgers remain replayable and idempotent, and success or failure states clearly identify the result and next step.

### Quick start

```powershell
Set-Location <your-project-root>
Get-Content .\README.md
Get-ChildItem .\docs\apg
python -X utf8 -m unittest discover -s tests -p 'test_*.py'
```

### Repository map

- `README.md` / `README_CN.md`: bilingual and Chinese-first onboarding;
- `docs/apg/`: APG contracts, Gates, acceptance, and rollback;
- `docs/diagrams/`: editable bilingual workflow assets;
- `scripts/`: offline preview, Controller, and Ledger helpers;
- `tests/`: offline tests;
- `.governance/receipts/`: transaction evidence;
- `.governance/progress/`: progress and Ledger projections;
- `artifacts/`: preimages, diffs, verification, and rollback materials.

### Scope and boundaries

OpenCoding is for requirement clarification, AI-coding task decomposition, offline execution preparation, evidence, rollback, and human-Gate orchestration. This repository does not implement the downstream child product and does not connect to providers, hosts, credentials, networks, or deployment environments.

### README completeness note

Large, popular GitHub projects commonly include positioning, features, quick start, documentation, repository structure, contribution/testing guidance, release or version notes, license, community feedback, and limitations. This README now covers the APG-relevant set in both languages; contribution, licensing, and community channels will be added in a separate lifecycle transaction when those surfaces are actually established.

## 名称与兼容标识 / Name and compatibility

- 正式品牌 / Brand: **OpenCoding**
- GitHub 仓库 / Repository: `lixiyulai-hub/opencoding-apg`
- 技术兼容别名 / Technical aliases: `adaptive-project-governance`, `Adaptive Project Governance`, `APG`, `$adaptive-project-governance`

历史脚本、测试、Ledger、receipts、snapshots 和 change IDs 保持原名，以保证可重放和证据链连续。
