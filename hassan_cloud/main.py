"""Hassan Cloud v0.4 — host-independent, Leapcell-ready."""

from __future__ import annotations

import logging
import mimetypes
import os
from typing import Any

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .auth import TokenService, auth_dependency
from .config import (
    ARTIFACT_STORE,
    DATA_DIR,
    DB_BACKEND,
    DB_PATH,
    ENV,
    FILE_DIR,
    JOB_RUNTIME,
    WORKSPACE_DIR,
    ensure_dirs,
)
from .providers.registry import list_providers, select_for_capability
from .radar.candidates import seed_radar
from .storage import get_file_store, get_repository
from .util import new_id, now_ms
from .workers.dispatcher import JobDispatcher
from .workers.job_worker import JobWorker

logger = logging.getLogger("hassan.cloud")

ensure_dirs()
repo = get_repository()
files = get_file_store(repo)
token_service = TokenService(repo)
verify_token = auth_dependency(token_service)
job_worker = JobWorker(repo, files, new_id, now_ms)
job_dispatcher = JobDispatcher(repo, files, new_id, now_ms)

app = FastAPI(title="Hassan Cloud", version="0.4.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_MIME_PREFIXES = ("image/",)
ALLOWED_MIME_EXACT = {
    "application/pdf",
    "text/plain",
    "application/zip",
    "application/x-zip-compressed",
    "application/octet-stream",
}


# --- Models ---


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    provider: str = "auto"
    messages: list[ChatMessage]
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    answer: str
    provider: str
    model: str | None = None
    status: str = "OK"


class ProjectCreate(BaseModel):
    name: str
    description: str = ""


class JobCreate(BaseModel):
    project_id: str
    conversation_id: str | None = None
    goal: str
    job_type: str = "general"
    idempotency_key: str | None = None


class AgentRunRequest(BaseModel):
    goal: str
    project_id: str | None = None


class TokenCreateRequest(BaseModel):
    label: str = "device"
    device_id: str | None = None


class RadarEvaluateRequest(BaseModel):
    status: str = Field(..., description="EVALUATING|TESTING|APPROVED|REJECTED|INTEGRATED")
    notes: str = ""


# --- Lifecycle ---


@app.on_event("startup")
def on_startup() -> None:
    repo.init_schema()
    raw = token_service.bootstrap(new_id, now_ms)
    if raw and ENV == "production":
        logger.warning("Production bootstrap token created — save from host logs once. len=%d", len(raw))
    elif raw and ENV == "development":
        logger.warning(
            "Development bootstrap token created. Save it now — it will not be shown again. Length=%d chars",
            len(raw),
        )
    if JOB_RUNTIME == "thread":
        job_worker.start()
    seeded = seed_radar(repo, new_id, now_ms)
    logger.info(
        "Hassan Cloud started env=%s backend=%s job_runtime=%s artifact_store=%s radar=%d",
        ENV, DB_BACKEND, JOB_RUNTIME, ARTIFACT_STORE, seeded,
    )


@app.on_event("shutdown")
def on_shutdown() -> None:
    job_worker.stop()
    job_dispatcher.shutdown()


# --- Health / auth ---


@app.get("/v1/health")
def health() -> dict[str, Any]:
    tokens = repo.list_tokens()
    active = sum(1 for t in tokens if t.get("revoked_at") is None)
    db_ok = True
    if hasattr(repo, "health_check"):
        db_ok = repo.health_check()
    worker_status = "thread" if JOB_RUNTIME == "thread" else "dispatch"
    artifact_mode = "EPHEMERAL_DB" if ARTIFACT_STORE == "db" else "LOCAL_FS"
    return {
        "status": "ok" if db_ok else "degraded",
        "service": "hassan-cloud",
        "version": "0.4.0",
        "env": ENV,
        "database": {"backend": DB_BACKEND, "status": "WORKING" if db_ok else "FAILED"},
        "job_runtime": {"mode": worker_status, "status": "WORKING"},
        "artifact_store": artifact_mode,
        "persistence": {
            "data_dir": str(DATA_DIR),
            "db_path": str(DB_PATH),
            "file_dir": str(FILE_DIR),
            "workspace_dir": str(WORKSPACE_DIR),
        },
        "auth": {"active_tokens": active, "configured": active > 0},
        "openai_configured": bool(os.environ.get("OPENAI_API_KEY")),
        "chat_status": "NOT_CONFIGURED" if not os.environ.get("OPENAI_API_KEY") else "AVAILABLE",
    }


@app.post("/v1/auth/verify")
def auth_verify(_token: str = Depends(verify_token)) -> dict[str, str]:
    return {"status": "valid"}


@app.post("/v1/auth/tokens")
def create_token(
    body: TokenCreateRequest,
    _token: str = Depends(verify_token),
) -> dict[str, str]:
    tid, raw = token_service.create_token(body.label, body.device_id, new_id, now_ms())
    return {"id": tid, "token": raw, "label": body.label}


@app.delete("/v1/auth/tokens/{token_id}")
def revoke_token(token_id: str, _token: str = Depends(verify_token)) -> dict[str, Any]:
    ok = token_service.revoke(token_id, now_ms())
    if not ok:
        raise HTTPException(status_code=404, detail="token not found or already revoked")
    return {"status": "revoked", "id": token_id}


# --- Chat (honest fallback — no paid API required) ---


def _honest_chat(provider: str, messages: list[ChatMessage]) -> ChatResponse:
    last = messages[-1].content if messages else ""
    return ChatResponse(
        answer=(
            f"Hassan Cloud يعمل.\n\n"
            f"المزوّد: {provider}\n"
            f"رسالتك: {last[:200]}\n\n"
            "حالة Chat: NOT_CONFIGURED — لا يوجد LLM مدفوع على الخادم.\n"
            "المهام السحابية والـ artifacts تعمل بدون OpenAI."
        ),
        provider=provider,
        model="hassan-honest",
        status="NOT_CONFIGURED",
    )


@app.post("/v1/chat", response_model=ChatResponse)
def chat(req: ChatRequest, _token: str = Depends(verify_token)) -> ChatResponse:
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages required")
    provider = (req.provider or "auto").lower()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if provider in ("chatgpt", "auto") and api_key:
        try:
            payload = {
                "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            }
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                return ChatResponse(
                    answer=data["choices"][0]["message"]["content"],
                    provider="chatgpt" if provider == "chatgpt" else "auto",
                    model=data.get("model"),
                    status="OK",
                )
        except Exception as exc:
            return ChatResponse(
                answer=f"تعذر الاتصال بـ OpenAI: {exc}",
                provider=provider,
                status="ERROR",
            )
    if provider in ("gemini", "deepseek"):
        return ChatResponse(
            answer=f"مزوّد {provider}: NOT_CONFIGURED على الخادم.",
            provider=provider,
            status="NOT_CONFIGURED",
        )
    return _honest_chat(provider, req.messages)


# --- Projects / workspace ---


@app.get("/v1/projects")
def list_projects(_token: str = Depends(verify_token)) -> list[dict[str, Any]]:
    return repo.list_projects()


@app.post("/v1/projects")
def create_project(body: ProjectCreate, _token: str = Depends(verify_token)) -> dict[str, Any]:
    ts = now_ms()
    workspace = str(WORKSPACE_DIR / new_id())
    return repo.create_project(new_id(), body.name, body.description, workspace, ts)


@app.get("/v1/projects/{project_id}")
def get_project(project_id: str, _token: str = Depends(verify_token)) -> dict[str, Any]:
    project = repo.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@app.get("/v1/projects/{project_id}/workspace")
def project_workspace(project_id: str, _token: str = Depends(verify_token)) -> dict[str, Any]:
    ws = repo.project_workspace(project_id)
    if not ws:
        raise HTTPException(status_code=404, detail="project not found")
    return ws


# --- Jobs (async worker — no long HTTP) ---


@app.post("/v1/jobs")
def create_job(body: JobCreate, _token: str = Depends(verify_token)) -> dict[str, Any]:
    if not repo.get_project(body.project_id):
        raise HTTPException(status_code=404, detail="project not found")
    job = repo.create_job(
        new_id(),
        body.project_id,
        body.conversation_id,
        body.goal,
        body.job_type,
        now_ms(),
        idempotency_key=body.idempotency_key,
    )
    if JOB_RUNTIME == "dispatch":
        job_dispatcher.enqueue(job["id"])
    return job


@app.get("/v1/jobs")
def list_jobs(_token: str = Depends(verify_token)) -> list[dict[str, Any]]:
    return repo.list_jobs()


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, _token: str = Depends(verify_token)) -> dict[str, Any]:
    job = repo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if JOB_RUNTIME == "dispatch" and job.get("state") == "QUEUED":
        job_dispatcher.enqueue(job_id)
    runs = repo.list_agent_runs(job_id)
    return {**job, "agent_runs": runs}


@app.post("/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str, _token: str = Depends(verify_token)) -> dict[str, Any]:
    if not repo.cancel_job(job_id, now_ms()):
        raise HTTPException(status_code=409, detail="job cannot be cancelled")
    return repo.get_job(job_id) or {}


# --- Artifacts / files ---


def _validate_mime(mime: str, filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    effective = mime or guessed or "application/octet-stream"
    if effective.startswith(ALLOWED_MIME_PREFIXES) or effective in ALLOWED_MIME_EXACT:
        return effective
    raise HTTPException(status_code=415, detail=f"mime type not allowed: {effective}")


@app.post("/v1/files/upload")
async def upload_file(
    file: UploadFile = File(...),
    project_id: str | None = Form(default=None),
    job_id: str | None = Form(default=None),
    conversation_id: str | None = Form(default=None),
    _token: str = Depends(verify_token),
) -> dict[str, Any]:
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file too large (max 25MB)")
    filename = file.filename or "upload.bin"
    mime = _validate_mime(file.content_type or "", filename)
    aid = new_id()
    rel, digest, size = files.save(aid, filename, data)
    row = repo.create_artifact({
        "id": aid,
        "project_id": project_id,
        "job_id": job_id,
        "conversation_id": conversation_id,
        "name": filename,
        "mime_type": mime,
        "size_bytes": size,
        "storage_path": rel,
        "sha256": digest,
        "created_at": now_ms(),
    })
    return row


@app.get("/v1/artifacts")
def list_artifacts(
    project_id: str | None = None,
    _token: str = Depends(verify_token),
) -> list[dict[str, Any]]:
    return repo.list_artifacts(project_id=project_id)


@app.get("/v1/files/{artifact_id}")
def download_file(artifact_id: str, _token: str = Depends(verify_token)) -> Response:
    artifact = repo.get_artifact(artifact_id)
    if not artifact or not artifact.get("storage_path"):
        raise HTTPException(status_code=404, detail="artifact not found")
    try:
        data = files.read(artifact["storage_path"])
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="file missing on disk")
    return Response(
        content=data,
        media_type=artifact["mime_type"],
        headers={"Content-Disposition": f'attachment; filename="{artifact["name"]}"'},
    )


# --- Agents ---


@app.post("/v1/agents/run")
def agents_run(body: AgentRunRequest, _token: str = Depends(verify_token)) -> dict[str, Any]:
    from .agents.pipeline import run_agent_pipeline

    jid = new_id()
    final, summary = run_agent_pipeline(repo, jid, body.goal, None, new_id, now_ms)
    return {"goal": body.goal, "status": final, "summary": summary, "job_id": jid}


@app.get("/v1/jobs/{job_id}/agent-runs")
def job_agent_runs(job_id: str, _token: str = Depends(verify_token)) -> list[dict[str, Any]]:
    return repo.list_agent_runs(job_id)


# --- Providers / capabilities ---


@app.get("/v1/providers")
def providers(_token: str = Depends(verify_token)) -> list[dict[str, Any]]:
    return list_providers()


@app.get("/v1/capabilities/{capability}")
def capability_providers(capability: str, _token: str = Depends(verify_token)) -> dict[str, Any]:
    matched = select_for_capability(capability)
    return {"capability": capability, "providers": matched}


# --- Radar 2.0 ---


@app.post("/v1/radar/scan")
def radar_scan(_token: str = Depends(verify_token)) -> dict[str, Any]:
    count = seed_radar(repo, new_id, now_ms)
    candidates = repo.list_radar_candidates()
    return {"status": "OK", "seeded": count, "candidates": candidates}


@app.get("/v1/radar/candidates")
def radar_candidates(_token: str = Depends(verify_token)) -> list[dict[str, Any]]:
    return repo.list_radar_candidates()


@app.post("/v1/radar/candidates/{candidate_id}/evaluate")
def radar_evaluate(
    candidate_id: str,
    body: RadarEvaluateRequest,
    _token: str = Depends(verify_token),
) -> dict[str, Any]:
    allowed = {"EVALUATING", "TESTING", "APPROVED", "REJECTED", "INTEGRATED"}
    if body.status not in allowed:
        raise HTTPException(status_code=400, detail=f"status must be one of {allowed}")
    candidates = repo.list_radar_candidates()
    match = next((c for c in candidates if c["id"] == candidate_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="candidate not found")
    match["status"] = body.status
    match["last_evaluated_at"] = now_ms()
    match["notes"] = body.notes or match.get("notes")
    repo.upsert_radar_candidate(match)
    if body.status == "APPROVED":
        caps = match.get("capabilities") or []
        for cap in caps:
            logger.info("Radar approved candidate %s maps to capability %s", match["name"], cap)
    return match
