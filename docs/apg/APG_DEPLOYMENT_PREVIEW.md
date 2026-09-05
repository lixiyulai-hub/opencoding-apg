# APG Deployment Preview

本文件描述离线部署预览夹具。它验证 APG 能在发布动作前读取本地证据、计算 Gate，并对不完整事务返回确定性 `BLOCK`。

## 输入

`evaluate_preview()` 只接受本地 JSON/映射，不读取网络，不调用 provider，不读取凭据，也不接触真实数据。关键字段为：

- `release_approval=true`：显式 owner 发布审批证据。
- `rollback_evidence=true`：可验证的回滚证据。
- provider、network、credentials、real-data、runtime、deployment、publication、pilot 需求字段：任何一项为 `true` 都阻断预览。

## 输出

- `status=ready-for-preview` 仅在审批和回滚证据齐全且所有外部/真实环境需求均为 `false` 时返回。
- 其他情况返回 `status=BLOCK` 与稳定排序的 `blocker_codes`。
- `release_action_executed`、`publication_action_executed`、`deployment_action_executed` 恒为 `false`，`external_actions` 恒为空数组。

## 边界

这是 APG 测试夹具，不是儿童知行星球产品实现。微信登录、多个家长、照片/语音长期保存、固定题库和规则辅助 AI、封闭试点仅作为 fixture metadata。真实部署、发布、provider、凭据、网络、运行时和 Git 均不执行。

## 验证

```text
python -X utf8 -m unittest discover -s tests -p 'test_*.py'
python -m project_governance doctor . --json
python -m project_governance audit . --json
python -m project_governance check . --phase full --json
```

`rollback_copy()` 只在临时副本上演练；项目中的 `MODIFIED_FILE` 保持修改状态，以便验证 pre-state 恢复与证据留存互不破坏。
