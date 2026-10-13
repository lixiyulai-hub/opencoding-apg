# Agent-Native Local Use

This guide is for Codex and other agents that use OpenCoding inside a local repository. It describes one local entry surface of the complete AI coding governance workflow: Python and CLI APIs for sessions, project documents, TaskPlans, controlled execution, verification, recovery, and optional AI evaluation/generation. The package also includes an optional loopback browser workbench (`python -m opencoding.workbench`); agents do not need it to use the local APIs. See `OFFLINE_INSTALLATION.md` for installed-package startup and AI configuration boundaries. No MCP server, plugin, or embedded Codex App compatibility is claimed here.

## Scope and authority

Every call uses an explicit existing local project root. The caller must establish that the root, user goal, reviewed preview, and any local write are within the user's authorization. An `approved` field inside an approval object is part of the local integrity check; it is not evidence that a user authorized an arbitrary scope, and it is never authorization for external work.

The local session/document and scheduler flows below can run without a provider or network. Optional AI evaluation and generation interfaces can call configured providers, so this is not a package-wide offline-only product and no provider is implied by the local APIs. Establish separate authorization for provider calls, credentials, costs, real data, deployment, and publication. Path checks and application locks are not an operating-system sandbox. A TaskPlan is a planning result, not a scheduler success record or permission to run arbitrary code.

## Discover the local entry points

The CLI entry is:

```powershell
python -m opencoding --root C:\path\to\authorized-project --help
```

Its local session modes are the interactive flow, `--resume SESSION_ID`, `--list`, `--preview SESSION_ID`, `--evaluate SESSION_ID`, `--adopt-plan SESSION_ID`, `--change SESSION_ID QUESTION_ID ANSWER`, `--rollback TRANSACTION_ID`, and `--status [--task-id TASK_ID] [--json]`. Additional autonomous modes include `--autorun lendreg`, `--cancel-run RUN_ID`, `--query-run RUN_ID`, and `--rollback-run RUN_ID`; consult `--help` and `AUTORUN_QUICKSTART.md` for their distinct authorization and delivery checks. `--mock-ai` is a simulation, not proof of provider connectivity or a successful delivery. There are no `--create`, `--apply`, `--run-next`, `--recover`, or `--requeue` aliases.

The Python entry points are `opencoding.service` for the document/session loop and `opencoding.scheduler` for explicit local scheduler tasks. They are ordinary in-process APIs, not a wire protocol. Use return values and exception types, not human-readable CLI text, for control flow.

## Session and document loop

The following service functions have these signatures and effects:

| Function | Effect |
| --- | --- |
| `session_view(root, session_id, *, include_preview=False)` | Read session and derived view. |
| `list_sessions(root)` | Read saved sessions. |
| `preview_session(root, session_id)` | Read the complete plan, exact targets, and diff without applying documents. |
| `execution_status(root, task_id=None)` | Zero-write scheduler snapshot; it does not initialize, migrate, or recover scheduler state. |
| `create_session(root, goal)` | Writes a persisted local session. |
| `submit_answer(root, session_id, expected_revision, question_id, answer)` | Writes a revisioned answer, or returns `busy`/`stale`. |
| `approve_preview(preview, *, expires_in_seconds=300)` | Validates a complete ready preview and returns an in-memory, expiring local approval object. |
| `apply_approved(root, approval)` | Revalidates identity, revision, digests, targets, expiry, and file-plan drift before local document writes. |
| `rollback(root, transaction_id)` | Performs the transaction layer's local rollback. |

The normal agent sequence is shown below as a calling pattern, not an automatically executed script. `goal` and each `answer` must come from the user or an authorized local task context. The conflict branch returns to the caller before it can obtain a preview or approval.

```python
from pathlib import Path
from opencoding.service import (
    ServiceError,
    apply_approved,
    approve_preview,
    create_session,
    preview_session,
    submit_answer,
)

def collect_preview(root, goal, answer_for):
    view = create_session(root, goal)
    session_id = view["session"]["id"]

    while view["frontier"]:
        question = view["frontier"][0]
        outcome = submit_answer(
            root,
            session_id,
            view["session"]["revision"],
            question["id"],
            answer_for(question),
        )
        if outcome.get("status") in {"busy", "stale"}:
            # Do not read outcome["session"] or continue to preview/apply.
            return {
                "status": "conflict",
                "session_id": session_id,
                "reason_codes": outcome.get("reason_codes", []),
                "result": outcome,
            }
        view = outcome
        session_id = view["session"]["id"]

    return {
        "status": "ready_for_authorization",
        "preview": preview_session(root, session_id),
    }


def apply_after_exact_user_authorization(prepared):
    if prepared["status"] != "ready_for_authorization":
        # Return the conflict to the calling control loop unchanged.
        return prepared
    preview = prepared["preview"]
    # Call this function only after the user has authorized this exact reviewed root,
    # targets, and diff through the caller's own recorded authority process.
    approval = approve_preview(preview)
    return apply_approved(preview["root"], approval)


root = Path(r"C:\path\to\authorized-project")
prepared = collect_preview(root, goal, answer_for)
result = apply_after_exact_user_authorization(prepared)
```

