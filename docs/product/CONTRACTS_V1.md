# OpenCoding 跨工作树契约 V1

日期：2026-09-05。实现目标为 Python 3.11+；schema_version 固定 "1.0"。
三工作树使用新 opencoding 包；仅协调者拥有 __init__.py、__main__.py、
service.py、pyproject.toml、共享 schema、tests/__init__.py 和集成测试。
不要引入 sibling worktree 路径依赖、修改 sys.path 指向其他工作树或复制别人的未完成代码。

## 共通约束

公开结果为可 JSON 序列化的 dict/list/string/bool/null，不放 dataclass 实例或 Path。
任何读取/写入必须接收显式 root，不使用 cwd 隐式决定用户文件位置。
sha256 指 UTF-8 编码规范 JSON（sort_keys=True, ensure_ascii=False,
separators=(",", ":"), allow_nan=False）或精确文件字节的 SHA256，字段必须说明是哪一种。
运行时数据使用 .opencoding，不覆写旧 .governance 或旧 Ledger。
未知字段/不兼容版本应失败，不能忽略安全字段。

## W1-A: 安全事务内核

独占 opencoding/safety.py、opencoding/transactions.py、
tests/test_product_safety.py、tests/test_product_transactions.py。

建议公开函数：

- sanitize_text(text: str) -> str：秘密信息脱敏，只返回可持久化文本。
- inspect_sensitive(text: str) -> dict：返回风险分类，不返回匹配的秘密原文。
- evaluate_action(action: dict, approval: dict | None = None) -> dict：
  返回 decision=allow/confirm/block、reason_codes、action_digest。
- preview_changes(root: Path, files: dict[str, str]) -> dict：
  完全不写盘，返回 root、plan_digest、entries 和状态。
- apply_changes(root: Path, plan: dict, *, approved_digest: str) -> dict：
  消费精确预览，不接受任意新文件列表；返回 transaction_id、status、
  changed_paths、receipt_path、rollback_ref。
- rollback_changes(root: Path, transaction_id: str) -> dict：
  仅恢复该事务；postimage 漂移即拒绝破坏用户新改动。

Action 至少绑定 kind、root、plan_digest、targets、external、cost_limit、
data_scope、irreversible。kind 为 preview/local_write/local_run/network/
provider/payment/notification/deploy/git_publish。规划支付与 payment 动作不能混淆。
Approval 至少绑定 action_digest、root、expires_at、approved，拒绝错 root、
过期、缺字段、摘要不符。生产持久撤销/一次性消费由 W2 扩展，未实现前不能声称具备。

preview entries 至少包含 path、operation=create/update、before_sha256（不存在为 null）、
after_sha256、content；计划含 schema_version。apply 必须重新验证所有 entry 与摘要。
preimage 和 append-only receipt 置于目标 root/.opencoding/transactions/<id>。
除声明写入与事务证据，不写其他文件。预览及失败前检查不能偷偷建目录。

## W1-B: 会话与决策

独占 opencoding/intake.py、opencoding/decisions.py、opencoding/sessions.py、
tests/test_product_intake.py、tests/test_product_decisions.py、tests/test_product_sessions.py。

- new_session(goal: str) -> dict
- answer_question(session: dict, question_id: str, answer: str) -> dict
- next_questions(session: dict) -> list[dict]
- build_recommendation(session: dict) -> dict
- save_session(root: Path, session: dict) -> dict
- load_session(root: Path, session_id: str) -> dict

Session 至少包含 schema_version、id、revision、goal、answers（question_id 到答案）、
requirements（结构化事实）、questions、state=clarifying/recommendation_ready。
每个问题有 id、question、why、required。未知/否定/冲突不得合并为 false；
在用户未回答时不谎称澄清完成。不要求小白指定 React/数据库类型等实现细节。

Recommendation 至少包含 schema_version、session_id、revision、status、
platforms（requested/primary/reason/confidence/unresolved）、stack、
capabilities、assumptions、unresolved、acceptance。
stack 是 client/backend/database/runtime 的 dict；各项包含 technology、
reason、alternatives、version_basis、maintenance、cost_note。
capabilities 为列表：id=server/database/api/auth/payment/notifications/admin/storage，
need=required/optional/not_needed/unknown，reason、source、activation_gate。
activation_gate 指未来执行的确认要求，不等于必须阻止离线生成方案。

无平台信息可给建议默认值，但 requested 仍为空、confidence 不得 high，
且 unresolved 保留问题。支持苹果手机、Mac 桌面、小程序、CLI，避免桌面=Windows。
会话持久化必须脱敏并通过版本冲突检测；未知复杂语义走追问而非假定已理解。
为独立实施可在 W1-B 自己实现内部脱敏；集成时协调者统一到 safety 模块，
不要在 W1-B 创建安全模块或临时 stub。

## W1-C: 文档与任务图

独占 opencoding/documents.py、opencoding/planning.py、
tests/test_product_documents.py、tests/test_product_planning.py。

- render_documents(recommendation: dict) -> dict[str, str]
- build_task_plan(recommendation: dict) -> dict
- validate_task_plan(plan: dict) -> dict
- task_waves(plan: dict) -> list[list[str]]

输入就是 W1-B Recommendation，测试可使用同 schema 的本地字典 fixture；
禁止依赖尚未集成的 W1-B 实现。
文档选择根据 need；核心 AGENTS.md/memory.md/PRG.md/plan.md 必须各有不同职责与正文，
并按需生成产品、架构、界面、数据、接口、权限、安全、支付、通知、部署文档。
这些函数只渲染；文件落地必须由 service 调用 W1-A 事务，不重复实现写盘机制。
不得把草案、未知决策或尚未运行的测试写成已通过。

Plan 至少包含 schema_version、session_id、revision、tasks、waves、unresolved。
每个 Task 至少有 id、title、description、depends_on、inputs、outputs（相对精确路径）、
action（类型化动作描述，不能任意 shell 字符串）、acceptance（可验证条目）、
rollback、retry（max_attempts）、activation_gate。
任务应体现业务验收和场景，而不是只列 server/database 名词。
显式检测循环、缺失依赖、重复 ID、同波写入路径冲突与未知 action 类型。
预算、平台或需求未知时，可形成草案，但不可伪造可执行命令。

## W2/W3 集成接口边界

service 负责 Session -> Recommendation -> Documents/Plan -> Preview -> Apply ->
Scheduler，避免 UI 自己拼接安全逻辑。
Scheduler 使用 SQLite；真实 task run 的输入摘要、attempt、exit_code、timeout、
日志脱敏摘要、产物哈希均绑定 run_id。mock 结果不能设置 live_verified。
Executor 接受结构化 action 和已核验 ActionContext；shell=False、明确 cwd 和超时。
Host 适配器与具体外部连接器必须在 W1 通过后另立受控实施事务，不由 foundation worker 顺手启动。

## 交付与报错

所有 worker 增量测试优先 unittest discover -s tests -p test_product_<area>*.py。
同时运行全部旧测试并报告数量；原始默认 unittest 0 项问题由协调者在 W2 修复。
预期阻断用结构化 status/reason_codes；程序错误不能 catch-all 后返回成功。
WORKER_RESULT.md 必须说明改动、测试、限制、commit、下一接口，以及哪些能力尚未完成。
