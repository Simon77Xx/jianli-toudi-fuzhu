from __future__ import annotations

import html
import os
from pathlib import Path

import gradio as gr

from resume_agent.config import ROOT
from resume_agent.llm import ModelServiceError
from resume_agent.models import format_score_delta
from resume_agent.pipeline import PipelineError, ResumePipeline
from resume_agent.storage import ensure_feedback_placeholder


pipeline = ResumePipeline()


CSS = """
:root {
  --ink: #102a43;
  --muted: #627d98;
  --paper: #f6f8fb;
  --line: #d9e2ec;
  --signal: #e97b3c;
  --signal-soft: #fff0e6;
  --teal: #0f766e;
}
.gradio-container { max-width: 1180px !important; background: var(--paper); color: var(--ink); }
.hero { border-left: 7px solid var(--signal); padding: 14px 20px; background: white; margin: 4px 0 22px; }
.hero h1 { font-family: Georgia, 'Noto Serif SC', serif; letter-spacing: .02em; color: var(--ink); margin: 0; }
.hero p { color: var(--muted); margin: 8px 0 0; }
.section-label { color: var(--teal); font-weight: 700; letter-spacing: .08em; font-size: .78rem; }
.score-card { background: white; border: 1px solid var(--line); border-radius: 10px; padding: 18px 20px; }
.warning { border-left: 4px solid var(--signal); background: var(--signal-soft); padding: 10px 14px; }
.download-row { background: white; border-top: 2px solid var(--ink); padding: 14px; }
button.primary { background: var(--ink) !important; }
/* Gradio renders upload copy from the browser locale. Replace the visible
   Japanese/English copy while retaining the native upload interaction. */
button[aria-dropeffect="copy"] .wrap {
  font-size: 0 !important;
}
button[aria-dropeffect="copy"] .wrap::after {
  content: "将文件拖放到这里\A - 或 -\A 点击上传";
  display: block;
  white-space: pre;
  font-size: 1rem;
  line-height: 1.8;
}
"""

# Gradio's built-in upload copy follows the browser locale. Override the
# relevant translation keys so the upload controls remain Chinese.
CHINESE_I18N = gr.I18n(
    en={
        "upload_text": {"drop_file": "将文件拖放到这里", "click_to_upload": "点击上传"},
        "common": {"or": "或"},
    },
    ja={
        "upload_text": {"drop_file": "将文件拖放到这里", "click_to_upload": "点击上传"},
        "common": {"or": "或"},
    },
    zh={
        "upload_text": {"drop_file": "将文件拖放到这里", "click_to_upload": "点击上传"},
        "common": {"or": "或"},
    },
    **{
        "zh-CN": {
            "upload_text": {"drop_file": "将文件拖放到这里", "click_to_upload": "点击上传"},
            "common": {"or": "或"},
        }
    },
)


def _question_text(question) -> str:
    return f"### 需要确认的事实\n\n{question.question}\n\n**为什么问：** {question.why}\n\n对应要求：{question.related_requirement}"


