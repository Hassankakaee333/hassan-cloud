"""Authenticated API for the zero-paid-API Gemini UI worker."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .provider_ui_worker import GeminiUiWorker


class GeminiWorkerRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128)
    goal: str = Field(..., min_length=1, max_length=12000)
    conversation_id: str | None = Field(default=None, max_length=128)
    max_steps: int = Field(default=8, ge=1, le=12)


def build_provider_ui_worker_router(*, repo, verify_token, new_id: Callable[[], str], now_ms: Callable[[], int]) -> APIRouter:
    router = APIRouter(prefix="/v1/provider-workers", tags=["provider-workers"])
    worker = GeminiUiWorker(repo, new_id, now_ms)

    @router.post("/gemini/start")
    def start_gemini(body: GeminiWorkerRequest, _token: str = Depends(verify_token)) -> dict:
        if not repo.get_project(body.project_id):
            raise HTTPException(status_code=404, detail="project not found")
        jid = new_id()
        job = repo.create_job(
            jid,
            body.project_id,
            body.conversation_id,
            body.goal,
            "gemini_ui_worker",
            now_ms(),
            idempotency_key=None,
        )
        started = worker.start(jid, body.goal, max_steps=body.max_steps)
        return {"job": job, "worker": started, "policy": {"paid_api": False, "transport": "official-android-ui", "stable_write": False, "secret_input": False}}

    @router.post("/gemini/{job_id}/resume")
    def resume_gemini(job_id: str, _token: str = Depends(verify_token)) -> dict:
        job = repo.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if job.get("state") == "COMPLETED":
            return {"status": "ALREADY_COMPLETED", "job_id": job_id}
        if job.get("state") == "CANCELLED":
            raise HTTPException(status_code=409, detail="job cancelled")
        return worker.start(job_id, str(job.get("goal") or ""), max_steps=8)

    return router
