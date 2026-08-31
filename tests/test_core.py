from __future__ import annotations

import unittest
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from resume_agent.extractors import InputError, find_main_tex, safe_extract_zip
from resume_agent.latex import apply_evidence_bound_edits, reorder_sections
from resume_agent.llm import _extract_json
from resume_agent.models import BaselineAnalysis, CompileResult, DefenseRecord, Evidence, FinalAnalysis, JobContext, TexEdit, format_score_delta
from resume_agent.pipeline import ResumePipeline


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
    # 使用项目内、已忽略的唯一目录，测试结束后不影响用户材料。
    path = root / f"case-{uuid.uuid4().hex}"
    path.mkdir()
    yield str(path)


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


class LatexSafetyTests(unittest.TestCase):
    def test_reorders_sections(self):
        original = "前言\\sectionTitle{教育背景}{x}教育\\sectionTitle{个人项目}{x}项目"
        updated, actions = reorder_sections(original, ["个人项目", "教育背景"])
        self.assertLess(updated.index("个人项目"), updated.index("教育背景"))
        self.assertTrue(actions)

    def test_rejects_new_numbers_in_edit(self):
        analysis = analysis_fixture(
            edits=[TexEdit(source="性能提升 10%", target="性能提升 20%", reason="测试", evidence_ids=["R1"])]
        )
        updated, actions, rejected = apply_evidence_bound_edits("性能提升 10%", analysis, {"R1"})
        self.assertEqual(updated, "性能提升 10%")
        self.assertFalse(actions)
        self.assertTrue(rejected)

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
                )
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
            evidence=[Evidence(id="R1", source="latex", claim="性能提升 10%", location="main.tex")],
        )
        guarded, warnings = ResumePipeline._apply_model_guards(context, analysis)
        self.assertEqual(guarded.tailored_summary, "")
        self.assertFalse(guarded.defense_records)
        self.assertEqual(len(warnings), 2)


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


class LLMParsingTests(unittest.TestCase):
    def test_extracts_json_from_fence(self):
        self.assertEqual(_extract_json("```json\n{\"ok\": true}\n```"), '{"ok": true}')


if __name__ == "__main__":
    unittest.main()
