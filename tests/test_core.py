from __future__ import annotations

import shutil
import unittest
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app as web_app
from resume_agent.config import Settings
from resume_agent.extractors import (
    InputError,
    evidence_from_materials,
    find_main_tex,
    github_repositories,
    safe_extract_zip,
)
from resume_agent.latex import (
    _remove_section,
    apply_evidence_bound_edits,
    reorder_sections,
    replace_summary_and_keywords,
)
from resume_agent.llm import LLMGateway, ModelServiceError, _extract_json
from resume_agent.models import (
    BaselineAnalysis,
    CompileResult,
    DefenseRecord,
    Evidence,
    FinalAnalysis,
    InitialAnalysis,
    JobContext,
    Question,
    TexEdit,
    format_score_delta,
)
from resume_agent.pipeline import PipelineError, ResumePipeline


def analysis_fixture(**changes) -> FinalAnalysis:
    payload = {
        "hr_summary": "测试摘要",
        "delivery_score": 90,
        "competitiveness_score": 70,
        "delivery_reasons": ["测试"],
        "competitiveness_reasons": ["测试"],
        "keywords": [],
        "tailored_summary": "",
        "section_order": [],
        "edits": [],
        "differences": [],
        "gaps": [],
        "future_suggestions": [],
        "defense_records": [],
    }
    payload.update(changes)
    return FinalAnalysis.model_validate(payload)


@contextmanager
def workspace_tempdir():
    root = Path("runtime") / "test-tmp"
    root.mkdir(parents=True, exist_ok=True)
    # Windows sandbox 下 tempfile 会对临时目录设置与当前令牌不兼容的 ACL；
    # 使用项目内、已忽略的唯一目录，并在测试结束后自动清理。
    path = root / f"case-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield str(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


class ExtractorTests(unittest.TestCase):
    def test_rejects_zip_path_traversal(self):
        with workspace_tempdir() as temp:
            archive = Path(temp) / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../outside.tex", "x")
            with self.assertRaises(InputError):
                safe_extract_zip(archive, Path(temp) / "output")

    def test_rejects_windows_zip_path_traversal(self):
        with workspace_tempdir() as temp:
            archive = Path(temp) / "unsafe-windows.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr(r"..\outside.tex", "x")
            with self.assertRaises(InputError):
                safe_extract_zip(archive, Path(temp) / "output")

    def test_rejects_zip_with_oversized_uncompressed_member(self):
        with workspace_tempdir() as temp:
            archive = Path(temp) / "large.zip"
            archive.write_bytes(b"placeholder")

            class FakeBundle:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def infolist(self):
                    return [SimpleNamespace(filename="large.bin", file_size=101 * 1024 * 1024, is_dir=lambda: False)]

            with patch("resume_agent.extractors.zipfile.ZipFile", return_value=FakeBundle()):
                with self.assertRaises(InputError):
                    safe_extract_zip(archive, Path(temp) / "output")

    def test_finds_unique_main_tex(self):
        with workspace_tempdir() as temp:
            root = Path(temp)
            (root / "main.tex").write_text("\\documentclass{article}\n\\begin{document}x\\end{document}", encoding="utf-8")
            (root / "fragment.tex").write_text("hello", encoding="utf-8")
            self.assertEqual(find_main_tex(root).name, "main.tex")

    def test_prefers_chinese_entrypoint_when_parallel_versions_exist(self):
        with workspace_tempdir() as temp:
            root = Path(temp)
            template = "\\documentclass{resume}\n\\begin{document}x\\end{document}"
            (root / "resume-en.tex").write_text(template, encoding="utf-8")
            (root / "resume-zh.tex").write_text(
                "\\documentclass[zh]{resume}\n\\begin{document}x\\end{document}", encoding="utf-8"
            )
            self.assertEqual(find_main_tex(root).name, "resume-zh.tex")

    def test_rejects_equally_ambiguous_main_tex_candidates(self):
        with workspace_tempdir() as temp:
            root = Path(temp)
            template = "\\documentclass{resume}\n\\begin{document}x\\end{document}"
            (root / "first.tex").write_text(template, encoding="utf-8")
            (root / "second.tex").write_text(template, encoding="utf-8")
            with self.assertRaises(InputError):
                find_main_tex(root)

    def test_github_repositories_preserves_trailing_git_chars_and_strips_dot_git(self):
        tex = (
            r"\link{https://github.com/alice/my-agent}{a} "
            r"\link{https://github.com/bob/ai-chat.git}{b} "
            r"\link{https://github.com/carol/llm-kit}{c}"
        )
        self.assertEqual(
            github_repositories(tex),
            [("alice", "my-agent"), ("bob", "ai-chat"), ("carol", "llm-kit")],
        )

    def test_evidence_from_materials_extracts_multiline_blocks(self):
        tex = """\\documentclass[zh]{resume}
\\begin{document}
\\begin{abstract}
计算机本科在读，
专注多模态与智能体工程。
\\end{abstract}
\\begin{educations}
  \\education%
    {2028.09}%
    [2024.09]%
    {重庆大学}%
    {数据科学与大数据技术}
\\end{educations}
\\begin{itemize}
  \\item \\textbf{OCR智能体}
  采用 Qwen2.5 + Qwen3，正确率 75\\%。
  \\item \\textbf{项目A} 内容A \\item \\textbf{项目B} 内容B
\\end{itemize}
\\end{document}
"""
        items = evidence_from_materials(tex, "", [])
        claims = [item.claim for item in items]
        self.assertTrue(any("专注多模态与智能体工程" in c for c in claims))
        self.assertTrue(any("重庆大学" in c and "数据科学与大数据技术" in c for c in claims))
        self.assertTrue(any("OCR智能体" in c and "75\\%" in c for c in claims))
        self.assertTrue(any("项目A" in c and "项目B" not in c for c in claims))
        self.assertTrue(any("项目B" in c for c in claims))


