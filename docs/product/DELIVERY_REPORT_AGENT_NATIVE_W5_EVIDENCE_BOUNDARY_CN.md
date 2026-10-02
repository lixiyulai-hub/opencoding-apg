# W5 证据边界与只读审核阶段报告

更新日期：2026-10-02。事务：`agent-native-w5-evidence-boundary`。本阶段只整理离线证据分类、平台兼容性声明和外部审核投影，不启动真实平台、Provider、connector、凭据、网络或部署。

## 三类证据

`opencoding.evidence_boundary` 固定使用 `real`、`synthetic`、`unverified` 三类标记：

- `real` 必须同时有 `real_user=true` 和当前 `live_verified=true`；
- `synthetic` 必须显式 fixture 标记，且不能带 live/provider/external 标记；
- 其他输入保持 `unverified`，历史测试、平台名称和工具链存在都不能升级状态。

## 平台兼容性

每个平台声明包含 `status=observed|unverified|blocked`、`claim`、`execution_observed`、`toolchain_status`、宿主族和证据类别。W5 的 Windows、macOS、iOS、Android、Web、小程序和 CLI 全部维持 `unverified`、`contract_only`，没有目标执行、浏览器、SDK、签名、打包或部署证据。

## 外部审核投影

`build_read_only_audit_snapshot` 只返回状态、计数、平台边界、证据类别和 payload SHA-256。输出设置 `read_only=true`，且 `raw_values_included=false`、`prompt_included=false`、`cookie_included=false`、`token_included=false`、`api_key_included=false`、`user_content_included=false`。原始 Prompt、Cookie、Token、API Key、用户回答、用户句子和自由文本不进入投影；投影函数不写文件、不调用网络、不消费授权。

## 测试证据

全量 `python -X utf8 -m unittest discover -s tests -p 'test*.py' -v` 已通过：`Ran 688 tests in 161.611s`、`OK (skipped=9)`，即 total=688、passed=679、skipped=9、failures=0、errors=0、returncode=0。原始日志位于 `/workspace/stage28-validation-20261002/w5-evidence-full/`。扩展定向证据边界/W5 矩阵/W4/packaging 回归为 44/44，原始日志位于 `/workspace/stage28-validation-20261002/w5-evidence-focused/`。

独立 detached worktree 从提交 `11a1e55` 完成复核：44/44 定向测试、离线 Cargo 4/4，skill verifier 为 `format_valid_project_discovered_host_unverified`，`project_loader_exercised=true`、`host_loaded=null`。独立原始日志位于 `/workspace/stage28-validation-20261002/w5-evidence-independent/`。

## 风险、回滚与人工 Gate

只读摘要是审计边界，不是身份认证、签名、沙箱或平台兼容承诺。若来源字段冲突，分类器 fail closed；若平台执行证据不足，状态保持 `unverified`。回滚为 `git revert` 本事务提交，既有 W5 矩阵和外部状态不受影响。唯一人工 Gate 是将合成输入替换为真实用户资料，或启用具体平台/provider/connector 前确认 exact root、targets、data、credentials、cost、network action 和 digest。
