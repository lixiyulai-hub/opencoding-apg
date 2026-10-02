# Stage12：可复用 skill 安装、发现与运行契约

Stage12 从 Stage11 单包恢复到新的私有目录，先复核归档，再把 skill 主链整理为可复用的命令契约，并用一个不同于问候工具的离线阅读清单项目验证。

## 可复用契约

`python scripts/run_skill_contract.py` 有两个明确层级：

1. 默认只做安装、发现和入口加载。它要求绝对项目根目录和显式私有 `--codex-home`，输出 `format_valid`、`resource_discovered`、`host_adapter_loaded` 与 `codex_managed_loader_observed`。
2. 只有同时传入 `--run-actions --confirm-synthetic --execution-root --action-file` 才执行本地动作。`--confirm-synthetic` 是夹具标记，不是真实用户确认；动作仍必须通过 LocalAgentAdapter 的授权绑定。可选 `--rollback-probe-path` 会在 execution root 使用 transaction receipt 写入并回滚一个探针文件。

该契约拒绝隐式执行。`python_module` 是同用户 Python 子进程，`sandbox=false`，可能产生任意副作用；契约只提供边界和收据，不把它描述成安全沙箱。

## 新项目验证

本阶段使用独立的 `offline-reading-log` 项目，不复用 Stage11 的问候工具：

- 用户回答和确认标为 synthetic；目标为命令行本地阅读进度记录。
- 服务流程生成并写入 `AGENTS.md`、`memory.md`、`PRG.md`、`plan.md`、`architecture.md`、`product.md`、`security.md` 等项目文档。
- preview 状态为 `ready`，确认后文档事务为 `applied`。
- skill 入口真实写入 `readinglog/` 文件；初始 `total_pages` 实现故意返回错误值，测试失败；随后修复实现并通过测试。
- transaction receipt 回滚 `readinglog/rollback-proof.txt`，残留路径为空，授权记录绑定 root、action digest、targets、expires_at、confirmation_id。

## 观察边界

`format_valid=true`、`resource_discovered=true`、`host_adapter_loaded=true` 和 `declared_entrypoint_executed=true` 只证明本地项目适配层从临时 Codex home 读取资源并调用声明入口。Codex home 保存的是 SKILL.md/skill.json/README.md；Python 实现从项目 source 导入。`codex_managed_loader_observed=null`，不能声称 managed loader 自动加载。

本阶段只在 Linux 做离线验证；没有 provider、密钥、网络、支付、通知、部署、远程 Git、合并或 Release，也没有补造历史 receipt、时间线、用户确认或旧 APG fixture 结果。