`create_session` and `submit_answer` write session state even when documents are not applied. A `busy` or `stale` answer result has no session payload for the example to continue with; return it to the caller, re-read and reconcile there, and obtain a fresh preview later. `preview_session` is the point to inspect exact proposed document paths and diff. `approve_preview` accepts only a complete, ready, canonical preview and gives the caller an expiry-bound object; callers must not synthesize an approval dictionary. `apply_after_exact_user_authorization` is an illustrative caller-side guard, not an authorization mechanism: it must be invoked only after separate authorization for that exact reviewed local scope. `apply_approved` can return `applied`, `stale`, or `busy` outcomes. A stale or busy result is a stop-and-reconcile condition, not a signal to reuse or broaden the prior approval. `rollback` returns the transaction layer result; inspect its `status` and receipt information before declaring recovery complete.

`ServiceError` exposes a `code`, but codes are specific to the called service operation. The CLI only promises the machine-readable status-error convention for `--status --json`; the interactive wizard and other CLI failures are not a complete, uniform JSON protocol.

## Scheduler control and TaskPlan candidates

The public scheduler module exports these functions:

```python
from opencoding.scheduler import (
    cancel,
    enqueue,
    get_task,
    list_runs,
    read_snapshot,
    recover,
    requeue,
    run_next,
)
```

Their signatures are `enqueue(root, task)`, `run_next(root, task_id=None)`, `cancel(root, task_id)`, `requeue(root, task_id, idempotency_key)`, `recover(root)`, `get_task(root, task_id)`, `list_runs(root, task_id=None)`, and `read_snapshot(root, task_id=None)`.

Only `read_snapshot` is a zero-write scheduler view: it does not initialize, migrate, or recover a store. It returns `not_initialized` when no scheduler store exists, `not_found` for an absent requested task, or `ready` with task/run summaries.

All other module helpers construct `Scheduler(root)`. Construction invokes `recover()` before the requested operation, so these helpers can initialize or migrate local scheduler state and can record recovery for abandoned runs. `enqueue`, `cancel`, `requeue`, and `recover` are mutating controls. `run_next` can execute an already validated structured local action and write local artifacts. `get_task` and `list_runs` should not be labelled zero-write merely because their final query is observational.

Use scheduler controls only for an already authorized, schema-valid local task. The executor accepts a small structured action set and binds a local `ActionContext`; it does not accept arbitrary shell commands or external actions. `requeue` requires the task's matching idempotency key and can return `already_queued` or `requeue_rejected` when state or attempt limits do not permit it. `recover` reports the recovered task IDs. Direct scheduler callers must handle `ValueError` and, for direct snapshots, `SchedulerSnapshotError` rather than assuming service-style errors.

## Verification and boundaries

Before reporting a local operation as complete, inspect the returned structured result, relevant receipt or rollback result, and the applicable local verification evidence. Do not infer that planning, a preview, an approval object, a nonzero CLI exit code, or an unavailable scheduler store proves execution success.

### Real AI calls

`aiconfig.load_config()` and `aiadapter.adapter_from_config()` expose configured providers without printing secrets. `probe_connection()` is only a low-cost service or model-directory probe: a gateway home page, HTTP 404/405/422, or any other error response does not prove credentials or generation. A real evaluation through `advisor.run_ai_evaluation()` must return `real: true`, a bound structured response, and an evaluation record before the result can be confirmed. Do not treat `response_empty`, `response_length_truncated`, `deadline_exceeded`, or `network_unavailable` as a successful AI connection; stop or reconfigure the provider instead of silently retrying.

This guide does not claim universal Codex, agent, operating-system, or platform compatibility. The complete workflow still needs the staged Host, verification, recovery, beginner, and release acceptance described in `AI_CODING_GOVERNANCE_WORKFLOW_V1.md`. Activating a real Host or connector, using credentials or cost-bearing resources, and publication each require a separately authorized Gate.


