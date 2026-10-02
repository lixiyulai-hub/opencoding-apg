# OpenCoding Stage13 delivery

Stage13 在 Stage12 契约之上验证跨项目输入变化、动作文件变化、重复执行和中断后恢复。

- `run_skill_contract.py` 增加 action/source review SHA256 绑定、全量 preflight、`--repeat 1..10`、失败停止；重复运行得到新的 run id 和 receipts。
- 新增 `recover_skill_transaction.py`，只读取显式 execution root 的 transaction receipt 并执行受绑定的文件回滚；恢复可由另一个进程重复调用。
- `readinglog-v2` 与 `todo-v2` 两个不同项目、不同 action file 均通过；旧 SHA 漂移被拒，新 SHA 重新审阅后才可执行；项目目录无交叉文件。
- 缺少 `--confirm-synthetic` 时返回 `blocked`、exit code 2 且无目标文件；恢复场景回滚后 `residual_paths=[]`。
- 定向测试：原 125 项核心加 6 项跨项目/漂移/重复/恢复/失败停止契约测试，共 131/131 通过；本阶段仍为 Linux、synthetic、no-model、no-external，managed loader 未观测。

详细说明见 `source/docs/product/STAGE13_CROSS_PROJECT_RECOVERY_CN.md`，机器矩阵见 `evidence/STAGE13_MATRIX.json`。
