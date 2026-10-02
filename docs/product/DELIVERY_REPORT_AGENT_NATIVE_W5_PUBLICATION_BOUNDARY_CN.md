# W5 基于证据的公开发布边界阶段报告

更新日期：2026-10-02。事务：`agent-native-w5-publication-boundary`。本阶段只生成离线 publication preview，不发布、不部署、不写远端 Git、不连接 Provider/connector，也不读取凭据。

## 公开发布最小证据集

`opencoding.publication_boundary` 要求以下最小证据集：`source_revision`、成功且可核对的 `test_baseline`、detached `independent_review`、只读隐私 `privacy_audit` 和可回滚的 `rollback_plan`。测试计数必须满足 total = passed + skipped + failures + errors，且 returncode、failures、errors 都为零；独立复核必须有完整 focused pass。

## 事实与未验证标记

publication preview 将平台兼容性、真实用户验收、许可证/贡献审核和公开范围审核分别标为 `observed`、`unverified` 或 `blocked`。当前平台兼容性与真实用户验收保持 `unverified`，合成矩阵不会升级为真实证据；历史记录也不会被当作当前公开证据。

## Gate、回滚和输出

preview 的状态固定为 `blocked_human_gate`，`gate.recorded=false`，并明确 `publish_executed=false`、`release_executed=false`、`deployment_executed=false`。摘要只包含 revision、计数、事实状态、回滚范围和 digest，不包含 Prompt、Cookie、Token、API Key、用户内容或原始文件内容。回滚计划只能是人工可审阅的非自动范围，例如 `git-revert-w5-boundary`。

测试证据：全量 `python -X utf8 -m unittest discover -s tests -p 'test*.py' -v` 为 `Ran 693 tests in 162.144s`、`OK (skipped=9)`，即 total=693、passed=684、skipped=9、failures=0、errors=0、returncode=0；扩展定向回归为 49/49。原始日志分别位于 `/workspace/stage28-validation-20261002/w5-publication-full/` 和 `/workspace/stage28-validation-20261002/w5-publication-focused/`。

独立 detached worktree 从提交 `929ffde` 完成复核：49/49 定向测试、离线 Cargo 4/4，skill verifier 为 `format_valid_project_discovered_host_unverified`，`project_loader_exercised=true`、`host_loaded=null`。独立原始日志位于 `/workspace/stage28-validation-20261002/w5-publication-independent/`。

当前唯一人工 Gate 是公开发布前确认 exact revision、公开文件范围、事实/未验证声明、隐私审计、许可证/贡献入口、回滚方案和发布目标。没有该 Gate，preview 只能作为只读审计记录。
