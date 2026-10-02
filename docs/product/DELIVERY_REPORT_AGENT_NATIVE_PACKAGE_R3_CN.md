# Agent-native package resource R3 阶段报告

## 目标与判断

本阶段复核了“安装包没有 skill 资源，因此 `project preview/run` 只能从源码 checkout 执行”的边界。该边界不是有意的安全隔离，而是打包清单与运行时发现逻辑遗漏：`pyproject.toml` 只列出 Python 模块，`MANIFEST.in` 也未包含 skill 资源，而 `skill_identity()` 固定读取 `.agents/skills/opencoding`。

## 代码变更

- 新增 `opencoding/resources/skill/SKILL.md` 与 `skill.json`，由 `pyproject.toml` 的 package-data 和 `MANIFEST.in` 明确纳入 wheel/sdist。
- 新增 `opencoding/skill_resources.py`。源码 checkout 按 `.agents/skills/opencoding`、`skills/opencoding`、包内资源的顺序查找；安装包没有 checkout 时使用 `opencoding/resources/skill`。部分资源直接失败，不能被低优先级副本静默遮蔽。
- `agent_tasks.skill_identity()` 改用该发现器；`CodexSkillHost` 在显式项目、Codex home 和额外根目录之后发现包内资源。授权对象、预览摘要、源指纹、同用户子进程、Provider/外部动作边界未放宽。
- 打包合约覆盖 wheel/sdist 成员、源码资源字节一致性、干净安装资源发现，以及 wheel 和从 sdist 重建 wheel 的合成产品闭环。
- 文档更新为：已安装 wheel/sdist 可以离线执行 `project preview/run`；完整 checkout 资源优先，Codex 专用安装脚本仍需 checkout。

## 合成小白验收

测试使用明确标注为 synthetic fixture 的一句话需求：“我想要一个离线中文命令行借还登记工具，记录物品与借用人并查看状态。”流程覆盖完整回答、规则计划、文档应用、动作 review/preview、缺少授权时拒绝、合成授权执行、实际 Python unittest 复核、状态复核和产品文件回滚。测试不连接 Provider、不使用真实数据、不执行部署或 Release。结果确认执行记录含 `synthetic_confirmation`，因此不能替代真实用户验收。

## 测试证据

- 全量：`Ran 663 tests in 155.578s`、`OK (skipped=9)`，即 **663 总数、654 通过、9 跳过、0 失败、0 错误**，返回码 0。原始日志位于 `/workspace/stage28-validation-20261002/package-r3-full-cargo/`。
- 无 Rust runtime 的复跑另存于 `/workspace/stage28-validation-20261002/package-r3-full/`，唯一错误是既有 cargo 测试找不到 `cargo`；显式使用预装 `/workspace/.onboarding/rust-root` 并设置 `CARGO_NET_OFFLINE=true` 后通过。
- cargo 离线回归：4 passed、0 failed，doc-tests 0；日志位于 `/workspace/stage28-validation-20261002/package-r3-final/`。
- 打包专项覆盖源码 wheel、sdist 重建 wheel、仓库外干净虚拟环境和无网络 pip 安装；两种安装产物均完成上述 synthetic 闭环。
- 本地 skill verifier 的 managed loader 仍只能报告 `host_unverified`；本阶段没有把项目发现误报为托管 Host 加载。

## 风险与回滚

风险是包内资源与 canonical `.agents` 文件若未来不同步会造成不同身份。资源合约逐字节比较两份文件，且预览/执行继续绑定资源摘要与完整 source fingerprint；任意漂移都要求重新 preview。若需回滚，撤销本阶段提交即可恢复源码-only 发现；不需要迁移用户项目数据。合成产品回滚只删除本次确认时不存在且仍匹配回执的代码/测试文件，文档事务仍按其独立 transaction 回滚。

## 下一步与边界

下一步是保留本 checkpoint 的独立 detached 验证和远端推送证据。真实用户、真实 Provider/Host、连接器、平台工具链、部署、合并和 Release 仍不在本阶段范围内；GitHub 草稿 PR 权限也不依赖、不重试。
