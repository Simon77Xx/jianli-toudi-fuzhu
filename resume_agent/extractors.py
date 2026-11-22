from __future__ import annotations

import base64
import os
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath

import requests
from pypdf import PdfReader

from .models import Evidence


class InputError(ValueError):
    """上传材料不符合本地安全或完整性要求。"""


MAX_ZIP_BYTES = 100 * 1024 * 1024
MAX_FILES = 500
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_MEMBER_BYTES = 100 * 1024 * 1024
MAX_PDF_BYTES = 25 * 1024 * 1024
GITHUB_RE = re.compile(r"https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)", re.I)


def safe_extract_zip(archive_path: str | Path, destination: Path) -> list[Path]:
    archive = Path(archive_path)
    if archive.suffix.lower() != ".zip":
        raise InputError("请上传包含完整 LaTeX 项目的 .zip 文件。")
    if archive.stat().st_size > MAX_ZIP_BYTES:
        raise InputError("LaTeX 项目 ZIP 超过 100MB，无法安全处理。")
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > MAX_FILES:
            raise InputError("ZIP 中的文件数量超过 500，无法安全处理。")
        extracted: list[Path] = []
        total_uncompressed = 0
        for member in members:
            # ZIP names are nominally POSIX paths, but Windows-created archives
            # may contain backslashes. Normalize both forms before validation.
            normalized_name = member.filename.replace("\\", "/")
            name = PurePosixPath(normalized_name)
            if (
                name.is_absolute()
                or normalized_name.startswith("//")
                or re.match(r"^[A-Za-z]:", normalized_name)
                or ".." in name.parts
            ):
                raise InputError("ZIP 包含不安全路径，已拒绝解压。")
            if member.is_dir():
                continue
            if member.file_size > MAX_MEMBER_BYTES:
                raise InputError("ZIP 中单个文件超过 100MB，无法安全处理。")
            total_uncompressed += member.file_size
            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise InputError("ZIP 解压后的总大小超过 250MB，无法安全处理。")
            target = destination.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            extracted.append(target)
    return extracted


def find_main_tex(project_dir: Path) -> Path:
    tex_files = list(project_dir.rglob("*.tex"))
    if not tex_files:
        raise InputError("ZIP 中未找到 .tex 文件。请上传包含主文件和依赖资源的完整 LaTeX 项目。")
    candidates: list[Path] = []
    for path in tex_files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "\\documentclass" in text and "\\begin{document}" in text:
            candidates.append(path)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # Resume templates commonly ship parallel English/Chinese entrypoints.
        # Prefer an explicit Chinese entrypoint because this app is Chinese-first.
        scored = sorted(
            ((_main_tex_score(path), path) for path in candidates),
            key=lambda item: (-item[0], str(item[1]).lower()),
        )
        if scored[0][0] > scored[1][0] and scored[0][0] > 0:
            return scored[0][1]
        names = "、".join(path.name for path in candidates or tex_files)
        raise InputError(
            f"无法唯一识别 LaTeX 主文件（候选：{names}）。"
            "请将中文入口命名为包含 zh/cn/chinese 的文件，或删除不使用的入口后重新压缩。"
        )
    raise InputError("ZIP 中的 .tex 文件都不是完整主文件（缺少 documentclass 或 begin{document}）。")


