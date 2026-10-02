# OpenCoding Stage18 delivery

Stage18 新增跨平台 Agent 执行适配层的能力矩阵与最小检查：

- `opencoding.host_capabilities` 统一报告 host、target、动作效果、模型/外部/sandbox/进程边界、路径/超时保护和 target toolchain 的 `unverified` 状态。
- `scripts/check_agent_adapter.py` 默认只读检查；显式 `--probe-local-write --confirm-synthetic` 时仅在临时未链接根执行一次本地 `write_text`，不调用 provider、网络、shell、cargo、浏览器或 managed loader。
- `skill.json` 与两份 `SKILL.md` 已声明能力矩阵和检查入口；`verify_codex_skill.py` 会验证这两个入口存在。
- Stage18 定向测试 32/32 通过；最终 ZIP 恢复后运行的跨阶段核心/skill/CLI/调度/规划/服务选择测试为 129/129 通过（证据：`evidence/stage18-restored-core-129.log`）。

Linux 实测将 `windows`/`web` 作为目标标签与宿主分离，均保持 `target_toolchain_verified=false`；managed loader 仍未观测。历史 APG、Rust、frontend、cargo 资产继续保持缺失边界，不复制、不补造、不伪造全量 PASS。

阶段 ZIP 仅内部保存，不上传 Library；未执行真实模型、外部服务、部署、远程 Git、合并或 Release。
