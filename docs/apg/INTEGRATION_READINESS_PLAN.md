# Integration Readiness Plan

本阶段只做本地准备，不连接真实服务。

| 边界 | 下一步 | 本地前置验证 | 发布 Gate |
|---|---|---|---|
| 微信登录 | 确认账号类型、登录和手机号策略 | auth fake + role matrix tests | owner + privacy review |
| 家长/监护人 | 确认 owner/co-guardian 权限 | permission matrix tests | independent review |
| 儿童证明存储 | 确认私有存储、30 天删除、家长提前删除 | storage fake + deletion tests | child-data review |
| AI Gateway | 确认 allowlist、无开放闲聊、家长升级 | deterministic fake gateway tests | safety review |
| 通知 | 确认通知范围、失败和关闭策略 | notification fake tests | product approval |
| 部署环境 | 确认环境、密钥、备份、恢复 | local deployment manifest lint | operations review |
| 微信发布 | 准备隐私披露、审核材料 | package/static contract checks | explicit release approval |

## 当前可自动执行

- 编写和运行本地 fake adapter 测试
- 补充权限、删除、审计事件契约
- 校验部署清单格式，不连接任何环境
- 继续维护 rollback 与 evidence

## 当前不可自动决定

账号主体、数据留存例外、AI provider、通知策略、生产环境和发布日期必须由项目负责人确认。