class LatexSafetyTests(unittest.TestCase):
    def test_reorders_sections(self):
        original = "前言\\sectionTitle{教育背景}{x}教育\\sectionTitle{个人项目}{x}项目"
        updated, actions = reorder_sections(original, ["个人项目", "教育背景"])
        self.assertLess(updated.index("个人项目"), updated.index("教育背景"))
        self.assertTrue(actions)

    def test_reorders_and_removes_last_section_without_breaking_end_document(self):
        original = (
            "\\begin{document}\n"
            "\\sectionTitle{教育背景}{a}\n教育内容\n"
            "\\sectionTitle{个人优势}{b}\n优势内容\n"
            "\\end{document}\n"
        )
        reordered, actions = reorder_sections(original, ["个人优势", "教育背景"])
        self.assertTrue(actions)
        self.assertLess(reordered.index("个人优势"), reordered.index("教育背景"))
        self.assertTrue(reordered.strip().endswith("\\end{document}"))
        self.assertLess(reordered.index("教育内容"), reordered.index("\\end{document}"))

        removed_tex, removed = _remove_section(original, "个人优势")
        self.assertTrue(removed)
        self.assertNotIn("优势内容", removed_tex)
        self.assertIn("\\end{document}", removed_tex)

    def test_replace_summary_and_keywords_escapes_special_chars_safely(self):
        tex = "\\keywords{old}\n\\begin{abstract}\n旧摘要\n\\end{abstract}\n"
        analysis = analysis_fixture(
            keywords=["C/C++ & Python", "C#\\Agent_v2", "100% 真实"],
            tailored_summary="提升 15% 且覆盖 A&B_test #1，已转义 70\\% 保持不变。",
        )
        updated, actions = replace_summary_and_keywords(tex, analysis)
        self.assertEqual(len(actions), 2)
        self.assertIn(r"C/C++ \& Python", updated)
        self.assertIn(r"C\#\Agent\_v2", updated)
        self.assertIn(r"100\% 真实", updated)
        self.assertIn(r"15\%", updated)
        self.assertIn(r"A\&B\_test \#1", updated)
        self.assertIn(r"70\%", updated)
        self.assertNotIn(r"70\\%", updated)

    def test_rejects_new_numbers_in_edit(self):
        analysis = analysis_fixture(
            edits=[TexEdit(source="性能提升 10%", target="性能提升 20%", reason="测试", evidence_ids=["R1"])]
        )
        updated, actions, rejected = apply_evidence_bound_edits("性能提升 10%", analysis, {"R1"})
        self.assertEqual(updated, "性能提升 10%")
        self.assertFalse(actions)
        self.assertTrue(rejected)

    def test_allows_numbers_from_cited_confirmed_fact_and_rejects_empty_evidence_or_bad_braces(self):
        analysis = analysis_fixture(
            edits=[
                TexEdit(source="完成数据清洗", target="完成数据清洗，过滤率达 35\\%", reason="补充真实指标", evidence_ids=["F1"]),
                TexEdit(source="旧文本", target="新文本", reason="漏证据", evidence_ids=[]),
                TexEdit(source="\\textbf{旧}", target="\\textbf{新", reason="坏括号", evidence_ids=["R1"]),
            ]
        )
        updated, actions, rejected = apply_evidence_bound_edits(
            "完成数据清洗\n旧文本\n\\textbf{旧}",
            analysis,
            {"R1", "F1"},
            evidence_by_id={"R1": "旧", "F1": "已确认过滤率达 35%"},
        )
        self.assertIn("过滤率达 35\\%", updated)
        self.assertEqual(actions, ["补充真实指标"])
        self.assertEqual(len(rejected), 2)

    def test_accepts_evidence_bound_edit(self):
        analysis = analysis_fixture(
            edits=[TexEdit(source="Python 项目", target="Python 多模态项目", reason="突出技术", evidence_ids=["R1"])]
        )
        updated, actions, rejected = apply_evidence_bound_edits("Python 项目", analysis, {"R1"})
        self.assertEqual(updated, "Python 多模态项目")
        self.assertTrue(actions)
        self.assertFalse(rejected)

    def test_rejects_summary_with_new_numbers_and_invalid_defense_evidence(self):
        analysis = analysis_fixture(
            tailored_summary="完成 99% 的优化。",
            defense_records=[
                DefenseRecord(
                    rewritten_text="测试改写",
                    source_evidence_ids=["BAD"],
                    rewrite_reason="测试",
                    possible_question="怎么做的？",
                    truthful_answer_outline="据实回答",
                ),
                DefenseRecord(
                    rewritten_text="缺少证据改写",
                    source_evidence_ids=[],
                    rewrite_reason="测试",
                    possible_question="怎么做的？",
                    truthful_answer_outline="据实回答",
                ),
            ],
        )
        context = JobContext(
            session_id="s",
            work_dir="runtime/test-tmp/s",
            project_dir="runtime/test-tmp/s",
            tex_path="runtime/test-tmp/s/main.tex",
            pdf_path="runtime/test-tmp/s/resume.pdf",
            jd_text="JD",
            tex_text="\n性能提升 10%\n",
            pdf_text="",
            evidence=[Evidence(id="R1", source="latex", claim="性能提升 10%", location="LaTeX 第 99 行")],
        )
        guarded, warnings = ResumePipeline._apply_model_guards(context, analysis)
        # 行号 99 不应泄露进数字白名单，因此 99% 摘要仍应被拦截。
        self.assertEqual(guarded.tailored_summary, "")
        self.assertFalse(guarded.defense_records)
        self.assertEqual(len(warnings), 3)


