# W2 Entry V1 Worker Report

日期：2026-09-06。工作树：`E:/儿童知行星球/.tmp/opencoding-product-foundation-20260905/wt-entry`。分支：`w2-entry`。

## 交付

已在上一失败 worker 留下的 partial 基础上完成 W2 Entry V1：

- `python -m opencoding` 中文向导，支持显式 `--root`、恢复、列会话、只读预览、改答差异和回滚。
- 服务层复用 Session -> Recommendation -> TaskPlan/Documents -> Preview -> Apply/Rollback。
- frontier 按 `GRILLING_ADAPTATION_20260905.md` 的依赖派生，不新增持久化 schema。
- preview 使用只读快照，未创建 metadata、guard、会话或日志；apply 在既有同会话 OS 锁临界区内复查 revision、业务图和 file plan。
- approval 严格绑定 root、session/revision、recommendation、task plan、documents、file plan、service digest、exact targets 和 local-only action context。
- 无 Host、Provider、网络、凭据、scheduler、真实外部服务或依赖安装。
- rollback 委托既有 `rollback_changes`，保留 transaction receipt，并尊重用户后续修改。

## 验证命令

环境：`PYTHONDONTWRITEBYTECODE=1`、`CARGO_NET_OFFLINE=true`，`TEMP/TMP=artifacts/w2-entry-v1/test-temp/`，`PYTHONIOENCODING=utf-8`。

| 命令 | 退出码 | 实际计数 |
| --- | ---: | ---: |
| `python -X utf8 -m unittest` | 0 | 166 |
| `python -X utf8 -m unittest discover -s tests -p test_*.py` | 0 | 166 |
| `python -X utf8 -m unittest tests.test_product_service` | 0 | 7 |
| `python -X utf8 -m unittest tests.test_product_cli` | 0 | 6 |
| `python -X utf8 -m unittest tests.test_product_sessions` | 0 | 20 |
| `python -X utf8 -m unittest tests.test_product_integration` | 0 | 14 |
| `python -X utf8 -m py_compile ...` | 0 | 9 files compiled |
| `git diff --check -- <W2 paths>` | 0 | no whitespace errors |
| controller `doctor <wt-entry> --json` | 0 | read-only proof passed |

日志位于本目录的 `VERIFICATION_*_20260906_1.log`。工作树当前既有测试实际为 153 项；本次新增 13 项测试后实际为 166 项，未回退且默认命令非零即失败。

## 真实行为证据

- CLI 使用真实 Python 子进程：拒绝确认保持业务文档未写入；确认后生成真实 AGENTS/memory/PRG/plan 等文档；随后真实 CLI 回滚成功。
- CLI EOF/退出后可用同一 session id 恢复；`--change` 生成新 revision 并显示更新后的方案/差异。
- 服务 zero-write 断言同时比较目标根 inventory，未触碰已有 guard 字节或新 metadata。
- 审批篡改、未知字段、过期、目标变化、session revision 过期和业务文件漂移均 fail closed。
- 并发服务测试使用真实自有子进程、Event/Queue 和既有 OS 锁；锁占用时 competing save 明确返回 busy，apply 完成后不会静默交叉覆盖。
- 合成 token 在会话、JSON 服务状态和生成文档中脱敏；输出明确 Host 未接通。

## 范围与哈希

本次源码路径严格限于：`opencoding/__init__.py`、`opencoding/__main__.py`、`opencoding/cli.py`、`opencoding/service.py`、`opencoding/sessions.py`、`pyproject.toml`、`tests/test_product_cli.py`、`tests/test_product_service.py`、`tests/test_product_sessions.py`。新增证据仅位于 `artifacts/w2-entry-v1/`。完整 postimage 与冻结路径哈希见 `POSTIMAGE_SHA256_20260906.txt`。

`opencoding/sessions.py` 的 preimage 哈希为 `2570362623b22958fc1e5853403778f55ac361ff958d8cc3722ab0470628ff4f`，postimage 哈希为 `dfda44bf9813f63b07ede20cb608d83f59636829b32bd1443c58c4618da1232d`；变更仅为只读快照和同一 OS 锁临界区 API。`tests/test_product_sessions.py` 保持 preimage/postimage 哈希 `b04b52cdf060462f3bfbbbe01e2bc0907341596b18d5defb037dce0222a45772`。

## 限制与边界

- 持久撤销/一次性审批消费、scheduler、Host、Provider、网络、凭据、真实数据、部署和发布仍未实现，属于后续精确事务边界。
- 本次未执行 `pip install` 或任何外部依赖安装；`pyproject.toml` 仅声明本地标准库包元数据。
- doctor 保留既有 `.governance/progress` 生成文件 warning；未删除或修改它以及任何历史 evidence/cache/untracked 文件。
- 有一次收口命令因误在治理 skill 目录运行而退出 1（工作目录错误，compile 找不到源码）；随后在正确工作树重跑通过，未产生源码写入。
- 本报告不声明主协调已接受，也不合并回 integration，不启动 W2-B/W3。
