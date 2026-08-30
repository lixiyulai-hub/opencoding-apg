# APG 项目验证（中文说明）

本仓库用于验证 **Adaptive Project Governance（APG）** 是否能够系统性编排项目治理、Gate、证据、回滚、部署预览与发布边界。它不是“儿童知行星球”产品源码，也不代表该产品已经实现、部署或上线。

## 验证结论

- APG 0.6.5-dev.20260819（commit `320b79aff044a691f38d97421aa1fa9024237573`）测试结果：**PASS**。
- 源绑定进度：`100/100`。
- 独立复核：**PASS**；离线单元测试：15 项通过。
- 发布内容仅包含 APG 验证结果、治理流程图和中文说明。

## 治理流程图

可编辑源文件：[`docs/diagrams/APG_GOVERNANCE_RELEASE_FLOW.drawio`](../diagrams/APG_GOVERNANCE_RELEASE_FLOW.drawio)

预览图：[`docs/diagrams/APG_GOVERNANCE_RELEASE_FLOW.svg`](../diagrams/APG_GOVERNANCE_RELEASE_FLOW.svg)

流程依次覆盖 Intake → plan-change → Gate → Evidence/Receipt → Independent Review → Deployment Preview → Release Boundary，并显式展示 PASS、BLOCK 与 Rollback 分支。

## 发布边界

本次 GitHub Tag/Release 是 **APG 项目验证结果发布**，不包含产品部署、微信接入、外部 AI、provider、真实儿童数据、运行时、试点或生产发布。
