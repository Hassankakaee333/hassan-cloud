"""Zero-PC API bridge for Frishta Agent artifact verification.

Public requests are authenticated with Hassan Cloud device auth. GitHub Actions evidence is accepted
only through a separate callback secret after the no-exec verification job has finished.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .agent_verification import AgentVerificationSpec, dispatch_agent_verification

REQUEST_ARTIFACT = "agent-verification-request.json"
EVIDENCE_ARTIFACT = "agent-artifact-verification.json"
JOB_TYPE = "agent_artifact_verify"


class AgentVerificationRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    static_evidence_id: str = Field(min_length=1, max_length=256)
    distribution_kind: str = Field(min_length=1, max_length=16)
    version: str = Field(min_length=1, max_length=128)
    source_url: str = Field(default="", max_length=2048)
    expected_sha256: str = Field(default="", max_length=64)
    package: str = Field(default="", max_length=256)
    idempotency_key: str | None = Field(default=None, max_length=256)


class AgentVerificationEvidence(BaseModel):
    schema_version: int = 1
    agent_id: str
    evidence_id: str
    static_evidence_id: str
    github_run_id: str | int | None = None
    distribution_kind: str | None = None
    version: str | None = None
    artifact_sha256: str = ""
    artifact_executed: bool
    secrets_used: bool
    integrity_verified: bool
    archive_safe: bool
    dependency_lock_complete: bool
    passed: bool
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source_url: str | None = None
    archive: dict[str, Any] | None = None


def _canonical_request(body: AgentVerificationRequest) -> bytes:
    payload = body.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _artifacts_for_job(repo: Any, job: dict[str, Any]) -> list[dict[str, Any]]:
    workspace = repo.project_workspace(job["project_id"])
    return [item for item in workspace.get("artifacts", []) if item.get("job_id") == job["id"]]


def _find_job_artifact(repo: Any, job: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((item for item in _artifacts_for_job(repo, job) if item.get("name") == name), None)


def _save_artifact(
    repo: Any,
    files: Any,
    new_id: Callable[[], str],
    now_ms: Callable[[], int],
    job: dict[str, Any],
    name: str,
    mime_type: str,
    data: bytes,
) -> dict[str, Any]:
    artifact_id = new_id()
    storage_path, digest, size = files.save(artifact_id, name, data)
    return repo.create_artifact(
        {
            "id": artifact_id,
            "project_id": job["project_id"],
            "job_id": job["id"],
            "conversation_id": job.get("conversation_id"),
            "name": name,
            "mime_type": mime_type,
            "size_bytes": size,
            "storage_path": storage_path,
            "sha256": digest,
            "created_at": now_ms(),
        }
    )


def _load_request(repo: Any, files: Any, job: dict[str, Any]) -> dict[str, Any]:
    artifact = _find_job_artifact(repo, job, REQUEST_ARTIFACT)
    if not artifact or not artifact.get("storage_path"):
        raise HTTPException(status_code=409, detail="agent verification request artifact missing")
    try:
        raw = files.read(artifact["storage_path"])
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"invalid request artifact: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=409, detail="invalid request artifact")
    return payload


def _callback_authorized(provided: str | None) -> None:
    expected = os.environ.get("HASSAN_CALLBACK_SECRET", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="HASSAN_CALLBACK_SECRET not configured")
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid callback secret")


def build_agent_verification_router(
    *,
    repo: Any,
    files: Any,
    verify_token: Callable[..., Any],
    new_id: Callable[[], str],
    now_ms: Callable[[], int],
) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/agent-verifications")
    def create_agent_verification(
        body: AgentVerificationRequest,
        _token: str = Depends(verify_token),
    ) -> dict[str, Any]:
        if not repo.get_project(body.project_id):
            raise HTTPException(status_code=404, detail="project not found")

        spec = AgentVerificationSpec(
            agent_id=body.agent_id,
            static_evidence_id=body.static_evidence_id,
            distribution_kind=body.distribution_kind,
            version=body.version,
            source_url=body.source_url,
            expected_sha256=body.expected_sha256,
            package=body.package,
        )
        try:
            spec.validate()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        job = repo.create_job(
            new_id(),
            body.project_id,
            body.conversation_id,
            f"Verify Agent artifact: {body.agent_id}@{body.version}",
            JOB_TYPE,
            now_ms(),
            idempotency_key=body.idempotency_key,
        )
        if job.get("job_type") != JOB_TYPE:
            raise HTTPException(status_code=409, detail="idempotency key belongs to another job type")

        existing_request = _find_job_artifact(repo, job, REQUEST_ARTIFACT)
        if existing_request is None:
            _save_artifact(
                repo,
                files,
                new_id,
                now_ms,
                job,
                REQUEST_ARTIFACT,
                "application/json",
                _canonical_request(body),
            )
        elif job.get("state") != "QUEUED":
            return {**job, "dispatch_status": "EXISTING", "request_artifact": existing_request}

        result = dispatch_agent_verification(job["id"], spec)
        if result.status == "QUEUED":
            repo.update_job(
                job["id"],
                "RUNNING",
                "[agent-verify] GitHub no-exec verification dispatched\n",
                "Agent artifact verification dispatched",
                now_ms(),
            )
        else:
            repo.update_job(
                job["id"],
                "FAILED",
                f"[agent-verify] dispatch {result.status}: {result.detail}\n",
                f"Agent verification dispatch {result.status}: {result.detail}",
                now_ms(),
            )
        current = repo.get_job(job["id"]) or job
        return {
            **current,
            "dispatch_status": result.status,
            "dispatch_detail": result.detail,
            "workflow": result.workflow,
            "repository": result.repository,
            "ref": result.ref,
        }

    @router.get("/v1/agent-verifications/{job_id}")
    def get_agent_verification(
        job_id: str,
        _token: str = Depends(verify_token),
    ) -> dict[str, Any]:
        job = repo.get_job(job_id)
        if not job or job.get("job_type") != JOB_TYPE:
            raise HTTPException(status_code=404, detail="agent verification not found")
        artifacts = _artifacts_for_job(repo, job)
        return {**job, "artifacts": artifacts}

    @router.post("/v1/internal/agent-verifications/{job_id}/evidence")
    def publish_agent_verification_evidence(
        job_id: str,
        body: AgentVerificationEvidence,
        x_hassan_callback_secret: str | None = Header(default=None, alias="X-Hassan-Callback-Secret"),
    ) -> dict[str, Any]:
        _callback_authorized(x_hassan_callback_secret)
        job = repo.get_job(job_id)
        if not job or job.get("job_type") != JOB_TYPE:
            raise HTTPException(status_code=404, detail="agent verification not found")
        request_payload = _load_request(repo, files, job)

        if body.agent_id != request_payload.get("agent_id"):
            raise HTTPException(status_code=409, detail="evidence agent_id mismatch")
        if body.static_evidence_id != request_payload.get("static_evidence_id"):
            raise HTTPException(status_code=409, detail="evidence static_evidence_id mismatch")
        if body.distribution_kind and body.distribution_kind != request_payload.get("distribution_kind"):
            raise HTTPException(status_code=409, detail="evidence distribution_kind mismatch")
        if body.version and body.version != request_payload.get("version"):
            raise HTTPException(status_code=409, detail="evidence version mismatch")
        if body.artifact_executed or body.secrets_used:
            raise HTTPException(status_code=409, detail="unsafe verification evidence rejected")

        existing = _find_job_artifact(repo, job, EVIDENCE_ARTIFACT)
        evidence_bytes = json.dumps(
            body.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        evidence_digest = hashlib.sha256(evidence_bytes).hexdigest()
        if existing:
            if existing.get("sha256") == evidence_digest:
                return {"status": "ALREADY_RECORDED", "job_id": job_id, "artifact": existing}
            raise HTTPException(status_code=409, detail="different evidence already recorded for job")

        artifact = _save_artifact(
            repo,
            files,
            new_id,
            now_ms,
            job,
            EVIDENCE_ARTIFACT,
            "application/json",
            evidence_bytes,
        )
        if body.blockers:
            state = "FAILED"
            summary = f"Agent artifact verification blocked: {', '.join(body.blockers[:3])}"
        elif body.passed:
            state = "COMPLETED"
            summary = "Agent artifact verification passed; evidence ready for Frishta Evaluation Lab"
        else:
            state = "COMPLETED"
            summary = "Agent artifact integrity checked; SECURITY_CHECKED remains blocked pending complete dependency evidence"
        repo.update_job(
            job_id,
            state,
            f"[agent-verify] evidence received passed={body.passed} blockers={len(body.blockers)}\n",
            summary,
            now_ms(),
        )
        return {
            "status": state,
            "job_id": job_id,
            "artifact": artifact,
            "passed": body.passed,
            "security_checked_automatically": False,
        }

    return router
