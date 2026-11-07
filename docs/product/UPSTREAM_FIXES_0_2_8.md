# 0.2.8 Early Access 修复说明

本次修复面向新项目试用，仍为 Early Access。未将真实业务项目的 Host、独立审核、应用、浏览器验收和回滚全链声明为已验收。

- 文档生成和任务图共用已有文件的实际大小写；保留用户及 APG 内容，OpenCoding 自有块经摘要校验更新，漂移和链接保护继续有效。
- 安装诊断新增 `python -m opencoding.preflight` 和源码独立脚本，报告实际 import origin、版本、内容摘要和降级路由。统一技能版本为 `0.1.1`；可用不随分发打包的 `runtime.local.json` 定位本机运行时。
- `--confirm-scenarios SESSION_ID JSON_FILE` 支持明确确认的业务旅程、输入、失败分支和验收。需求或事实变化会使旧计划绑定失效；此入口不等于任意自然语言的自动拆分质量已经验收。
- `--stage-report SESSION_ID` 分开显示计划、文档、执行、Gate 和事务证据，避免将计划采用或测试子进程成功误报为交付完成。真实审核和用户验收缺失时保持未验证。
- `compat/apg-output-contract/` 保存 APG 0.3.0 的隔离修复候选，为绑定 policy 的 phase 检查增加逐 Gate 输出目录。它不随 wheel 安装，不替换全局 APG；组合限制和扫描上限见该目录 README。

本地验证：工作区完整 unittest 共 828 项，824 通过、4 跳过、0 失败；其中包含已有未提交的 Host 漂移回归，不将该数量当成干净发布树的测试数量。Windows/WSL 定向回归通过；实际已安装 APG 初始化后的原生文档预览、应用、重复预览和回滚通过，原文逐字节恢复。入口同步后另跑安装诊断与打包回归，并从不同工作目录核对本机入口摘要。

新项目使用方式及自检命令见 `OFFLINE_INSTALLATION.md`。用户可先在新项目调用 `/opencoding`，明确项目目录和目标；真实部署、支付、凭据使用和公开发布仍沿用明确授权边界。
