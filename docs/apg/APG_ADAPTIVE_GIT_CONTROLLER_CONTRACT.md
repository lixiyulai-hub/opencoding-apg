# APG 主动自适应 Git Controller Bridge 契约

确认日期：2026-09-02  
范围：APG-only、中文优先、离线自动计划循环。

## 目的

`apg_adaptive_git_controller.py` 把 APG 的 PRG/初心者预览状态传给主动自适应 Git 检查点适配器。它只产生机器可读的预览，不执行 Git。

## 输入

```json
{
  "success_node": "validation-passed",
  "verified_stages": ["intake-ready", "plan-ready"],
  "evidence_complete": true,
  "tests_passed": true,
  "scope_clean": true,
  "repeated": false,
  "failure": null
}
```

字段由 APG progress state 提供；缺少成功节点时自动等待，失败或证据不完整时自动冻结。

## 输出

- `controller`：来源和只读策略；
- `prg`：自动循环路由、终态和恢复条件；
- `git_checkpoint`：既有适配器的 checkpoint/revert 建议；
- `ledger_event`：确定性检查点事件 ID，不写入持久化 ledger；
- `next_action`：系统下一步自动动作，不是审批；
- `replay_digest`：同一输入的确定性摘要。

## 自动动作规则

| Git 状态 | 自动下一步 |
| --- | --- |
| `CHECKPOINT_RECOMMENDED` | 记录检查点预览，继续 APG 循环 |
| `ALREADY_RECOMMENDED` | 复用原检查点，继续循环 |
| `FREEZE` | 保留最近回退点，修复后自动 requeue |
| `WAIT` | 继续收集证据，直到出现成功节点 |

## 离线边界

输出固定为 `mode=offline-simulation`、`git_action=PREVIEW_ONLY`、`execution_performed=false`。`external_actions` 中 Git、网络、远端、Provider、Host、runtime、部署和发布均为 `false`。真实本地 Git、GitHub、Host 或 Provider 连接属于后续独立事务。
