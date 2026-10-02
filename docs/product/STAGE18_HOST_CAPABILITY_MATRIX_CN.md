# Stage18：跨平台宿主能力矩阵与最小适配器检查

Stage18 把 OpenCoding 的宿主事实、生成项目目标和当前 Agent 适配器能力整理为一个可重复报告。它只依赖 Python、路径安全和本地结构化动作，不读取 APG historical fixtures，也不需要 Rust、frontend、cargo、provider 或 managed loader。

## 三个维度

1. `capability_matrix.host` 是实际运行 OpenCoding 的宿主：OS、平台、Python 版本和宿主族。
2. `capability_matrix.target` 是规划阶段选择的生成项目目标，例如 `web`、`windows`、`ios`、`cli`；识别成功只代表标签可理解，不代表目标 SDK 或工具链已安装。
3. `capability_matrix.actions` 是当前适配器真实接受的结构化动作：`write_text` 和 `python_module`。前者只覆盖审查根内文件，回滚依赖事务层；后者是同用户 Python，任意副作用和网络由模块自行触发，适配器不控制，也不是 sandbox。

矩阵固定声明 `model.available=false`、`sandbox.enabled=false`、`process_boundary=same-user-subprocess`、`toolchain.status=unverified`，并保留 `managed_loader.observed=null`。这些未知状态不能升级成 Windows/macOS、真实浏览器、模型或托管宿主验收。

## 最小检查

只读能力检查：

```bash
python scripts/check_agent_adapter.py --project-root /absolute/project --target-platform windows
```

合成本地写入探针（自动使用临时未链接根，执行后删除）：

```bash
python scripts/check_agent_adapter.py --project-root /absolute/project \
  --target-platform web --probe-local-write --confirm-synthetic
```

Stage18 Linux 实测中，`windows` 和 `web` 目标都报告 `planning_supported=true`、`target_toolchain_verified=false`；临时 `write_text` 探针通过，未调用模型、网络或外部服务。该结果证明适配器契约与本地低风险动作可观察，不证明目标平台工具链或真实用户确认。
