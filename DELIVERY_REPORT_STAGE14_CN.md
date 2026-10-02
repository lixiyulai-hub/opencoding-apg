# OpenCoding Stage14 delivery

Stage14 基于 Stage13 增加跨项目重复执行与恢复边界压力验证，保持 Linux 离线、synthetic confirmation 和无外部服务。

- `opencoding.source_identity.source_fingerprint` 与 `--expected-source-sha256` 将审阅的可执行 source 绑定到 action 执行；源码修改、缺少 source SHA、根目录重叠均在安装/写入前阻断。
- 三个互不嵌套项目并行运行：每个项目首次 `repeat=3`，再用新 Codex home 对相同 action snapshot 执行两次。稳定文件写入最终 SHA 收敛；计数模块由 3 增到 5，明确 repeat 是重新执行，不承诺 arbitrary Python 幂等或去重。
- 每个项目用独立进程重开 transaction receipt，回滚旧文件和新增目录，并再次回滚；故意失败的项目停止后续 action/repeat，receipt 回滚不删除失败 Python 留下的 `failer.py`。
- Stage13 原矩阵仍 11/11 true；Stage14 stress acceptance 11/11 true；核心定向测试 133/133 通过。

完整机器证据见 `evidence/STAGE14_STRESS.json`、`evidence/STAGE14_STAGE13_MATRIX.json` 和 `evidence/STAGE14_SOURCE_FINGERPRINT.txt`；说明见 `source/docs/product/STAGE14_CROSS_PROJECT_STRESS_CN.md`。

边界保持：输入/确认是合成夹具，model/provider/external/credentials/network/deploy/remote Git 均未使用；managed loader 仍 unknown/null；Python 为同用户子进程、`sandbox=false`，无任意 Python 自动回滚，也没有断电/崩溃耐久性结论。主仓库不变。
