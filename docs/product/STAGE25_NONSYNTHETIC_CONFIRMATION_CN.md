# Stage25：同一预览动作的非合成确认

本阶段复用了 Stage24 的离线命令行植物浇水追踪器预览动作。用户已明确授权系统执行项目内部正常测试、失败修复、回滚和验收，因此本次 LocalAgentTaskExecutor 使用 `confirmed=true`、`synthetic=false`，并以 `user-confirmed-stage24-*` 绑定确认标识。

实际链路为：采用计划 → 预览动作和 before hash → 用户确认授权 → `product_loop` 注入 `LocalAgentTaskExecutor` → `LocalAgentAdapter` 事务写入/测试 → receipt 与 skill/source hash 验证 → acceptance report 标记本次检查为 `observed`。第一次实现故意让测试失败；重新预览并用新的确认标识修复后，测试通过；随后只回滚本次 receipt 覆盖且仍匹配的新增文件，项目文件残留为空。

证据：`evidence/STAGE25_NONSYNTHETIC_CONFIRMATION.json`。输入问答仍来自 Stage24 离线夹具，不能据此声称真实用户需求访谈；本阶段只把执行确认标记为非合成。宿主为 Linux，目标标签为 `cli`；没有调用模型、provider、网络、密钥或外部服务。`managed_loader=null` 仍表示未观测托管宿主内部加载，`sandbox=false` 表示同用户子进程边界。
