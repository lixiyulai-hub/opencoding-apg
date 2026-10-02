# agent-native-docs-r2 阶段报告

更新日期：2026-10-02。此阶段在 `checkpoint/stage28-20261002` 上完成，提交 `d50c6c6` 已推送。没有合并、部署或 Release，也没有伪造真实用户验收。

## 代码与文档变更

- `docs/product/OFFLINE_INSTALLATION.md` 将 wheel 示例固定到当前 `pyproject.toml` 的 `0.2.7`，并要求使用构建产出的完整文件名；补充 wheel/sdist 与源码 checkout 的入口边界，说明 `project preview/run` 所需 skill 资源不在安装包中。
- 同一文档加入可调用的会话/文档 Python 示例：有限回答循环、`None`/未解决答案、`busy`/`stale` 停止返回、精确 preview 授权、事务 ID 与回滚检查。示例只定义函数，不在加载时创建项目或写文件。
- `docs/product/AGENT_NATIVE_USE.md` 改为链接该自包含示例，明确 legacy `--status` 与 Stage28 `project status` 是不同状态协议，并把 provider-capable/same-user Python/Node 边界写清楚。
- `docs/product/DELIVERY_PLAN_AGENT_NATIVE_V2.json` 标记当前 docs-r2 事务为 `completed_with_independent_verification`，加入当前 checkout 的验证摘要；W2-B/W2-C1/W2-C2/W3-A 历史 acceptance 路径保留为 `historical_claim_unverified_in_checkout`，不再暗示这些文件存在于当前 checkout。恢复 ZIP 仅列为候选材料，未声称其包含这些具体成员。
- `tests/test_agent_native_docs_contract.py` 新增 4 项契约测试，检查 wheel 版本、API 列表、示例 Python 语法、冲突/只读边界和历史证据状态。

## 测试与独立复核证据

- `python -X utf8 -m unittest`：659 项，0 failures，0 errors，9 skipped。
- focused 文档/服务/CLI/调度/打包：66 项通过。
- 推送后 detached checkout focused：67 项通过；离线 Cargo：4 项通过，doc-tests 0 项；worktree 已删除，未改写分支。
- skill verifier：`format_valid_project_discovered_host_unverified`；managed loader 仍是 `unverified_host_api_has_no_project_skill_loader`。
- adapter probe：Python CLI toolchain observed；没有外部动作。

日志保存在仓库外 `/workspace/stage28-validation-20261002/docs-r2-final/logs/` 和 `/workspace/stage28-validation-20261002/docs-r2-independent-2-logs/`，没有把缓存或运行时路径提交进仓库。

## 风险、回滚与下一步

安装包仍不包含 `.agents/skills/opencoding`、`AGENT_NATIVE_USE.md` 或 `scripts/`；因此 wheel 的 `project preview/run` 不能宣称完成结构化 Agent 任务闭环。managed loader、真实模型/provider、Windows、浏览器、外部服务、真实平台与生产发布仍未验证。历史 W2/W3 acceptance 只保留来源标签，不构成当前 checkout 的再验证。

回滚本阶段可先检查 `git show d50c6c6`，再创建新的 revert 提交；保留前序 source-drift 修复和 Stage28 checkpoint，不强推、不执行破坏性 reset。文档契约变化本身不改变运行时授权、执行或回滚代码。

下一步自动工作是继续维护 source-bound 文档/测试证据；若要进行真实小白产品闭环，仍需用户提供具体需求、完整澄清答案和明确本地范围，再使用 `project init → plan → apply-docs → preview → run → status/rollback`。W4 Host/connectors、W5 平台/小白验收和发布仍是后续独立 Gate。
