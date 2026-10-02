# Stage14：跨项目重复执行、来源绑定与恢复边界压力验证

Stage14 在 Stage13 的 runner/恢复契约上增加了可选但本阶段强制使用的源码指纹：`--expected-source-sha256`。它把审阅的可执行 OpenCoding 源（`opencoding/`、两个 skill 资源目录和两个 runner 脚本）与 action 文件 SHA 一起绑定。源码或动作字节漂移时，在安装 Codex home 或创建执行根目录前返回 `blocked`。

## Linux 离线压力夹具

`evidence/run_stage14_stress.py` 在三个互不嵌套的项目根并行运行作者预写的不同小项目动作。每个项目：

- 使用独立 Codex home、execution root 和 action 文件；
- 首次以 `repeat=3`，随后用新的 home 对同一 action snapshot 再执行两次；
- 真实创建项目文件并运行本地 Python 测试；稳定文件写入的最终 SHA 收敛；
- 同时写入一个计数文件，明确证明 arbitrary Python 会重新执行（计数为 5），因此没有宣称通用幂等或去重；
- 由另一个进程重新打开 transaction receipt，回滚旧文件/新增目录，再次回滚并检查无 residual。

另有失败项目验证：第二个动作失败后，第三个动作和剩余 repeat 均停止；receipt 回滚只恢复 receipt 覆盖的文件，失败 Python 动作留下的 `failer.py` 明确保留，避免把局部回滚误称为全局恢复。

源码复制后修改 skill 文档、缺少源码指纹、根目录重叠三种情况都在副作用前阻断。矩阵输出包含每个项目的 action/source SHA、run IDs、计数、恢复报告和隔离断言。

当前入口与历史证据的关系见 `evidence/STAGE14_EVIDENCE_INDEX.md`；其中旧版
`evidence/run_skill_contract.py` 只为历史溯源保留，不用于新执行。

## 解释边界

本阶段使用合成输入和 `--confirm-synthetic`，没有真实用户审批、provider、模型、密钥、网络、部署或远程 Git。`managed loader` 仍为 `null/unobserved`。Python 模块以同一用户子进程运行，`sandbox=false`；源码指纹是内容绑定，不是签名或安全沙箱。重复执行的幂等性只对本阶段明确检查的文件写入成立，不能推广到任意 Python 副作用。恢复不是断电/崩溃耐久性测试，也不撤销任意 Python 副作用。
