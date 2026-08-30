# Findings & Decisions

## Requirements

- 本地 `localhost` 网页，不需要登录、云端存储或多用户。
- 上传完整 LaTeX 项目 ZIP 与当前 PDF；粘贴一份纯文本 JD。
- `.env` 配置 OpenAI 兼容和 Anthropic 兼容 API；页面绝不暴露 API Key。
- GitHub 证据开关默认关闭；只读公开仓库，并经过可信度门禁。
- 每次最多五个页面内追问；仅用户确认事实可写入简历和本地事实库。
- 输出定制 ZIP、编译成功时的 PDF、Markdown 报告；前端展示分数、差异、缺口与建议。
- 绝不覆盖输入；限制 1--2 页；超过两页时自动精简并重编译。

## Research Findings

- 设计说明位于 `docs/superpowers/specs/2026-08-30-resume-agent-design.md`，已由用户确认。
- 工作区目前只有 PDF 简历和设计文档，没有现成应用代码。
- 本机已安装 XeLaTeX：`D:\Program Files (x86)\LaTex\MiKTeX\miktex\bin\x64\xelatex.exe`，并已启用自动安装缺失包。
- Anthropic Python SDK 支持 `Anthropic(api_key=..., base_url=...)` 与 `messages.create`；需用官方 SDK 处理 Anthropic 兼容协议。

## Technical Decisions

| Decision | Rationale |
|----------|-----------|
| Pydantic 模型 + JSON 结构化提示 | 容易验证模型响应，失败时可报告而不是写入不可信简历。 |
| 保守降级模式 | API 未配置时允许完成文件解析、报告骨架和编译验证，但不伪造模型优化结果。 |
| 所有会话文件位于 `runtime/` | 与输入和 `output/` 隔离，便于本地清理且不进入 Git。 |
| 正则辅助 LaTeX 变换 | MVP 只做安全、有限的摘要/关键词/块顺序编辑，避免破坏未知模板。 |

## Issues Encountered

| Issue | Resolution |
|-------|------------|
| Git 工作树的初始 PDF 未跟踪 | 视为用户材料，保持不变。 |
| Gradio 页面包含长连接，Playwright `networkidle` 无法稳定结束 | 改用 `DOMContentLoaded` + 1 秒渲染等待；关键控件断言通过。 |
| ZIP 仅限制压缩包大小不足以防压缩炸弹 | 增加单文件 100MB、解压总量 250MB 限制；PDF 限制 25MB。 |
| Windows 反斜杠路径可能绕过 POSIX ZIP 路径检查 | 统一转换 `/` 并拒绝 `..`、盘符和 UNC 路径；增加 Windows 路径回归测试。 |
| Gradio 文件组件跟随浏览器语言显示日文 | 通过上传控件专用 CSS 伪元素覆盖可见提示并关闭默认页脚链接，保留原生上传交互；浏览器截图确认中文显示。 |

## Resources

- 设计文档：`docs/superpowers/specs/2026-08-30-resume-agent-design.md`
- Anthropic Python SDK 指引：`C:\Users\xiejiaxu\.codex\skills\claude-api\python\claude-api\README.md`