### R3 已采用计划的 Host 候选入口（2026-10-12）

正式入口读取当前已明确采用的计划，不接受调用方替换任务图或伪造来源：

```python
from opencoding import enqueue_adopted_plan, preview_host_candidates, apply_host_candidates
from opencoding.scheduler import Scheduler
from opencoding.task_execution import run_task_wave

# root/session_id 来自已有项目会话；registry 已显式登记能力与验证证据。
# 此调用只用于已授权的具体项目与 Host 范围。
batch = enqueue_adopted_plan(root, session_id, registry, host_id, max_requests=20)
scheduler = Scheduler(root, host_registry=registry)
wave = run_task_wave(scheduler, batch["plan_digest"])
# 检查 wave 的每个结果；按已保存状态继续后续波次，失败时处理原原因。
# 全部候选完成后：
preview = preview_host_candidates(root, batch["plan_digest"])
```

入口绑定完整采用记录摘要、当前会话及事实快照、计划版本、外部输入原始字节和目标输出前像。缺少采用、输入缺失、计划未解决或激活 Gate 未满足时拒绝。派发、消息组装后和结果入库都重新核验；网络调用在会话锁外。需求、事实、采用记录或文件改变后，旧结果不能成为成功候选，后继任务冻结。

候选沿依赖祖先按计划波次传递，后续生产者覆盖同路径的先前候选；已有目标内容作为绑定前像上下文，新建自引用输出会显式标明。重复入队只有同一绑定才幂等；新的采用记录不能冒充原有执行批次。`max_requests` 是计划累计 Host 派发次数，失败和未知尝试也计数；它不约束 CLI 内部模型调用次数或美元费用。

`preview_host_candidates` 核验完整生产绑定、任务、运行、请求、回执及候选摘要，并检查当前输出前像，再返回文件变更预览。它不写项目输出，但使用协作会话锁，因此不是零写入元数据接口。底层 `enqueue_task_plan` 可用于内部合约测试，其回执标为 `internal_test`，不能进入正式候选预览；正式入口标为 `production`。这两个字段不等同于 `live`：模拟 Host 仍明确记录 `simulated`。

`apply_host_candidates` 在缺少可信 `ReviewRegistry` 的一次性授权时返回 `missing_independent_validation_review_gate`。普通 `approved_digest` 不能解锁。R4 将验证与独立审核绑定到同一候选、采用记录、任务、计划、前像及运行回执集合；通用底层事务 API 仍是可信调用方工具，不是抵抗同权限代码的安全边界。

`CodexCLIHost.from_provider_config(config)` 接受在内存中加载的现有 `openai_compatible` 配置，使用显式中转地址、模型及子进程专用密钥环境变量。该方式要求 Responses 兼容；Chat Completions 的历史成功不能代替真实 Codex 验证。默认 OpenAI 路径支持 `CODEX_API_KEY`。临时工作目录、隔离配置和 read-only 模式不构成仅读取所供输入的安全保证。

2026-10-12 已按用户授权从本地附件恢复用户级配置，并通过 gpt-6.1-sol 的最小真实 Codex Host 验证：完整身份、请求摘要和候选内容一致，未写项目输出。修正了中转结构化输出对数组 enum 及 Windows 根路径 enum 的兼容问题；根路径仍在返回后严格逐字核验。证据：`.opencoding-dev-runs/relay-20261012/host-smoke.json`。这仅证明该模型/中转/本地 CLI 组合的最小候选协议，不代表完整业务任务、真实独立 Reviewer 或新手旅程通过。50 元是用户账户额度，实际费用和余额未知，派发数和超时不能当作人民币硬限制。


### R4 验证、独立审核与受控应用（2026-10-12）

`ReviewRegistry` 注册两个可信本地回调，分别担任 `validator` 和 `reviewer`。两者的 `principal` 必须不同，且不能与候选生成 Host 的标识重合。登记是调用方的信任决定；别名不能证明组织独立性，任意同权限 Python 代码也不在隔离范围内。回调必须只读，并实际执行其声称的验证或审查；仅构造通过字符串不构成有效验证。

```python
from opencoding import ReviewRegistry, apply_host_candidates

reviews = ReviewRegistry()
# trusted_validator/trusted_reviewer 由调用方接入已核实的独立实现。
reviews.register("project-tests", trusted_validator, role="validator",
                 principal="project-test-runner", verification="live")
reviews.register("independent-review", trusted_reviewer, role="reviewer",
                 principal="separate-reviewer", verification="live")
checked = reviews.review(root, batch["plan_digest"], validator="project-tests",
                         reviewer="independent-review", timeout_seconds=180)
if checked["status"] == "review_passed":
    # 本地写入范围仍须属于已有用户授权。
    applied = apply_host_candidates(root, batch["plan_digest"],
        review_registry=reviews, grant=checked["grant"],
        approved_digest=checked["approved_digest"])
```

