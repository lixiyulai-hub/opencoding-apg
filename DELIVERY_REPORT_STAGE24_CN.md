# Stage24 交付报告：最小本地 Agent 接线

## 改动

- `product_loop` 接受显式 `LocalAgentTaskExecutor`，失败停止、任务历史、执行中崩溃保护、root/plan 绑定和安全接续。
- `agent_tasks` 增加动作预览、skill/source 指纹、精确 CLI Python 实现/测试约束和一次性授权消费。
- `LocalAgentAdapter` 增加受控文件事务写入；`rollback_product_run` 只处理 receipt 覆盖且仍匹配的文件。
- `acceptance_report` 默认不把历史测试当作当前 observed；通过 run digest、skill hash、receipt、文件 bytes 和测试结果校验后才转 observed。
- CLI 增加 `--preview-agent-tasks`、`--agent-actions`、`--expected-agent-preview`、`--confirm-local-actions` 和 `--synthetic-confirmation`。

## 验证

`tests.test_agent_product_loop_bridge` 3/3、产品闭环/验收状态回归 13/13 focused 通过。Stage24 脚本在全新目录跑通：第一次测试 exit 1，重新预览和授权后 exit 0；验收报告两个当前检查为 observed；回滚文件残留为空。完整源测试仍需按既有 300 秒有界基线解释缺失 APG/Rust/frontend/cargo 资产和慢测试，不能用总数冒称产品完成。

## 限制

没有真实用户确认、provider/model、外部网络、部署、发布、managed loader、Windows/macOS/browser 或目标工具链验收。skill 入口被项目资源发现并 exercise，不代表宿主内部自动加载。

主仓库保持 clean、HEAD `91a20392a856ff4717d230467d32fc154bcb82b1`；所有改动在隔离候选 `/workspace/opencoding_work`。
