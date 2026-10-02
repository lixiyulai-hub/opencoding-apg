# Stage27：Linux 本地 Web/Node 闭环验证

本阶段把 OpenCoding 的 skill 入口接到一个真实的本地 Web 小项目上，验证范围是宿主 Linux 上的 Node/HTTP 运行时。它不是浏览器、Vite、部署或托管服务验收。

## 已执行链路

1. 通过现有服务入口创建会话，使用明确标注的离线回答夹具完成需求追问：离线植物浇水追踪器，记录植物与最近浇水日期，找出逾期植物；数据持久化、账号、通知、支付、外部数据均回答“不需要”。这些回答不是实时用户访谈。
2. 运行评估、采纳方案、预览并应用项目文档。产生 `AGENTS.md`、`memory.md`、`PRG.md`、`plan.md` 及架构、部署、接口、安全和 UI 文档；方案标记为 `web`，客户端建议为 `TypeScript + Vite`。
3. 通过项目级 `.agents/skills/opencoding` 发现并加载 `opencoding.agent_adapter:LocalAgentAdapter`，动作预览包含 `write_text` 与 `node_script`。执行确认使用用户已授权的本次产品事务标识，`synthetic_confirmation=false`；没有虚构逐动作人工点击。
4. 首次实现故意使用严格 `>` 判断，Node 测试真实退出码 1，错误为 `test_failed_or_empty`；执行停止并留下失败回执。
5. 重新预览修复版，重新授权并恢复同一 product run。Node v24.19.0 在同用户子进程中实际执行 TypeScript 测试（`--experimental-strip-types`），启动 `127.0.0.1` 临时端口 HTTP 服务并验证：`/` 返回 200 且页面含 `Due: 1`，`/missing` 返回 404；输出 `OPENCODING_TESTS_RUN=2`，退出码 0。
6. 运行产品证据校验、验收报告和事务回滚。两个项目文件均是本次执行中新建且仍与回执匹配，回滚状态为 `files_rolled_back`，`residual_project_ts=[]`。

## 证据

- 机器可读结果：`evidence/STAGE27_LOCAL_WEB.json`
- Node/HTTP 收据摘要：`evidence/STAGE27_NODE_HTTP.log`
- 浏览器阻塞复现：`evidence/STAGE27_BROWSER_BLOCKER.json`
- 本次运行根目录（保留完整账本供复核）：`/workspace/opencoding_stage27_local_web_final`
- 关键结果摘要：首次验证 `failed/test_failed_or_empty`，修复后 `succeeded/tests_run=2`，记录摘要 `record_digest=92f6b0ec0b66223d519a9c314e42eebb7ecc3baefbad4d355dd853931e621b8d`。

## 能力边界

`local_structured_actions=observed`、`skill_project_discovery=observed`。Node/HTTP 的本地运行已观察；`web_target_execution=unverified`，因为当前 target adapter 明确把浏览器、框架、打包器和部署排除在 Node/npm 运行时之外。`managed_loader=null`、`model_used=false`、`external_actions=false`。Python/Node 子进程边界是同用户进程，适配器不是沙箱，Node/HTTP 网络行为由脚本自行决定；本次脚本只绑定 loopback 临时端口。

## 浏览器阻塞

`/usr/bin/chromium` 可执行文件存在，但 Playwright 的沙箱启动在当前环境被 Chromium 拒绝：`/usr/lib/chromium/chrome-sandbox` 的属主是 `nobody`（uid 65534），权限 `04755`，而沙箱启动要求 root 属主。Playwright 的捆绑浏览器路径也不存在。为保持安全边界，本阶段没有使用 `--no-sandbox`、修改系统权限、安装浏览器或更改安全设置。要完成浏览器层验收，环境需要由运维提供正确 root-owned sandbox helper 的浏览器运行时，或提供已批准的浏览器执行环境；随后应重跑同一项目验证。

本阶段没有生成或上传 ZIP；主仓库、远程 Git、外部服务、密钥、provider、部署和发布均未触碰。