回调接收 `(request, cancel_event)`。`request` 含完整候选预览、验收条目、`binding_digest` 与角色；Reviewer 还收到验证器结果。回调返回且只返回 `request_digest`（对整个 request 使用 `opencoding.host.request_digest`）、`status`、`findings`、`evidence`。只有 `status="passed"`、空 findings、非空证据且摘要匹配可继续。所有非空 findings、异常、unavailable、取消和超时均禁止授权。验证在会话锁外运行，结束后及应用前重读当前绑定；回调获得独立副本，修改副本不会修改候选。

候选 Host、验证器与 Reviewer 任一是 simulated，仅返回 `reviewed_simulated`，不会创建应用 grant。live 标识仍是可信登记者声明，不自动把测试 fixture 升级为真实证据。grant 在当前 registry 中保留 15 分钟，只能使用一次，可通过 `reviews.revoke(grant)` 撤销；进程重启必须重新验证和审核。磁盘上的 `.opencoding/host-reviews/<digest>.json` 是绑定摘要的审计证据，不能恢复 grant 或替代审核。

受控应用复用事务前像、写入前 guard、回执与回滚。输入/采用记录/数据库变化或授权撤销会停止写入，部分应用自动尝试安全回滚；必须检查返回的 `rollback.status`，不能把回滚尝试等同于完成。故障接续使用返回的 `transaction_id` 调用 `opencoding.transactions.read_receipt_state` 或 `rollback_changes`；保留用户后续修改。修复候选必须产生新摘要并重新验证，旧 grant 不得复用。本版不自动生成修复候选或无限重试。R4 真实独立适配器、源码独立复核、G6/G7 验收仍待完成。


### R4 源码独立复核结果（2026-10-12）

`.opencoding-dev-runs/r4-20261012/independent-review.json` 记录了只读源码审查：授权角色与主体分离、grant 一次性消费、候选/数据库/采用绑定复核、写入 guard、部分失败回滚、模拟证据降级、Key 隔离和 Host 返回身份严格比较均通过；针对性回归 63 项通过。结论为 `pass_with_open_gates`。

仍开放两个 Gate：没有可核对的外部验证器/Reviewer身份绑定，且没有真实用途项目 G6/G7 验收。ReviewRegistry 的 `verification="live"` 是可信调用方声明，不能由本地 fixture 或普通回归自动升级。


### G6 真实项目只读实验（2026-10-12）

目标项目为 `E:/线稿讲解视频`，用途是自媒体线稿讲解视频发布。用户明确要求“不改代码，只用于项目做使用”。本轮只读检查了项目规则、`episodes/20261006-libai-catpaw` 单集文档和四个既有视频；FFprobe 与 FFmpeg 完整解码均通过。证据：`.opencoding-dev-runs/g6-20261012/media-baseline.json`。

尝试一次受限真实 Host 优化请求，要求只基于项目文档提出优化实验草案，输出写入 OpenCoding 隔离证据目录。适配器返回 `failed`，没有候选文件，也没有写入 `E:/线稿讲解视频`；证据：`.opencoding-dev-runs/g6-20261012/host-result-summary.json`。该结果不构成 G6 通过，不追加重复付费请求。

项目已有边界：v006 首场和 v007 无声着色有局部验收记录，但整片和配音仍为 `pending-user`；已登记的配音漏字问题仍未修复。后续实验必须继续只读或输出到隔离目录，使用新版本路径，不能覆盖既有素材和成片。


G6 后续自动工作已完成只读失败分析与优化方案，证据位于 `.opencoding-dev-runs/g6-20261012/host-failure-analysis.json` 和 `readonly-optimization-plan.md`。因适配器未保留远端错误码，本轮不重复付费请求；项目原目录没有写入。


用户审阅回执（2026-10-12）：v008 画面整体评价为“非常不错，其他地方很好了”，但口播仍少读了“个”字。该回执只确认视觉方向良好，不构成版本验收；G6 保持 `user_reviewed_pending_tts_correction`，后续需在隔离目录修复音频并重新验证，目标项目仍不得写入。


### G6 v009 漏字修复实验（2026-10-12）

