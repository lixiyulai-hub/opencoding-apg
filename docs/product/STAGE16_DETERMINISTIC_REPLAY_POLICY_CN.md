# Stage16：本地一次性确认与重放策略

Stage15 暴露了一个真实边界：confirmation binding 虽绑定了 root、targets、动作摘要和 expiry，但同一授权仍可重复调用。Stage16 增加了不依赖密钥或外部服务的本地一次性消费策略。

## 规则

首次执行前，适配器在 `<execution-root>/.opencoding/authorizations/<binding>.json` 以原子 `O_EXCL` 创建消费记录。相同 binding 再次执行返回 `authorization_replayed`，不会调用 Executor；回滚事务不会删除消费记录，因此不能通过回滚绕过一次性确认。错 root、错动作、篡改 confirmation 或过期授权仍在消费前拒绝，foreign 项目不会创建 claim。

这是同用户本地策略，不是签名、身份认证、跨进程远程锁或宿主级安全边界。拥有同一项目写权限的进程可以删除 claim 文件；真实用户 Gate 仍需由更高层宿主实现。每次新的确认必须生成新的 binding/confirmation，旧 binding 不能重放。

## 验证

`tests/test_stage16_replay_policy.py` 覆盖一次消费和回滚后不可重放；`evidence/run_stage15_auth_stress.py` 的三项目并行结果现要求每个项目 tamper/cross-project/changed-action/replay 全部拒绝，并保留失败停止、root-bound receipt recovery 和无跨项目污染检查。Stage16 还把旧 P06 并发夹具改成先让首个调用进入真实派发点再启动竞争调用，避免夹具自身的预预约调度竞态；生产 O_EXCL 锁逻辑未放宽。

仅使用 Linux、synthetic confirmation、无 model/provider/network/credentials/deploy/managed loader。Python 仍以同用户子进程运行，`sandbox=false`；未做断电/崩溃耐久性或真实用户审批验收。
