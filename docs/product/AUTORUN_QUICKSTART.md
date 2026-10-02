# OpenCoding 自主模式快速开始（中文）

本文描述 v1.1 实施新增的自主工作模式：有限批次授权 + 连续执行 + 中文
交互。原有的只读查看、文档会话和手工精确批准入口保持不变。

## 一分钟演示（模拟链路，零费用）

```powershell
# 准备合成项目目录（空目录即可）
mkdir C:\oc-demo\synth

# 运行十任务借还示例（明确标记的模拟 AI，用于演示控制循环）
python -m opencoding --root C:\oc-demo\synth --autorun lendreg --mock-ai --run-id demo-1
```

预期：中文进度逐条输出，十项任务因模拟适配器返回空候选而全部诚实冻结，
批次结果与中断统计清晰可见。这演示机制，不冒充真实接通。

## 真实 AI 链路（需要已授权服务）

真实调用只使用你已有且明确授权的服务。通过环境变量告诉 OpenCoding：

```powershell
$env:OPENCODING_AI_BASE_URL = "https://你的已授权端点/v1"
$env:OPENCODING_AI_API_KEY  = "你的密钥（只进请求头，不写入日志）"
$env:OPENCODING_AI_MODEL    = "模型名"
$env:OPENCODING_AI_PROVIDER = "提供方标识"   # 可选

python -m opencoding --root C:\oc-demo\synth --autorun lendreg --run-id demo-real-1
```

- 批次授权：命令运行即代表你确认本批范围（合成借还项目内
  `lendreg/`、`tests/`、`scripts/`、`data/`、`RECOVERY.md`）。
- 预算默认值：AI 请求 ≤ 20；自动修复 ≤ 6 轮；并发 = 1；有效期 8 小时
  （`--grant-ttl` 可调短，不自动续长）。
- 每一步由程序核对批次授权并签发一次性短期凭据；旧凭据过期即拒绝，
  有效批次内可重新预览、重新校验、重新签发。

## 断点接续与取消

- 中断后重跑同一 `--run-id`：已验收且文件未被改动的任务直接接续，
  不重复请求 AI。
- 取消：`from opencoding.autorun import cancel_run` 或在新批次中显式重开；
  已取消的运行不会自动复活。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `python -m opencoding --root <目录> --autorun lendreg --mock-ai` | 模拟链路演示 |
| `python -m opencoding --root <目录> --autorun lendreg` | 真实 AI 链路 |
| `python -m opencoding --root <目录> --status` | 只读任务状态（零写入） |
| `python -m opencoding --root <目录> --resume <会话>` | 原中文向导（未改动） |

## 机读状态

- 运行账本：`<项目>/.opencoding/autoruns/<run-id>.json`
- 中断记录：`<项目>/.opencoding/interruptions.jsonl`
- 批次授权：`<项目>/.opencoding/grants/`
