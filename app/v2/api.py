from contextlib import asynccontextmanager
import os
import secrets
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app.config import MAX_BYTES
from .presentation import InvalidPresentation
from .repository import Conflict
from .service import Service, TooLarge


JobId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
SubmissionId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]


class ScoreUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_job_ids: list[JobId] = Field(min_length=1, max_length=200)


class QueryScore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: JobId


def authorized(role, authorization):
    secret = os.getenv(f"RW_TOKEN_{role}", "")
    return bool(secret and authorization and secrets.compare_digest(
        authorization.encode(), f"Bearer {secret}".encode()
    ))


def require(role):
    def check(authorization: Annotated[str | None, Header()] = None):
        if not authorized(role, authorization):
            raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    return Depends(check)


class RequestGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        path = scope["path"].rstrip("/")
        limits = {"/v2/checklist-jobs": ("SUBMIT", MAX_BYTES + 64 * 1024),
                  "/v2/scoring-jobs": ("JUDGE", 64 * 1024)}
        if path not in limits:
            return await self.app(scope, receive, send)
        role, limit = limits[path]
        headers = Headers(scope=scope)
        if not authorized(role, headers.get("authorization")):
            return await JSONResponse({"detail": "Authentication required"}, 401,
                                      headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        length = headers.get("content-length", "")
        if length.isdecimal() and (len(length) > 16 or int(length) > limit):
            return await JSONResponse({"detail": "Request exceeds limit"}, 413)(scope, receive, send)
        total = 0

        async def bounded_receive():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > limit:
                raise HTTPException(413, "Request exceeds limit")
            return message

        return await self.app(scope, bounded_receive, send)


def create_app(service=None):
    service = service if service is not None else Service()

    @asynccontextmanager
    async def lifespan(_):
        tokens = tuple(os.getenv(f"RW_TOKEN_{role}") for role in ("SUBMIT", "JUDGE"))
        if not all(tokens) or tokens[0] == tokens[1]:
            raise RuntimeError("Configure distinct RW_TOKEN_SUBMIT and RW_TOKEN_JUDGE credentials")
        service.repo.initialize()
        yield

    api = FastAPI(title="Robotic Workshop Preliminary Review", version="2.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    api.add_middleware(RequestGuard)

    def query(identifier, operation):
        result = service.repo.snapshot(identifier, operation)
        if result is None:
            raise HTTPException(404, "Job unavailable")
        return result

    @api.post("/v2/checklist-jobs", dependencies=[require("SUBMIT")],
              responses={202: {"description": "Checklist job accepted"}})
    def request_checklist(
        response: Response,
        job_id: JobId | None = None,
        submission_id: Annotated[SubmissionId | None, Form()] = None,
        category: Annotated[Literal["A", "B", "C"] | None, Form()] = None,
        pptx: Annotated[UploadFile | None, File()] = None,
    ):
        if job_id is not None:
            if any(value is not None for value in (submission_id, category, pptx)):
                raise HTTPException(422, "Send job_id or an upload")
            return query(job_id, "checklist")
        if not submission_id or not category or pptx is None:
            raise HTTPException(422, "Provide submission_id, category and pptx")
        try:
            job = service.submit(submission_id, category, pptx.file.read(MAX_BYTES + 1))
        except TooLarge as exc:
            raise HTTPException(413, str(exc)) from None
        except InvalidPresentation as exc:
            raise HTTPException(422, str(exc)) from None
        response.status_code = 202
        return {"job_id": job, "status": "queued"}

    @api.post("/v2/scoring-jobs", dependencies=[require("JUDGE")],
              responses={202: {"description": "Scoring jobs accepted"}})
    def request_scoring(body: ScoreUpload | QueryScore, response: Response):
        if isinstance(body, QueryScore):
            return query(body.job_id, "scoring")
        try:
            jobs = service.repo.insert_scoring(body.upload_job_ids, service.settings)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        response.status_code = 202
        return {"job_ids": jobs}

    return api


app = create_app()
