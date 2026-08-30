# Task Plan: 本地简历定制智能体

## Goal

实现一个本地 Gradio 网页：上传完整 LaTeX 简历 ZIP 与 PDF、粘贴单份 JD，通过可配置模型完成证据映射和最多五轮追问，生成可下载的定制 LaTeX 投递包、PDF 与报告。

## Current Phase

Phase 5：文档与交付（已完成）

## Phases

### Phase 1: 项目骨架与安全配置

- [x] 建立 Python 项目、依赖、`.env.example` 与 Git 忽略规则
- [x] 定义领域模型、路径隔离和本地事实库
- [x] 记录本地 XeLaTeX 与依赖发现
- **Status:** complete

### Phase 2: 简历/JD/模型服务

- [x] 实现 ZIP、PDF、LaTeX 的安全提取与证据解析
- [x] 实现 OpenAI 兼容与 Anthropic 兼容的模型客户端
- [x] 实现结构化 JD 映射、真实性约束与追问状态机
- **Status:** complete

### Phase 3: 定制、编译与投递包

- [x] 实现受约束的 LaTeX 定制与报告生成
- [x] 实现 XeLaTeX 编译、页数检测、自动压缩与 ZIP 输出
- [x] 确保所有产物保留在本地并不覆盖输入
- **Status:** complete

### Phase 4: Gradio 前端与验证

- [x] 实现本地网页上传、追问、结果和下载交互
- [x] 添加离线单元测试与示例运行
- [x] 启动应用并进行冒烟测试
- **Status:** complete

### Phase 5: 文档与交付

- [x] 编写使用说明和限制说明
- [x] 完成代码质量检查并提交
- **Status:** complete

## Key Questions

1. LaTeX 项目的主入口如何可靠识别？
2. 未配置 API 或模型输出不可用时如何保持可测试与不伪造成功？
3. 如何在不依赖模型响应格式过度稳定的前提下实现安全结构化输出？

## Decisions Made

| Decision | Rationale |
|----------|-----------|
| Python + Gradio | 用户已确认；适合本地单用户文件上传、问答与下载。 |
| 双 API 协议 | 支持用户偏好的 DeepSeek/Qwen 与 Anthropic 兼容网关。 |
| 本地 JSON 事实库 | 仅保存用户确认事实；简单、可审查且无额外 token 成本。 |
| XeLaTeX 质检 | 用户本机已安装并可用于编译和页数验证。 |
| 工作流而非开放式工具 Agent | 需求步骤可控、涉及真实信息，代码控制的追问/校验更安全。 |

## Errors Encountered

| Error | Attempt | Resolution |
|-------|---------|------------|
| 早期 MiKTeX 镜像下载中断 | 1 | 已切换镜像并成功安装 XeLaTeX。 |
| Python 默认临时目录不可写 | 1 | `tempfile` 在 Windows 沙盒上会设置不兼容 ACL；测试改用项目内、已忽略的手工唯一目录。 |
| Gradio `launch()` 返回元组 | 1 | 冒烟测试改为在原始 `Blocks` 实例上调用 `close()`，应用本身不受影响。 |
| Playwright 使用 `networkidle` 在 Gradio 长连接上等待不稳定 | 1 | UI 冒烟改为 `DOMContentLoaded` 加短暂渲染等待，并增加 15 秒导航超时。 |
| 测试服务固定端口导致残留进程冲突 | 1 | 服务脚本支持 `TEST_PORT`，最终测试前清理了本项目残留 Python 服务。 |
