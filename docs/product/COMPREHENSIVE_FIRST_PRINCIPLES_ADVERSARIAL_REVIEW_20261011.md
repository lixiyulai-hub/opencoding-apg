# OpenCoding/APG 全面第一性原理与对抗性审查

审查日期：2026-10-11
审查范围：需求澄清、方案判断、文档与任务图、授权、Agent 执行、候选提交、验证、审核、恢复、工作台安全、发布边界。
审查方式：三个独立子代理并行审查，主代理做源码核对、最小复现和回归验证。
审查性质：以下发现记录审查时的基线；审查阶段没有修改业务代码；但事后发现一次临时连接探测误写了用户级 AI 配置，不能再称整个审查完全只读，详见文末操作事件。2026-10-11 后续修复记录见文末，不能把基线缺陷直接当作修复后的状态。

## 结论

当前项目可以准确定位为：**AI Coding 治理工作流内核和范围明确的阶段性原型，具备合成执行与恢复证据**。它还不能被描述为已经完成的通用 AI Coding 交付系统，也不能宣称支持所有 Agent、任意项目、自动独立审核或商业级端到端交付。

第一性原理链路应为：

`小白想法 → 需求事实 → 平台/架构判断 → 项目文档与 TaskPlan → 授权预览 → Agent Host 执行 → 候选 → 验证 → 独立 Review → 修复/恢复 → 交付`

当前前半段（事实、判断、文档、TaskPlan、授权、本地事务、预算和部分恢复）已有实现与测试；后半段仍有三类断点：

1. `TaskPlan → 通用 Agent Host → 多任务/多波次执行` 尚未贯通，当前生成路径仍以固定 `generate-app-v3` 与 `lendreg` 示例为主。
2. 候选通过静态/事务验证后即可写为 `delivered`，没有强制的独立 Reviewer artifact 和 blocker Gate。
3. 没有真实项目 G6 和新手 G7 的独立验收证据，不能由单元测试或合成项目替代。

## 已核对的证据

| 检查 | 结果 | 含义 |
| --- | --- | --- |
| `python -X utf8 -m unittest` | 592 tests；589 passed；3 skipped；OK | 回归通过，不等于真实 Agent 交付完成 |
| 重点回归（grants、executor、workbench） | 41 tests；OK | 顺序行为覆盖良好，但未覆盖发现的并发/类型边界 |
| `python -X utf8 -m compileall -q opencoding tests` | OK | 语法与导入级检查通过 |
| 一次性凭据并发最小复现 | 两个线程均返回 `accepted` | 真实存在 TOCTOU 重放窗口 |
| `confirm-evaluation` 类型最小复现 | `accepted="false"` 被转为 `True` | 真实存在布尔类型绕过 |
| `.opencoding` symlink 最小复现 | 外部目标目录被写入 `project.json` 与 `sessions/` | 工作台项目身份写入存在根外路径风险 |
| 跨会话 `gen-status` 最小复现 | 会话 B 可读取会话 A 的 `delivered` receipt | 运行记录缺少 session owner 校验 |
| `project_governance doctor/audit/check` | unavailable：环境缺少 `project_governance` 模块 | 不能宣称治理命令通过 |
| GitHub 远端本轮复核 | unavailable：网络代理无法连接 | 不能把本轮失败当成远端回滚证据 |

## 按优先级的问题

### P0：完整交付闭环尚未成立

#### P0-1 未强制独立审核就进入 `delivered`

`opencoding/generic_run.py:2553,2935` 在候选验证、事务提交后直接设置 `receipt["status"] = "delivered"`。当前代码没有要求独立 reviewer 记录、结构化 finding、候选摘要绑定和 blocker 处理结果。

影响：生成器自报成功或自动测试通过，可能被误认为已经完成需求、安全和范围审核；这直接违背“验证与独立审核后交付”的产品定义。

验收标准：

- 引入独立 Review 状态：`pending/pass/blocker/non_blocker/unavailable`。
- Review artifact 必须绑定 `candidate_digest`、`task_digest`、`plan_digest` 和验证结果摘要。
- `blocker` 或 `unavailable` 时禁止 `delivered`；只能进入冻结或人工处理状态。
- 修复必须生成新候选摘要，并重新验证和审核，不能修改旧 receipt 补造成功。

