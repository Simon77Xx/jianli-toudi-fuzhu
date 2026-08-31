from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from .config import ROOT, Settings
from .extractors import (
    InputError,
    evidence_from_materials,
    extract_pdf_text,
    find_main_tex,
    inspect_public_github,
    pdf_tex_consistency_warning,
    safe_extract_zip,
)
from .latex import compile_latex, compact_to_two_pages, copy_compiled_pdf, create_tailored_project, make_overleaf_zip
from .llm import ModelServiceError, require_configured_gateway
from .models import BaselineAnalysis, Evidence, FinalAnalysis, InitialAnalysis, JobContext, Question, format_score_delta
from .storage import load_facts, save_confirmed_fact


SYSTEM_PROMPT = """你是中文技术实习简历的证据审查员，不是文案夸大器。
只可使用输入中带 ID 的证据和用户明确确认的答案。不得虚构技术、数字、论文、竞赛、职责或项目结果。
JD 要求必须分为 hard_gate、transferable、core_gap、bonus：相近关键词不是核心能力满足。
“投递准备度”只表示材料可信、清晰、针对 JD 和可投递，绝不代表获得面试概率。
只输出一个合法 JSON 对象，不使用 Markdown 代码块或额外文字。"""


class PipelineError(RuntimeError):
    pass


def _slug(value: str) -> str:
    compact = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value.strip())
    return (compact.strip("-") or "jd")[:40]


def _evidence_text(evidence: list[Evidence]) -> str:
    return "\n".join(f"[{item.id}] ({item.source}) {item.claim} | {item.location}" for item in evidence)


def _initial_prompt(context: JobContext) -> str:
    return f"""请先分析一份 JD 与候选人真实简历的匹配情况。

JD：
{context.jd_text}

证据库：
{_evidence_text(context.evidence)}

PDF 交叉校验文本：
{context.pdf_text}

请严格输出：
{{
  "quick_assessment": "不承诺面试概率的简短判断",
  "requirements": [{{"name":"要求","tier":"hard_gate|transferable|core_gap|bonus","status":"supported|needs_confirmation|unsupported","rationale":"原因","evidence_ids":["R1"]}}],
  "questions": [{{"id":"Q1","question":"单一、可由候选人真实回答的问题","why":"为什么它影响投递判断","related_requirement":"对应 JD 要求"}}]
}}
最多给出 5 个问题；只提出会显著改变项目表述、硬门槛判断或竞争匹配度的问题。"""


def _final_prompt(context: JobContext) -> str:
    answers = "\n".join(f"{key}: {value}" for key, value in context.answers.items()) or "无新增回答"
    initial = context.initial.model_dump_json(ensure_ascii=False) if context.initial else "{}"
    return f"""请完成这份单 JD 中文 LaTeX 简历的定制决策。

JD：
{context.jd_text}

可引用证据（只能引用这些 ID）：
{_evidence_text(context.evidence)}

前序映射：
{initial}

用户确认回答：
{answers}

LaTeX 原文（可改写必须提供完全匹配的 source，并且 target 中不得加入 source 没有的量化数字）：
{context.tex_text}

输出 JSON：
{{
 "hr_summary":"供 HR/AI 初筛阅读的中文摘要，不含面试概率承诺",
 "delivery_score":0,
 "competitiveness_score":0,
 "delivery_reasons":["..."],
 "competitiveness_reasons":["..."],
 "keywords":["..."],
 "tailored_summary":"仅基于证据的中文个人摘要",
 "section_order":["教育背景","实习经历","个人项目","计算机技能"],
 "edits":[{{"source":"LaTeX 中完全存在的原文","target":"证据约束下的替换文本","reason":"改写理由","evidence_ids":["R1"]}}],
 "differences":["修改前后差异"],
 "gaps":["真实未覆盖缺口"],
 "future_suggestions":["未来 4--6 个月补强建议"],
 "defense_records":[{{"rewritten_text":"与 edits 中的目标内容相同或其明确片段","source_evidence_ids":["R1"],"rewrite_reason":"为何改写","possible_question":"面试可能追问","truthful_answer_outline":"候选人可据真实经历回答的提纲"}}]
}}
规则：
1. 任一 edits 和 defense_records 必须含有效 evidence_ids；没有可靠证据时不要给 edits。
2. 不足 50 分时竞争匹配度需反映核心缺口；90 分的投递准备度仅表示材料质量门槛。
3. 不要为了塞入关键词而捏造长程规划、Memory、Skill、强化学习、论文或指标。"""


