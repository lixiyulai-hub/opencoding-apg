# APG GitHub 公共交付图文介绍 Preview

## 目标

让第一次打开 GitHub README 的人，在一屏内理解 APG：它把一句中文想法变成可执行、可验证、可回滚的项目计划，并默认替用户完成普通技术决策。

## 图文资产

- 可编辑源：`artifacts/apg-github-public-delivery-preview/APG_GITHUB_INTRO.drawio`
- GitHub 可嵌入预览：`artifacts/apg-github-public-delivery-preview/APG_GITHUB_INTRO.svg`
- 首屏文案：`artifacts/apg-github-public-delivery-preview/APG_PUBLIC_HERO_PREVIEW.md`

图稿沿用 draw.io 可维护 XML 结构（`mxGraphModel`、`mxCell`、连接线和标签），主路径从左到右，适合后续在 draw.io 中继续编辑。图稿生成只使用本地文件，不连接 `https://github.com/Agents365-ai/drawio-skill`；该地址仅作为用户指定的图稿工作流参考。

## 公开交付 preview 计划

未来目标仓库：`https://github.com/lixiyulai-hub/adaptive-project-governance`

计划远端路径：

- `README.md` / `README_CN.md`：首屏钩子、三步流程、自动循环和 SVG；
- `docs/validation/APG_PROJECT_VALIDATION_20260902.md`：APG-only 验收摘要；
- `docs/attribution/GRILL_ME_UPSTREAM.md`：保留 [mattpocock/skills](https://github.com/mattpocock/skills) 归属。

本 preview 不执行 Git 初始化、commit、push、Tag、Release、网络部署或远端写入。未来执行这些动作时，必须创建新的外部交付 Gate、plan-change、证据和回滚。
