# B/C 本地浏览器等价流程证据

本批次提供 `opencoding.browser_fixture.LocalBrowserFixture` 作为离线浏览器等价
夹具。它只写入项目 `.opencoding/browser-fixture/history.jsonl`，并在预览记录中
明确 `network: disabled`；因此这些检查证明页面状态、确认、接续和历史契约，
不等价于真实 HTTP 浏览器或真实模型验收。

候选产物由 `extract_versioned_artifact` 解包。解包前检查清单版本、完整文件集合、
每个文件 SHA256 和相对路径；解包写入随机 partial 目录，成功后才原子改名，目标
已存在或任一路径不安全都会拒绝。

验证命令：

```text
python -X utf8 -m unittest tests.test_browser_fixture_contract
```

结果：3 tests OK（Linux/Python 3.12）。Windows 重解析点、真实浏览器、HTTP 服务、
付费模型和 Docker 仍是后续独立 gate。
