# OpenCoding / APG Independent Review

正式品牌：OpenCoding；本报告中的 APG 指兼容技术标识。

本报告对应 `zhi-xing-planet-apg-independent-review-20260830`，只复核 APG deployment-preview 测试夹具，不执行儿童产品开发、部署或发布。

## Review contract

- 输入仅为项目本地文件与离线 simulator 输出。
- `PASS` 要求 simulator 的完整证据场景为 `ready-for-preview`，缺少审批场景为确定性 `BLOCK`。
- 发布、publication、deployment、provider、network、credentials、real data 等外部动作必须为未执行。
- 原始 `PROJECT_BRIEF.md` 保持不变；惰性夹具副本 `MODIFIED_FILE` 保持修改状态。

## Commands

```text
python scripts/apg_independent_review.py --root .
python -X utf8 -m unittest discover -s tests -p 'test_*.py'
python -m project_governance doctor . --json
python -m project_governance audit . --json
python -m project_governance check . --phase full --json
```

## Boundary

本复核不调用 provider、网络、凭据、真实儿童数据、运行时、部署目标、发布渠道、试点环境或 Git。