class ScoreComparisonTests(unittest.TestCase):
    def test_baseline_score_range_is_validated(self):
        baseline = BaselineAnalysis(
            delivery_score=73,
            competitiveness_score=38,
            delivery_reasons=["原始简历可读"],
            competitiveness_reasons=["缺少核心数据链路证据"],
        )
        self.assertEqual(baseline.delivery_score, 73)
        with self.assertRaises(ValueError):
            BaselineAnalysis(
                delivery_score=101,
                competitiveness_score=38,
                delivery_reasons=[],
                competitiveness_reasons=[],
            )

    def test_score_delta_is_after_minus_before(self):
        self.assertEqual(format_score_delta(73, 86), "+13")
        self.assertEqual(format_score_delta(86, 73), "-13")
        self.assertEqual(format_score_delta(73, 73), "0")

    def test_report_contains_before_after_scores_and_delta(self):
        context = JobContext(
            session_id="s",
            work_dir="runtime/test-tmp/s",
            project_dir="runtime/test-tmp/s",
            tex_path="runtime/test-tmp/s/main.tex",
            pdf_path="runtime/test-tmp/s/resume.pdf",
            jd_text="JD",
            tex_text="简历",
            pdf_text="",
            evidence=[],
        )
        baseline = BaselineAnalysis(
            delivery_score=73,
            competitiveness_score=38,
            delivery_reasons=["修改前理由"],
            competitiveness_reasons=["修改前缺口"],
        )
        analysis = analysis_fixture(delivery_score=86, competitiveness_score=52)
        compiled = CompileResult(success=True, page_count=1)
        report = ResumePipeline()._render_report(context, baseline, compiled, analysis, compiled, [], [], [])
        self.assertIn("| 投递准备度 | 73/100 | 86/100 | +13 |", report)
        self.assertIn("| 岗位竞争匹配度 | 38/100 | 52/100 | +14 |", report)
        self.assertIn("修改前缺口", report)

    def test_report_includes_compile_failure_log_excerpt(self):
        context = JobContext(
            session_id="s",
            work_dir="runtime/test-tmp/s",
            project_dir="runtime/test-tmp/s",
            tex_path="runtime/test-tmp/s/main.tex",
            pdf_path="runtime/test-tmp/s/resume.pdf",
            jd_text="JD",
            tex_text="简历",
            pdf_text="",
            evidence=[],
        )
        baseline = BaselineAnalysis(
            delivery_score=70,
            competitiveness_score=60,
            delivery_reasons=["r"],
            competitiveness_reasons=["c"],
        )
        analysis = analysis_fixture(delivery_score=75, competitiveness_score=65)
        failed = CompileResult(success=False, log_excerpt="诊断：本机未安装模板字体（IBM Plex Serif）。")
        report = ResumePipeline()._render_report(context, baseline, failed, analysis, failed, [], [], [])
        self.assertIn("本机未安装模板字体（IBM Plex Serif）", report)


