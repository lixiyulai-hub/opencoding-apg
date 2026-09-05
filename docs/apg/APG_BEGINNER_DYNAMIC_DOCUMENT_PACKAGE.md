# APG 初心者动态文档包

用户只说目标和场景，APG 按项目需求生成文档清单和内存 Markdown 预览。核心文档包括：

`PROJECT_BRIEF.md`、`PRODUCT_PLAN.md`、`UX_FLOW.md`、`ARCHITECTURE.md`、`STACK_DECISION.md`、`TASK_GRAPH.md`、`QUALITY_PLAN.md`、`DEPLOYMENT_PLAN.md`。

按需文档包括：

`AGENTS.md`（项目执行规则）、`memory.md`（长期记忆）、`PRG.md`（自动计划循环）、`plan.md`（当前计划）、`design.md`（视觉或交互设计），以及项目类型要求的其他 Markdown 文件。

每份文档都返回：

- 项目相对路径；
- 文档用途；
- 前置依赖；
- `status=preview`；
- 内容 SHA-256。

本阶段不把这些预览写入用户项目。未来写入时必须采用 bounded apply，保留 preimage、postimage、receipt 和 rollback。
