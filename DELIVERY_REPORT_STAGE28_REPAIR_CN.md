# Stage28 有限修复与离线复核报告

更新日期：2026-10-02。报告对应独立分支 `checkpoint/stage28-20261002`，最新提交为 `5ce3651840d757f0fa3c0ffcd64be516f4a65e6e`，已推送到 GitHub。没有合并、部署或 Release。

## 变更

`tests/test_skill_runtime_contract.py` 的 source-drift 夹具现在只复制运行时明确需要的输入：`opencoding/`、两份 `opencoding` skill 资源，以及两个 skill runner 脚本。复制前后先核对 `source_fingerprint`，再修改副本中的 skill 文件验证漂移拒绝。夹具不再追随 `artifacts/w1-isolated-integration-20260905/test-temp/` 中六个旧 Windows `E:/...` 绝对链接；这些历史证据没有删除，也没有改变生产路径安全检查。

## 验证证据

- 修复前基线：`python -X utf8 -m unittest` 为 656 项、645 通过、2 errors、9 skipped。两个错误是旧 Windows 链接复制失败和环境找不到 `cargo`。
- 修复后：`python -X utf8 -m unittest` 为 656 项、0 failures、0 errors、9 skipped。
- Stage28 focused：109/109 通过。
- 预置私有 Rust 工具链以显式 `PATH`、`LD_LIBRARY_PATH`、`CARGO_HOME`、`CARGO_TARGET_DIR` 和 `CARGO_NET_OFFLINE=true` 运行 `cargo test --offline --locked --manifest-path services/domain/Cargo.toml`：4/4 Rust 测试通过，doc-tests 0 项通过；没有全局安装或联网取包。
- 技能 verifier：`format_valid_project_discovered_host_unverified`；项目发现、入口导入和能力读取通过，managed loader 仍为 `unverified`。
- 独立 detached worktree（推送后的 `5ce3651`）：focused 子集 103/103、Cargo 4/4 通过。
- 现有 Stage19 本地闭环以 fixture 运行：初始测试失败被记录，修复后测试通过，事务回滚 `rolled_back` 且残留为空。该输入明确为 `synthetic`，不计作真实用户验收。
- 另以仓库推荐的 CLI 路由做了一次完整离线演练：`project init → plan → apply-docs → preview → run → status → rollback`，10/10 计划任务完成、1 个 Python 测试通过，两个新建源码文件均由 `project rollback` 移除。答案来源明确为 `agent-assumptions`，因此仍不计作真实用户验收。

原始终端日志保存在仓库外 `/workspace/stage28-validation-20261002/logs/`，避免把运行缓存和环境路径提交进仓库。

## 风险、回滚与下一步

当前仍未观测 managed loader、真实模型/provider、Windows、浏览器、真实用户需求、外部服务、部署或发布。Python/Node 动作仍是同用户子进程，网络行为不由适配器控制，文件事务也不回滚任意子进程副作用。

本次提交只改测试夹具；回滚可先检查 `git show 5ce3651`，再用新的 revert 提交撤销，保留 `411aab6` 交接点和源码 checkpoint `88e0aa8`。不要对 checkpoint 强推或在未提交工作树执行破坏性 reset。

下一步需要一个明确的真实小白需求、完整澄清答案和本地验收范围；收到后按 `project init → plan (answers-origin=user-conversation) → apply-docs → preview → run → status/rollback` 执行一次有限离线闭环，并把真实回答与 fixture/agent assumptions 分开记录。若没有该输入，只能继续保持 synthetic 证据边界。

## Gate 边界

交接中记录的 GitHub 草稿 PR 请求曾返回 `Post "https://api.github.com/graphql": Forbidden`（403）。本阶段没有重试被拒绝 API，也没有创建或暗示存在 PR；后续若权限和明确授权恢复，只创建待审草稿 PR，不合并、不部署、不 Release。
