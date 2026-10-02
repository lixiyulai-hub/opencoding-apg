# OpenCoding Stage12 delivery

Stage12 在 Stage11 单包恢复基础上，把 skill 安装/发现/运行整理成可复用契约，并用独立的离线阅读清单项目验证。

- `scripts/run_skill_contract.py` 默认只安装、发现和加载；执行必须显式传入 `--run-actions --confirm-synthetic --execution-root --action-file`，缺少合成确认时返回 `blocked`、exit code 2。
- 新项目生成 `AGENTS.md`、`memory.md`、`PRG.md`、`plan.md` 等文档，preview/确认后真实写入；故意失败的页数测试经修复后通过；transaction receipt 回滚无残留。
- Linux core focused：原有 125/125 加上 2 项可复用契约测试，共 127/127；skill 入口、文件、测试和授权账本均有 JSON 证据。输入/确认是 synthetic，model=false、external_actions=false，managed loader=null。
- Stage11 的 192 source（含 Stage11 文档）全部保留；无 APG artifact、provider、密钥、网络、部署、远程 Git、合并或 Release。

详细契约见 `source/docs/product/STAGE12_REUSABLE_SKILL_CONTRACT_CN.md`，机器证据见 `evidence/STAGE12_RUN.json`。