def _baseline_prompt(context: JobContext) -> str:
    answers = "\n".join(f"{key}: {value}" for key, value in context.answers.items()) or "无新增回答"
    return f"""请独立评估候选人原始简历相对于目标 JD 的投递价值。你只能使用下方 JD、证据和已确认事实，不考虑任何尚未发生的简历改写。

JD：
{context.jd_text}

证据库：
{_evidence_text(context.evidence)}

用户确认回答：
{answers}

原始 LaTeX 简历：
{context.tex_text}

请严格输出 JSON：
{{
  "delivery_score": 0,
  "competitiveness_score": 0,
  "delivery_reasons": ["修改前材料质量、清晰度和针对性的理由"],
  "competitiveness_reasons": ["修改前与 JD 技能和项目要求匹配度的理由"]
}}

规则：
1. 两个分数均为 0--100 的整数；投递准备度不是面试概率。
2. 岗位竞争匹配度低于 50 分时必须体现核心缺口，不能用相近关键词虚增分数。
3. 只评价当前原始简历，不提出改写内容，不新增任何技能、数字、职责或结果。"""


class ResumePipeline:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings
        self.runtime_root = ROOT / "runtime" / "sessions"
        self.output_root = ROOT / "output"

    def _save_context(self, context: JobContext) -> None:
        path = context.root / "session.json"
        path.write_text(context.model_dump_json(ensure_ascii=False, indent=2), encoding="utf-8")

    def load_context(self, session_id: str) -> JobContext:
        path = self.runtime_root / session_id / "session.json"
        if not path.exists():
            raise PipelineError("当前会话已失效，请重新上传材料。")
        return JobContext.model_validate_json(path.read_text(encoding="utf-8"))

    def start(self, latex_zip: str, pdf_path: str, jd_text: str, github_enabled: bool) -> tuple[JobContext, Question | None, list[str]]:
        if not latex_zip or not pdf_path or not jd_text.strip():
            raise PipelineError("请同时上传 LaTeX 项目 ZIP、PDF 简历，并粘贴一份 JD。")
        session_id = uuid.uuid4().hex
        work_dir = self.runtime_root / session_id
        source_dir = work_dir / "source"
        try:
            files = safe_extract_zip(latex_zip, source_dir)
            tex_path = find_main_tex(source_dir)
            target_pdf = work_dir / "input-resume.pdf"
            work_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pdf_path, target_pdf)
            tex_text = tex_path.read_text(encoding="utf-8", errors="ignore")
            pdf_text = extract_pdf_text(target_pdf)
        except InputError as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise PipelineError(str(exc)) from exc

        facts = load_facts()
        evidence = evidence_from_materials(tex_text, pdf_text, facts)
        notes: list[str] = []
        warning = pdf_tex_consistency_warning(pdf_text, tex_text)
        if warning:
            notes.append(warning)
        if github_enabled:
            github_evidence, github_notes = inspect_public_github(tex_text)
            evidence.extend(github_evidence)
            notes.extend(github_notes)
        context = JobContext(
            session_id=session_id,
            work_dir=str(work_dir),
            project_dir=str(source_dir),
            tex_path=str(tex_path),
            pdf_path=str(target_pdf),
            jd_text=jd_text.strip(),
            tex_text=tex_text,
            pdf_text=pdf_text,
            evidence=evidence,
            github_enabled=github_enabled,
            github_notes=notes,
        )
        self._ensure_request_size(context)
        gateway = require_configured_gateway()
        context.initial = gateway.structured(SYSTEM_PROMPT, _initial_prompt(context), InitialAnalysis)
        self._save_context(context)
        question = context.initial.questions[0] if context.initial.questions else None
        return context, question, notes

    def answer(self, session_id: str, answer: str) -> tuple[JobContext, Question | None]:
        context = self.load_context(session_id)
        if not context.initial:
            raise PipelineError("会话缺少 JD 分析结果，请重新开始。")
        if not answer.strip():
            raise PipelineError("请填写真实回答；如确实没有相关经历，可直接说明“暂无”。")
        index = context.current_question_index
        if index >= len(context.initial.questions):
            raise PipelineError("本轮追问已经结束。")
        question = context.initial.questions[index]
        context.answers[question.id] = answer.strip()
        fact = save_confirmed_fact(question.question, answer, context.session_id)
        context.evidence.append(
            Evidence(id=fact["id"], source="confirmed_fact", claim=fact["value"], location=f"已确认事实：{question.question}")
        )
        context.current_question_index += 1
        self._save_context(context)
        next_question = context.initial.questions[context.current_question_index] if context.current_question_index < len(context.initial.questions) else None
        return context, next_question

    def finalize(self, session_id: str) -> dict[str, object]:
        context = self.load_context(session_id)
        if context.initial and context.current_question_index < len(context.initial.questions):
            raise PipelineError("请先完成当前追问，或逐题回答“暂无”。")
        self._ensure_request_size(context)
        gateway = require_configured_gateway()
        settings = self.settings or Settings.from_env()
        baseline = gateway.structured(SYSTEM_PROMPT, _baseline_prompt(context), BaselineAnalysis)
        baseline_compile = compile_latex(settings.xelatex_path, Path(context.tex_path))
        baseline, baseline_quality_warnings = self._apply_compile_guard(baseline, baseline_compile)
        analysis = gateway.structured(SYSTEM_PROMPT, _final_prompt(context), FinalAnalysis)
        analysis, guard_warnings = self._apply_model_guards(context, analysis)
        valid_evidence = {item.id for item in context.evidence}
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_dir = self.output_root / f"{_slug(context.jd_text.splitlines()[0])}-{timestamp}"
        target_project = output_dir / "project"
        output_dir.mkdir(parents=True, exist_ok=True)
        target_tex, actions, rejected = create_tailored_project(
            Path(context.project_dir), Path(context.tex_path), target_project, analysis, valid_evidence
        )
        rejected.extend(guard_warnings)
        compile_result = compact_to_two_pages(settings.xelatex_path, target_tex)
        analysis, quality_warnings = self._apply_compile_guard(analysis, compile_result)
        rejected.extend(quality_warnings)
        pdf_file = copy_compiled_pdf(compile_result, output_dir)
        zip_file = make_overleaf_zip(target_project, output_dir / "resume-tailored.zip")
        facts_file = output_dir / "facts-confirmed.json"
        facts_file.write_text(json.dumps(context.answers, ensure_ascii=False, indent=2), encoding="utf-8")
        report_file = output_dir / "report.md"
        report_file.write_text(
            self._render_report(
                context,
                baseline,
                baseline_compile,
                analysis,
                compile_result,
                actions,
                rejected,
                baseline_quality_warnings,
            ),
            encoding="utf-8",
        )
        return {
            "baseline": baseline,
            "baseline_compile": baseline_compile,
            "analysis": analysis,
            "compile": compile_result,
            "actions": actions,
            "rejected": rejected,
            "notes": context.github_notes,
            "report_path": str(report_file),
            "zip_path": str(zip_file),
            "pdf_path": str(pdf_file) if pdf_file else None,
            "facts_path": str(facts_file),
            "output_dir": str(output_dir),
        }

    def _ensure_request_size(self, context: JobContext) -> None:
        max_chars = (self.settings.max_input_chars if self.settings else int(os.getenv("MAX_INPUT_CHARS", "180000")))
        total = len(context.jd_text) + len(context.tex_text) + len(context.pdf_text) + sum(len(item.claim) for item in context.evidence)
        if total > max_chars:
            raise PipelineError(
                f"材料共 {total} 个字符，超过 .env 的 MAX_INPUT_CHARS={max_chars}。系统不会静默截断简历，请提高限制或精简输入后重试。"
            )

    @staticmethod
    def _apply_compile_guard(
        analysis: FinalAnalysis | BaselineAnalysis, compile_result
    ) -> tuple[FinalAnalysis | BaselineAnalysis, list[str]]:
        warnings: list[str] = []
        if not compile_result.success:
            analysis.delivery_score = min(analysis.delivery_score, 75)
            analysis.delivery_reasons.append("LaTeX 本地编译失败，尚未达到可投递版质量门槛。")
            warnings.append("编译失败：投递准备度已被上限限制为 75 分。")
        elif not compile_result.page_count or compile_result.page_count > 2:
            analysis.delivery_score = min(analysis.delivery_score, 85)
            analysis.delivery_reasons.append("简历未能稳定控制在两页内，需继续精简。")
            warnings.append("页数不合规：投递准备度已被上限限制为 85 分。")
        return analysis, warnings

    @staticmethod
    def _apply_model_guards(context: JobContext, analysis: FinalAnalysis) -> tuple[FinalAnalysis, list[str]]:
        """Reject high-risk model output before it can reach the editable resume."""
        warnings: list[str] = []
        reference = "\n".join(
            [context.tex_text, context.pdf_text, _evidence_text(context.evidence), *context.answers.values()]
        )
        source_numbers = set(re.findall(r"\d+(?:\.\d+)?%?", reference))
        summary_numbers = set(re.findall(r"\d+(?:\.\d+)?%?", analysis.tailored_summary))
        if not summary_numbers.issubset(source_numbers):
            analysis.tailored_summary = ""
            warnings.append("拒绝个人摘要改写：包含原始材料和已确认事实中不存在的新数字。")

        valid_ids = {item.id for item in context.evidence}
        safe_records = []
        for record in analysis.defense_records:
            if set(record.source_evidence_ids).issubset(valid_ids):
                safe_records.append(record)
            else:
                warnings.append(
                    f"过滤一条面试可辩护记录：引用了不存在的证据 ID（{', '.join(record.source_evidence_ids)}）。"
                )
        analysis.defense_records = safe_records
        return analysis, warnings

    @staticmethod
    def _recommendation(score: int) -> str:
        if score < 50:
            return "低优先级/不建议投递"
        if score < 70:
            return "可尝试"
        if score < 85:
            return "较匹配"
        return "强匹配"

    def _render_report(
        self,
        context: JobContext,
        baseline: BaselineAnalysis,
        baseline_compile,
        analysis: FinalAnalysis,
        compile_result,
        actions: list[str],
        rejected: list[str],
        baseline_warnings: list[str],
    ) -> str:
        lines = [
            "# JD 定制简历报告",
            "",
            "## HR / AI 初筛匹配摘要",
            analysis.hr_summary,
            "",
            "## 修改前后评分对比",
            "| 指标 | 修改前 | 修改后 | 变化值 |",
            "| --- | ---: | ---: | ---: |",
            f"| 投递准备度 | {baseline.delivery_score}/100 | {analysis.delivery_score}/100 | {format_score_delta(baseline.delivery_score, analysis.delivery_score)} |",
            f"| 岗位竞争匹配度 | {baseline.competitiveness_score}/100 | {analysis.competitiveness_score}/100 | {format_score_delta(baseline.competitiveness_score, analysis.competitiveness_score)} |",
            "",
            "## 双评分",
            f"- 投递准备度：**{analysis.delivery_score}/100**（≥90 仅代表材料可投递，不表示面试概率）",
            f"- 岗位竞争匹配度：**{analysis.competitiveness_score}/100**（{self._recommendation(analysis.competitiveness_score)}）",
            "",
            "### 修改前投递准备度理由",
            *[f"- {reason}" for reason in baseline.delivery_reasons],
            "",
            "### 修改后投递准备度理由",
            *[f"- {reason}" for reason in analysis.delivery_reasons],
            "",
            "### 修改前岗位竞争匹配度理由",
            *[f"- {reason}" for reason in baseline.competitiveness_reasons],
            "",
            "### 修改后岗位竞争匹配度理由",
            *[f"- {reason}" for reason in analysis.competitiveness_reasons],
            "",
            "## 修改前后差异",
            *[f"- {item}" for item in analysis.differences],
            *[f"- 已应用：{item}" for item in actions],
            *[f"- 未应用：{item}" for item in rejected],
            "",
            "## 面试可辩护记录",
        ]
        for record in analysis.defense_records:
            lines.extend(
                [
                    f"### {record.rewritten_text}",
                    f"- 证据：{', '.join(record.source_evidence_ids)}",
                    f"- 改写理由：{record.rewrite_reason}",
                    f"- 可能追问：{record.possible_question}",
                    f"- 真实回答提纲：{record.truthful_answer_outline}",
                ]
            )
        lines.extend(["", "## 未覆盖缺口", *[f"- {item}" for item in analysis.gaps], "", "## 下一轮补强建议", *[f"- {item}" for item in analysis.future_suggestions], "", "## 编译质检"])
        if compile_result.success:
            lines.append(f"- 编译成功，页数：{compile_result.page_count}。")
        else:
            lines.append("- 编译失败；请查看日志摘要并修复后再投递。")
        lines.extend(f"- 自动压缩：{item}" for item in compile_result.compression_actions)
        lines.extend(f"- 修改前质量约束：{item}" for item in baseline_warnings)
        if baseline_compile.success:
            lines.append(f"- 修改前原始 LaTeX 编译成功，页数：{baseline_compile.page_count}。")
        else:
            lines.append(f"- 修改前原始 LaTeX 编译失败：{baseline_compile.log_excerpt[-1200:]}")
        lines.extend(f"- GitHub/一致性提示：{item}" for item in context.github_notes)
        lines.extend(["", "## 证据来源"])
        lines.extend(f"- [{item.id}] {item.source}：{item.claim}（{item.location}）" for item in context.evidence)
        return "\n".join(lines) + "\n"
