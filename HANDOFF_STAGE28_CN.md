# OpenCoding Stage28：新窗口交接入口

更新日期：2026-10-02。先阅读本文件，再阅读 [AGENTS.md](AGENTS.md) 和 [中文快速开始](docs/product/QUICKSTART_CN.md)。本文只涉及 OpenCoding，不包含其他项目的研究、CDN 或生产部署流程。

## 1. 交接基线与授权边界

- 仓库：`https://github.com/lixiyulai-hub/opencoding-apg`
- 工作分支：`checkpoint/stage28-20261002`
- 源码 checkpoint：`88e0aa8a8fcf830daaa2cb15481a7abca7f7188b`
- 本次集成的 main 基线：`91a20392a856ff4717d230467d32fc154bcb82b1`。这是已核实的历史基线，不代表未来 main 的最新状态。
- checkpoint 已推送；源码相对基线有 170 个文件变更，无文件删除。保留 main 原有内容；未导入恢复包中的 `evidence/` 运行输出、缓存或 wheel。
- 草稿 PR 尚未创建。此前 `gh pr create --draft` 返回 `Post "https://api.github.com/graphql": Forbidden`（403）；不得把远端分支等同于 PR。没有独立证据证明具体权限原因，不重试被拒绝的 API，不扩大凭据权限。
- 后续工作继续使用该独立分支；本交接不授权合并 main、部署或 Release。

## 2. 产品目标与实际完成范围

目标是让小白用户通过当前 Agent 描述想法，得到澄清、方案、实现、测试、恢复和交付路径，并逐步支持不同宿主和目标平台。跨平台是产品方向，不是已经完成所有平台验证的承诺。

当前源码已具备：持久会话与问题澄清、规则化评估和计划采用、项目文档预览及应用、按计划组织任务、由宿主提交结构化实现/测试动作、本地受控执行、内容摘要与授权校验、失败记录、续跑、验收证据读取和有限文件回滚。还包含能力报告、Python/Node 工具链探测、目标适配器和项目 skill 资源。

OpenCoding 程序本身不提供通用模型或自动代码生成器；理解需求和生成动作由宿主 Agent 完成。源码存在某项模块不等于真实 provider、目标设备、Windows、浏览器或生产部署已经验收。

## 3. Stage28 的证据边界

Stage28 默认入口使用合成的植物浇水示例、明确标记的夹具答案和动作，演示跨进程的初始化、计划、文档、执行、状态与恢复。历史恢复包记录 10/10 计划任务成功和示例测试成功；这些是局部流程证据，不是真实用户需求验收或通用自主开发能力证明。

本地结构化 Python/Node 动作运行在同用户子进程中，不能宣称强隔离；网络行为不由该适配器控制，任意副作用不保证回滚。宿主 managed loader、真实模型/provider、真实浏览器和部署仍需各自的观测证据。真实回答应标为 `user-conversation`，推断为 `agent-assumptions`，夹具为 `fixture`；不得把 `--synthetic` 证据改称真实用户确认。

## 4. 新 checkout 的核心入口

需要 Git 和 Python 3.11+。以下从终端依次执行；`python` 必须指向符合版本要求的解释器。源码运行不需要安装第三方运行依赖，也不依赖恢复包中的 wheel。

```sh
git clone --branch checkpoint/stage28-20261002 --single-branch https://github.com/lixiyulai-hub/opencoding-apg.git opencoding-stage28
cd opencoding-stage28
git status --short --branch
git log -1 --format=%H
python --version
python -m opencoding --help
python -m opencoding project --help
```

所有后续命令均从此仓库根目录运行。`python -m opencoding.project_entry --help` **不是有效的 CLI 验证**：该模块没有模块级 main 调用，可能仅退出 0 而无任何操作。使用上面的 `python -m opencoding project ...` 路由。

下面创建一个仓库旁的独立空项目，仅初始化澄清会话，不执行实现动作：

```sh
python -c "from pathlib import Path; p=Path('../opencoding-stage28-demo').resolve(); p.mkdir(exist_ok=False); print(p)"
```

将输出的绝对目录替换下文的 `PROJECT_ROOT`（Windows 也使用实际绝对路径），不要原样复制占位符：

```sh
python -m opencoding project init --root "PROJECT_ROOT" --idea "一个离线命令行清单"
python -m opencoding project status --root "PROJECT_ROOT"
```

`init` 返回澄清问题。宿主收集完整答案后准备绝对路径的 answers JSON，按 [快速开始](docs/product/QUICKSTART_CN.md) 执行 `plan → apply-docs → preview → run → status`。不要默认所有未知答案为否；不要编造授权 ID 或复用失效 digest。运行前必须有已复核动作和相应本地任务授权。新 checkout 不包含旧项目状态，不能直接恢复不存在的项目。

## 5. 测试记录与当前缺口

2026-10-02 checkpoint 运行结果的校正口径：

| 检查 | 总数 | 通过 | failures | errors | skipped |
| --- | ---: | ---: | ---: | ---: | ---: |
| `python -X utf8 -m unittest` | 656 | 645 | 0 | 2 | 9 |
| Stage28 focused | 109 | 108 | 0 | 1 | 0 |

