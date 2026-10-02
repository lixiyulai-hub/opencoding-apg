# Stage15：并行项目授权、确认绑定与恢复隔离

Stage15 在 Stage14 的三项目并行夹具上验证两类容易被误读的边界：授权记录不能跨项目或跨动作复用；事务回滚不能因为目录被移动而误作用于另一个根。

## 已实现的绑定

`LocalAgentAdapter.authorization_for()` 现在写入 `confirmation_binding`，它是对 confirmation label、canonical root、action digest、targets、scope 和 expiry 的内容摘要。`execute()` 在写入或启动子进程前核对该摘要，因此修改确认标识、改动作或把授权交给另一个项目都会被拒绝且不产生文件。

这个摘要不是签名、身份认证或一次性审批。相同授权在有效期内重复调用仍可能执行；本阶段把该边界记录为显式结果，真正的人类审批与一次性消费需要更高层的宿主门控。

事务 manifest 现在保存 canonical execution root。`read_receipt_state()`、`rollback_changes()` 和恢复 helper 对新回执核对 root；把完整项目树移动或复制到另一个路径后，回滚返回 `blocked`，不会覆盖新根。旧无 root 回执仍可只读解析，但恢复 helper 只允许 root-bound 回执。

## Linux 并行验证

`evidence/run_stage15_auth_stress.py` 并行运行 alpha、beta、gamma 三个项目。每个项目验证合法本地写入、篡改 confirmation、跨项目授权、改动作、重复调用、receipt recovery 和故意失败停止；foreign root 不应出现任何文件。另有 moved-root probe 验证回执路径绑定。

## 限制

输入与确认均为 synthetic fixture；没有 provider、模型、密钥、网络、部署、远程 Git 或 managed loader 观测。Python 仍是同用户子进程，`sandbox=false`。`confirmation_binding` 未签名，source fingerprint 也不是签名；它只覆盖列出的本地 source 文件，不证明 symlink 外部目标或动态依赖闭包未变化。旧历史回执只可按兼容策略读取，不能作为新恢复授权。未做断电/崩溃耐久性、Windows/macOS 或真实用户审批验收。
