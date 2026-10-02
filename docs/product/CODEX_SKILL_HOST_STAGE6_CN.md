# Codex Agent 宿主与 OpenCoding skill 验收（Stage 6）

本阶段把 OpenCoding 的项目级 skill 入口放到两个位置：

- `.agents/skills/opencoding/`：标准项目级发现位置，包含 `SKILL.md`、`skill.json` 和安装说明；
- `skills/opencoding/`：源码树中的可复制资源与 fallback 入口。

## 三层结果

`python scripts/verify_codex_skill.py --root /绝对项目路径 --exercise` 会分别输出：

1. `format_valid=true`：YAML frontmatter 和 JSON manifest 符合本项目契约；
2. `project_discovered=true`：在 `.agents/skills/opencoding` 找到成套文件；
3. `project_loader_exercised=true`：在隔离项目根导入 manifest 声明的
   `opencoding.agent_adapter:LocalAgentAdapter` 并读取能力报告；
4. `host_loaded=null`：当前执行环境没有提供“宿主已经加载该项目 skill”的可观测确认。

当前 `skills.list` 的 executor authority 返回空集，cloud authority 只列出平台云 skill；这证明本次接口没有把项目 skill 作为已加载能力返回，不能据此断言 Codex 永远不支持项目 skill。尝试使用本机 Codex app-server 的 `skills/list` 进行只读探针时，协议在本环境没有返回该请求，故保留为 `unverified`，不冒称宿主加载。

## 安装与使用

在获得授权的项目根复制：

```bash
mkdir -p .agents/skills/opencoding
cp /path/to/OpenCoding/skills/opencoding/SKILL.md .agents/skills/opencoding/
cp /path/to/OpenCoding/skills/opencoding/skill.json .agents/skills/opencoding/
python scripts/verify_codex_skill.py --root "$PWD" --exercise
```

宿主若有自己的 skill 配置或发现机制，需由宿主文档确认是否扫描 `.agents/skills`；本 verifier 不写入宿主配置，也不伪造加载结果。

## 执行与安全边界

`LocalAgentAdapter` 要求授权记录绑定同一 canonical root、动作摘要、targets、短期 `expires_at` 和 `confirmation_id`。拒绝过期、错误 root、错误动作摘要、错误 targets、外部标记或非零费用。

这不是安全沙箱：`python_module` 会以当前操作系统用户启动 Python 子进程，模块可以读取/修改该用户有权限的文件并自行发起网络或其他副作用。`shell=False` 只表示没有经 shell 解释命令；`sandbox=false` 和 `process_boundary=same-user-subprocess` 是刻意公开的能力字段。文件事务的回滚证据来自 transaction layer；任意 Python 副作用不可假定可回滚。

本阶段的 Linux E2E 使用合成回答和合成确认夹具，明确 `model_used=false`、`external_actions=false`。它证明项目级入口可检查、适配器可绑定授权并执行本地小项目流程，不证明真实用户确认、真实模型、通用项目完成或远程服务能力。
