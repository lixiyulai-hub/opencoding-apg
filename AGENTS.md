<!-- project-governance:begin -->
<!-- project-governance:metadata policy-version=0.1.0 policy-digest=93e44a674b20aa6cb3fd6b75e70a9b1a663d16e104dfa8c9f98cab92f9e697f7 generator-version=1 scope=. body-digest=f9021b39f96c15959ebf5c29dc80651855189ca02ec874a0cc889870f258ea8f -->
Run governance checks from the project root.
Required phases: inspect, validate, verify, report.
Validation command: - `python -X utf8 -m unittest`
Do not install tools globally or claim external protections.
<!-- project-governance:end -->

## 最终状态快照

每个实质性检查点（无论成功或失败的终态、`BLOCK` 或 `CONFIRM`），面向
用户的最终回复必须以恰好一个「最终状态快照」小节结尾。它是最后的小节：
其后不得再出现分类、结论或下一步文字。

快照必须说明：当前阶段、已完成工作、有来源依据的总进度与当前阶段进度
（或缺少来源时写 `not-computable`）、当前交付与 Gate 状态、下一步自动
工作、至多一个真实的人类 Gate、阻塞项与独立复核状态、后续交付边界，
以及确切的 Continuation（恢复接续）条件。当已声明项目路线图时，还必须
分别说明项目总进度与当前项目阶段百分比、当前事务、后续阶段以及按序排
列的后续事务。当前事务是用户下一步可执行的工作；不得把更晚的确认边界
说成当前步骤。绝不按时间、token、改动文件数、收据或 Gate 数量推断百分比。
普通用户可见文字使用中文；机读字段、错误码、命令与文件名保持精确原文。
仓库控制器对其自身人类可读的非 JSON 路由机械执行该约束；任意宿主或模
型的最终自由文本不受仓库代码拦截，宿主/适配器侧执行属于后续独立事务。
