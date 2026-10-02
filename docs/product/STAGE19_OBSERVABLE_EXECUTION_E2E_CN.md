# Stage19：可观测执行边界与非模板项目验证

Stage18 的能力矩阵回答“当前 Agent 能做什么”。Stage19 把同一份事实附到每个已完成的适配器回执中，回答“这次动作实际观察到什么”。`LocalAgentAdapter.execute` 现在返回 `capability_observation`，其中同时记录实际 Linux 宿主、请求的目标标签、目标工具链是否验证、动作类型、模型/外部动作、sandbox、进程边界和 managed loader 状态。

目标平台标签只表示规划意图。Stage19 的 `cli` 标签被识别，但 `target_toolchain_verified=false`，没有把 Linux Python 子进程称作 CLI 产品工具链。`python_module` 仍是同用户子进程，模块可以产生适配器无法控制的副作用或网络访问，且没有自动回滚；`write_text` 的回滚仍由事务层提供。

## 非模板项目

`scripts/run_stage19_non_template.py` 从一组明确标注的合成需求生成一个家庭支出分类 CLI 小项目，包含项目级 `AGENTS.md`、`plan.md`、README、实现和测试。它通过真实 `LocalAgentAdapter` 写入文件，再用真实 `python -m unittest discover` 执行：

1. 初版故意包含 `category` 字段拼写错误，测试真实失败；
2. 以新的授权绑定写入修复，测试重新通过；
3. 用事务层创建并回滚临时证据文件，回执显示 `rolled_back` 且无残留路径。

证据 `evidence/STAGE19_NON_TEMPLATE_E2E.json` 明确记录 synthetic input/confirmation、`model_used=false`、`external_actions=false`、工具链未验证和 managed loader 未观测。它是一个小项目的端到端证据，不是通用 Agent 能力、真实用户确认或目标平台验收。
