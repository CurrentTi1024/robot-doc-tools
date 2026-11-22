from typing import Literal

from pydantic import BaseModel

CheckStatus = Literal["present", "missing", "uncertain"]
JobState = Literal["queued", "running", "completed", "failed", "superseded"]


class JobCreated(BaseModel):
    job_id: str


class BatchCreated(BaseModel):
    job_ids: list[str]


class JobStatus(BaseModel):
    id: str
    submission_id: str
    revision: int
    operation: Literal["checklist", "scoring"]
    state: JobState
    error: str | None


class ResultVersion(BaseModel):
    submission_id: str
    revision: int
    rules_version: str
    rules_sha256: str
    parser: str


class ChecklistItem(BaseModel):
    item_id: str
    title: str
    template_page: int
    status: CheckStatus
    evidence_pages: list[int]
    hint: str | None


class ChecklistResult(ResultVersion):
    status: Literal["completed"]
    items: list[ChecklistItem]
    summary: dict[CheckStatus, int]


class ScoringReason(BaseModel):
    text: str
    evidence_ids: list[str]
    evidence_pages: list[int]


class ScoreItem(BaseModel):
    id: Literal["A1", "A2", "A3", "B1", "B2", "B3"]
    score: float | None
    level: str | None
    reasons: list[ScoringReason]
    evidence_pages: list[int]
    questions: list[str]
    needs_review: bool


class SummaryPoint(ScoringReason):
    criterion_id: str


class Summary(BaseModel):
    text: str
    strengths: list[SummaryPoint]
    gaps: list[SummaryPoint]
    review_points: list[str]


class EvaluationResult(ResultVersion):
    category: Literal["A", "B", "C"]
    rubric_version: str
    status: Literal["completed", "completed_with_review_flags"]
    criteria: list[ScoreItem]
    summary: Summary
