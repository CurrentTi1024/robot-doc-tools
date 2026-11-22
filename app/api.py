import os
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from .config import MAX_BYTES
from .contracts import BatchCreated, ChecklistResult, EvaluationResult, JobCreated, JobStatus
from .document import InvalidPDF
from .repository import Conflict
from .service import Service, TooLarge

service = Service()


@asynccontextmanager
async def lifespan(_):
    tokens = [os.getenv(f"RW_TOKEN_{role}", "") for role in ("SUBMIT", "JUDGE")]
    if not all(tokens) or tokens[0] == tokens[1]:
        raise RuntimeError("Configure distinct RW_TOKEN_SUBMIT and RW_TOKEN_JUDGE credentials")
    service.repo.initialize()
    yield


app = FastAPI(title="Robotic Workshop Preliminary AI Service", version="1.0", lifespan=lifespan)


def token_valid(role, authorization):
    expected = os.getenv(f"RW_TOKEN_{role}", "")
    return bool(expected and authorization and secrets.compare_digest(
        authorization.encode(), f"Bearer {expected}".encode()
    ))


class PDFUploadGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or (scope["method"], scope["path"].rstrip("/")) != (
            "POST", "/v1/checklist-jobs"
        ):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        if not token_valid("SUBMIT", headers.get("authorization")):
            response = JSONResponse({"detail": "Authentication required"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
            return await response(scope, receive, send)
        limit = MAX_BYTES + 64 * 1024  # Allow the multipart envelope.
        length = headers.get("content-length", "")
        if length.isdecimal() and (len(length) > 16 or int(length) > limit):
            return await JSONResponse({"detail": "PDF exceeds upload limit"}, 413)(scope, receive, send)
        total = 0

        async def bounded_receive():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > limit:
                raise HTTPException(413, "PDF exceeds upload limit")
            return message

        return await self.app(scope, bounded_receive, send)


app.add_middleware(PDFUploadGuard)


def require(role):
    def authorize(authorization: Annotated[str | None, Header()] = None):
        if not token_valid(role, authorization):
            raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    return authorize


submit_access = Depends(require("SUBMIT"))
judge_access = Depends(require("JUDGE"))


class SubmissionVersion(BaseModel):
    submission_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    revision: int = Field(ge=1, le=2_147_483_647)


class ScoringJobs(BaseModel):
    submissions: list[SubmissionVersion] = Field(min_length=1, max_length=200)


@app.post("/v1/checklist-jobs", status_code=202, response_model=JobCreated, dependencies=[submit_access])
def request_checklist(
    submission_id: Annotated[str, Form()],
    revision: Annotated[int, Form()],
    category: Annotated[Literal["A", "B", "C"], Form()],
    request_id: Annotated[str, Form(min_length=1, max_length=100)],
    pdf: Annotated[UploadFile, File()],
):
    try:
        if pdf.content_type not in {"application/pdf", "application/octet-stream"}:
            raise InvalidPDF("Expected a PDF upload")
        return {"job_id": service.submit(submission_id, revision, category, pdf.file.read(MAX_BYTES + 1))}
    except TooLarge as exc:
        raise HTTPException(413, str(exc)) from None
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except (InvalidPDF, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None


@app.post("/v1/scoring-jobs", status_code=202, response_model=BatchCreated, dependencies=[judge_access])
def request_scoring(batch: ScoringJobs):
    try:
        return {"job_ids": service.score_jobs([(item.submission_id, item.revision) for item in batch.submissions])}
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None


@app.get("/v1/jobs/{job_id}", response_model=JobStatus)
def job_status(job_id: str, authorization: Annotated[str | None, Header()] = None):
    credentials = {role for role in ("SUBMIT", "JUDGE") if token_valid(role, authorization)}
    if not credentials:
        raise HTTPException(401, "Authentication required")
    job = service.repo.job(job_id)
    if job is None or (job["operation"] == "scoring" and "JUDGE" not in credentials):
        raise HTTPException(404, "Job unavailable")
    return job


@app.get("/v1/submissions/{submission_id}/checklist", response_model=ChecklistResult, dependencies=[submit_access])
def get_checklist(submission_id: str):
    return get_result(submission_id, "checklist")


@app.get("/v1/submissions/{submission_id}/evaluation", response_model=EvaluationResult, dependencies=[judge_access])
def get_evaluation(submission_id: str):
    return get_result(submission_id, "scoring")


def get_result(submission_id, operation):
    result = service.result(submission_id, operation)
    if result is None:
        raise HTTPException(404, "Current result unavailable")
    return result


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    try:
        with service.repo.transaction() as q:
            q("SELECT 1").fetchone()
    except Exception:
        raise HTTPException(503, "Database unavailable") from None
    return {"status": "ready"}
