# 简历投递决策助手使用说明

这是一个运行在本机的中文网页工具。它读取一份 LaTeX 简历项目、一份当前 PDF 简历和一份目标岗位 JD，依据可核验的简历证据生成针对该岗位的定制版本。上传区和操作按钮均已固定为中文，不受浏览器语言影响。

工具不会承诺面试概率，也不会在没有原始证据或你明确确认的情况下新增项目、技能、数字、论文或职责。

## 1. 准备环境

项目需要 Python 3.11 或更高版本。项目已配置 XeLaTeX 自动探测；当前本机安装位置为：

`D:\Program Files (x86)\LaTex\MiKTeX\miktex\bin\x64\xelatex.exe`

首次使用时，在项目根目录打开 PowerShell：

```powershell
cd "D:\Users\xiejiaxu\Documents\ChatGPT\简历优化智能体"
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

## 2. 配置模型

只在本地 `.env` 文件填写模型配置，网页不会显示 API Key：

```env
LLM_PROTOCOL=openai
LLM_API_KEY=你的API密钥
LLM_BASE_URL=https://你的兼容接口/v1
LLM_MODEL=你的模型名称
```

DeepSeek、Qwen 等通常使用 `openai` 协议。Anthropic 或 Anthropic 兼容网关使用：

```env
LLM_PROTOCOL=anthropic
LLM_API_KEY=你的API密钥
LLM_BASE_URL=https://你的兼容接口/v1
LLM_MODEL=你的模型名称
```

如果没有配置 `.env`，网页仍可以打开，但点击分析时会提示配置错误，不会生成虚假结果。

## 3. 启动网页

```powershell
python app.py
```

浏览器打开：<http://127.0.0.1:7860>

如果 7860 端口被占用，可以启动时指定其他端口：

```powershell
$env:PORT="7861"
python app.py
```

## 4. 网页操作流程

1. 上传完整的 LaTeX 项目 ZIP。ZIP 中应包含主 `.tex`、`.cls`、图片、字体和其他编译依赖。
2. 上传当前 PDF 简历，用于文本交叉校验。
3. 将目标岗位的完整 JD 粘贴到文本框。
4. 根据需要开启“读取 GitHub 公开证据”。默认关闭；开启后只读取简历中出现的公开仓库链接。
5. 点击“开始证据分析”。如果存在关键事实缺口，页面会一次提出一个问题，最多 5 个问题。
6. 每个问题只填写你在面试中可以如实说明的内容；没有相关经历时填写“暂无”。
7. 完成确认后，页面会展示 HR/AI 初筛匹配摘要、双评分、修改差异、未覆盖缺口、补强建议和面试可辩护记录。

## 5. 分数如何理解

- **投递准备度**：衡量简历是否可信、清晰、针对 JD、可编译且不超过两页。达到 90 分表示材料达到可投递质量门槛，不代表面试概率。
- **岗位竞争匹配度**：衡量你的真实经历与该 JD 的竞争匹配程度。
- 竞争匹配度低于 50 分时，页面会明确标记“低优先级/不建议投递”，并列出核心缺口。

## 6. 下载结果

每次分析会在页面提供：

- `resume-tailored.zip`：完整定制 LaTeX 项目，可上传到 Overleaf；
- `resume-tailored.pdf`：本地 XeLaTeX 编译成功时提供；
- `report.md`：评分、差异、证据、缺口和补强建议。

文件同时保存在项目的 `output/<JD标识>-<时间>/` 目录。原始上传材料不会被覆盖。

> 出于隐私保护，个人简历 PDF 默认被 `.gitignore` 排除，不应提交到公开 GitHub 仓库。

## 7. 安全限制和注意事项

- ZIP 拒绝路径穿越、盘符路径和 UNC 路径；单文件不超过 100MB，解压总量不超过 250MB。
- PDF 简历不超过 25MB，并且需要有可读取的文本层。
- GitHub 证据只有通过公开仓库、README 和仓库内容的基础可信度检查后才会参与分析。
- 模型输出中的新数字和无效证据引用会被拦截。
- 简历会自动尝试控制在 1--2 页；编译失败或仍超过两页时，结果会被标记为不可直接投递。

## 8. 开发者自检

```powershell
python -m compileall -q app.py resume_agent tests
python -m unittest discover -s tests -v
python C:\Users\xiejiaxu\.codex\skills\webapp-testing\scripts\with_server.py --server "python tests/serve_for_test.py" --port 7863 -- python tests/ui_smoke.py
```

最后一条命令会生成 `output/playwright/landing.png`。如果 Windows 沙盒禁止浏览器自动化，请在允许本地浏览器进程的终端执行。
