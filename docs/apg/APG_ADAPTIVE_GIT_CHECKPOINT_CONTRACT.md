# APG 主动自适应 Git 检查点契约

确认日期：2026-09-02  
范围：APG-only、中文初心者、离线 Git 适配预览。

## APG 自动判断何时需要 Git

用户不需要判断“现在该不该 Git”。APG 在成功节点完成、证据齐全、测试通过且 scope 干净时自动生成一个检查点建议：

- `intake-ready`：想法与边界已整理；
- `plan-ready`：计划、依赖和 Gate 已确定；
- `implementation-slice`：一个可独立回退的实现切片完成；
- `validation-passed`：验证套件通过；
- `release-candidate`：公开交付前的本地候选状态。

每个建议都告诉用户：检查点编号、为什么现在保存、应保存什么、出问题退回哪里，以及继续条件。系统自动选择安全默认值，不要求理解 Git 术语。

## 自动回退判断

APG 根据最近一次“已验证”检查点选择回退点。出现证据缺失、测试失败、scope drift 或状态不确定时，系统进入 `FREEZE`：不创建新的成功点，指出最近可回退点和机器可恢复的 `resume_condition`。

## 离线边界

本阶段只输出：`git_action=PREVIEW_ONLY`、建议 checkpoint、建议 revert point、原因、证据摘要和恢复条件。不会初始化仓库、创建 commit、切换分支、推送、发布或修改远端。

真实 Git 写入属于单独外部影响事务，必须另建 plan-change 并触发一次 consequential Gate。
