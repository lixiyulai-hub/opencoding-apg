# OpenCoding Stage16 delivery

Stage16 修复 Stage15 的确认重放边界。

- `LocalAgentAdapter` 以 root 内原子 claim 文件消费每个 confirmation binding；同一 binding 再次使用返回 `authorization_replayed`，回滚不会恢复可消费状态。
- 三项目并行 auth stress 现在覆盖合法执行、篡改确认、跨项目授权、改动作和重放拒绝；失败隔离与 root-bound receipt recovery 保持通过。
- 新增 focused replay tests，覆盖一次消费与回滚后不可重放；保留 confirmation binding 未签名、同用户本地 claim 可被拥有写权限者删除的诚实边界。
- 当前定向核心套件 `167/167` 通过；P06 夹具改为先进入真实派发点再启动竞争调用，并以 10 次重复验证。全量 `python -X utf8 -m unittest` 在 120 秒门限内未完成且此前输出含失败/错误标记，完整门禁保持未通过，详见 `evidence/stage16-full-unittest-v2.log` 与退出码 `124`。

无 provider、密钥、外部服务、网络、部署、远程 Git、managed loader 或真实用户审批；ZIP/证据仅内部保存。
