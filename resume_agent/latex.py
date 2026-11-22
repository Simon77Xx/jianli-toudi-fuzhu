from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from pypdf import PdfReader

from .models import CompileResult, FinalAnalysis


COMPILE_SUFFIXES = {".aux", ".log", ".out", ".synctex.gz", ".fls", ".fdb_latexmk"}
_SECTION_PATTERNS = (
    re.compile(r"\\sectionTitle\{([^}]+)\}"),
    re.compile(r"\\(?:cvsection|section\*?)\{([^}]+)\}"),
)
_UNESCAPED_LATEX_SPECIAL_RE = re.compile(r"(?<!\\)([%&#_])")


def _escape_latex_plain(text: str) -> str:
    """Escape unescaped LaTeX special characters in plain-text fields (keywords/abstract)."""
    return _UNESCAPED_LATEX_SPECIAL_RE.sub(r"\\\1", text)


def extract_numbers(text: str) -> set[str]:
    """Extract numeric/percentage tokens while normalizing LaTeX escaped \\% to %."""
    return set(re.findall(r"\d+(?:\.\d+)?%?", text.replace(r"\%", "%")))


def _balanced_braces(text: str) -> bool:
    depth = 0
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _section_spans(tex: str) -> list[tuple[str, int, int]]:
    begin_match = re.search(r"\\begin\{document\}", tex)
    end_match = re.search(r"\\end\{document\}", tex)
    body_start = begin_match.end() if begin_match else 0
    body_end = end_match.start() if end_match and end_match.start() >= body_start else len(tex)
    body = tex[body_start:body_end]

    matches: list[re.Match[str]] = []
    for pattern in _SECTION_PATTERNS:
        matches = list(pattern.finditer(body))
        if matches:
            break
    return [
        (
            match.group(1),
            body_start + match.start(),
            body_start + matches[index + 1].start() if index + 1 < len(matches) else body_end,
        )
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
    last_end = spans[-1][2]
    suffix = tex[last_end:]
    middle = "\n\n".join(blocks[name].strip() for name in final_order)
    if suffix:
        reordered = tex[:first_start] + middle + "\n\n" + suffix.lstrip("\n")
    else:
        reordered = tex[:first_start] + middle
    return reordered, [f"模块顺序调整为：{' → '.join(final_order)}"]


def replace_summary_and_keywords(tex: str, analysis: FinalAnalysis) -> tuple[str, list[str]]:
    actions: list[str] = []
    if analysis.keywords:
        value = ", ".join(
            dict.fromkeys(_escape_latex_plain(keyword.strip()) for keyword in analysis.keywords if keyword.strip())
        )
        updated, count = re.subn(r"\\keywords\{[^}]*\}", lambda _m: f"\\keywords{{{value}}}", tex, count=1)
        if count:
            tex = updated
            actions.append("更新了简历关键词，使其对应目标 JD 的真实证据。")
    if analysis.tailored_summary.strip():
        summary = _escape_latex_plain(analysis.tailored_summary.strip())
        updated, count = re.subn(
            r"(\\begin\{abstract\})(.*?)(\\end\{abstract\})",
            lambda match: f"{match.group(1)}\n{summary}\n{match.group(3)}",
            tex,
            count=1,
            flags=re.S,
        )
        if count:
            tex = updated
            actions.append("更新了个人摘要，突出 JD 最相关的真实经历。")
    return tex, actions


def _replace_unique_whitespace_tolerant(tex: str, source: str, target: str) -> tuple[str, bool]:
    if source in tex:
        return tex.replace(source, target, 1), True
    tokens = source.strip().split()
    if not tokens:
        return tex, False
    pattern = re.compile(r"\s+".join(re.escape(token) for token in tokens))
    matches = list(pattern.finditer(tex))
    if len(matches) != 1:
        return tex, False
    match = matches[0]
    return tex[: match.start()] + target + tex[match.end() :], True


def apply_evidence_bound_edits(
    tex: str,
    analysis: FinalAnalysis,
    valid_evidence_ids: set[str],
    evidence_by_id: dict[str, str] | None = None,
) -> tuple[str, list[str], list[str]]:
    actions: list[str] = []
    rejected: list[str] = []
    for edit in analysis.edits:
        if not edit.evidence_ids:
            rejected.append(f"拒绝一项改写：未提供任何证据 ID（{edit.reason}）。")
            continue
        if not set(edit.evidence_ids).issubset(valid_evidence_ids):
            rejected.append(f"拒绝一项改写：引用了不存在的证据 ID（{', '.join(edit.evidence_ids)}）。")
            continue
        if _balanced_braces(edit.source) and not _balanced_braces(edit.target):
            rejected.append(f"拒绝一项改写：目标 LaTeX 花括号不匹配（{edit.reason}）。")
            continue
        # 防止模型在没有原始数字或已引用证据（含已确认事实）的情况下将新的量化数字写进简历。
        allowed_numbers = extract_numbers(edit.source)
        if evidence_by_id:
            for eid in edit.evidence_ids:
                allowed_numbers.update(extract_numbers(evidence_by_id.get(eid, "")))
        target_numbers = extract_numbers(edit.target)
        if not target_numbers.issubset(allowed_numbers):
            rejected.append(f"拒绝一项改写：目标文本包含原文和引用证据中均不存在的新数字（{edit.reason}）。")
            continue
        updated_tex, replaced = _replace_unique_whitespace_tolerant(tex, edit.source, edit.target)
        if not replaced:
            rejected.append(f"未应用一项改写：原文定位失败（{edit.reason}）。")
            continue
        tex = updated_tex
        actions.append(edit.reason)
    return tex, actions, rejected


def create_tailored_project(
    source_project: Path,
    tex_path: Path,
    target_project: Path,
    analysis: FinalAnalysis,
    evidence_ids: set[str],
    evidence_by_id: dict[str, str] | None = None,
) -> tuple[Path, list[str], list[str]]:
    if target_project.exists():
        shutil.rmtree(target_project)
    shutil.copytree(source_project, target_project)
    target_tex = target_project / tex_path.relative_to(source_project)
    original = target_tex.read_text(encoding="utf-8", errors="ignore")
    tailored, actions = reorder_sections(original, analysis.section_order)
    tailored, summary_actions = replace_summary_and_keywords(tailored, analysis)
    tailored, edit_actions, rejected = apply_evidence_bound_edits(
        tailored, analysis, evidence_ids, evidence_by_id=evidence_by_id
    )
    actions.extend(summary_actions)
    actions.extend(edit_actions)
    target_tex.write_text(tailored, encoding="utf-8")
    return target_tex, actions, rejected


def _compiler_command(xelatex_path: str, tex_path: Path, compile_dir: Path) -> list[str]:
    cmd = [xelatex_path]
    if "miktex" in xelatex_path.lower() and os.getenv("MIKTEX_AUTO_INSTALL", "").strip() != "1":
        cmd.append("--disable-installer")
    cmd.extend(
        [
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-output-directory={compile_dir}",
            tex_path.name,
        ]
    )
    return cmd


def _diagnose_compile_failure(log: str) -> str:
    hints: list[str] = []
    missing_fonts = re.findall(r'The font "([^"]+)" cannot be found', log)
    if missing_fonts:
        fonts_str = "、".join(dict.fromkeys(missing_fonts))
        hints.append(
            f"诊断：本机未安装模板字体（{fonts_str}）。可在本机安装对应字体后重试，或直接将生成的 Overleaf ZIP 上传至 Overleaf 编译。"
        )
    missing_files = re.findall(r"File `([^']+)' not found", log)
    if missing_files:
        files_str = "、".join(dict.fromkeys(missing_files))
        hints.append(
            f"诊断：缺少 LaTeX 依赖文件或宏包（{files_str}）。请检查 ZIP 是否完整，或在 MiKTeX Console 中安装对应宏包。"
        )
    return "\n".join(hints)


def _read_compile_log_tail(compile_dir: Path, tex_path: Path, fallback: str = "") -> str:
    log_file = compile_dir / f"{tex_path.stem}.log"
    if log_file.exists():
        try:
            content = log_file.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                return content[-4000:]
        except OSError:
            pass
    return fallback[-4000:]


def compile_latex(xelatex_path: str, tex_path: Path) -> CompileResult:
    if not xelatex_path or not Path(xelatex_path).is_file():
        return CompileResult(success=False, log_excerpt="未找到 xelatex。请在 .env 中设置 XELATEX_PATH。")
    tex_path = tex_path.resolve()
    compile_dir = tex_path.parent / ".compile"
    if compile_dir.exists():
        shutil.rmtree(compile_dir)
    compile_dir.mkdir(parents=True)
    cmd = _compiler_command(xelatex_path, tex_path, compile_dir)

    completed: subprocess.CompletedProcess[str] | None = None
    for pass_index in range(2):
        try:
            completed = subprocess.run(
                cmd,
                cwd=tex_path.parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=150,
                check=False,
            )
        except subprocess.TimeoutExpired:
            tail = _read_compile_log_tail(compile_dir, tex_path)
            detail = f"\n日志尾部：\n{tail[-1200:]}" if tail else ""
            return CompileResult(success=False, log_excerpt=f"XeLaTeX 编译超时（150 秒）。{detail}")
        except OSError as exc:
            return CompileResult(success=False, log_excerpt=f"无法启动 XeLaTeX：{exc}")
        log = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        if completed.returncode != 0:
            break
        if pass_index == 0 and ("Rerun to get" in log or "Label(s) may have changed" in log):
            continue
        break

    assert completed is not None
    log = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    pdf = compile_dir / f"{tex_path.stem}.pdf"
    if completed.returncode != 0 or not pdf.exists():
        raw_excerpt = log[-4000:] if log else _read_compile_log_tail(compile_dir, tex_path)
        diagnosis = _diagnose_compile_failure(raw_excerpt)
        excerpt = f"{raw_excerpt[-1000:]}\n{diagnosis}" if diagnosis else raw_excerpt
        return CompileResult(success=False, log_excerpt=excerpt)
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
