# OpenCoding Stage15 delivery

Stage15 完成三项目并行下的授权、确认和恢复隔离验证，保持 Linux 离线与 synthetic confirmation。

- `confirmation_binding` 绑定 confirmation label、root、action digest、targets、scope、expiry；错项目、错动作和篡改确认均在写入前拒绝。
- 三项目并行各自执行合法写入、故意失败批次和跨进程 receipt recovery；foreign roots 无污染，失败项目停止后续动作。
- transaction manifest 增加 canonical root；移动整个项目目录后 rollback 为 `blocked`，新根文件保持原状。旧无 root 回执只能兼容读取，恢复 helper 不接受。
- 核心定向测试 165/165 通过；Stage15 authorization stress acceptance 9/9 true；Stage14/Stage13 历史证据保留。

说明与机器证据见 `source/docs/product/STAGE15_AUTHORIZATION_RECOVERY_CN.md`、`evidence/STAGE15_AUTHORIZATION_STRESS.json`、`evidence/STAGE15_PREFIX_MOVED_ROOT.json`。

边界：confirmation binding 不是签名、身份认证或一次性审批；source fingerprint 不覆盖 symlink 外部目标或动态依赖闭包；Python 同用户子进程、`sandbox=false`；无 model/provider/external/network/deploy/remote Git/managed loader 验收。主仓库不变。
