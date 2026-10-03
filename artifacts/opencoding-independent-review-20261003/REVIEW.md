# 独立复核：stage28 基线与需求来源

日期：2026-10-03

## 基线核验

当前工作分支实现修复后的完整 HEAD 是 `faae5a6a7dfb2b0fa9cf2d65f7dcf512216617fb`。
本轮上一证据 checkpoint 是 `4849a95d2e4e7bbf1597098b1acbe8dc5abdad4a`，其父提交为
`81105b661fe62ab475a13b29ddefe6412d18e468`，共同祖先为
`91a20392a856ff4717d230467d32fc154bcb82b1`（当前 `origin/main`）。

本地对象库没有 `9c05a5a`，也没有 `checkpoint/stage28-20261002` 引用；
`git rev-parse --verify 9c05a5a^{commit}` 返回 `fatal: Needed a single revision`，
`git fsck --no-reflogs --unreachable` 未发现该提交。因而 stage28 与当前分支的
merge-base、左右提交差异和 694 项测试清单均 **not-computable**。不能据此宣称
stage28 的成果已经保留，也不能把当前 226 项测试当作 stage28 的替代物。

## 复核发现与修复

1. 发现 fail-open 反例：领域回答“责任边界待法务确认”等含待确认语义的文本，旧解析器会标成 `known`，可能把未完成需求推进到 `ready`。
2. `intake.py` 现在把“待确认/待澄清/需要确认/尚未确认/未确定”等标记保留为 `unknown`；新增回归测试确保 Recommendation 仍为 `draft`。
3. 能力来源不再凭空列出不存在的 `user.answers.*`；未收到回答时标为 `system.unresolved.*`，真实回答才进入 `user.answers.*`。
4. `agent.assumption:` 仍与用户事实分开；本轮未发现其被写入 `requirements` 或用户场景 source 的新反例。

## 安全移植方案（待取得 stage28 对象后）

1. 提供 stage28 的只读 bundle 或包含该对象的本地 clone；先验证完整 SHA、签名/标签（如有）和测试入口。
2. 在临时引用上执行 `git merge-base`、`git rev-list --left-right --count`、`git diff --name-status` 和 `git range-diff`，不改动 `work`、`origin/main` 或 stage28 对象。
3. 建立独立 `review/stage28-compare` 和 `integration/stage28-marketplace` 分支，保留双方引用；按文件/提交分类后只 cherry-pick 必要提交。
4. 在移植分支分别运行 stage28 原有测试、当前全量测试和 marketplace 定向测试；出现冲突或测试回退时停在 FREEZE，不合并、不强推。
5. 只有差异清单、测试证据和回滚点都可复核后，才提出新的人工 Gate；当前 `work` 不做 reset、merge 或覆盖。
