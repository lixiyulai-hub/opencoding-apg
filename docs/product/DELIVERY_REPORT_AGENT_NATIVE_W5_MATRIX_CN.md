# W5 合成小白与平台边界矩阵阶段报告

更新日期：2026-10-02。事务：`agent-native-w5-synthetic-matrix`。本阶段只做离线验收矩阵、平台判断和受摘要绑定的波次记录，不接真实平台、Provider、connector、账号、凭据或网络。

## 矩阵范围

新增 `opencoding.w5_acceptance`，把现有中文 intake 的 12 个澄清问题、推荐计划的 8 类能力和平台适配状态组合为一个可复核矩阵。三个合成一句话需求覆盖：

- 离线命令行清单：本地持久化，服务器不需要；
- 多人网页任务：跨设备、附件、登录、后台、通知和数据库需要；
- Windows + Mac 家庭记录：保留多平台低置信度草案和 `multiple_targets` 未解决项。

平台矩阵覆盖 Windows、macOS、iOS、Android、Web、小程序和 CLI。Windows/macOS/Web 通过现有 contract adapter 报告 `unverified`；其余目标只作为规划输入，明确没有目标执行器。任何工具链观察都不能升级为目标执行。

## 波次与权限

`SyntheticAcceptanceMatrix` 固定记录 `clarify → plan → preview → execute → verify → rollback → review` 七个波次。preview 产生摘要；approve 必须显式 `synthetic=true` 且匹配摘要；权限查询不等同执行授权。verify 必须带正数测试证据，rollback 只接受 `receipt_covered_files_only`。所有记录带 `synthetic=true`、`real_user=false`、`provider_used=false`，矩阵本身不写文件。

真实本地动作仍必须走现有 `LocalAgentAdapter`、产品 preview 和授权；真实目标或 connector 激活仍由 W4 的独立人工 Gate 控制。

## 测试证据

`tests/test_w5_acceptance_matrix.py` 覆盖 6 项：问题/能力/平台覆盖、平台判断、摘要绑定的预览与波次、测试与回滚证据、网络/进程哨兵、敏感/不完整 fixture 拒绝。测试不会建立 socket、子进程、凭据或外部连接。

全量回归已完成：`python -X utf8 -m unittest discover -s tests -p 'test*.py' -v` 原始输出为 `Ran 682 tests in 160.414s`、`OK (skipped=9)`，即 total=682、passed=673、skipped=9、failures=0、errors=0、unittest returncode=0。原始日志位于 `/workspace/stage28-validation-20261002/w5-matrix-full/`；此前采集器只解析 stdout（unittest 将详细输出写到 stderr）而留下 collector returncode=2，已在 `raw-summary.txt` 明确区分，不能解释为测试失败。定向 W5/W4/packaging 回归为 31/31，原始日志位于 `/workspace/stage28-validation-20261002/w5-matrix-focused/`。detached 独立验证将在本阶段提交后执行并补入路线图。当前 R3/W4 既有证据保持不变，不被合成矩阵升级为真实用户验收。

## 风险、回滚和人工 Gate

矩阵是内部验收记录格式，不是任何平台或服务的兼容承诺；目标执行器仍为 `unverified`。回滚为 `git revert` 本阶段提交，矩阵本身没有持久外部副作用。唯一人工 Gate 是将任一合成场景替换为真实用户需求，或启用具体目标/Provider/connector 前，确认 exact root、targets、data、credentials、cost、network action 和 digest。
