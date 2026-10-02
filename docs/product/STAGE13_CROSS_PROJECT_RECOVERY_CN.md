# Stage13：输入变化、确认绑定、失败停止与文件事务恢复

Stage13 保留 Stage12 全部源码路径，修改 runner、契约测试和两份 SKILL.md，新增恢复脚本及本文。Stage01–12 历史证据保持原样；旧报告只代表当时版本，不可当作当前验收。

## 本阶段修复

独立反例验证发现：Stage12 runner 缺确认时仍会创建目录/安装 skill；action 失败后继续执行却返回成功；无效 rollback path 在动作之后才拒绝；链接祖先会在被拒前创建目录；确认没有绑定预先审阅的动作文件字节。这些是脚本缺陷，旧测试没有覆盖，并非平台限制。

Stage13 的 action 执行现在还要求 `--expected-source-sha256`：先校验全部 action schema、action 文件 SHA、可执行 source 指纹、写入路径、rollback path、根目录隔离和链接祖先，再安装/创建目录。执行脚本必须来自 `--project-root` 指定的同版 source。一次读取/解析的已校验 action snapshot 用于所有 repeat，不会在中途换用另一个文件。两项 SHA 绑定防止已审阅的 action/source 字节漂移，但不认证真实人类确认，也不把 Python 可导入依赖变成安全沙箱。

失败动作返回 exit 1 和 `status=failed`，后续动作及 repeat 不再执行；先前已经发生的写入保留在报告中，不宣称自动回滚。配置、确认或哈希错误返回 exit 2。默认 install/load 是本地安装写入，不称为只读。

## 可复用命令

在解压后的 `source` 根执行，下面路径需替换为三个互不嵌套的绝对目录；不会使用真实用户 Codex home。

```bash
python scripts/run_skill_contract.py --project-root /abs/source --codex-home /abs/private-home --target-platform cli
```

执行前审阅 JSON action file 并保留字节 SHA256。只在合成验收使用：

```bash
python scripts/run_skill_contract.py --project-root /abs/source --codex-home /abs/another-private-home --execution-root /abs/new-project --run-actions --confirm-synthetic --action-file /abs/actions.json --expected-action-file-sha256 <reviewed-action-sha256> --expected-source-sha256 <reviewed-source-fingerprint> --repeat 2
```

已有 home 默认不覆盖。重复指有意重跑同一 action snapshot（1–10 次），每个新进程 invocation 和每个 action 都有新 ID，不是去重或恰好一次执行保证。不要对任意有副作用模块使用 repeat。

## 实验矩阵（合成输入与确认）

- 阅读统计和待办筛选使用不同目标、受众、结果和动作文件。规则规划生成不同项目文档；preview 本身不写计划，合成确认后才写文档。两 execution root 无交叉文件。
- 阅读统计相同 action file 连跑两次，均为真实 Python 子进程测试。
- 同一待办项目审阅动作后修改文件：旧 SHA 被拒，源码未变化、Codex home 未创建；重新审阅新 SHA 后才允许更新并通过新测试。
- 缺确认时没有安装或创建 execution root。未澄清的需求无法通过 `approve_preview`，proposal 不等于确认。
- 故意失败的 Python 动作后，后续文件没有被写入，下一 repeat 没有启动。
- 文件事务修改旧文件并新增文件后，用另一个进程读 receipt 回滚；新文件/新目录清除，旧文件恢复原字节。再次回滚仍无残留。这是进程间重新打开 receipt，不是杀进程/断电/崩溃耐久性验收。

复跑命令（output root 必须尚不存在）：

```bash
python -B evidence/run_stage13_matrix.py --archive-root /abs/extracted --output-root /abs/new-matrix
```

## 边界

`--confirm-synthetic` 是调用方对测试夹具的明确断言，不是真实用户审批。当前没有从 proposal 到人类审批的身份认证机制。两例动作由 Agent 作者预写，证明不同输入和变更路径可执行，不证明通用 AI 编码。

安装资源/适配层可发现、加载并执行；managed Codex loader 始终 unknown/null。Python 是同用户子进程，`sandbox=false`，不提供网络隔离或任意副作用回滚。只测试 Linux，不外推 Windows/macOS；没有 provider、密钥、外部服务、部署、远程 Git、合并或 Release。现有 APG 历史资产仍缺于候选，旧全量缺口不得伪造补齐。