本次测试原始终端输出未保存为仓库中的持久化日志。数字依据当时终端摘要与错误清单校正，不能冒称已有可下载的完整日志。恢复包历史的 109/109 通过对应隔离候选目录，不能替代合并后的 checkpoint 结果。

两个 errors：

1. `tests.test_skill_runtime_contract.SkillRuntimeContractTests.test_source_drift_blocks_before_installation` 使用 `shutil.copytree(self.source_root, copied)`，遍历到 main 原有的 6 个损坏 symlink。它们位于 `artifacts/w1-isolated-integration-20260905/test-temp/`，指向旧 Windows `E:/...` 临时路径；不是 Stage28 新增。新 checkout 仍会携带它们。
2. `tests.test_domain_contract.DomainContractTests.test_cargo_tests_pass` 找不到 `cargo` 可执行文件。Rust 测试属于保留的 `services/domain/`，不能删除测试或伪装为已通过。

项目 skill verifier 已返回 `format_valid_project_discovered_host_unverified`：格式和项目发现通过，宿主自动加载仍未验证。重跑时须给它绝对仓库路径：

```sh
python scripts/verify_codex_skill.py --root "ABSOLUTE_CHECKOUT_ROOT" --exercise
```

需要重跑验证时，从仓库根执行并保存原始输出与退出码（以下重定向语法适用于常见 shell）：

```sh
python -X utf8 -m unittest > ../stage28-unittest.log 2>&1
python -X utf8 -m unittest tests.test_project_entry tests.test_product_packaging tests.test_product_executor tests.test_agent_adapter tests.test_skill_runtime_contract tests.test_codex_host_adapter tests.test_stage18_capability_matrix tests.test_stage20_toolchain_probe tests.test_stage21_capability_contract tests.test_agent_product_loop_bridge tests.test_stage26_target_adapters tests.test_product_cli tests.test_product_planning tests.test_product_loop tests.test_stage22_acceptance_report tests.test_stage23_acceptance_state > ../stage28-focused.log 2>&1
```

每条命令后立即记录退出码：POSIX shell 用 `echo $?`，PowerShell 用 `$LASTEXITCODE`，cmd 用 `echo %ERRORLEVEL%`。日志先存仓库外，提交前另行检查敏感信息。

## 6. 下一个窗口的有限任务顺序

1. 读取仓库指令、确认分支/HEAD/工作树，核对本交接与代码；按上面的真实 CLI 路由做入口检查。
2. 单独设计针对旧损坏 symlink 的最小修复：优先让 source-drift 测试仅复制明确的源码输入，或另行清理历史临时产物。保留原有漂移拒绝断言，不静默忽略任意源码链接，不先做大范围删除。
3. 检查 Rust 工具链能否在允许的环境提供；不全局安装工具。无法运行就保留明确的 cargo 环境缺口。
4. 对实际修复运行相关测试，再运行 focused 与仓库 gate，保存输出和退出码，更新准确统计。随后开展一个明确标记为真实用户需求的有限本地端到端验收；先确定需求与动作范围。
5. GitHub API 可用且获准恢复 PR 操作后，创建并核实草稿 PR 的 base/head、提交和 draft 状态。保持待审，不合并/部署/Release。

不要把等待 PR 权限当作上述本地核查的前置条件；也不要凭测试数量或阶段名称推导产品完成百分比。

## 7. 恢复与回滚

源码 checkpoint 可在新的目录独立检查：

```sh
git clone --branch checkpoint/stage28-20261002 --single-branch https://github.com/lixiyulai-hub/opencoding-apg.git opencoding-stage28-recovery
cd opencoding-stage28-recovery
git checkout --detach 88e0aa8a8fcf830daaa2cb15481a7abca7f7188b
```

这不会回写远端或改动 main；不要在有未提交工作的目录执行破坏性 reset。撤销后续变更应先检查 diff，再用新的 revert 提交保留审计历史，不强推改写 checkpoint。

运行中项目需保留完整 `.opencoding/` 状态及业务文件。用同一绝对根目录执行 `project status`，根据失败任务重新预览动作后续跑；根路径或内容变化会使旧授权失效，不能跨根复用。文件回滚通过 `project rollback`，需提供真实授权标识和原因；先检查回执覆盖范围，不能视为撤销任意子进程、网络或外部服务副作用。

恢复归档：`OpenCoding-Stage28-Recovery-20261001.zip`，Library ID `libfile_d468555c3edc8191a7148ea335c10a9c`。它是备援材料，不是新 checkout 的运行依赖。需要使用时按 Library 当前流程解析和材料化，核实本地可读文件，不猜 URL 或路径。

- ZIP SHA256：`685fd2f8bb0bd1fb4e9b784df03890540ad0a8372af52ee1376d633446cb7da2`
- Manifest source fingerprint：`008c765a143026e4c4a8888847867deb2ca3ee6f5c93380cf7d8126f0ae50dec`（已核对一致；此前不一致提示源于抄录错误）

保留归档原样。仓库的 `scripts/verify_candidate_archive.py` 期待另一种 `MANIFEST.json` 格式，不能直接用它验证此 `RECOVERY_MANIFEST.json` 恢复包。
