from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    id: str
    source: Literal["latex", "pdf", "confirmed_fact", "github"]
    claim: str
    location: str


class Requirement(BaseModel):
    name: str
    tier: Literal["hard_gate", "transferable", "core_gap", "bonus"]
    status: Literal["supported", "needs_confirmation", "unsupported"]
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)


class Question(BaseModel):
    id: str
    question: str
    why: str
    related_requirement: str


class InitialAnalysis(BaseModel):
    quick_assessment: str
    requirements: list[Requirement]
    questions: list[Question] = Field(default_factory=list, max_length=5)


class TexEdit(BaseModel):
    source: str
    target: str
    reason: str
    evidence_ids: list[str] = Field(min_length=1)


class DefenseRecord(BaseModel):
    rewritten_text: str
    source_evidence_ids: list[str] = Field(min_length=1)
    rewrite_reason: str
    possible_question: str
    truthful_answer_outline: str


class FinalAnalysis(BaseModel):
    hr_summary: str
    delivery_score: int = Field(ge=0, le=100)
    competitiveness_score: int = Field(ge=0, le=100)
    delivery_reasons: list[str]
    competitiveness_reasons: list[str]
    keywords: list[str] = Field(default_factory=list)
    tailored_summary: str = ""
    section_order: list[str] = Field(default_factory=list)
    edits: list[TexEdit] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    future_suggestions: list[str] = Field(default_factory=list)
    defense_records: list[DefenseRecord] = Field(default_factory=list)


class CompileResult(BaseModel):
    success: bool
    page_count: int | None = None
    log_excerpt: str = ""
    pdf_path: str | None = None
    compression_actions: list[str] = Field(default_factory=list)


class JobContext(BaseModel):
    session_id: str
    work_dir: str
    project_dir: str
    tex_path: str
    pdf_path: str
    jd_text: str
    tex_text: str
    pdf_text: str
    evidence: list[Evidence]
    github_enabled: bool = False
    github_notes: list[str] = Field(default_factory=list)
    initial: InitialAnalysis | None = None
    answers: dict[str, str] = Field(default_factory=dict)
    current_question_index: int = 0

    @property
    def root(self) -> Path:
        return Path(self.work_dir)