#### P0-2 Agent 执行链不是通用工作流

`opencoding/generic_run.py:44` 固定 `TASK_ID = "generate-app-v3"`；`opencoding/service.py:163-228` 的 executor mapping 仍有 `lendreg`/示例业务契约。工作台生成路由没有统一的 `host_id`、adapter 版本、能力发现、TaskPlan 波次映射、幂等、迟到响应和 Host live verification 合约。

影响：现有证据只能证明受限的合成路径，不能证明“OpenCoding 编排任意项目并把任务交给 Agent 执行”。

验收标准：首个真实 Host 通过 conformance tests 和 live verification；每个请求绑定 `host_id/adapter_version/task_id/run_id/plan_digest/root_digest`；支持查询、取消、超时、重复/迟到响应与恢复；至少一个非 `lendreg` 真实项目走通多任务波次。

### P1：会阻断或削弱真实产品安全的缺口

#### P1-1 工作台运行操作缺少会话归属校验

`opencoding/workbench.py:330-350,522-608` 的会话级 `gen-status`、`resume`、`cancel-generation`、`ack-unknown` 等路径主要按 `run_id` 查询或操作，没有强制检查 URL `session_id == receipt.session_id`。`approve/apply` 也没有把 body 中的 preview/approval 与 URL 会话绑定。

影响：知道同一项目其它 `run_id` 或拿到其它会话的合法 approval 后，可能跨会话读取、取消、接续或触发写入。

修复验收：会话级读写、取消和恢复接口同时校验 project root、session id、run id、candidate/receipt owner；不匹配统一 fail-closed；增加跨会话攻击测试。项目级 `/result` 是历史查询入口，不据此判定为跨会话越权。

#### P1-2 `confirm-evaluation` 接受非布尔值

`opencoding/workbench.py:395` 使用 `bool(body.get("accepted"))`。最小复现已确认 `accepted="false"` 传给 `advisor.confirm_evaluation` 的参数为 `True`。

修复验收：只接受 `type(value) is bool`；字符串、数字、列表、字典、缺失值全部拒绝；拒绝不写状态、不改变确认记录。

#### P1-3 工作台缺少 Origin/CSRF 和 Content-Type 防护

`workbench.py:951-1047` 主要依赖 Cookie token，写请求没有 Origin/Host 校验，`_read_body()` 也不强制 `Content-Type: application/json`。Cookie 已设置 `SameSite=Strict`，普通跨站页面不应被描述为可自动携带该 Cookie。可达威胁重点是同站不同端口的本地恶意服务，以及同源展示的生成 HTML；写入口仍应防止这些来源触发配置、评估、生成、预算或 apply。

修复验收：写请求严格要求 `Host/Origin` 为 loopback 允许来源；强制 JSON Content-Type；拒绝跨来源、`text/plain` 和浏览器缺失 Origin 的写请求；无 Origin 的非浏览器 JSON 客户端仍须通过鉴权，不机械禁止现有本地客户端。

#### P1-4 账本、预算和一次性凭据缺少原子竞争控制

`generic_run.py:1363-1484` 与 `grants.py:492-635` 使用读-改-写。一次性凭据的 `used` 检查与写回之间没有锁；最小复现中同一 credential 两线程均被接受。账本并发写入还可能造成预算、dispatch 计数和状态丢失更新。

修复验收：以文件锁/事务锁或 CAS 包住检查、消费、预算递增和日志追加；冲突必须拒绝并重读；并发测试证明一次凭据最多成功一次，预算不会超过上限，receipt 不被旧写覆盖。

#### P1-5 评估请求没有纳入 AI 预算和频率控制

`workbench.py:382-390` 的 `evaluate` 直接调用 `advisor.run_ai_evaluation`，没有 step credential、任务累计账本或频率上限。`/api/ai/test` 也可以重复触发真实网络探测。

影响：同站脚本或误操作可以反复消耗 Provider 额度；即使评估不是代码生成，也属于真实外部动作。

