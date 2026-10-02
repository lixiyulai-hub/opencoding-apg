# Stage24：最小本地 Agent 接线

本阶段把现有 Codex skill 可调用的 `LocalAgentAdapter` 接入已采用任务图的实现/验证波次。它只接受调用方先生成的精确动作映射：

1. `preview_agent_tasks` 读取当前会话、采用计划、任务输出、目标文件前像和 skill/source hash；验证节点必须写入测试输出并执行非空 `unittest discover`。
2. `LocalAgentTaskExecutor` 只消费调用方已明确确认的动作授权，不生成需求、代码或确认。每个动作仍绑定 root、action digest、targets、scope、expiry 和一次性 claim。
3. `start_product_run`/`resume_product_run` 通过显式 `project_executor` 派发，账本保存 preview digest、skill/source hash、授权和 receipt。没有执行器仍然 `blocked_capability`；执行器不会序列化，接续时必须重新构造并重新检查。
4. 文件写入走 `transactional_write=True`。`rollback_product_run` 只清理首次确认时不存在、且仍匹配最新 receipt 的文件；已有文件和任意 Python 副作用不自动恢复。
5. `build_acceptance_report` 没有本次 run evidence 时将本地动作和 skill discovery 保持 `unverified`；只有当前 run 的 plan/root、source/skill hash、授权 claim、文件 bytes 和非空成功测试全部匹配才转为 `observed`。

本阶段没有 provider、模型、网络、密钥、外部服务、部署或 managed loader。合成回答和确认仅用于 Linux 离线验证，不能冒充真实用户确认。Windows/macOS/browser/目标工具链仍需各自适配器与环境证据。

CLI 入口：

```bash
python -m opencoding --root /abs/project \
  --preview-agent-tasks SESSION_ID --agent-actions /abs/reviewed-actions.json
python -m opencoding --root /abs/project \
  --run-plan SESSION_ID --agent-actions /abs/reviewed-actions.json \
  --expected-agent-preview PREVIEW_DIGEST \
  --confirm-local-actions --synthetic-confirmation --confirmation-id CONFIRM_ID
```

## 本阶段实测

新需求为离线命令行植物浇水追踪器。真实流程为合成澄清 → 文档预览/采用 → skill 入口 → 文件写入 → 故意测试失败 → 新预览/新授权修复 → 测试通过 → receipt 绑定验收 → 文件回滚。证据：`evidence/STAGE24_AGENT_BRIDGE.json`；运行脚本：`scripts/run_stage24_agent_bridge.py`。

证据边界：Linux host；target `cli` 是计划标签；model/provider/external=false；managed loader=null/unobserved；sandbox=false，同用户 Python 子进程；任意 Python 副作用不保证自动回滚。
