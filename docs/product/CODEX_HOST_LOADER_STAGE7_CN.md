# Stage 7 Codex 宿主 skill loader 适配与验收

## 已验证的宿主路径

Codex 本地 skill 资源的宿主约定是：

```text
$CODEX_HOME/skills/<skill-name>/SKILL.md
```

本项目新增 `opencoding.codex_host.CodexSkillHost` 和
`scripts/install_codex_skill.py`，在一个明确的私有 Codex home 中完成：

1. 从项目 `.agents/skills/opencoding` 读取资源；
2. 复制 `SKILL.md`、`skill.json` 和安装说明到
   `$CODEX_HOME/skills/opencoding`；
3. 重新从 Codex home 发现资源；
4. 校验 frontmatter、manifest 和 host-independent / model=false /
   external=false / sandbox=false 契约；
5. 导入 `opencoding.agent_adapter:LocalAgentAdapter`，读取能力并返回
   `host_adapter_loaded=true`。

使用的真实命令（目标目录为隔离测试目录）：

```bash
python scripts/install_codex_skill.py \
  --project-root /workspace/opencoding_work \
  --codex-home /workspace/opencoding_stage7_codex_home \
  --load
```

证据为 `evidence/codex-host-adapter-report-stage7.json`，测试为
`tests/test_codex_host_adapter.py`。

## 三个层次仍分开

- `format_valid=true`：资源格式正确；
- `resource_discovered=true`：项目或 `$CODEX_HOME/skills` 找到资源；
- `host_adapter_loaded=true`：本项目 loader 导入入口并读取 capabilities；
- `codex_managed_loader_observed=null`：没有把项目 loader 的本地复现冒充
  为运行中的 Codex managed loader 已加载。

当前环境的 `skills.list` executor authority 仍返回空集。Codex app-server 的
只读 `initialize` 成功，`skills/extraRoots/set` 产生了 `skills/changed` 通知，
但后续 `skills/list` 在限定等待窗口内没有返回结果；原始探针保存在
`evidence/codex-app-server-stage7-probe-stderr.log`。因此宿主自动加载仍是
`unknown/unobserved`，不是“宿主不支持”。

## 执行闭环

Stage06 的隔离 Linux E2E 继续作为闭环证据：合成回答与合成确认、文档和规划、
预览确认边界、真实文件生成、真实测试失败、文件修复、再次通过和事务回滚。
Stage07 先从 Codex home 加载同一 skill 资源，再读取相同适配器能力；不执行真实
模型/provider、网络、密钥、付费、部署或远程 Git。

## 安全边界

宿主 loader 只复制和导入资源，不扩大 LocalAgentAdapter 的权限。适配器授权仍绑定
canonical root、动作摘要、targets、过期时间及 confirmation_id；`python_module`
仍是同用户 Python 子进程，不是沙箱，任意副作用不能假定可回滚。文件回滚必须走
transaction layer。安装器拒绝符号链接形式的 `$CODEX_HOME`、已有符号链接祖先和符号链接
目标，并使用 staging 目录与原子目录替换保护覆盖安装。
