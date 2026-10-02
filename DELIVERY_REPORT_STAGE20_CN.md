# OpenCoding Stage20 delivery

Stage20 在 Stage19 基础上完成：

- 新增 `opencoding.toolchain_probe` 和 `--probe-toolchain`，固定 profile 以无 shell、无网络的版本命令检查工具链；只有所有命令成功且 host family 兼容时才标记 `observed`，并把 scope 限定到 runtime。
- capability matrix 可携带经过探测的工具链观察；默认与失败/不兼容情况仍为 `unverified`。
- 新增两个不同领域的离线项目验证：CLI recipe index 与 Web stock delta。两者均真实文件生成、真实测试失败、重新授权修复、测试通过、授权篡改前置拒绝和事务回滚无残留。

本阶段实际 Linux 结果：Python CLI runtime observed；Node/npm Web runtime observed（不代表浏览器或 Web 应用工具链）；Windows .NET profile 因 host 不兼容保持 unverified。所有输入/确认是 synthetic，model/provider/external services 未使用，managed loader 未观测。

阶段 ZIP 仅内部保存；未添加 APG/Rust/frontend/cargo 资产，未部署、发布、合并或写入远程 Git。
