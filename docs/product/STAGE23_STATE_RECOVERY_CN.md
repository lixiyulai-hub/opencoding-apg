# Stage23：验收状态恢复与阻断一致性

Stage23 新增持久化验收状态 `opencoding-acceptance-state-v1`。状态保存报告摘要、每个 check 的三态和状态摘要；重启读取、重复 resume 或报告变化都会重新校验，状态不会从 `blocked`/`unverified` 漂移成 `observed`。

`require_observed` 是动作前的门。provider/model 的 `blocked` check 和 Windows 目标执行的 `unverified` check 都在创建适配器授权之前停止，因此不会消费 authorization claim，也不会写目标文件。只有 `local_structured_actions=observed` 可以进入本地适配器。

`scripts/run_stage23_state_recovery.py` 在临时 Linux 根目录完成一次 observed 本地动作、两次 resume、blocked Gate、unverified Gate，并重新运行三个非模板项目的失败→修复→回滚矩阵。所有输入和确认仍是 synthetic；没有模型、provider、外部服务、部署或 managed loader。
