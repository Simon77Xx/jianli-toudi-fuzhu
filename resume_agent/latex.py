from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from pypdf import PdfReader

from .models import CompileResult, FinalAnalysis


COMPILE_SUFFIXES = {".aux", ".log", ".out", ".synctex.gz", ".fls", ".fdb_latexmk"}


def _section_spans(tex: str) -> list[tuple[str, int, int]]:
    matches = list(re.finditer(r"\\sectionTitle\{([^}]+)\}", tex))
    return [
        (match.group(1), match.start(), matches[index + 1].start() if index + 1 < len(matches) else len(tex))
        for index, match in enumerate(matches)
    ]


def reorder_sections(tex: str, requested_order: list[str]) -> tuple[str, list[str]]:
    if not requested_order:
        return tex, []
    spans = _section_spans(tex)
    if not spans:
        return tex, []
    blocks = {name: tex[start:end] for name, start, end in spans}
    available = list(blocks)
    requested = [name for name in requested_order if name in blocks]
    if not requested:
        return tex, []
    final_order = requested + [name for name in available if name not in requested]
    first_start = spans[0][1]
    reordered = tex[:first_start] + "\n".join(blocks[name] for name in final_order)
    return reordered, [f"模块顺序调整为：{' → '.join(final_order)}"]


def replace_summary_and_keywords(tex: str, analysis: FinalAnalysis) -> tuple[str, list[str]]:
    actions: list[str] = []
    if analysis.keywords:
        value = ", ".join(dict.fromkeys(keyword.strip() for keyword in analysis.keywords if keyword.strip()))
        updated, count = re.subn(r"\\keywords\{[^}]*\}", rf"\\keywords{{{value}}}", tex, count=1)
        if count:
            tex = updated
            actions.append("更新了简历关键词，使其对应目标 JD 的真实证据。")
    if analysis.tailored_summary.strip():
        updated, count = re.subn(
            r"(\\begin\{abstract\})(.*?)(\\end\{abstract\})",
            lambda match: f"{match.group(1)}\n{analysis.tailored_summary.strip()}\n{match.group(3)}",
            tex,
            count=1,
            flags=re.S,
        )
        if count:
            tex = updated
            actions.append("更新了个人摘要，突出 JD 最相关的真实经历。")
    return tex, actions


def apply_evidence_bound_edits(tex: str, analysis: FinalAnalysis, valid_evidence_ids: set[str]) -> tuple[str, list[str], list[str]]:
    actions: list[str] = []
    rejected: list[str] = []
    for edit in analysis.edits:
        if not set(edit.evidence_ids).issubset(valid_evidence_ids):
            rejected.append(f"拒绝一项改写：引用了不存在的证据 ID（{', '.join(edit.evidence_ids)}）。")
            continue
        if edit.source not in tex:
            rejected.append(f"未应用一项改写：原文定位失败（{edit.reason}）。")
            continue
        # 防止模型在没有原始数字或确认事实的情况下将新的量化数字写进简历。
        source_numbers = set(re.findall(r"\d+(?:\.\d+)?%?", edit.source))
        target_numbers = set(re.findall(r"\d+(?:\.\d+)?%?", edit.target))
        if not target_numbers.issubset(source_numbers):
            rejected.append(f"拒绝一项改写：目标文本包含原文没有的新数字（{edit.reason}）。")
            continue
        tex = tex.replace(edit.source, edit.target, 1)
        actions.append(edit.reason)
    return tex, actions, rejected


def create_tailored_project(source_project: Path, tex_path: Path, target_project: Path, analysis: FinalAnalysis, evidence_ids: set[str]) -> tuple[Path, list[str], list[str]]:
    if target_project.exists():
        shutil.rmtree(target_project)
    shutil.copytree(source_project, target_project)
    target_tex = target_project / tex_path.relative_to(source_project)
    original = target_tex.read_text(encoding="utf-8", errors="ignore")
    tailored, actions = reorder_sections(original, analysis.section_order)
    tailored, summary_actions = replace_summary_and_keywords(tailored, analysis)
    tailored, edit_actions, rejected = apply_evidence_bound_edits(tailored, analysis, evidence_ids)
    actions.extend(summary_actions)
    actions.extend(edit_actions)
    target_tex.write_text(tailored, encoding="utf-8")
    return target_tex, actions, rejected