def _main_tex_score(path: Path) -> int:
    """Return a conservative preference score for parallel resume entrypoints."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    stem = path.stem.lower()
    score = 0
    if re.search(r"(?:^|[-_.])(zh|cn|chinese)(?:$|[-_.])", stem):
        score += 100
    if re.search(r"\\documentclass\s*\[\s*(?:zh|cn|chinese)(?:[,\]])", text, re.I):
        score += 100
    if stem in {"main", "resume", "cv"}:
        score += 10
    if re.search(r"(?:^|[-_.])(en|english)(?:$|[-_.])", stem):
        score -= 10
    return score


def extract_pdf_text(pdf_path: str | Path) -> str:
    path = Path(pdf_path)
    try:
        if path.stat().st_size > MAX_PDF_BYTES:
            raise InputError("PDF 简历超过 25MB，无法安全处理。")
    except OSError as exc:
        raise InputError(f"无法读取 PDF 简历文件：{exc}") from exc
    try:
        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # pypdf errors vary by malformed PDF
        raise InputError(f"无法读取 PDF 简历：{exc}") from exc
    if not text.strip():
        raise InputError("PDF 未提取到可读文本；请上传包含文本层的简历 PDF。")
    return text


_BLOCK_START_RE = re.compile(r"\\(?:item\b|experience\b|education\b|begin\{abstract\})")
_BLOCK_STOP_RE = re.compile(
    r"\\(?:end\{(?:abstract|itemize|enumerate|educations|experiences|document)\}"
    r"|begin\{(?:itemize|enumerate|educations|experiences)\}"
    r"|sectionTitle\b|cvsection\b|section\*?\b|separator\b)"
)


def _strip_latex_comment(line: str) -> str:
    return re.sub(r"(?<!\\)%.*$", "", line).strip()


def _extract_tex_evidence_blocks(tex_text: str) -> list[tuple[int, int, str]]:
    """Extract multi-line LaTeX resume blocks (abstract, education, experience, items)."""
    lines = tex_text.splitlines()
    start_idx = 0
    end_idx = len(lines)
    for idx, raw in enumerate(lines):
        if "\\begin{document}" in raw:
            start_idx = idx + 1
            break
    for idx in range(start_idx, len(lines)):
        if "\\end{document}" in lines[idx]:
            end_idx = idx
            break

    blocks: list[tuple[int, int, str]] = []
    current_parts: list[str] = []
    current_start = 0
    current_end = 0
    in_abstract = False

    def flush() -> None:
        nonlocal current_parts, current_start, current_end
        if not current_parts:
            return
        text = re.sub(r"\s+", " ", " ".join(current_parts)).strip()
        if text and text not in {"\\begin{abstract}", "\\end{abstract}"}:
            blocks.append((current_start, current_end, text))
        current_parts = []
        current_start = 0
        current_end = 0

    for line_no in range(start_idx + 1, end_idx + 1):
        cleaned = _strip_latex_comment(lines[line_no - 1])
        if not cleaned:
            continue

        if "\\begin{abstract}" in cleaned:
            flush()
            in_abstract = True
            remainder = cleaned.split("\\begin{abstract}", 1)[1]
            if "\\end{abstract}" in remainder:
                body = remainder.split("\\end{abstract}", 1)[0].strip()
                if body:
                    blocks.append((line_no, line_no, body))
                in_abstract = False
            else:
                current_start = line_no
                current_end = line_no
                if remainder.strip():
                    current_parts.append(remainder.strip())
            continue

        if in_abstract:
            if "\\end{abstract}" in cleaned:
                before = cleaned.split("\\end{abstract}", 1)[0].strip()
                if before:
                    if not current_start:
                        current_start = line_no
                    current_end = line_no
                    current_parts.append(before)
                flush()
                in_abstract = False
            else:
                if not current_start:
                    current_start = line_no
                current_end = line_no
                current_parts.append(cleaned)
            continue

        # Handle lines containing multiple \item commands (e.g. two bullets on one line).
        if "\\item" in cleaned:
            segments = [seg.strip() for seg in re.split(r"(?=\\item\b)", cleaned) if seg.strip()]
            for seg in segments:
                if seg.startswith("\\item"):
                    flush()
                    current_start = line_no
                    current_end = line_no
                    current_parts = [seg]
                elif current_parts:
                    current_end = line_no
                    current_parts.append(seg)
            continue

        if _BLOCK_START_RE.search(cleaned):
            flush()
            current_start = line_no
            current_end = line_no
            current_parts = [cleaned]
            continue

        if _BLOCK_STOP_RE.search(cleaned):
            flush()
            continue

        if current_parts:
            current_end = line_no
            current_parts.append(cleaned)

    flush()
    return blocks


def evidence_from_materials(tex_text: str, pdf_text: str, confirmed_facts: list[dict[str, str]]) -> list[Evidence]:
    evidence: list[Evidence] = []
    for start_line, end_line, claim in _extract_tex_evidence_blocks(tex_text):
        location = f"LaTeX 第 {start_line} 行" if start_line == end_line else f"LaTeX 第 {start_line}-{end_line} 行"
        evidence.append(Evidence(id=f"R{len(evidence) + 1}", source="latex", claim=claim[:800], location=location))
    if not evidence and pdf_text.strip():
        for index, para in enumerate(
            (line.strip() for line in pdf_text.splitlines() if len(line.strip()) >= 8), start=1
        ):
            evidence.append(Evidence(id=f"R{len(evidence) + 1}", source="pdf", claim=para[:800], location=f"PDF 第 {index} 段"))
            if len(evidence) >= 40:
                break
    for fact in confirmed_facts:
        evidence.append(
            Evidence(
                id=fact["id"],
                source="confirmed_fact",
                claim=fact["value"],
                location=f"已确认事实：{fact['question']}",
            )
        )
    if not evidence:
        evidence.append(Evidence(id="R1", source="latex", claim="LaTeX 主文件已上传。", location="LaTeX 主文件"))
    return evidence


def github_repositories(tex_text: str) -> list[tuple[str, str]]:
    repositories: list[tuple[str, str]] = []
    for owner, repo in GITHUB_RE.findall(tex_text):
        repo = re.sub(r"\.git$", "", repo, flags=re.I).rstrip(".")
        if not repo:
            continue
        item = (owner, repo)
        if item not in repositories:
            repositories.append(item)
    return repositories


def _decode_github_readme(response: requests.Response, max_chars: int = 400) -> str:
    try:
        data = response.json()
        encoded = data.get("content", "")
        if not encoded:
            return ""
        raw = base64.b64decode(encoded).decode("utf-8", errors="ignore")
    except Exception:
        return ""
    compact = re.sub(r"\s+", " ", raw).strip()
    return compact[:max_chars]


def inspect_public_github(tex_text: str) -> tuple[list[Evidence], list[str]]:
    evidence: list[Evidence] = []
    notes: list[str] = []
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "local-resume-agent"}
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for owner, repo in github_repositories(tex_text):
        url = f"https://api.github.com/repos/{owner}/{repo}"
        label = f"{owner}/{repo}"
        try:
            response = requests.get(url, headers=headers, timeout=12)
            if response.status_code != 200:
                notes.append(f"GitHub 仓库 {label} 不可访问（HTTP {response.status_code}），未作为强化证据。")
                continue
            payload = response.json()
            readme = requests.get(f"{url}/readme", headers=headers, timeout=12)
            readme_ok = readme.status_code == 200
            has_code = payload.get("size", 0) > 0
            description = payload.get("description") or "无仓库简介"
            language = payload.get("language") or "未知语言"
            if readme_ok and has_code:
                readme_excerpt = _decode_github_readme(readme)
                excerpt_part = f"；README 摘要：{readme_excerpt}" if readme_excerpt else "；README 可访问"
                claim = f"公开仓库 {label}（主语言：{language}）：{description}{excerpt_part}"
                evidence.append(Evidence(id=f"G{len(evidence) + 1}", source="github", claim=claim[:800], location=url))
                notes.append(f"GitHub 可信度门禁通过：{label}（README 与仓库内容均可访问）。")
            else:
                missing = "README" if not readme_ok else "可识别的仓库内容"
                notes.append(f"GitHub 可信度门禁未通过：{label} 缺少{missing}，不用于加分或强化表述。")
        except requests.RequestException as exc:
            notes.append(f"读取 GitHub 仓库 {label} 失败：{exc.__class__.__name__}；本次忽略外部证据。")
    return evidence, notes


def pdf_tex_consistency_warning(pdf_text: str, tex_text: str) -> str | None:
    pdf_dates = set(re.findall(r"20\d{2}[.\-/]\d{1,2}", pdf_text))
    tex_dates = set(re.findall(r"20\d{2}[.\-/]\d{1,2}", tex_text))
    only_pdf = pdf_dates - tex_dates
    if only_pdf:
        return f"PDF 中存在 LaTeX 未出现的日期：{', '.join(sorted(only_pdf))}。请在投递前核对日期一致性。"
    return None