用户要求测试修复“个”字漏读。已在隔离副本 `.opencoding-dev-runs/g6-20261012/isolated-episode-v009` 中使用本地 F5-TTS 重新合成第 2 句，并按原句时长替换隔离音轨；目标项目与 v008 均未修改。技术核验通过：32.1 秒、1280×720、30fps、音视频时长一致、完整解码通过。

当前状态为 `technical_candidate_ready_pending_user_listening_review`。重新合成成功不等于听审通过，仍需用户回听确认“个”字已读出。证据：`.opencoding-dev-runs/g6-20261012/v009-repair-report.json`。


### G6 ASR 自检结果（2026-10-12）

按用户要求接入 `D:/短视频下载解析` 的本地 SenseVoice/FunASR 服务，对旧音频及 v010/v011 隔离修复音频执行实际转写。旧音频转写为“先记住，这还没有成为诗仙的年轻人”，确认漏读“个”；v010 转写为“新的年轻人，他也曾坐在山间读书，面对着尚未展开的世界”；v011 转写为“仙祭组织还没有成为诗仙”。两版均未可靠检出目标短语，因此 ASR 自检失败，不能算修复通过。证据：`.opencoding-dev-runs/g6-20261012/asr-self-check-v010-v011.json`。


### G6 v013 VoxCPM2 参数复验（2026-10-13）

用户确认可重新占用 VoxCPM2 后，按 `R2-03` 的 VoxCPM2 参数和 moderate 降噪链在隔离目录生成 v013。实际 ASR 转写为“先记住这个还没有成为诗仙的年轻人……”，确认包含“个”。技术核验通过，输出位于 `.opencoding-dev-runs/g6-20261012/isolated-episode-v013`。当前仍待用户回听，不代表正式采用。证据：`.opencoding-dev-runs/g6-20261012/v013-r2-03-repair-report.json`。


### G6 v013 用户通过与 OpenCoding 下一轮落地（2026-10-13）

用户明确通过 v013 隔离代表片段，接受范围为“v013 代表片段及 R2-03 音频方法进入下一轮”。这证明 OpenCoding 可以进入真实项目隔离候选、ASR 自检、媒体核验和用户验收绑定流程。下一轮转回 OpenCoding 自身：先绑定证据并重放正式入口，再执行 G7 新手旅程；不把单个片段扩写成整集发布通过，也不自动写入目标项目。推进说明：`.opencoding-dev-runs/g6-20261012/next-round-opencoding-landing.md`。


### G7 正式入口真实项目隔离回放（2026-10-13）

真实项目候选使用 `E:/线稿讲解视频` 的 `episodes/20261006-libai-catpaw` 单集。OpenCoding 从一句话目标开始完成会话澄清、计划评估、显式采用、生产绑定 Host 任务波次、候选预览和独立 Reviewer 绑定。回放共 10 个任务、8 个波次、10 个运行回执；G6 v013、ASR 自检、媒体基线和只读证明按 SHA-256 绑定。证据：`.opencoding-dev-runs/g7-20261013/g7-formal-replay.json`。

本轮 Host 与 Reviewer 是受控 `simulated` fixture，用于证明正式入口和 Gate 绑定；结果为 `pending_live_validation_review`，未发放写入授权。真实项目原目录前后 SHA-256 一致，整片发布、真实项目写入和 live provider 验证仍未完成。


### Codex Host 失败诊断修复（2026-10-13）

`CodexCLIHost` 现在在适配器实例上保存粗粒度、无秘密的 `last_diagnostics`，包括 `process_failed`、`timed_out`、`invalid_response` 和 `cli_unavailable` 等代码；严格 Host 结果信封保持不变，不把 stderr、凭据或 provider 响应写入候选结果。目标是为下一次真实 Host 验证保留可用的本地失败定位。17 项 `tests.test_product_codex_host` 已通过；证据：`.opencoding-dev-runs/g7-20261013/codex-host-diagnostics-repair.json`。


### G7 live Host 最小探针（2026-10-13）

针对真实项目 `E:/线稿讲解视频` 只发起了两次有界单文件候选探针，输出目标为隔离目录中的 `optimization-proposal.md`。第 1 次为 `process_launch_failed`，第 2 次已进入执行但在 60 秒达到 `timed_out`；两次请求摘要和身份均匹配，没有生成候选，也没有写入真实项目。证据：`.opencoding-dev-runs/g7-20261013/live-host-probe-history.json`。本轮停止自动重试；下一次 live 验证需要明确可用的 Codex CLI/provider 执行路径和超时/费用边界。
