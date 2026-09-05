# APG Release / Deployment Orchestration Test Plan

## Objective
验证 Adaptive Project Governance 是否能系统性编排：需求 → plan-change → Gate → evidence → rollback → deployment preview → release approval。

本项目是 APG 测试夹具，不是儿童知行星球产品开发项目。

## Test stages
1. **Intake** — 记录项目目的、边界、风险和未知项。
2. **Plan-change** — 验证 changed paths、审批、风险分类和 rollback 计划。
3. **Gate selection** — 验证阶段 Gate、缺失 Gate、full fallback 和失败处理。
4. **Evidence ledger** — 验证 receipt、manifest、hash、pre/post state 和 canonical retention。
5. **Deployment preview** — 只生成部署计划与环境边界，不连接真实环境。
6. **Release approval** — 验证发布前检查、独立复核和单一 owner release gate。
7. **Rollback rehearsal** — 在副本上恢复 pre-state，确认原始夹具不被破坏。

## Fixture metadata only
微信登录、多个家长、手机长期保存照片/语音、固定题库 + 规则辅助 AI、封闭试点，均只作为测试输入样例，不代表真实产品实施授权。

## Success criteria
- 每个阶段都有明确输入、输出、Gate 和 receipt。
- 未获批准时不执行 deployment/publication。
- provider、credentials、real data、runtime 和 Git 边界可审计。
- rollback 能恢复精确 pre-state。
- APG 能输出清晰的阻塞、确认和继续条件。
