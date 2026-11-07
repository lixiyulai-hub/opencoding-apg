# APG Gate 输出契约：本地候选

以本机 APG 0.3.0 的 37 个 Python 源文件为基线，原始字节摘要见 BASELINE.json。此目录是隔离的修复候选，不修改全局技能，不随 OpenCoding wheel 自动安装，不代表完整 APG 发行包（未包含 init/adopt 模板）。

仅为 `command` Gate 增加 `options.output_dir = ".cache/gate-runs/<名称>"`。控制器在每次实际执行前创建唯一子目录，通过环境变量 `APG_GATE_OUTPUT_DIR` 传给测试程序。SQLite、pytest 临时文件和日志必须明确使用该变量指向的目录；原有测试写任意 `.cache` 位置仍失败。输出权限参与 Gate contract 和 policy digest；必须绑定当前 policy 才能使用。

```powershell
python -B -X utf8 compat/apg-output-contract/check.py --root <项目目录> --phase full
```

测试命令示例（policy 的 command 数组）：`python -B -c "import os; from pathlib import Path; Path(os.environ['APG_GATE_OUTPUT_DIR'], 'probe.txt').write_text('ok')"`。

默认没有声明输出的检查仍只读。声明后逐 Gate 检查全工作区；不忽略 `.cache`、`.git` 或其他目录。产物目录、文件摘要、大小、Gate contract、policy 绑定、命令退出码及范围结果写入回执。失败保留产物与回执，并停止本轮后续 Gate。

候选边界：仅 phase 检查；plan-bound/feedback-loop 与 output_dir 组合明确拒绝。完整快照限制为 10,000 项、128 MiB，超限拒绝执行；大仓库需后续改进有界扫描。检测持久化越界、链接、hardlink、目录身份替换和 policy 漂移，不承诺隔离恶意同权限进程或发现写后复原。原始 APG 子进程的取消/进程树限制未在本候选扩展。

通过测试不等于用户验收或全局升级授权。后续在实际项目配置中接入输出变量，再进行组合验收。
