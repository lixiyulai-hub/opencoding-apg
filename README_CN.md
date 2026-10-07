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

普通离线任务在已授权范围内推进；外部影响动作保留 Human Gate。当前 0.2.7 包含本地 CLI/Python API、可选的本机浏览器工作台和 AI 提供方接口。离线文档流程不需要 AI，真实 AI 调用需单独配置与授权；功能存在不代表下游产品已经完成或验收。

## 反馈与验收

1. 先看 receipt、`VERIFICATION.txt` 和测试输出；
2. 发现问题就补充需求、修正路径或更新任务输入；
3. 重新生成 bounded preview；
4. 从最近有效 checkpoint Requeue；
5. 验收时确认路径、哈希、证据、测试、回滚和最终状态全部可追溯。

## 从哪里开始？

```powershell
Set-Location <your-project-root>
python -m opencoding.workbench --workspace C:\path\to\OpenCoding-projects --port 0
# 或只读查询已有项目
python -m opencoding --root C:\path\to\existing-project --status --json
```

安装包使用方法见 [`安装与本地使用`](docs/product/OFFLINE_INSTALLATION.md)。工作台只绑定本机，使用启动窗口给出的完整地址进入；Ctrl+C 停止，项目数据保留。开发验证运行 `python -X utf8 -m unittest`。

重点入口：`docs/apg/`（契约、Gate、验收、回滚）、`docs/diagrams/`（双语流程图）、`scripts/`（离线脚本）、`tests/`（测试）、`.governance/receipts/`（证据）。

## English quick view

OpenCoding helps non-coders and AI-coding beginners turn a rough idea into a clear, executable, verifiable, and reversible plan. It clarifies requirements with Grill Me, builds task and evidence structure, previews bounded changes offline, supports failure freeze and Requeue, and keeps human Gates for secrets, money, network, deployment, remote GitHub, and release actions.

## 项目边界 / Project boundary

本仓库提供 OpenCoding/APG 的本地规划、文档事务、受控执行和可选 AI 接入；不代表任何特定下游产品已经实现或发布，也不承诺所有平台或 Host 已通过验收。技术兼容标识继续保留：`adaptive-project-governance`、`Adaptive Project Governance`、`APG`、`$adaptive-project-governance`。

## 上游致谢与来源

本项目的 **Grill Me** 追问式需求澄清能力，参考并致谢 [mattpocock/skills](https://github.com/mattpocock/skills) 中的 `/grill-me` 入口；需要结合项目文档时，可参考其 `/grill-with-docs` 入口。

OpenCoding 这里只做中文优先、离线的 APG 契约与流程模拟，不会自动连接或安装上游项目，也不表示得到上游作者背书。上游项目的许可证、使用方式和最新内容请以其仓库为准。