def _render_result(result: dict) -> tuple[str, str]:
    baseline = result["baseline"]
    baseline_compile = result["baseline_compile"]
    analysis = result["analysis"]
    compile_result = result["compile"]
    if analysis.competitiveness_score < 50:
        recommendation = "低优先级/不建议投递"
    elif analysis.competitiveness_score < 70:
        recommendation = "可尝试"
    elif analysis.competitiveness_score < 85:
        recommendation = "较匹配"
    else:
        recommendation = "强匹配"
    defense = "".join(
        "<details><summary>" + html.escape(record.rewritten_text[:90]) + "</summary>"
        + f"<p><b>证据：</b>{html.escape(', '.join(record.source_evidence_ids))}</p>"
        + f"<p><b>可能追问：</b>{html.escape(record.possible_question)}</p>"
        + f"<p><b>真实回答提纲：</b>{html.escape(record.truthful_answer_outline)}</p></details>"
        for record in analysis.defense_records
    ) or "暂无需要展开的强化 bullet。"
    markdown = f"""## 修改前后评分对比

| 指标 | 修改前 | 修改后 | 变化值 |
| --- | ---: | ---: | ---: |
| 投递准备度 | {baseline.delivery_score}/100 | {analysis.delivery_score}/100 | {format_score_delta(baseline.delivery_score, analysis.delivery_score)} |
| 岗位竞争匹配度 | {baseline.competitiveness_score}/100 | {analysis.competitiveness_score}/100 | {format_score_delta(baseline.competitiveness_score, analysis.competitiveness_score)} |

<div class=\"score-card\">
<h2>修改后投递决策</h2>
<p><b>投递准备度：{analysis.delivery_score}/100</b>　{('可投递' if analysis.delivery_score >= 90 else '尚需修正')}</p>
<p><b>岗位竞争匹配度：{analysis.competitiveness_score}/100</b>　{recommendation}</p>
</div>

## HR / AI 初筛匹配摘要
{analysis.hr_summary}

## 评分理由
**修改前投递准备度**
{''.join(f'- {item}\n' for item in baseline.delivery_reasons)}

**修改后投递准备度**
{''.join(f'- {item}\n' for item in analysis.delivery_reasons)}

**修改前岗位竞争匹配度**
{''.join(f'- {item}\n' for item in baseline.competitiveness_reasons)}

**修改后岗位竞争匹配度**
{''.join(f'- {item}\n' for item in analysis.competitiveness_reasons)}

## 修改前后差异
{''.join(f'- {item}\n' for item in analysis.differences)}
{''.join(f'- 已应用：{item}\n' for item in result['actions'])}

## 未覆盖缺口
{''.join(f'- {item}\n' for item in analysis.gaps)}

## 下一轮补强建议
{''.join(f'- {item}\n' for item in analysis.future_suggestions)}

## 面试可辩护记录
{defense}
"""
    warnings = list(result["notes"]) + list(result["rejected"])
    if compile_result.success:
        warnings.append(f"本地 XeLaTeX 编译成功：{compile_result.page_count} 页。")
    else:
        warnings.append("本地 XeLaTeX 编译失败；请先修复再投递。")
        warnings.append(f"日志摘要：{compile_result.log_excerpt[-1200:]}")
    if baseline_compile.success:
        warnings.append(f"修改前原始 LaTeX 编译成功：{baseline_compile.page_count} 页。")
    else:
        warnings.append("修改前原始 LaTeX 编译失败；修改前投递准备度已按同等质量规则限制。")
        warnings.append(f"修改前编译日志摘要：{baseline_compile.log_excerpt[-1200:]}")
    warnings.extend(compile_result.compression_actions)
    warning_md = "### 质检与提示\n\n" + "\n".join(f"- {item}" for item in warnings)
    return markdown, warning_md


def _empty_downloads():
    return gr.update(value=None, visible=False), gr.update(value=None, visible=False), gr.update(value=None, visible=False)


def _result_outputs(result: dict):
    result_md, warning_md = _render_result(result)
    report = gr.update(value=result["report_path"], visible=True)
    archive = gr.update(value=result["zip_path"], visible=True)
    pdf = gr.update(value=result["pdf_path"], visible=bool(result["pdf_path"]))
    return result_md, warning_md, report, archive, pdf


def start_analysis(latex_zip, resume_pdf, jd_text, github_enabled):
    try:
        context, question, notes = pipeline.start(latex_zip, resume_pdf, jd_text, github_enabled)
        base = [context.session_id, "", gr.update(visible=False), "材料解析完成。", "", ""]
        if question:
            return (*base[:1], _question_text(question), gr.update(visible=True), "已完成匹配分析，请确认第 1 个事实。", "", "", *_empty_downloads())
        result = pipeline.finalize(context.session_id)
        result_md, warning_md, report, archive, pdf = _result_outputs(result)
        return (context.session_id, "", gr.update(visible=False), "分析与投递包已生成。", result_md, warning_md, report, archive, pdf)
    except (PipelineError, ModelServiceError) as exc:
        return ("", "", gr.update(visible=False), f"### 无法继续\n\n{exc}", "", "", *_empty_downloads())
    except Exception as exc:  # avoid exposing API key / local paths in the UI
        return ("", "", gr.update(visible=False), f"### 本地处理出错\n\n{exc.__class__.__name__}。请查看终端日志后重试。", "", "", *_empty_downloads())


