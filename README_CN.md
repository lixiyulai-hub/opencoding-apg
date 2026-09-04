# OpenCoding（APG）中文入口

## 一句话认识 OpenCoding

OpenCoding 面向不会写代码、刚开始使用 AI coding 的人：你只要说清楚目标和场景，系统就会用大白话补齐需求，直接给出平台和技术方案，再把项目拆成可以一步步完成的计划。

## 你可以用它做什么？

- 用自然语言说出想法；
- 让 Grill Me 追问目标、用户、边界和验收条件；
- 主动判断 Windows、Mac、iPhone、安卓、网页等使用平台；
- 不要求小白选择前端技术，系统直接给出客户端、后端、数据库和 API 方案；
- 主动判断是否需要服务器、数据库、登录、支付、消息通知、后台和文件存储；
- 按项目需要生成 `AGENTS.md`、`memory.md`、`PRG.md`、`plan.md` 及其他 Markdown 文档；
- 自动整理知识包、任务清单、依赖顺序和执行波次；
- 先 preview，再按授权范围 apply；
- 为每一步保留测试、receipt、哈希和 rollback；
- 失败时冻结任务，修正输入后 Requeue；
- 在密钥、金钱、网络、部署、远程 GitHub 或发布前停在人工 Gate。

## 适合哪些人？

小白、AI coding 初学者、学生、独立开发者、产品人员，以及需要维护旧项目证据链的项目负责人，都可以先从“说清楚目标”开始，而不是先学习复杂工具链。

## 如何运作？

**想法 → Grill Me 澄清 → 平台与方案建议 → 能力判断 → 项目文档 → 任务与波次 → bounded preview → 离线执行 → 测试/doctor/audit → 验收或 Requeue**。

普通离线任务自动推进；外部影响动作保留 Human Gate。本仓库目前是 APG-only 离线治理夹具，不连接真实 Git、网络、Provider、Host 或部署环境。这里展示的是“从想法到方案与执行准备”的能力，不代表下游产品已经完成。

## 反馈与验收

1. 先看 receipt、`VERIFICATION.txt` 和测试输出；
2. 发现问题就补充需求、修正路径或更新任务输入；
3. 重新生成 bounded preview；
4. 从最近有效 checkpoint Requeue；
5. 验收时确认路径、哈希、证据、测试、回滚和最终状态全部可追溯。

## 从哪里开始？

```powershell
Set-Location <your-project-root>
Get-Content .\README.md
Get-ChildItem .\docs\apg
python -X utf8 -m unittest discover -s tests -p 'test_*.py'
```

重点入口：`docs/apg/`（契约、Gate、验收、回滚）、`docs/diagrams/`（双语流程图）、`scripts/`（离线脚本）、`tests/`（测试）、`.governance/receipts/`（证据）。

## English quick view

OpenCoding helps non-coders and AI-coding beginners turn a rough idea into a clear, executable, verifiable, and reversible plan. It clarifies requirements with Grill Me, builds task and evidence structure, previews bounded changes offline, supports failure freeze and Requeue, and keeps human Gates for secrets, money, network, deployment, remote GitHub, and release actions.

## 项目边界 / Project boundary

本仓库只验证 OpenCoding/APG 治理流程，不代表任何特定下游产品已经实现或发布。技术兼容标识继续保留：`adaptive-project-governance`、`Adaptive Project Governance`、`APG`、`$adaptive-project-governance`。