class LLMParsingTests(unittest.TestCase):
    def test_extracts_json_from_fence(self):
        self.assertEqual(_extract_json("```json\n{\"ok\": true}\n```"), '{"ok": true}')

    def test_extracts_json_with_surrounding_latex_braces_and_repairs_escapes(self):
        raw = r'说明：参考 \textbf{Python}。结果如下：{"source": "\item 正确率 75\%", "ok": true} 完毕 \section{End}'
        extracted = _extract_json(raw)
        self.assertIn('"ok": true', extracted)
        self.assertIn(r'\\item', extracted)

    def test_initial_analysis_caps_more_than_five_questions(self):
        questions = [
            Question(id=f"Q{i}", question=f"问题{i}", why="理由", related_requirement="要求")
            for i in range(1, 8)
        ]
        initial = InitialAnalysis(quick_assessment="ok", requirements=[], questions=questions)
        self.assertEqual(len(initial.questions), 5)

    def test_llm_gateway_retries_on_bad_json_then_succeeds(self):
        settings = Settings(
            protocol="openai",
            api_key="test-key",
            base_url=None,
            model="test-model",
            max_input_chars=180000,
            xelatex_path="",
        )
        gateway = LLMGateway(settings=settings, max_attempts=2)
        responses = [
            "not valid json at all",
            '{"delivery_score": 80, "competitiveness_score": 65, "delivery_reasons": ["ok"], "competitiveness_reasons": ["ok"]}',
        ]
        with patch.object(gateway, "_call", side_effect=responses) as mock_call:
            result = gateway.structured("sys", "user", BaselineAnalysis)
        self.assertEqual(result.delivery_score, 80)
        self.assertEqual(mock_call.call_count, 2)


class AppCallbackTests(unittest.TestCase):
    def test_submit_answer_clears_textbox_and_keeps_question_on_validation_error(self):
        q2 = Question(id="Q2", question="第二个问题", why="影响判断", related_requirement="PyTorch")
        ctx = JobContext(
            session_id="abc123",
            work_dir="runtime/test-tmp/abc123",
            project_dir="runtime/test-tmp/abc123",
            tex_path="runtime/test-tmp/abc123/main.tex",
            pdf_path="runtime/test-tmp/abc123/resume.pdf",
            jd_text="JD",
            tex_text="tex",
            pdf_text="pdf",
            evidence=[],
            current_question_index=1,
        )
        with patch.object(web_app.pipeline, "answer", return_value=(ctx, q2)):
            out = web_app.submit_answer("abc123", "第一题回答")
        # answer_text (index 3) 必须被清空，retry_button (index 4) 隐藏
        self.assertEqual(out[3]["value"], "")
        self.assertFalse(out[4]["visible"])

        with patch.object(web_app.pipeline, "answer", side_effect=PipelineError("请填写真实回答")):
            err_out = web_app.submit_answer("abc123", "")
        # 出错时不清空当前题目（返回 gr.update() 无 value 键）
        self.assertNotIn("value", err_out[1])
        self.assertTrue(err_out[2]["visible"])

    def test_finalize_failure_exposes_retry_button(self):
        ctx = JobContext(
            session_id="abc123",
            work_dir="runtime/test-tmp/abc123",
            project_dir="runtime/test-tmp/abc123",
            tex_path="runtime/test-tmp/abc123/main.tex",
            pdf_path="runtime/test-tmp/abc123/resume.pdf",
            jd_text="JD",
            tex_text="tex",
            pdf_text="pdf",
            evidence=[],
            current_question_index=1,
        )
        with (
            patch.object(web_app.pipeline, "answer", return_value=(ctx, None)),
            patch.object(web_app.pipeline, "finalize", side_effect=ModelServiceError("模型限流")),
        ):
            out = web_app.submit_answer("abc123", "最后一题回答")
        self.assertEqual(out[0], "abc123")
        self.assertFalse(out[2]["visible"])  # question_group 隐藏
        self.assertEqual(out[3]["value"], "")  # answer_text 已清空
        self.assertTrue(out[4]["visible"])  # retry_button 可见
        self.assertIn("重试生成投递包", out[5])


if __name__ == "__main__":
    unittest.main()