修复验收：评估和连接测试分别有次数/时间窗预算，失败和效果未知同样记账；超限进入明确状态；每次外部请求都有 task/run/request id 和用户可见记录。

#### P1-6 正式 AI 生成可通过 `adoption_binding=None`

`generic_run.py:2597-2603` 在 binding 为空时直接返回。低层 API 直调可以绕过“已确认并采用计划”的约束，而工作台只是调用方自觉传入 binding。

修复验收：正式 AI 生成默认要求 adoption binding；若保留低层测试接口，必须显式标记为 test/internal mode，生产入口无 binding 一律拒绝。

#### P1-7 AI 计划预览与 apply 可能出现来源漂移

预览层可沿用 adopted AI plan override（`service.py:330-387`），而 `apply_approved()` 的重新验证路径可能回到规则 `_graph`（`service.py:861-879`）。在 AI 计划与规则计划不同的项目中，这会触发 `business_or_file_plan_drift`，导致合法的“采用 AI 计划后写入”被拒绝。

这属于 fail-closed 阻断，但会让核心产品流程无法完成。修复必须让 preview、approval、apply 使用同一 adopted plan source，同时保留 revision、digest 和输入漂移拒绝。

#### P1-8 本地 `python_module` 不是操作系统沙箱

`opencoding/executor.py:317-386` 以当前用户权限启动 Python 模块；源码注释也明确写着“not an operating-system sandbox”。`sandbox.py` 的 `sitecustomize`、open/socket 拦截属于进程内辅助守卫，可被子解释器、ctypes、原生扩展或 PowerShell 绕过。

影响：未知代码可能读取项目外文件、联网、启动原生子进程或写用户可写路径。未知候选必须进入已实测的 Docker/OS provider；辅助守卫不能单独成为执行资格。

#### P1-9 `trusted_fixture` 是调用者可传入的测试豁免

`sandbox.py:263,489-542` 仅按字符串是否在 `REVIEWED_FIXTURES` 放行。此项只在生产入口允许不可信输入选择该参数时构成边界漏洞；同权限 Python 调用者本就能执行任意代码，不能把字符串参数描述为独立远程提权。若生产调用者能控制该参数，缺少真实受限后端时可获得测试夹具豁免。应把它隔离到测试构建或内部 capability token，生产入口不可传入。

#### P1-10 工作区项目身份可通过 `.opencoding` symlink 越出项目根

对抗性最小复现已确认：预置 `project/.opencoding` 指向工作区外目录，再调用工作台创建项目，会把 `project.json` 和 `sessions/` 写到外部目标。`_write_project_identity` 只检查 root 本身，不检查 marker 目录及其祖先。

修复验收：项目创建、导入、运行前统一检查 root、`.opencoding`、sessions、receipts 及所有祖先的 symlink/junction/reparse；任何链接或普通文件替代目录都拒绝；增加 Windows junction/reparse 测试。

#### P1-11 推荐能力范围大于真实执行能力

需求与决策模块可以推荐 Windows、Mac、iPhone、安卓、网页及服务器、数据库、支付、部署等能力，但当前真实执行器和 Host 证据没有覆盖这些组合。推荐必须带 capability evidence 和“仅方案建议/尚未接通”的明确状态；不能把方案生成当成连接器或部署能力。

### P2：需要在发布前收敛的边界问题

