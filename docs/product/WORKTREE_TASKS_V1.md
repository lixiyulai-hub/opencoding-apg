# OpenCoding 工作树任务单 V1

日期：2026-09-05。所有实施任务模型固定 gpt-5.6-luna，推理强度 high。

## 原始范围

原工作区 E:/儿童知行星球 保留；其中没有 .git，不能原地 init。
隔离集成仓库：
E:/儿童知行星球/.tmp/opencoding-product-foundation-20260905/integration
三个工作树为上述目录的同级 wt-safety、wt-intake、wt-planning。
分支 w1-safety、w1-intake、w1-planning 从同一个本地 seed commit 创建。
没有 remote，不执行 fetch/pull/push、不启动真实 Provider、Host 或部署。

## 共通交付规则

先读取分配工作树内 AGENTS.md、docs/product/ARCHITECTURE_V1.md、
CONTRACTS_V1.md 与本文件。所有 shell 命令显式使用分配的工作树为 workdir。
用户已授权当前产品化实施；本任务单仅授权各自文件及自己的新增治理事务证据。
APG doctor/audit/plan-change 从工作树根运行，先 preview 后 apply，不改旧治理记录。
可调用已安装本地 controller 的绝对脚本路径，无需全局安装。
controller plan-change 只保存计划证据，不是源码自动 apply 或测试通过证明。

你不是独自在代码库工作。不得回滚他人修改，不得改共享契约和其他模块，
不得给共享 __init__.py 增加 stub。接口冲突先报告，由协调者处理。
预期错误和失败路径应有测试；当前旧 tests 只作基线。
修改用 apply_patch，子进程 shell=False，模拟账号/秘密值仅使用明显测试数据。
只在自己的 Git 工作树提交 exact owned paths 及新增自己的治理记录。
交付 WORKER_RESULT.md：变更清单、测试命令/数量、已知限制、提交号、后续依赖。
WORKER_RESULT.md 与 worker 的新治理记录不直接合并；代码由协调者审查后集成。

## W1-A：安全事务

工作树 wt-safety。独占文件详见 CONTRACTS_V1.md W1-A。
实现动作级风险分类、脱敏前风险识别、摘要绑定确认、严格路径、
真实 preview/apply/rollback 与不可覆盖事务证据。
必须测试：否定业务需求不能变执行授权；纯规划不误拦截；
未知危险 action 拒绝；假 token 风险被检测且不泄露；
越界/别名/符号链接/硬链接/漂移/重放/部分失败不产生静默成功。
生产级跨平台沙箱若尚无证明必须明示，不用路径检查冒充 OS 隔离。
不实现 UI、会话、技术方案、Host、支付调用或任意命令执行器。

## W1-B：需求会话与具体决策

工作树 wt-intake。独占文件详见 CONTRACTS_V1.md W1-B。
实现真正多轮业务追问、回答回流、改答版本、会话持久与恢复、
平台推荐、八项能力判断、具体技术方案目录与解释。
测试重点：不联网不登录不支付、Mac桌面、苹果手机、小程序、CLI、
未说明设备、高置信度限制、冲突回答、未回答不能ready、会话秘密脱敏和并发版本。
不要让小白选框架；方案有具体技术及依据，但未实测版本和费用必须明示。
不调用模型、网络或 provider；复杂语义走确认/unknown 兜底。
不实现写项目文档、事务内核、实际执行。

## W1-C：文档与业务任务图

工作树 wt-planning。独占文件详见 CONTRACTS_V1.md W1-C。
实现不同职责、按需求出现的 Markdown 文档与具体业务任务图。
测试重点：十二份正文不能一样；明确不用支付就不安排支付文档/任务；
任务有真正的产物、验收、动作和依赖；拒绝循环、缺失节点、同波写冲突。
只生成文档内容和任务计划，不写入用户项目、不运行命令、不接外部服务。
不要依赖尚未完成的 W1-B，使用共享 Recommendation schema fixture。

## 后续下发顺序

1. 协调者审查 W1 的 exact diff，冻结共享契约，集成三个提交并执行交叉测试。
2. W2-entry：中文 CLI/service、非零测试发现、真实写文档、会话恢复。
3. W2-executor：SQLite 状态机、允许动作的本地执行、取消/超时/崩溃恢复/Requeue。
4. W3-workbench：中文本地 Web 工作台和安装包，浏览器及干净环境验证。
5. W4-host / W4-integrations：受控 AI coding Host、八类连接器，真实激活单独确认。
6. W5-review / W5-release：跨平台、独立新手体验、公开文档与发布。

W2 及以后必须基于集成通过的前序代码创建新的工作树，不从过时 seed 并行盲写。
任务排队不是运行中；启动必须记录实际 task/thread ID 与模型。
