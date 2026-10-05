# OpenCoding 跨工作树契约 V1.1

日期：2026-09-05。状态：W1 返修输入，尚未集成验收。
本文件是协调者新增的修订，不覆写 CONTRACTS_V1.md 和历史证据。
原函数名、工作树归属和显式 root 原则不变；本文件优先解释冲突条目。

## 版本与职责

- Session、Recommendation、TaskPlan 使用 schema_version="1.1"。
- W1-A 的文件预览、动作、事务证据继续使用自身 "1.0"；不得混用版本常量。
- 不兼容的旧 Session/Recommendation/TaskPlan 明确拒绝，不能静默迁移或覆盖旧文件。
- 原始数据版本 revision 为非负整数，0 是合法草案，bool 不是整数。
- 返回值只含可 JSON 序列化的数据。嵌套结构的未知字段、错误类型均须拒绝。
- 纯函数不读写文件。草案可渲染和预览，但不能标成已授权执行或已完成。
- 文档落地由后续 service 调用事务内核，W1-C 不得实现另一套写盘逻辑。

## W1-B：把业务目标真正传下去

不要求用户选择框架、数据库产品或云服务商。只询问目标、使用者、
使用设备和业务行为；技术方案由系统提出，并说明理由、替代方案、
维护负担、离线未核实的版本和费用。

Session 保留 V1 的 id、revision、goal、answers、requirements、questions、
answer_history、state。新增业务问题采用以下固定 ID，问题正文可按上下文表达：

| ID | 大白话问题 | 决策用途 |
| --- | --- | --- |
| cross_device | 数据是否要在不同设备之间同步？ | 区分本机保存和远程服务 |
| file_storage | 是否要上传、保存照片或附件？ | 文件存储，不等同于数据库 |
| external_data | 是否要从别的服务获取内容，比如地图、天气或智能问答？ | 第三方接口，不要求用户懂 API |
| admin_access | 是否需要专人管理内容、成员或处理订单？ | 后台，不等同于所有多人使用 |

当 `goal` 明确包含二手奢侈品交易、独立站或同义业务时，Session 会在上述问题后追加一组必答的领域问题：
`seller_onboarding`、`identity_verification`、`product_listing`、
`authentication_responsibility`、`orders_commissions_settlement`、`logistics`、
`after_sales_disputes`、`risk_governance`。这些问题分别覆盖卖家入驻、身份核验、商品发布、
鉴定责任、订单/佣金/结算、物流、售后争议和平台风控治理；用户回答“未知”时必须保持
`unknown` 和待确认状态，不能由 agent 代填。`answer_history.source` 固定为 `user`，
Recommendation 中的场景 source 使用 `user.answers.<question_id>`；任何临时推断只允许放在
`assumptions` 并标记 `agent.assumption:`，不得伪装成用户需求。

原有 data_persistence 问题只问是否保存，不能把本地保存和跨设备同步合并。
新问题必须进入版本化问答流程，不能只出现在说明文档。
缺失、未知、否定、修改、冲突是不同状态；复杂语义可以追问，但不能冒充理解。
明确说“不要 Windows，只要 Mac”不能推荐 Windows；无法可靠解析时标成待澄清。
改答案后提高 revision 并记录历史，冲突须有明确、可测试的再次确认路径。
只有所有必要问题已解决、无冲突且 Recommendation.unresolved 为空，才可 ready。
可以对未知事项先给低置信度草案；不因此要求小白批准技术细节。

持久化前和向下游导出前都要脱敏，包括 goal、历史答案和多行私钥。
会话目录、文件、临时文件、锁文件及 root 的链接/重解析点都需要检查。
同一 session 的读取版本、比较、写入必须在跨进程排他锁内完成；
不得仅靠检查 revision 后 os.replace。两个竞争写入不得双双“成功”而静默覆盖。
读操作不建目录；校验失败不写入；损坏/不兼容会话不能覆盖恢复。
不要把应用级锁和路径校验称为操作系统沙箱。

## Recommendation 精确结构

顶层键恰为：
schema_version、session_id、revision、status、project、platforms、stack、
capabilities、assumptions、unresolved、acceptance。

status 只能为 draft 或 ready；有 unresolved 时必须 draft。
project 恰含以下字段，原始文字是业务数据，不得当作模型或 shell 指令：