- `workbench.py:982-1001` 对 raw path 先 `resolve()` 再判断 `is_symlink()`，可能把原始 symlink 解析成普通目标；还存在 resolve→read 的 TOCTOU。应逐级检查 reparse/symlink，并使用 no-follow 打开策略。
- 冻结 checker 与候选在同一解释器/进程中加载，候选可在 import 阶段 monkeypatch checker 依赖；`assert_file_exists` 等契约路径也缺少严格的 `app/` 相对路径校验。验证必须使用不可被候选修改的观测通道，不能用硬编码返回值冒充持久化。
- `advisor.confirm_evaluation` 接收 `expected_revision` 但未严格校验它等于当前 revision，存在过期确认和伪造审计版本的证据一致性问题。
- 用户目标、事实、`PRG.md`/`plan.md` 等不可信内容直接进入模型消息；需要来源分层和明确不可信分隔符，模型文本不得改变能力、路径和外部动作边界。
- Windows 项目名缺少保留设备名、尾随点/空格、大小写和 Unicode 规范化碰撞检查。
- 工作区根和 `.opencoding` 祖先的 Windows junction/reparse 检查不完整；启动时应统一经过 runtime root 校验。
- `aiconfig.py:78-95` 需要严格字段白名单；`base_url` 默认应优先 HTTPS，localhost 作为明确例外；日志和错误必须继续脱敏。
- 普通 SHA-256 fingerprint 只能检测偶发篡改，不能抵抗同权限进程重算；若需要抗篡改，应使用受保护密钥或系统凭据存储。
- 普通 JSON receipt 没有不可抵赖摘要链/签名；同权限本地进程可伪造候选、transaction id 或 unknown effect。至少要把 receipt digest 链接到上游输入和事件序列，并明确其信任边界。
- 需求澄清目前偏固定问卷，隐私、合规、性能、维护、升级和发布责任等风险维度仍不足，需在真实新手验收中验证问题是否足够且可理解。

## 工作流级漏洞与缺失

1. **没有统一执行包预览。** 用户在真正生成前应一次看到任务、波次、Host、写入范围、预算、验证、回滚和外部影响；当前确认、采用和生成授权分散在多处。
2. **没有统一的任务状态机。** 任务计划、Agent run、候选、验证、Review、Delivery 的状态和摘要绑定尚未由单一状态机强制。
3. **失败与恢复覆盖不完整。** 已有部分 unknown effect、输入漂移和恢复路径，但 Host 断连、迟到/重复响应、取消后迟到落盘、部分回滚失败仍缺少通用契约测试。
4. **外部人工 Gate 没有统一 denylist。** 服务器、数据库、支付、部署、密钥、远程 Git 和公开发布必须在动作层统一暂停，不能依赖某个具体项目路径自觉处理。
5. **G6/G7 缺证据。** 真实项目端到端验收和新手独立旅程尚未完成；这些是发布 Gate，不是文档工作。

## 建议的执行顺序

1. 先做安全阻断修复：严格布尔、会话/运行归属、CSRF/Content-Type、正式入口强制 adoption binding。
2. 再做一致性修复：adopted AI plan 同源 preview/apply；receipt 在 Review 前不得标记 delivered。
3. 为 ledger、grant、credential 加锁/CAS，并补并发与重复响应测试。
4. 定义 Host Adapter/Registry/conformance contract，完成一个真实 Host 的 live verification。
5. 实现独立 Reviewer Gate，覆盖 blocker、修复后新候选和 unavailable 状态。
6. 选择一个不含真实外部服务的真实项目完成 G6，再由未参与实现者执行 G7 新手验收。
7. 最后收敛路径、配置、凭据、receipt 完整性和发布文案边界。

## 当前宣传边界

可以宣传：面向小白用户的 AI Coding 治理工作流方向；需求澄清、方案判断、项目文档、任务图、授权预览、本地受控事务、预算和部分合成恢复能力。

暂不能宣传：支持所有 Agent/Provider；任意平台真实生成；自动独立审核；商业级端到端交付；自动部署、支付、发布或零人工外部操作。

## 独立复核结论

本轮独立复核已完成源码、文档、测试、并发最小复现和工作台边界审查。审查结论不是代码修复；修复后必须重新执行相关 Gate，并由未参与实现的复核者确认。


## 2026-10-11 修复编排与首批实施

用户已授权自动修复。首批事务限定为已复现的入口安全与状态一致性修复；完整产品的 G2/G4/G6/G7 仍待独立验收。此次没有增加 Docker GUI、MCP 或特定 IDE 前提。

