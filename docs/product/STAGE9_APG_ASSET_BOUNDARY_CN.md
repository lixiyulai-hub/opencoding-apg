# Stage09：APG review/release 资产边界

Stage09 只读核查交接源缺失项，不创建任何猜测的 `artifacts/apg-*` 文件。核查脚本为 `scripts/apg_independent_review.py`，完整机器可读结果见 `evidence/STAGE9_APG_ASSET_BOUNDARY.json`。

## 结论

脚本、docs 和 tests 均存在；缺失项全部集中在 26 个历史 artifact 文件：`RESULT.json`、`MODIFIED_FILE`、`DIFF_FILE`、`VERIFICATION.txt`、`ROLLBACK.sh`。因此 `apg_independent_review.py` 的整体结果仍为 `BLOCK`，但阻塞的是当前候选的旧 APG review 证据闭合。当前核心路径无直接引用，125 项定向测试和固定本地 E2E 均可运行；这不等于一般任务或全平台验收。

| 组 | 缺失内容 | 对当前核心影响 | 边界 |
| --- | --- | --- | --- |
| deployment preview | 3 个 artifact | 不阻断 | 离线部署/发布预览与 disposable rollback 的历史证据；不执行部署 |
| autonomous policy | 4 个 artifact | 不阻断 | APG 自主 PRG policy fixture；当前 product_loop/generic_run 有独立证据 |
| beginner executor | 4 个 artifact | 不阻断 | APG beginner executor fixture；Stage05–08 LocalAgentAdapter/Linux E2E 不依赖 |
| adaptive Git checkpoint | 4 个 artifact | 不阻断 | 可选本地 checkpoint preview；远程 Git 本阶段禁止 |
| adaptive Git controller | 3 个 artifact | 不阻断 | 可选 Git controller 连接 fixture |
| adaptive Git ledger | 3 个 artifact | 不阻断 | 可选 ledger replay fixture |
| adaptive Git ledger persistence | 5 个 artifact | 不阻断 | 可选 ledger persistence preview fixture |

## 当前核心证据

- Stage09 core focused：125 tests 全部通过，覆盖 Codex host、skill contract、LocalAgentAdapter、product loop、规划、CLI、generic run、scheduler、service。
- Stage09 Linux host E2E：`format_valid=true`、`resource_discovered=true`、`host_adapter_loaded=true`、`workflow_followed=true`；合成输入/确认明确标注，`model_used=false`、`external_actions=false`，失败→修复→通过→回滚残留为空。
- APG review 不写候选，但会在 `TemporaryDirectory` 中两次执行 ledger persistence smoke check。其 `external_actions_executed=false` 是旧报告字段，不能解释为整个测试零本地写入；无 provider、网络、密钥、发布或真实部署。

缺失的 APG artifact 不应通过复制空文件、猜测历史结果或伪造回滚脚本来消除。若未来需要 APG review PASS，必须恢复同版来源证据；这与当前跨平台 skill/Agent 核心验收是两个边界。

## 来源核对后的更正

“候选缺失”不等于“环境无来源”。`evidence/STAGE9_ASSET_PROVENANCE.json` 对比原始内层 ZIP（SHA256 `262e648873ac7f1021648602a907cb88e0b6213c1d6af864ff5280279da9f92a`）与本地 Git `91a20392a856ff4717d230467d32fc154bcb82b1`：

- 26 个 APG artifact 和 6 个 Rust/frontend 文件在原交接 `source/` 中均未提供；在旧 Git HEAD 中全部存在，逐项保留 blob、bytes、SHA256。
- 26 个 APG 共用 scripts/docs/tests 与 HEAD、原交接源码逐字节相同。旧 artifact 仍是旧时期证据，不能因此宣称当前候选全验收通过。当前没有导入旧 artifact，没有执行任何旧 `ROLLBACK.sh`。
- `docs/apg/APG_TEST_FIXTURE_BOUNDARY.md` 明确 `services/domain`、`apps/miniapp` 是惰性 APG 测试夹具。内容契约是旧的儿童/家长任务流；不是 Windows 依赖，也不是 OpenCoding 的 Agent 核心。
- `pyproject.toml` 只安装 `opencoding`，依赖为空；核心 `opencoding`、`skills`、`.agents/skills` 未出现这些路径或 APG review/deployment 脚本的直接引用。文本检查不能替代完整动态依赖证明。
- `cargo` 当前确实不在 PATH，但仅阻塞旧 Rust fixture 的编译测试，不阻断当前 Python 核心；不需要要求用户重新提供本环境已有的 APG/Rust/frontend 文件。

本阶段没有修改运行时代码或测试判定，不重跑无关全量；Stage08 的 603 tests / 1 failure / 6 errors / 9 skips 是继承日志，不是 Stage09 新全量成绩。Stage09 实际重验为 125 tests 及固定 Linux E2E。该 E2E 是合成回答与固定修复代码的真实子进程执行，不证明通用 AI 生成或宿主自动遵循 skill。

后续可在另一个隔离目录比对旧资产内容与当前契约、单独验证可选旧夹具，保留来源后再决定是否纳入发行测试；无需覆盖候选或等待 Windows。核心开发不以旧 APG review PASS 为全局前提。
