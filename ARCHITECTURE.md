# OpenCoding Architecture

OpenCoding 使用 APG 作为兼容技术标识；历史脚本、测试、ledger、receipts、snapshots 和 change IDs 保持原名。

## Module boundaries
Project types: APG-only test harness
Operational dependencies: none discovered

## Public contracts
APG offline preview scripts, tests, receipts, and documentation are the public evidence contracts。

## Invariants
- The canonical policy remains authoritative at `.governance/policy.toml`。
- Historical APG identifiers remain valid compatibility aliases。