| 批次 | 改动与验收条件 | 当前状态 |
| --- | --- | --- |
| R1-A 工作台 | JSON bool/int 严格类型；URL会话与run、preview、approval一致；链接目录拒绝；HTTP来源/JSON校验；GET写动作405；同源HTML/SVG/JS惰性文本预览 | 99项相关定向通过；独立静态复核通过 |
| R1-B 授权 | grant/credential 文件名与内部ID一致；跨进程签发、撤销、消费串行；一次性与累计预算反例；损坏流水失败关闭 | 最终37项定向通过，含17项新增；旧库纯读零写与首次写并发已验证 |
| R1-C 预算 | 累计账本全程加锁；最后一次额度最多一个进程消费；重复记账幂等；拒绝后不多计回执且保留本次请求身份 | 最终21项定向通过（含拒绝回执与跨进程竞争）；独立复核通过 |
| R1-D 计划 | adopted AI计划同源preview/apply；严格revision与accepted；确认写入和会话修改互斥；锁竞争映射预期错误 | 46项初始定向通过；最终16项plan_source回归通过；独立复核通过 |

集成全量：`python -X utf8 -m unittest` 为 **633 tests，630 passed，3 skipped，OK**，耗时595.660秒。日志：`artifacts/audit-remediation-20261011/unittest.log`。之后新增的确认锁错误映射及旧库只读兼容修正使用增量回归，不把全量结果冒称为这些后续字节的完整重跑。

独立复核实际改变了实现：预算预约拒绝曾使回执多记一次派发，现先保留请求身份，预约成功后才计数；旧授权库纯读取创建锁的问题已修正并经未参与实现的代理补审通过。普通文件锁只保证协作进程一致性，不隔离同权限恶意程序，不赋予自由文本issued_by不可伪造的身份。

### 后续有序事务与验收边界

1. R2：正式生成入口强制计划/采用绑定；评估与连接探测预算；评估替换与确认并发一致性；消费端action/input/targets摘要核验。非法输入或超限须零外部请求、零写入。
2. R3（G2/G3）：统一Host能力与身份、TaskPlan任务波次、幂等/取消/超时/迟到响应，验证首个真实Host。模拟合约通过和真实接入通过分开记录。
3. R4（G4/G5）：独立Reviewer绑定候选/任务/计划/验证摘要；blocker与unavailable禁止交付；修复产生新候选并重新测试复核，冻结与恢复保留历史证据。
4. R5（G6/G7）：真实用途项目全流程验收，再做目标新手独立旅程与发布准备。任何单元测试或合成示例不能替代。

剩余明确限制：空评估会话的旧UI卡片清理尚待完善；未知候选的操作系统级隔离不是本批解决事项；HTML产物当前只在工作台中作为文本展示，交互预览应使用独立受限来源；授权文件不是抗同权限恶意程序的安全边界；本批没有完成正式发布验收。

### 审查操作事件：用户级AI配置被测试占位值覆盖

前一轮临时探测未隔离LOCALAPPDATA，调用aiconfig.save_config覆盖了用户级ai_provider.json，并写入ai_last_test.json。本轮核对确认当前配置为该次测试占位值；同配置目录无备份，项目父目录按文件名检索亦未发现恢复副本。未输出密钥，未以占位值调用真实模型。

已采取的恢复措施：只在内容严格匹配已知占位配置时，将其移至同目录ai_provider.audit-placeholder-20261011.json；与其写入时间匹配的探测状态移至ai_last_test.audit-placeholder-20261011.json。当前AI配置为未配置，避免误显示真实服务可用。**原始配置未能恢复**；后续真实API验证需要从可信备份恢复，或由用户在本地接入向导重新填写。不能让用户把密钥发到报告、日志或聊天中。新工作台安全测试全部使用临时LOCALAPPDATA隔离。

此事件是独立的未解决环境问题；不阻断本地源码回归，但阻断真实Provider验收。用户要求自动修复不等于接受配置丢失，也不等于接受本批或整个产品。

最终累计预算兼容回归：21 tests，OK，38.028秒；日志 `artifacts/audit-remediation-20261011/budget-final.log`。最终源码与测试摘要见 `artifacts/audit-remediation-20261011/source-sha256.json`；`git diff --check` 和 `compileall` 通过。POSIX锁分支尚未在本Windows环境实测。