def _compiler_command(xelatex_path: str, tex_path: Path, compile_dir: Path) -> list[str]:
    return [
        xelatex_path,
        "-interaction=nonstopmode",
        "-halt-on-error",
        f"-output-directory={compile_dir}",
        tex_path.name,
    ]


def compile_latex(xelatex_path: str, tex_path: Path) -> CompileResult:
    if not xelatex_path or not Path(xelatex_path).is_file():
        return CompileResult(success=False, log_excerpt="未找到 xelatex。请在 .env 中设置 XELATEX_PATH。")
    tex_path = tex_path.resolve()
    compile_dir = tex_path.parent / ".compile"
    if compile_dir.exists():
        shutil.rmtree(compile_dir)
    compile_dir.mkdir(parents=True)
    try:
        completed = subprocess.run(
            _compiler_command(xelatex_path, tex_path, compile_dir),
            cwd=tex_path.parent,
            capture_output=True,
            text=True,
            timeout=150,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CompileResult(success=False, log_excerpt="XeLaTeX 编译超时（150 秒）。")
    except OSError as exc:
        return CompileResult(success=False, log_excerpt=f"无法启动 XeLaTeX：{exc}")
    log = (completed.stdout + "\n" + completed.stderr).strip()
    pdf = compile_dir / f"{tex_path.stem}.pdf"
    if completed.returncode != 0 or not pdf.exists():
        return CompileResult(success=False, log_excerpt=log[-4000:])
    try:
        pages = len(PdfReader(str(pdf)).pages)
    except Exception as exc:
        return CompileResult(success=False, log_excerpt=f"编译生成 PDF 但无法读取页数：{exc}")
    return CompileResult(success=True, page_count=pages, log_excerpt=log[-1000:], pdf_path=str(pdf))


def _remove_section(tex: str, section_name: str) -> tuple[str, bool]:
    spans = _section_spans(tex)
    for name, start, end in spans:
        if name == section_name:
            return tex[:start] + tex[end:], True
    return tex, False


def compact_to_two_pages(xelatex_path: str, tex_path: Path) -> CompileResult:
    result = compile_latex(xelatex_path, tex_path)
    if not result.success or (result.page_count or 0) <= 2:
        return result

    tex = tex_path.read_text(encoding="utf-8", errors="ignore")
    actions: list[str] = []
    tex, removed = _remove_section(tex, "个人优势")
    if removed:
        actions.append("为满足两页限制，删除了弱相关且重复的“个人优势”模块。")
        tex_path.write_text(tex, encoding="utf-8")
        result = compile_latex(xelatex_path, tex_path)
        if result.success and (result.page_count or 0) <= 2:
            result.compression_actions = actions
            return result

    tex = tex_path.read_text(encoding="utf-8", errors="ignore")
    marker = "\\begin{document}"
    if marker in tex and "\\setlength{\\parskip}{0pt}" not in tex:
        tex = tex.replace(marker, marker + "\n\\setlength{\\parskip}{0pt}\n\\setlength{\\itemsep}{0pt}", 1)
        actions.append("压缩条目间距以满足两页限制。")
        tex_path.write_text(tex, encoding="utf-8")
        result = compile_latex(xelatex_path, tex_path)
    result.compression_actions = actions
    return result


def copy_compiled_pdf(result: CompileResult, destination: Path) -> Path | None:
    if not result.success or not result.pdf_path:
        return None
    target = destination / "resume-tailored.pdf"
    shutil.copy2(result.pdf_path, target)
    return target


def make_overleaf_zip(project_dir: Path, zip_path: Path) -> Path:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in project_dir.rglob("*"):
            if not path.is_file() or ".compile" in path.parts:
                continue
            suffix = ".synctex.gz" if path.name.endswith(".synctex.gz") else path.suffix
            if suffix in COMPILE_SUFFIXES:
                continue
            archive.write(path, path.relative_to(project_dir))
    return zip_path
