# APG Plan / Loop Harness 自动流程

```text
INSPECT -> PROGRESS -> PLAN -> DISPATCH -> VALIDATE -> REPORT -> REQUEUE
```

## 自动路径

1. `INSPECT` 读取 APG 源版本、manifest、已保留 receipt 和当前证据。
2. `PROGRESS` 计算来源绑定的进度；缺失来源时报告 `not-computable`，不要求用户推断。
3. `PLAN` 生成任务、依赖、Gate、验收和回滚。
4. `DISPATCH` 只派发已具备合法本地权限的工作；不会借此产生新权限。
5. `VALIDATE` 执行离线检查并在首次失败时停止扩散。
6. `REPORT` 用中文短句输出结果、原因和下一步。
7. `REQUEUE` 自动安排下一轮；只有外部影响才切换到单一 `CONSEQUENTIAL_GATE`。

## 面向小白的呈现

- 默认隐藏 P3/P4/P5 等内部编号；
- 不要求用户选择框架、目录、测试工具等专业细节；
- 系统自动选择安全默认值并记录理由；
- 仅在密钥、金钱、网络、部署目标、真实数据或不可逆动作时显示一次必要选择；
- 失败时由系统冻结、保留证据并自动给出恢复条件。

## 停止条件

`FREEZE` 的恢复条件使用稳定代码，例如：

- `resume.after-scope-drift-is-resolved`
- `resume.after-missing-evidence-is-resolved`
- `resume.after-selected-gates-pass`
- `resume.after-bounded-loop-continues-without-stop-condition`

## 验收口径

常规路径必须 `human_gate=false`、`dispatch_permitted=true`；必要外部边界必须只产生一个 Gate，并且所有外部动作执行标志保持 `false`，直到后续独立事务被明确授权。
