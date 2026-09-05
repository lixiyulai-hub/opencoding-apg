# W1 已审字节隔离集成报告

日期：2026-09-05

本报告记录 `integration` 隔离根中的 W1 本地字节导入与验证。它不是产品完成证明，不代表真实服务、Provider、Host、部署、发布或 W2 已启动。

## 范围与来源

- 目标根：`E:/儿童知行星球/.tmp/opencoding-product-foundation-20260905/integration`
- 种子 HEAD：`9586fb52d9260c5c45f1b25c728a7fb740ebfdbf`
- 远端：无
- 清单：`E:/儿童知行星球/artifacts/opencoding-w1-review-20260905/integration-input-0940/MANIFEST.json`
- 清单 SHA256：`05ce45c6dec8a685ba9444242a8deb3d9eb166010dc04ea5296ae87820b8f168`
- controller change：`opencoding-w1-accepted-byte-integration-20260905`
- A 来源提交：`1eadd14b6f9ccebd2ceb37af2b3e78f6c3973336`
- B 来源提交：`e0fa40b8aaf7737f30b4da69ee9b5e2a7819b596`
- C 来源提交：`1fba4bf42609b18c512fd893b44850fe841024ec`

导入前已确认 17 个目标路径均不存在、均不是符号链接；导入仅使用清单 `postimages/`，没有 cherry-pick、整支分支合并或导入 worker 报告、旧缓存、README、AGENTS 或旧治理历史。

## 导入的 17 个路径

| 路径 | SHA256 |
|---|---|
| `opencoding/safety.py` | `b9b7b7cd8f376549fd0d727e5aa517917bd0d1f4f55bb58895cacb3f67f96fdc` |
| `tests/test_product_safety.py` | `06d8ad89b9698509548297e373f350e92130a22dff1925865adeea4d16115d6b` |
| `opencoding/transactions.py` | `8e8f74110e0df10e77a5b121c1f919aec17b0c3e983905abffe94b1246b938db` |
| `tests/test_product_transactions.py` | `f367331e8a488fe743f2a2348895bb120c789fd0abc5366b5925caeabab6de97` |
| `opencoding/intake.py` | `d55b97ad4222c68e988e2f6a45c7d7c9fd0627b0ca0d865624d95b8dee992b23` |
| `tests/test_product_intake.py` | `e516b1d8acae4c6fd64a1649d00f89bc1daf7d91b066847fe5eea017609ce246` |
| `opencoding/decisions.py` | `a65e1709aa3ec0718cbf5086492bf6541cdee4e3e5a61eb0de84974d4b853319` |
| `tests/test_product_decisions.py` | `19022ddf55e979d538a6814830dfaeea2b9a0d6a9c1c20e143a38d6e9709647a` |
| `opencoding/sessions.py` | `2570362623b22958fc1e5853403778f55ac361ff958d8cc3722ab0470628ff4f` |
| `tests/test_product_sessions.py` | `f0a0da287aa294a6cbae3cc769d4d3b39f4ef9ff56c2f6a5839bc8aa050b0672` |
| `opencoding/documents.py` | `bee97dbe861e59106cbe020d9b5fac9fbfd2a746754beafd6768ebb7f2098f25` |
| `tests/test_product_documents.py` | `4a8743613f00068b2329f82ddb5194ccb7c116161774df7616cefa0043014ec0` |
| `opencoding/planning.py` | `ff2418e0d63fe138707e6f2efdf15c13cc1df85fdcef96e214c8885f6221d795` |
| `tests/test_product_planning.py` | `62120292bf8a574ab1481c040c1a0b434c7edf6d5ab6377dfcc7ed28eb4c8727` |
| `tests/__init__.py` | `9bed6b89c7422b1db4b08a9b61762b3f487f352470f8f792c16f557c1d9cce74` |
| `tests/test_product_integration.py` | `2fb6cbba05801246f1bc4ec25648b91082a291bfe8a7a164de407b4795ccc646` |
| `docs/product/CONTRACTS_V1_1.md` | `da9946a18b605aa9be23f6097918ad9117dff3a409dca1417cfe36decf3417ca` |

导入 receipt：`artifacts/w1-isolated-integration-20260905/import-receipt.json`。导入后 17/17 路径再次哈希匹配，详见 `final-hash-verification.json`。

## 验证结果

环境变量：`CARGO_NET_OFFLINE=true`、`PYTHONDONTWRITEBYTECODE=1`；TEMP/TMP 均位于 `artifacts/w1-isolated-integration-20260905/test-temp/`。

| 命令 | 结果 |
|---|---|
| `python -B -X utf8 -m unittest` | exit 0，152 tests，OK |
| `python -B -X utf8 -m unittest discover -s tests -p test_*.py -v` | exit 0，152 tests，OK |
| `python -B -X utf8 -m unittest tests.test_product_integration -v` | exit 0，14 tests，OK |
| `git diff --check` | exit 0 |
| post-import `doctor . --json` | exit 0，pass；保留既有 `.governance/progress` generated-files warning |
| post-import `audit . --json` | exit 0，read-only pass；保留既有 discovery/profile warnings |

审查现场保留在 `default-unittest.log`、`full-discovery-unittest.log`、`integration-unittest.log`、`doctor-post-import.json`、`audit-post-import-pass.json` 与 `final-hash-verification.json`。

有一个额外的失败现场 `audit-post-import.json`：当 audit 的 stdout 通过 `Tee-Object` 直接写入被扫描的 evidence 目录时，controller 正确报告该日志文件为本次扫描期间的 `changed_path` 并返回 exit 4。随后不在扫描期间写入目标根的 audit 重跑 exit 0；该失败属于证据采集方式，不是源代码或集成失败，现场未删除。

## 边界与结论

本次确认的是：17 个清单字节已在隔离根逐一导入，跨模块默认测试与完整发现均为非零且通过，14 项独立集成旅程通过，来源与目标哈希一致。

本次没有确认真实 Provider、Host、生产数据、网络、部署、公开发布、下游 runtime 或 W2；也没有修改受审字节、旧 worker worktree、原始根、README、AGENTS 或旧治理/缓存。`services/domain/target` 等既有/生成缓存不在交付提交中。

因此本候选可交给主协调进行独立 `review_integration.ps1` 验收；在主协调验收前，不宣称 W1 产品里程碑完成或真实产品集成成功。