def submit_answer(session_id, answer):
    try:
        context, question = pipeline.answer(session_id, answer)
        if question:
            return (context.session_id, _question_text(question), gr.update(visible=True), f"已保存回答。请继续第 {context.current_question_index + 1} 个问题。", "", "", *_empty_downloads())
        result = pipeline.finalize(context.session_id)
        result_md, warning_md, report, archive, pdf = _result_outputs(result)
        return (context.session_id, "", gr.update(visible=False), "已保存全部确认事实并生成投递包。", result_md, warning_md, report, archive, pdf)
    except (PipelineError, ModelServiceError) as exc:
        return (session_id, "", gr.update(visible=True), f"### 无法继续\n\n{exc}", "", "", *_empty_downloads())
    except Exception as exc:
        return (session_id, "", gr.update(visible=True), f"### 本地处理出错\n\n{exc.__class__.__name__}。请查看终端日志后重试。", "", "", *_empty_downloads())


def build_app() -> gr.Blocks:
    ensure_feedback_placeholder()
    with gr.Blocks(title="简历投递决策助手") as demo:
        gr.HTML("""<div class=\"hero\"><h1>投递前，把事实变成证据</h1><p>单份 JD · 受约束改写 · 本地编译质检。90 分只表示材料可投递，不承诺面试结果。</p></div>""")
        session = gr.State("")
        with gr.Row():
            with gr.Column(scale=1):
                gr.Markdown("<span class='section-label'>01 / 输入材料</span>")
                latex_zip = gr.File(label="完整 LaTeX 项目 ZIP", file_types=[".zip"], type="filepath", elem_classes=["resume-upload"])
                resume_pdf = gr.File(label="当前 PDF 简历", file_types=[".pdf"], type="filepath", elem_classes=["resume-upload"])
                github_enabled = gr.Checkbox(label="读取 GitHub 公开证据（默认关闭）", value=False)
                gr.Markdown("开启后仅检查公开 README/仓库基础可信度；未通过门禁不会用于加分。")
            with gr.Column(scale=2):
                gr.Markdown("<span class='section-label'>02 / 目标岗位</span>")
                jd_text = gr.Textbox(label="粘贴一份 JD", lines=18, placeholder="粘贴完整岗位描述与任职要求…")
                start_button = gr.Button("开始证据分析", variant="primary")
                status = gr.Markdown()
        with gr.Group(visible=False) as question_group:
            gr.Markdown("<span class='section-label'>03 / 事实确认</span>")
            question_md = gr.Markdown()
            answer_text = gr.Textbox(label="你的真实回答", lines=4, placeholder="只填写可在面试中如实说明的事实；没有则填写“暂无”。")
            answer_button = gr.Button("保存回答并继续", variant="primary")
        gr.Markdown("<span class='section-label'>04 / 投递结果</span>")
        result_md = gr.Markdown()
        warning_md = gr.Markdown()
        with gr.Row(elem_classes=["download-row"]):
            report_download = gr.DownloadButton("下载完整报告", visible=False)
            zip_download = gr.DownloadButton("下载 Overleaf ZIP", visible=False, variant="primary")
            pdf_download = gr.DownloadButton("下载编译 PDF", visible=False)
        outputs = [session, question_md, question_group, status, result_md, warning_md, report_download, zip_download, pdf_download]
        start_button.click(start_analysis, [latex_zip, resume_pdf, jd_text, github_enabled], outputs)
        answer_button.click(submit_answer, [session, answer_text], outputs)
    return demo


if __name__ == "__main__":
    build_app().launch(
        server_name=os.getenv("HOST", "127.0.0.1"),
        server_port=int(os.getenv("PORT", "7860")),
        inbrowser=True,
        theme=gr.themes.Base(),
        css=CSS,
        i18n=CHINESE_I18N,
        footer_links=[],
    )