```json
{
  "goal": "做一个社区工具借还登记系统",
  "audience": "社区居民和管理员",
  "outcome": "居民登记借用工具，管理员确认归还",
  "scenarios": [
    {
      "id": "scenario-1",
      "title": "工具借用与归还",
      "actor": "社区居民和管理员",
      "action": "登记借用并确认归还",
      "result": "借还状态可查看",
      "source": "user.answers.outcome"
    }
  ]
}
```

goal 是非空字符串；audience/outcome 为字符串或 null。
scenarios 为列表，每项键恰为 id/title/actor/action/result/source，值均为非空字符串；`source` 必须统一为
`user.answers.<question_id>`，不得使用旧的 `answers.<question_id>` 或把 `agent.assumption:*` 冒充用户事实。
scenario id 使用稳定 ASCII 标识 [a-z][a-z0-9-]*；必须唯一。
场景只能来自目标和回答，不能凭空补造业务。未知 outcome 时允许空列表并保留 unresolved。
ready 必须有非空 audience/outcome 和至少一个有来源的场景。
acceptance 必须包含该项目的业务结果，不能全是“看得到技术建议”等 OpenCoding 自身功能。

platforms 恰含 requested、primary、reason、confidence、unresolved。
requested 是去重平台列表；primary 是候选平台；reason 是非空字符串；
confidence 为 low/medium/high，unresolved 为字符串列表。
平台枚举仍为 windows/macos/ios/android/web/mini_program/cli。
无已确认平台不能 high，默认建议不能伪装成 requested。

stack 恰含 client/backend/database/runtime。各项恰含
technology/reason/alternatives/version_basis/maintenance/cost_note。
除 alternatives 为字符串列表，其余为非空字符串。
capabilities 必须完整且唯一地包含
server/database/api/auth/payment/notifications/admin/storage 八项。
每项恰含 id/need/reason/source/activation_gate；除 need 的枚举外，其他为字符串。
need 为 required/optional/not_needed/unknown；activation_gate 为非空中文说明，
不是 dict，不是授权令牌。当前“做方案”和以后“激活外部服务”不得混淆。

仅保存本机结果不推出 server=required；
本地数据库、远程同步、附件存储、第三方 API、后台分别判断。
unknown 不能在 stack 理由里写成“用户明确不需要”。
需要付费或通知意味着要设计对应业务与风险边界，不代表已经接通或必须立即发出请求。

## W1-C：中文动态文档与真正的业务任务

用户可见标题、正文、任务说明默认中文；保留代码标识和文件名。
core 文档固定为 AGENTS.md、memory.md、PRG.md、plan.md，各有不同职责：

- AGENTS.md：项目工作规则、允许范围、验证方式和确认边界。
- memory.md：真实目标、已确认事实、选择理由、未解决问题与变更依据。
- PRG.md：自动推进规则，明确 INSPECT/PROGRESS/PLAN/DISPATCH/VALIDATE/REPORT/REQUEUE，
  首次失败 FREEZE、保留证据、恢复条件、普通本地事务自动继续和未来外部事务确认。
  不得写成 Product Requirements Guide 或第二份产品需求书。
- plan.md：来自同一任务图的顺序、依赖、产物、验收、回滚与待解决问题，
  不能另生成与 task_waves 不一致的通用清单。

product.md、architecture.md 及条件文档要带上实际 goal、audience、outcome 和场景。
不把 OpenCoding 的“收集需求、展示方案”流程套成用户项目的业务流程。
是否需要界面、数据、接口、权限、支付、通知、部署文档，由平台与 need 决定；
未知需要明确草案标签，not_needed 不安排对应集成工作。
禁止伪造测试通过、已连接、已部署、已付款等状态。

TaskPlan 顶层仍为 schema_version/session_id/revision/tasks/waves/unresolved，
schema_version="1.1"，revision>=0。Task 字段仍按 V1 严格校验。
任务既包括必要的设计文档，也要为已知业务场景给出具体实现与验证任务，
绑定场景、具体源码或测试输出路径和可验证业务结果。
尚无 Host 的实现任务是待执行计划，不能执行、标记成功或假装代码已经存在。
空场景的 revision=0 草案仍可形成澄清/规划任务，不捏造可执行项目。

