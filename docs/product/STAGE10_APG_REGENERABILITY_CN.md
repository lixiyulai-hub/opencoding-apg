# Stage10：旧 APG fixture 的内容/契约可重验性边界

Stage10 在不改主仓库、不导入候选、不执行旧 `ROLLBACK.sh` 的前提下，对旧 Git HEAD 的 APG fixtures 与当前隔离候选进行比对。机器结果见 `evidence/STAGE10_APG_CONTRACT_AUDIT.json` 和 `evidence/STAGE9_ASSET_PROVENANCE.json`。

## 先区分两个事实

- **候选缺失**：原始内层交接 ZIP 没有 26 个 `artifacts/apg-*` 文件，也没有 6 个 Rust/frontend fixture 路径。
- **来源存在**：本地 Git HEAD `91a20392a856ff4717d230467d32fc154bcb82b1` 有这些文件；旧 HEAD 的 APG 专项 suite 在新临时快照中 41/41 通过。旧 HEAD 来源不能自动变成同版交接证据，因此没有复制进候选。

`docs/apg/APG_TEST_FIXTURE_BOUNDARY.md` 明确 `services/domain` 和 `apps/miniapp` 是 APG 惰性测试夹具；它们不是 OpenCoding 的跨平台 skill/Agent 运行时。

## 分类矩阵

| 组 | 当前候选缺失 | 现有代码能否重新验证行为 | 能否恢复**同一历史证据** | 对跨平台核心 |
| --- | ---: | --- | --- | --- |
| deployment preview | 3 | 可以：ready/block 预览和 disposable `rollback_copy` 在临时目录可运行 | 不能从契约重造原始修改文件、diff、回滚发生时点 | 不阻断 |
| autonomous policy | 4 | 可以：auto、consequential-gate、FREEZE、脱敏和确定性 replay | 不能重造旧 `RESULT.json` 的路径哈希、命令输出、旧验证时间线 | 不阻断；是 APG 专项 |
| beginner executor | 4 | 可以：中文澄清、知识包、任务 lane、Gate、FREEZE 和脱敏 replay | 不能重造旧知识包哈希、doctor 路径、旧 receipt | 不阻断；Stage05–10 有独立核心 E2E |
| adaptive Git checkpoint | 4 | 可以：CHECKPOINT_RECOMMENDED、WAIT、FREEZE、重复抑制 | 不能重造历史 change id、fixture 字节和回滚事件 | 不阻断；可选 Git preview |
| adaptive Git controller | 3 | 可以：controller→checkpoint→ledger 的离线投影 | 不能重造旧 fixture 修改与回滚 receipt | 不阻断；可选 Git 集成 |
| adaptive Git ledger replay | 3 | 可以：append/reuse/freeze 的确定性投影 | 不能重造旧 ledger replay 发生时点和原始 fixture | 不阻断；可选 Git ledger |
| adaptive Git ledger persistence | 5 | 可以：临时根目录 `WRITE_CANDIDATE→PERSISTED→ALREADY_PERSISTED` | 不能重造原 preimage、receipt、时间线；新 apply 会产生本地临时写入 | 不阻断；可选本地 ledger |

精确缺失路径、旧 HEAD bytes/SHA256、交接包是否包含、候选是否导入，见 `STAGE10_APG_CONTRACT_AUDIT.json`。当前审计的 7 组 fresh semantic checks 均为离线调用；ledger persistence 只写入自动删除的临时目录。

## 哪些“可以重新生成”

可以从现有 scripts/tests 重新生成一份**新的、明确带新运行时间和新输入的离线证据**：预览状态、Gate 分类、任务图、checkpoint/ledger 投影、哈希计划、临时 ledger 原子写入和幂等回放。旧 HEAD APG suite 的 41/41 结果证明这些契约仍可执行，但不证明当前交接候选包含旧证据。

`MODIFIED_FILE`、`DIFF_FILE` 和 `ROLLBACK.sh` 的文本也可以由工程师依据契约重新编写，但那是新 fixture，不是原历史事件。若要恢复旧字节，应从有 provenance 的同一 Git commit 或交接包恢复并单独标记版本，不能把新文本命名成历史证据。

## 哪些永远不能补造

以下事实只能由当时的权威记录证明，不能靠重跑脚本、复制模板或猜测补回：

- 当时用户/所有者的确认、授权、撤销和预算决定；
- 当时 provider、网络、主机或外部动作的真实结果；
- 旧 receipt 的时间、进程路径、preimage/postimage、literal test output 和环境状态；
- 原始 fixture 变更及其 rollback 事件发生过这一历史事实；
- 当前环境从未返回的 managed Codex `skills/list` 结果。

因此本阶段不把 APG `BLOCK` 改成 PASS，不把旧 HEAD 文件复制到候选，也不把临时 ledger 写入说成外部动作或零本地写入。

## 当前核心重验

- Stage10 core focused：125 tests，全部通过。
- Linux host E2E：skill format/discovery/adapter load/workflow 全部通过；合成输入和确认明确标记，`model_used=false`、`external_actions=false`，失败→修复→通过→回滚无残留。
- managed loader、Windows/macOS、真实 provider、Rust `cargo`、外部服务和部署仍未验证。
