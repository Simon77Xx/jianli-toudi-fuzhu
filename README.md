# 本地简历投递决策助手

完整中文使用手册见 [README.zh-CN.md](README.zh-CN.md)。

根据单份 JD，对真实 LaTeX 简历做受约束的定制与本地编译质检。它不承诺面试结果，也不会在没有证据或用户确认时编造经历。

## 首次运行

1. 在项目根目录复制 `.env.example` 为 `.env`。
2. 填写 API 协议和模型配置：

   ```env
   LLM_PROTOCOL=openai
   LLM_API_KEY=你的密钥
   LLM_BASE_URL=https://你的兼容接口/v1
   LLM_MODEL=你的模型名
   ```

   - `LLM_PROTOCOL=openai`：DeepSeek、Qwen 等 OpenAI 兼容接口；
   - `LLM_PROTOCOL=anthropic`：Anthropic 或 Anthropic 兼容网关；
   - `.env` 已被 Git 忽略，页面不会显示或保存密钥。

3. 安装依赖并启动：

   ```powershell
   python -m pip install -r requirements.txt
   python app.py
   ```

4. 在浏览器打开 `http://127.0.0.1:7860`。上传完整 LaTeX 项目 ZIP（主 `.tex`、`.cls`、头像等资源）、当前 PDF 简历，并粘贴一份 JD。

## 开发者自检

在项目根目录执行：

```powershell
python -m compileall -q app.py resume_agent tests
python -m unittest discover -s tests -v
python C:\Users\xiejiaxu\.codex\skills\webapp-testing\scripts\with_server.py --server "python tests/serve_for_test.py" --port 7863 -- python tests/ui_smoke.py
```

浏览器冒烟测试会生成 `output/playwright/landing.png`。Windows 沙盒若禁止 Playwright 创建浏览器进程，需要在允许本地浏览器自动化的终端中执行最后一条命令。

## 输出

每次运行都在页面展示并写入 `output/<JD>-<时间戳>/`：

- `resume-tailored.zip`：可直接上传 Overleaf 的完整项目；
- `resume-tailored.pdf`：本地 XeLaTeX 编译成功时提供；
- `report.md`：双评分、差异、缺口、可辩护记录和补强建议；
- `facts-confirmed.json`：本次页面确认的事实。

确认过的事实保存在本地 `data/confirmed_facts.json`，该文件不会提交到 Git。`data/feedback.json` 只是未来可选反馈接口的空占位，首版不会显示或使用它。

## 安全与限制

- ZIP 会检查路径穿越、体积和文件数量；输入不被覆盖。
- ZIP 解压总量限制为 250MB、单个文件限制为 100MB；PDF 简历限制为 25MB。
- GitHub 开关默认关闭；开启后只读取公开仓库的基础证据，未通过可信度门禁不会用于加分。
- 每次最多 5 个追问；未确认项不会写入简历。
- 自动尝试将简历控制在 1--2 页。若编译失败或仍超过两页，会降低投递准备度且不标记为可投递。
- `投递准备度 ≥90` 仅表示材料可信、清晰、针对 JD 且完成质检；不表示面试概率。
