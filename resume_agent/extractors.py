from __future__ import annotations

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


def evidence_from_materials(tex_text: str, pdf_text: str, confirmed_facts: list[dict[str, str]]) -> list[Evidence]:
    evidence: list[Evidence] = []
    for index, line in enumerate(tex_text.splitlines(), start=1):
        compact = line.strip()
        if compact and ("\\item" in compact or "\\experience" in compact or "\\education" in compact):
            evidence.append(Evidence(id=f"R{len(evidence) + 1}", source="latex", claim=compact[:800], location=f"LaTeX 第 {index} 行"))
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
        repo = repo.rstrip(".git")
        item = (owner, repo)
        if item not in repositories:
            repositories.append(item)
    return repositories


def inspect_public_github(tex_text: str) -> tuple[list[Evidence], list[str]]:
    evidence: list[Evidence] = []
    notes: list[str] = []
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "local-resume-agent"}
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
            if readme_ok and has_code:
                claim = f"公开仓库 {label}：{description}；README 可访问，仓库包含内容。"
                evidence.append(Evidence(id=f"G{len(evidence) + 1}", source="github", claim=claim, location=url))
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
