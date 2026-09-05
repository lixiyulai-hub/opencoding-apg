# APG Release Approval Simulation

已记录项目负责人“授权发布”，并将其作为本地 APG fixture 输入。由于当前项目是离线测试夹具，不存在可发布产品、运行时、provider、凭据或真实发布目标，因此本事务只执行本地 release simulation。

模拟输入同时包含 `release_approval=true` 与 publication/deployment/provider/network/credentials/real-data 请求。预期 APG 返回 `BLOCK`，且所有外部动作执行标志保持 `false`。

实际发布仍属于独立外部事务，不在本测试夹具中执行。
