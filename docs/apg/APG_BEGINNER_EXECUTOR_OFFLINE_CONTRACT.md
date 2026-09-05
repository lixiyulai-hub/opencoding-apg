# APG 初心者 Grill Me 与离线执行器契约

确认日期：2026-09-01  
范围：APG-only、中文优先、纯离线模拟。

## 一句话入口

用户只需写一句中文想法。APG 自动把它作为项目目标，随后用 **Grill Me** 进行三项最小澄清：服务对象与问题、完成标准、限制或必须保留的要求。系统还会根据想法追加大白话问题，例如准备在哪些设备使用、是否联网登录、是否保存数据、是否需要支付、通知或后台。不要求用户先选 React、Vue、Flutter 等技术。

## 结果：项目 Markdown 知识包

离线预览为每个输入稳定生成 Markdown，且包含：

1. 项目目标；
2. 初心者澄清问题及理由；
3. 需求与默认方案；
4. 任务、依赖与工作波次；
5. Gate；
6. 证据；
7. 回滚；
8. Requeue（下一自动工作）。

知识包是可读的项目专属说明，不创建下游项目文件，也不调用任何 Host 或 Provider。预览会列出适合当前项目的 `PROJECT_BRIEF.md`、`PRODUCT_PLAN.md`、`UX_FLOW.md`、`ARCHITECTURE.md`、`STACK_DECISION.md`、`TASK_GRAPH.md`、`QUALITY_PLAN.md`、`DEPLOYMENT_PLAN.md`，并按需列出 `AGENTS.md`、`memory.md`、`PRG.md`、`plan.md`、`design.md` 等文档。

## 平台与方案建议

APG 根据目标设备直接给方案：Windows、macOS、iOS、Android、Web 等。小白不需要先回答“前端用什么技术”，系统会返回客户端、后端、数据库、API 和集成建议，并说明理由、置信度和未决问题。

## 能力主动发现

系统会主动判断服务器、数据库、登录、文件存储、API、支付、消息通知和后台管理是否必要。每项能力都记录 `need`、`reason`、`source`、`recommendation`、`risk`、`gate_required`、`evidence` 和 `rollback`。没有证据的能力标记为未指示或 `unknown`，不凭空增加。

## 方案确认

推荐生成后进入 `recommendation-ready`；如果涉及真实服务、费用、密钥、网络、部署、生产数据或公开发布，则进入 `awaiting-human-confirmation`。确认只决定是否进入下一步，不等于已经连接或执行外部服务。

## 任务图呈现

任务图固定区分三种状态：

- **自动推进**：离线整理、知识包生成、任务图生成、契约验证和报告；
- **建议选择**：系统采用安全默认值，并用中文说明；这不是人工审批；
- **唯一 consequential Gate**：仅当输入涉及密钥、金钱、网络、部署目标、真实数据、Git/公开发布或不可逆外部动作时合并成一次 Gate。

普通输入的路径是 `INSPECT → PROGRESS → PLAN → DISPATCH → VALIDATE → REPORT → REQUEUE`。出现模拟失败时，路径在 `FREEZE` 停止，并给出精确 `resume_condition`。

## 离线 executor adapter 契约

`apg_beginner_executor_preview.py` 只生成内存结果。它必须返回：任务上下文、计划读写范围、Gate 前置条件、模拟输出、错误分类、重试策略、回滚说明和恢复条件。`external_actions` 中的 host、provider、network、runtime、deployment、git、publication 永远为 `false`。

因此本阶段证明的是 APG 的输入、编排和 adapter **契约**，不是执行器已连接、已安装、已运行或已部署。

## 验收与边界

离线测试覆盖 AUTO、合并 Gate、FREEZE/恢复、密钥脱敏、知识包必备章节与确定性重放。独立复核会再次运行这些离线案例并验证证据文件哈希。

不实现、测试、部署或发布儿童知行星球；不连接 Codex、Claude Code、Cursor、其他 Host、Provider 或网络；不读取凭据；不运行目标 runtime；不进行 Git 或 GitHub 操作。
