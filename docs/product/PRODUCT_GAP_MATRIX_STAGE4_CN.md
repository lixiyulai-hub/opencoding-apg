# 产品化缺口矩阵（批次 04）

| 用户闭环能力 | 当前证据 | 状态 | 余缺/边界 |
|---|---|---|---|
| 大白话目标输入 | `intake.py`、CLI wizard、`test_product_intake` | 已达到 | 仅离线输入，不接模型 |
| 关键问题追问与接续 | session revision/frontier、answer history、resume tests | 已达到 | 真实浏览器尚未验收 |
| 平台与技术栈判断 | `decisions.py`、7 平台计划测试 | 已达到 | 真实工具链版本未联网核实 |
| 服务/数据/API/登录/支付/通知/后台/存储评估 | capability map、conditional docs、planning tests | 已达到（规划层） | 连接和部署仍是人工 Gate |
| 项目专属 AGENTS/memory/PRG/plan | `documents.render_documents`、事务预览/应用 | 已达到 | 文档是计划产物，不声称实现完成 |
| 任务拆解、依赖、波次 | `planning.py`、TaskPlan 校验与测试 | 已达到 | 真实项目任务执行器缺失 |
| 预览与明确确认 | `preview_session`/`approve_preview`、哈希/expiry tests | 已达到 | 浏览器 UI 尚为 fixture |
| 逐步执行、测试、证据、回滚、人审记录 | 新增 `product_loop.py` 与 `test_product_loop` | 离线达到 | 只执行文档契约；代码实现停 `blocked_capability` |
| 自动审核与最终验收 | security task 检查敏感内容、账本 acceptance | 离线达到 | 真实代码/浏览器/跨平台审核未完成 |
| 登录、支付、通知、服务器、数据库、部署 | 设计文档与 activation gates | 未连接 | 禁止本批调用或部署 |
| Windows/Docker/Rust/frontend | Batch03 对账和 readiness plan | 未达到 | 缺同版资产/环境，不能冒称通过 |

本矩阵把“规划完成”“本地文档事务完成”“实际产品实现完成”分开统计，避免 APG-only 证据被误报成产品落地。
