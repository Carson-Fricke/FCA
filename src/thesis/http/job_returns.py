"""Authenticated harness completion intake; all persistence belongs to Conversations."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from thesis.application.job_returns import deliver_job_completion


class JobCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    job_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    kind: Literal["backtest", "study", "screen"]
    state: Literal["succeeded", "failed", "cancelled"]
    metrics: dict[str, str]
    artifact_count: int = Field(ge=0)
    error_message: str | None


def create_job_return_router() -> APIRouter:
    router = APIRouter()

    @router.post("/v1/internal/harness/completions", status_code=204)
    def completion(body: JobCompletion, request: Request) -> None:
        conversations = getattr(request.app.state, "conversations", None)
        submissions = getattr(request.app.state, "submission_store", None)
        if conversations is None or submissions is None:
            raise HTTPException(503, "job completion delivery is not configured")
        try:
            deliver_job_completion(submissions, conversations, body.model_dump())
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    return router