action 按 type 使用精确字段集合，不允许任意额外字段或 shell/command/cmd/execute：

| type | 精确字段（均包含 type） |
| --- | --- |
| review_requirements | type, document |
| render_document | type, document |
| define_schema / define_interface / define_access | type, capability |
| security_review / plan_delivery | type |
| integration_design | type, capability |
| implement_feature / verify_feature | type, scenario_id, platform |

document 必须是该任务 outputs 中的安全路径；
capability 必须是八项能力之一；`integration_design` 还允许 server、database、auth、storage、api、external_data、deployment
以及 marketplace 的 identity、kyc、logistics、authentication、risk 编排标识。每个外部编排任务必须
绑定人工 Gate、`offline_design_only` 未启用状态和本地草案回滚条件；它不创建账号、不接真实服务、不读取密钥。
scenario_id 必须为安全 ASCII 标识，platform 必须是平台枚举。
新增两种动作只描述工作，真实受控实现/验证的执行适配留待 W2/W4，
不能借本轮契约调用 Host 或任意命令。

所有 path 必须相对、规范且可移植：拒绝盘符/UNC、ADS 冒号、空段、
点段/父目录、Windows 设备名、尾随空格或点、保留治理/Git/事务证据目录。
规范化 Unicode 与大小写后检测同路径及父子路径的冲突。
同波写写冲突必须拒绝；读写冲突也要串行化或拒绝，不能把有依赖的数据并行读取。
重复 ID、缺依赖、环、错误 waves、未知字段必须返回 valid=false/status=invalid/errors，
无论输入是 null、非字典、不完整嵌套列表还是无法哈希的值，都不能抛出意外异常。
校验结构通过后才能构建 set、casefold 或计算拓扑波次。

## W1-A：阻断已复现的数据损坏

用户目标路径与内部证据路径都必须检查所有已有祖先、链接、重解析点、
硬链接及 Windows 别名。普通计划禁止写 .git/.governance/.opencoding。
Evidence 是允许的内部路径，并不因此获得越界或跟随链接的特权。
preview 及失败前检查不创建目录；别名必须安全拒绝或一致规范化，不能 StopIteration。

apply 的计划需绑定 root、所有 before/after 字节哈希、内容、operation 与摘要。
在排他锁内重新验证，并在每个实际文件写入前复查身份和 before 哈希。
中途发现用户改动应停止，准确报告实际已改子集，不得静默覆盖。
并发锁只能保护合作进程；恶意本机进程造成的完整 TOCTOU 隔离无证明时必须明示。

rollback 先验证 receipt、manifest、events 的严格结构、事务身份、
所有 preimage 的路径与哈希、postimage 当前状态，再开始恢复。
证据只提供完整性校验，不应声称能抵抗有权修改整套证据的管理员。
篡改单个备份或 preimage_file 路径必须 fail closed，不能读任意路径并写回。
部分恢复需逐项记录，重试时已恢复项可识别，余项可继续；
不能把“已恢复项等于 before”误报成阻止全部恢复的 postimage 漂移。
准确记录实际改变子集；证据创建失败、写失败、进程中断也需明确恢复状态。
不得吞异常后返回成功。事务重放不能重复制造无意义写入或覆盖后续修改。
脱敏要覆盖通用/算法前缀/加密私钥；未匹配不能保证文本绝无秘密。

## 验收与回滚

三个 worker 仍只拥有 V1 分配的源码和测试。共享契约只读，不跨工作树导入代码。
各自使用本地 fixture，协调者最终从不可变提交集成并检查真实 B -> C -> A 流程。
worker 在自己的新增治理事务中保存 exact paths、before/after SHA256、
新 receipt、测试命令与计数、失败回归、commit 和剩余限制。
保留 WORKER_RESULT.md 的上一版，新报告使用 WORKER_REPAIR_RESULT.md。
允许只提交自己的 exact paths 到已有本地分支；该本地交付无需新人工 Gate。
不能 add .，不能清理他人缓存、旧 receipts、snapshots、Ledger 或整个工作树。
失败时留存证据和分支，不合并回原项目；后续恢复须按哈希判定，禁止盲目覆盖。
