# Progress Log

## Session: 2026-08-30

### Phase 1--5: 实现、验证与交付

- **Status:** complete
- Actions taken:
  - 已确认产品设计，建立文件化实施计划。
  - 已确认本地 XeLaTeX 安装可用，准备检查 Python 运行时和依赖。
  - 已建立 Gradio 页面、双协议 SDK、ZIP/PDF/LaTeX 处理、证据约束改写和投递包生成代码。
  - 首次测试发现 Python `tempfile` 在 Windows 沙盒中创建了不可写 ACL；测试已改为使用 `runtime/test-tmp/` 下的手工唯一目录。
  - XeLaTeX 冒烟测试发现相对路径会令 `-output-directory` 相对当前工作目录再次拼接；编译器已统一解析为绝对路径。
  - Gradio 本地服务器已成功监听；测试清理代码错误地在 `launch()` 返回元组上调用 `close()`，后续改为关闭 `Blocks` 实例。
  - 增加 ZIP 解压总大小、单文件大小和 PDF 文件大小限制，降低压缩炸弹与超大输入风险。
  - UI 冒烟脚本改为适配 Gradio 长连接的 `DOMContentLoaded` 等待，并验证文件上传控件、JD 输入、开始按钮和 GitHub 默认关闭状态。
  - 将前端上传区可见提示固定为中文并关闭 Gradio 默认页脚链接，避免浏览器 locale 影响使用体验；截图已确认“将文件拖放到这里 / 或 / 点击上传”。
  - 补充 ZIP 大小边界、Windows 反斜杠路径和模型输出门禁单元测试；当前共 11 个单元测试全部通过。
  - 修复双语 LaTeX ZIP 无法识别主文件的问题：对并行入口进行中文优先评分，并为仍有歧义的情况保留明确错误提示。
  - XeLaTeX 冒烟编译成功，生成 1 页 PDF；浏览器截图写入 `output/playwright/landing.png`。
- Files created/modified:
  - `task_plan.md`
  - `findings.md`
  - `progress.md`
  - `app.py`
  - `resume_agent/`
  - `tests/test_core.py`

## Test Results

| Test | Input | Expected | Actual | Status |
|------|-------|----------|--------|--------|
| `python -m compileall -q app.py resume_agent tests` | Python 源码 | 无语法错误 | 通过 | ✅ |
| `python -m unittest discover -s tests -v` | 11 个离线单元测试 | 全部通过 | 11/11 通过 | ✅ |
| `python C:\\Users\\xiejiaxu\\.codex\\skills\\webapp-testing\\scripts\\with_server.py --server "python tests/serve_for_test.py" --port 7863 -- python tests/ui_smoke.py` | 本地 Gradio | 页面可访问且关键控件存在 | 通过，截图已生成 | ✅ |
| XeLaTeX 冒烟编译 | `runtime/compile-smoke-elevated-fixed/main.tex` | 生成可读 PDF 且页数可读 | 1 页，成功 | ✅ |

## Error Log

| Timestamp | Error | Attempt | Resolution |
|-----------|-------|---------|------------|
| 2026-08-30 | Playwright 启动浏览器时报 `WinError 5` | 沙盒内执行 | 在允许本地浏览器自动化的提权终端完成同一测试，断言通过。 |
| 2026-08-30 | 测试辅助进程残留并占用 7863/7864 | 固定端口重复启动 | 清理明确的本项目 Python 服务，并让测试脚本支持可配置端口。 |

## 5-Question Reboot Check

| Question | Answer |
|----------|--------|
| Where am I? | Phase 5：文档与交付，已完成。 |
| Where am I going? | 首版已交付；后续可按真实投递反馈增加评测与反馈接口。 |
| What's the goal? | 本地、可信、可下载的 JD 定制 LaTeX 简历助手。 |
| What have I learned? | 见 `findings.md`。 |
| What have I done? | 已完成代码、测试、网页冒烟、XeLaTeX 验证与文档同步。 |
