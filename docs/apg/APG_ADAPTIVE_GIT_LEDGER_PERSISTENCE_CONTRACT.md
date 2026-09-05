# APG 本地 Ledger 持久化适配器契约

确认日期：2026-09-02
范围：APG-only；仅项目本地 `.governance/progress/apg-adaptive-git-ledger.json`。

## 契约

适配器接收已通过离线 projection 的 `snapshot`，写入前校验 `schema_version=1.0`、固定 `target_path`、记录唯一性、源 `snapshot_digest`、目标 containment 与 `preimage_sha256`。计划输出 `idempotency_key`、`postimage_sha256`、`write_plan_digest`、`atomic_replace=true` 和 rollback 命令。

`build_plan` 永远不写盘；`persist(..., apply=True)` 才执行本地写入。写入使用同目录临时文件、flush/fsync、`os.replace` 原子替换，并记录幂等标记文件。相同幂等键和 postimage 返回 `ALREADY_PERSISTED`。基线漂移、非法输入或原子替换失败返回 `FREEZE`，不产生新 ledger 内容。

所有网络、远端、Provider、Host、runtime、部署、发布和 Git action 均为 false；本事务不初始化或操作 Git。
