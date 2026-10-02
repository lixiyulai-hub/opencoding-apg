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

## 证据边界与外部审核输出

新增 `opencoding.evidence_boundary`，把证据明确标为 `real`、`synthetic` 或 `unverified`。`real` 必须同时有真实用户标记和当前执行验证；只有 fixture 标记的矩阵是 `synthetic`；其余状态保持 `unverified`，不会因历史测试、工具链存在或平台名称而升级。

平台声明单独记录 `status=observed|unverified|blocked`、`claim`、`execution_observed`、`toolchain_status` 和宿主族。当前七个平台全部是 `unverified`/`contract_only`，没有浏览器、SDK、签名、打包、部署或目标执行证据。

preview/report 附带 `external_audit` 只读投影：只输出状态、摘要、平台边界、计数和 payload SHA-256，不复制 Prompt、Cookie、Token、API Key、用户答案、句子或其他自由文本；`raw_values_included=false`、`prompt_included=false`、`cookie_included=false`、`token_included=false`、`api_key_included=false`、`user_content_included=false`。审计投影不会写文件、调用网络或改变授权。

## 测试证据

`tests/test_w5_acceptance_matrix.py` 覆盖 6 项；`tests/test_evidence_boundary.py` 覆盖三类证据、平台声明、只读审计输出和 Prompt/Cookie/Token/API Key 脱敏。测试不会建立 socket、子进程、凭据或外部连接。

全量回归已完成：`python -X utf8 -m unittest discover -s tests -p 'test*.py' -v` 原始输出为 `Ran 688 tests in 161.611s`、`OK (skipped=9)`，即 total=688、passed=679、skipped=9、failures=0、errors=0、unittest returncode=0。原始日志位于 `/workspace/stage28-validation-20261002/w5-evidence-full/`；此前采集器只解析 stdout（unittest 将详细输出写到 stderr）而留下 collector returncode=2，已在 `raw-summary.txt` 明确区分，不能解释为测试失败。扩展定向 W5 证据边界/W5 矩阵/W4/packaging 回归为 44/44，原始日志位于 `/workspace/stage28-validation-20261002/w5-evidence-focused/`。detached 独立验证已从提交 `4979dc1` 的干净 worktree `/tmp/opencoding-stage28-w5-detached` 完成：定向 31/31（skipped=0、failures=0、errors=0、returncode=0），services/domain 离线 Cargo 为 4 passed、0 failed、0 doc-tests，skill verifier 为 `format_valid_project_discovered_host_unverified`，`project_loader_exercised=true` 但 `host_loaded=null`。独立原始日志位于 `/workspace/stage28-validation-20261002/w5-independent/`。当前 R3/W4 既有证据保持不变，不被合成矩阵升级为真实用户验收。

## 风险、回滚和人工 Gate

矩阵是内部验收记录格式，不是任何平台或服务的兼容承诺；目标执行器仍为 `unverified`。回滚为 `git revert` 本阶段提交，矩阵本身没有持久外部副作用。唯一人工 Gate 是将任一合成场景替换为真实用户需求，或启用具体目标/Provider/connector 前，确认 exact root、targets、data、credentials、cost、network action 和 digest。
