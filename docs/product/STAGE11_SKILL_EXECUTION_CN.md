# Stage11：隔离候选中的 skill 入口与执行契约

Stage11 从 Stage10 单包恢复到新的私有目录，先验证归档、manifest、CRC、350 个条目和 14 个 required source paths，再验证 `.agents/skills/opencoding` 的格式、项目发现和入口 exercise。

## 真实 Linux 验证

在恢复目录中把项目 skill 安装到临时 `$CODEX_HOME/skills/opencoding`，通过 `CodexSkillHost` 发现并加载，随后按 `skill.json` 的 `opencoding.agent_adapter:LocalAgentAdapter` 入口执行真实本地动作：

- 写入 `skilldemo/main.py` 和测试文件；
- 运行 `python_module` 测试并得到 `stage11 skill test passed`；
- 对一个故意失败的测试执行修复，随后得到通过结果；
- 用 transaction receipt 写入并回滚 `rollback-proof.txt`，回滚残留为空；
- 每个动作都带有 root、action digest、targets、expires_at、confirmation_id 绑定。

这些输入和确认是标注过的 synthetic fixture，不代表真实用户确认。运行环境是 Linux，`model_used=false`，`external_actions=false`，适配器明确 `sandbox=false` 且为同用户子进程；`python_module` 仍可能产生任意副作用，不能当作安全沙箱。

## 三层观察边界

- `format_valid=true`：SKILL.md frontmatter 和 skill.json 合规。
- `resource_discovered=true`、`host_adapter_loaded=true`：项目宿主适配层确实从临时 Codex home 发现并加载入口，且该入口被实际调用。
- `codex_managed_loader_observed=null`：当前环境没有可观察的 managed Codex skill loader 回执，不能声称宿主内部自动加载。

归档中的历史 `evidence/run_stage7_host_e2e.py` 首次按错误根目录执行时 fail-closed，因为 Stage10 包的目录布局是 `source/` 与 `evidence/` 分离；修正 runner 显式传入 `source` 根后才得到本阶段结果。该边界记录在 `evidence/stage11-initial-script-boundary.log`，不被计入 PASS。

## 未覆盖范围

本阶段没有 provider、密钥、网络、支付、通知、部署、远程 Git、合并或 Release；没有补造历史 receipt、时间线、用户确认或旧 APG fixture 修改结果。Windows/macOS、真实模型和 managed loader 仍未观测。
