# OpenCoding 中文快速开始

OpenCoding 是跨平台 Agent AI coding 的规划与受控执行核心。当前 Agent 负责理解开放式想法、回答追问并生成经过检查的动作；OpenCoding 程序负责把这些输入变成可恢复的会话、方案、文档、任务波次、测试证据和回滚记录。程序本身没有通用模型，也不会凭空生成代码。

## 1. 安装或直接运行

Python 3.11 以上、无第三方运行依赖。源码目录中可直接运行：

```bash
python -m opencoding --help
```

安装包提供同一个入口：

```bash
python -m pip install --no-index --no-deps ./opencoding_local_entry-*.whl
opencoding --help
```

安装资源时使用私有的显式 Codex home，不要把项目目录或未知目录当成 home：

```bash
python scripts/install_codex_skill.py \
  --project-root /absolute/opencoding-root \
  --codex-home /absolute/private-codex-home --load
```

这会验证并复制项目 skill；managed loader 是否由宿主自动加载仍须宿主返回证据。

## 2. 宿主 Agent 的默认项目入口

每个项目用独立的绝对目录。下面的命令可在不同进程、重启后继续：

```bash
python -m opencoding project init --root /absolute/my-project \
  --idea "我想做一个记录植物浇水日期的离线网页"
```

`init` 返回 12 个需要澄清的问题。宿主 Agent 用真实对话回答后写入 `answers.json`，并标注来源：

```bash
python -m opencoding project plan --root /absolute/my-project \
  --answers /absolute/answers.json --answers-origin user-conversation
```

命令返回平台判断、客户端/服务/数据能力评估、任务波次、`AGENTS.md`、`memory.md`、`PRG.md`、`plan.md` 等文档的精确预览及 `service_digest`。复核后，调用方在已有授权范围内应用文档：

```bash
python -m opencoding project apply-docs --root /absolute/my-project \
  --expected-digest <service_digest> \
  --authorized-local --authorization-id <caller-confirmation>
```

## 3. 接入 Agent 的实现动作

宿主 Agent 根据已采用计划生成 `reviewed-actions.json`。它不是任意脚本：每个输出路径、写入内容和最终测试动作都必须通过项目入口校验。先预览：

```bash
python -m opencoding project preview --root /absolute/my-project \
  --actions /absolute/reviewed-actions.json
```

人工/调用方复核返回的 `preview_digest` 后，在既有本地授权范围内执行：

```bash
python -m opencoding project run --root /absolute/my-project \
  --actions /absolute/reviewed-actions.json \
  --expected-digest <preview_digest> \
  --authorized-local --authorization-id <caller-confirmation>
```

实现或测试失败会停止当前波次并保存失败收据。宿主 Agent 修正动作，生成新的动作文件和 preview，再次 `run` 接续；不得复用旧 digest 或确认。`status` 可在重启后读取状态：

```bash
python -m opencoding project status --root /absolute/my-project
```

成功产物可由调用方按项目说明运行。回滚只处理收据覆盖、且执行前不存在的文件：

```bash
python -m opencoding project rollback --root /absolute/my-project \
  --reason "撤回本次本地试作" \
  --authorized-local --authorization-id <caller-confirmation>
```

Python/Node 动作是同用户子进程，`sandbox=false`，网络由代码自行决定，任意副作用不会自动回滚。支付、通知、登录服务、外部 API、部署、远程 Git、发布和真实 provider 必须停在独立人工 Gate。

## 4. 真实能力边界

`local_structured_actions` 与项目 skill 发现/加载可在本地证据中变成 `observed`；Node/HTTP 只证明本地运行时，不证明浏览器、框架、打包、部署或目标平台落地。宿主 managed loader、模型和 provider 没有观测证据时保持 `null`/`unverified`。回答来源为夹具时必须写 `fixture`，不得冒充真实用户确认。
