# Stage17：全量测试超时的有界诊断

Stage16 的 120 秒全量命令没有完成摘要。本阶段不放宽断言、不删除测试，也不导入缺失的历史 fixture；使用独立进程和有界分片确定超时来源。

## 结果

- 官方命令 `python -X utf8 -m unittest` 以 300 秒门限完成：617 tests，1 failure、6 errors、9 skipped，用时 180.146 秒，退出码 1。
- 51 个测试模块按 4 路并行、每模块 100 秒分片：46 个模块通过、5 个模块失败、0 个模块超时。
- `tests.test_product_commit_consistency_w0` 7/7 通过，用时约 72.584 秒。其中 `test_live_slow_owner_not_preempted_single_event` 按契约真实等待 stale threshold 60 秒以上，证明存活持锁者不会被年龄阈值抢占；这不是死锁。
- `tests.test_product_autorun` 14/14 通过，用时约 32.296 秒。Stage16 replay、CLI、scheduler、generic run、planning、skill contract 等主链分片均通过。

## 失败边界

保留以下失败，不伪造 PASS：

1. `tests.test_apg_independent_review`：26 个 `artifacts/apg-*` 历史 review evidence 缺失。
2. `tests.test_apg_release_orchestration`：缺失 `artifacts/apg-deployment-preview/MODIFIED_FILE` 等历史部署 fixture。
3. `tests.test_domain_contract`：`services/domain/src/lib.rs` 与 Cargo 工具链/manifest 不在当前候选。
4. `tests.test_frontend_contract`：`apps/miniapp/src/*` 不在当前候选。
5. `tests.test_verification_contract`：依赖上述 Rust/frontend 资产，无法验证旧跨项目契约。

这些失败属于交接资产或工具链门槛，不改变跨平台 skill/Agent 核心的 Linux 结果。旧 APG receipt、时间线、用户确认、Windows/Rust/frontend 结果均未补造。

完整日志与机器可读分类见 `evidence/STAGE17_TEST_CLASSIFICATION.json`、`evidence/stage17-full-unittest-300s.log`、`evidence/STAGE17_MODULE_SHARDS.json` 和 `evidence/stage17_module_shards_v2/`。本阶段仍未执行 provider、密钥、外部网络、真实浏览器、部署或 managed loader。