最终独立补审：`b010cccbcdc14dbebd94f7ad4d95507e09c767bb` 的旧库只读修正通过；原P2已消除，未发现新增阻断。该源码提交已推送main，并以git ls-remote核对远端一致。首批修复验收不代表剩余R2–R5或完整产品已验收。

## 2026-10-11 R2：正式执行绑定与请求预算

本批承接用户“继续下一轮工作”的授权，未调用真实 API。所有测试在临时 `LOCALAPPDATA` 中运行；用户级配置恢复仍是后续真实 Provider 验证的独立阻塞。

- 正式生成默认核对当前项目、会话、已确认评估、采用版本、计划摘要和实际执行契约；模拟适配器也必须走正式检查。低层测试豁免须显式标记 `internal_test`，其回执不能进入工作台正式操作或恢复。旧无绑定回执须重新评估采用。
- 接续在首次写入前对照原回执承诺，核对冻结材料的运行身份、校验器和 spec 摘要、权威采用契约；执行验收器再次匹配该承诺。非默认 `data_dir` 取自权威契约，不能回退成另一个目录。完整替换其他运行的冻结包，或只同步篡改 spec 与回执摘要，均拒绝且不写入。
- 每次生成和修复在消息组装后，以短会话锁复核采用与输入，再登记请求、消费预算；网络在锁外调用。该登记点之后的需求变更按已授权请求处理，响应和提交边界会拒绝旧结果，不能声称能够撤回已开始的请求。
- 评估共享会话锁抓取输入、锁外请求、返回后复查；需求、事实、确认或采用变化时，迟到响应不得覆盖当前方案。评估记录保留本地请求 ID、预算预约 ID、运行和任务身份；适配器 ID 单独保存。
- 评估预算按会话持久化，默认累计 20 次、每 60 秒 5 次；连接测试按本地配置目录共享，默认累计 20 次、每 60 秒 3 次。更换配置、模型或运行 ID 不重置对应账本。失败及效果未知请求仍占额度；扩额须明确确认、整数额度及原因，且不会解除频控。**次数预算不是美元余额或费用硬上限。**
- 消费凭据时强制核对任务、尝试、动作类别、动作摘要、实际输入摘要和精确目标；生产调用额外绑定授权 ID。写锁内再次检查后消费，保留只读兼容。autorun 验证以实际副本字节计算输入摘要。
- 工作台清理空评估/切换会话后的旧卡片，提供剩余额度和明确扩额入口；连接探测只陈述模型目录响应，不能据此宣称鉴权或生成能力通过。

### 独立复核发现与验收

首次全量为 691 tests，688 passed，3 skipped，870.245 秒，日志 `artifacts/audit-remediation-r2-20261011/unittest-before-final-review.log`。独立复核随后发现派发前需求漂移窗口，以及接续使用默认 data_dir、未对照原回执核验冻结契约来源的问题。首轮结果不覆盖这些后续修正。

独立复核：评估/事实/预算与主代理接入代码交叉审阅通过；凭据/autorun 由未参与实现者复核通过；正式生成/接续最终 18 项独立测试通过（36.434 秒）。复核确实发现并促成了上述派发与冻结来源修复。`generic_run.py` 复核前后 SHA-256 均为 `02d8a993d5455cbdd7f5c21fea32a91d4bcd8fbd666a91314f12f00aad87bb9f`。这些复核只覆盖本轮改动，不等于后续产品 Reviewer Gate 已实现。

最终全量：`python -X utf8 -m unittest` 为 **697 tests，694 passed，3 skipped，OK**，耗时 817.859 秒。日志 `artifacts/audit-remediation-r2-20261011/unittest.log`；最终源码与测试摘要 `artifacts/audit-remediation-r2-20261011/source-sha256.json`。测试前后 23 个本轮源码与测试文件摘要均未变化。`git diff --check`、`compileall` 与工作台 JavaScript 语法检查通过。跳过项不计为已验证能力；POSIX 锁分支仍未在此 Windows 环境实测。

后续顺序仍为 R3 Host/任务波次 → R4 独立 Reviewer/有界修复 → R5 真实项目与新手验收。R2 不完成这些 Gate；用户执行授权不等于成果验收。总进度与阶段进度继续为 `not-computable`。
