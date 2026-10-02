# OpenCoding（APG）中文入口

## 一句话认识 OpenCoding

OpenCoding 是一个可通过 skill 接入不同 Agent 的跨平台 Agent AI coding 规划与受控执行核心。它不要求运行在 Windows，也不把宿主系统当成生成项目的平台：你只要说清楚目标和场景，系统就会用大白话补齐需求，给出目标平台和技术方案，再把项目拆成可以一步步验证的计划。

## 你可以用它做什么？

- 用自然语言说出想法；
- 让 Grill Me 追问目标、用户、边界和验收条件；
- 主动判断 Windows、Mac、iPhone、安卓、网页、命令行等目标平台；
- 不要求小白选择前端技术，系统直接给出客户端、后端、数据库和 API 方案；
- 主动判断是否需要服务器、数据库、登录、支付、消息通知、后台和文件存储；
- 按项目需要生成 `AGENTS.md`、`memory.md`、`PRG.md`、`plan.md` 及其他 Markdown 文档；
- 自动整理知识包、任务清单、依赖顺序和执行波次；
- 先 preview，再按授权范围 apply；
- 为每一步保留测试、receipt、哈希和 rollback；
- 通过 `skills/opencoding/SKILL.md` 和 `LocalAgentAdapter` 接入 Agent 工具能力，并明确区分宿主系统、目标平台和实际动作能力；
- 失败时冻结任务，修正输入后 Requeue；
- 在密钥、金钱、网络、部署、远程 GitHub 或发布前停在人工 Gate。

## 适合哪些人？

小白、AI coding 初学者、学生、独立开发者、产品人员，以及需要维护旧项目证据链的项目负责人，都可以先从“说清楚目标”开始，而不是先学习复杂工具链。

## 如何运作？

**想法 → Grill Me 澄清 → 平台与方案建议 → 能力判断 → 项目文档 → 任务与波次 → bounded preview → 离线执行 → 测试/doctor/audit → 验收或 Requeue**。

普通离线任务自动推进；外部影响动作保留 Human Gate。APG 是 OpenCoding 的治理与证据基础，不是产品的全部定位。本仓库目前不连接真实 Git、网络、Provider、Host 或部署环境；本地结构化适配器可以真实写文件和运行受控 Python 模块，但不冒称通用模型或下游产品已经完成。

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

本仓库验证 OpenCoding 的跨平台核心规划、skill 接入、受控本地执行和 APG 证据流程，不代表任何特定下游产品已经实现或发布。技术兼容标识继续保留：`adaptive-project-governance`、`Adaptive Project Governance`、`APG`、`$adaptive-project-governance`。

## 上游致谢与来源

本项目的 **Grill Me** 追问式需求澄清能力，参考并致谢 [mattpocock/skills](https://github.com/mattpocock/skills) 中的 `/grill-me` 入口；需要结合项目文档时，可参考其 `/grill-with-docs` 入口。

OpenCoding 这里只做中文优先、离线的 APG 契约与流程模拟，不会自动连接或安装上游项目，也不表示得到上游作者背书。上游项目的许可证、使用方式和最新内容请以其仓库为准。
## 项目 skill 入口

OpenCoding 可以作为 Agent 的项目级 skill 接入。标准入口位于
`.agents/skills/opencoding/`，源码副本位于 `skills/opencoding/`；运行
`python scripts/verify_codex_skill.py --root /绝对项目根 --exercise` 可分别核验
文档格式、项目发现和入口导入。当前环境没有宿主已加载项目 skill 的可观测确认，
因此 verifier 不把本地导入冒称为 Codex 宿主加载。

`LocalAgentAdapter` 的 `python_module` 是同用户权限的受限超时子进程，不是安全
沙箱；授权必须绑定 root、动作摘要、targets、短期有效期与 confirmation_id。

若接入 Codex-compatible 本地宿主，可把资源复制到明确的
`$CODEX_HOME/skills/opencoding`：
`python scripts/install_codex_skill.py --project-root /绝对项目根 --codex-home /绝对私有CodexHome --load`。
该命令能验证资源发现和适配器导入，但不会把 managed Codex 的未知状态冒称为已加载。
