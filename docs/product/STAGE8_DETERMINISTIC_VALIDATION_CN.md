# Stage08：确定性并发与跨进程 busy-lock 验证

Stage08 沿用 Stage01–07 的跨平台 Agent 核心和隔离候选，只修复 Linux 可复现的验证缺口，不引入 provider、密钥、外部服务或平台专属产品假设。

## 并发预约

`test_p06_concurrent_dispatch_blocked` 原先只用两个线程同时调用。因为候选流程很快，获胜线程可能在另一线程预约前释放锁，导致第二次调用进入候选暂存并返回 `failed`，测试偶发失败。

现在测试让获胜线程在适配器派发点等待，直到竞争调用观察到 `already_running`，再释放获胜线程。这样锁仍由真实 `run_generic_app` 持有，测试验证的是原子预约和 active-run 保护，不是 sleep 或固定返回值。十次单测复跑全部通过。

## busy-lock

原测试在同一进程打开 SQLite `BEGIN EXCLUSIVE` 后调用 `inventory(root)`。POSIX SQLite 锁与进程关联，读取同一数据库的临时文件描述符可能释放该进程锁；所以 CLI 子进程看到 ready 是夹具失效，不是产品通过。

现在使用 `multiprocessing` 的 `spawn` 上下文，在独立进程持有 `BEGIN EXCLUSIVE`，父进程在锁建立后运行真实 `python -m opencoding --status --json`，最后释放锁并核对目录未变化。该夹具不依赖 Linux `/proc/locks` 的可见性，Windows 也沿用同一独立进程边界。

## 边界

这两个修复只改变测试可观测性，不改变 OpenCoding 执行授权、路径校验、事务、skill loader 或 LocalAgentAdapter。Stage08 仍不声称 managed Codex loader 已加载；真实模型、provider、密钥、外部服务、部署、远程 Git、合并和 Release 均未执行。
