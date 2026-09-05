"""Pinned-device-signed P2.9 Agent execution API with P2.10 privacy cleanup.

This is the only executor router exposed by the production entrypoint. It refuses to create any job
until the exact execution scope is signed by Hassan's enrolled Android Keystore key. After final
evidence is durably stored, the raw goal/file request is replaced by a hash-only audit and purged.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import Field

from .agent_execution_privacy import (
    HASH_ONLY_AUDIT_ARTIFACT,
    build_hash_only_audit,
    purge_private_artifact,
    raw_request_digest,
)
from .agent_executor import dispatch_agent_execution
from .agent_executor_api import (
    AgentExecutionEvidence,
    AgentExecutionRequest,
    EVIDENCE_ARTIFACT,
    JOB_TYPE,
    REQUEST_ARTIFACT,
    _assert_prior_shadow_gate,
    _failure_reasons,
    _validate_request,
)
from .agent_verification_api import _callback_authorized, _find_job_artifact, _save_artifact
from .device_identity import DeviceIdentityStore, verify_pinned_execution_signature


class SignedAgentExecutionRequest(AgentExecutionRequest):
    device_id: str = Field(min_length=1, max_length=128)
    device_signature_base64: str = Field(min_length=8, max_length=1024)


def _canonical_request(body: SignedAgentExecutionRequest) -> bytes:
    return json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_secure_agent_execution_router(
    *, repo: Any, files: Any, verify_token: Callable[..., Any], new_id: Callable[[], str], now_ms: Callable[[], int],
) -> APIRouter:
    router = APIRouter()
    identity_store = DeviceIdentityStore(repo)

    @router.post("/v1/agent-executions")
    def create_agent_execution(body: SignedAgentExecutionRequest, _token: str = Depends(verify_token)) -> dict[str, Any]:
        if not repo.get_project(body.project_id):
            raise HTTPException(status_code=404, detail="project not found")
        _validate_request(body)
        _assert_prior_shadow_gate(repo, files, body)
        try:
            verify_pinned_execution_signature(
                identity_store,
                body.device_id,
                body.model_dump(mode="json"),
                body.device_signature_base64,
            )
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        canonical = _canonical_request(body)
        requested_job_id = new_id()
        job = repo.create_job(
            requested_job_id,
            body.project_id,
            body.conversation_id,
            f"One-time device-signed ACP task: {body.agent_id}@{body.version} / {body.task_id}",
            JOB_TYPE,
            now_ms(),
            idempotency_key=f"agent-permit:{body.permit_id}",
        )
        if job.get("job_type") != JOB_TYPE:
            raise HTTPException(status_code=409, detail="permit id belongs to another job type")

        existing_request = _find_job_artifact(repo, job, REQUEST_ARTIFACT)
        if job.get("id") != requested_job_id:
            if existing_request is None:
                # A completed P2.10 job normally has only the hash-only audit left. Never redispatch.
                audit = _find_job_artifact(repo, job, HASH_ONLY_AUDIT_ARTIFACT)
                if audit is not None:
                    return {
                        **job,
                        "dispatch_status": "EXISTING",
                        "request_artifact": None,
                        "privacy_audit": audit,
                        "raw_request_purged": True,
                    }
                raise HTTPException(status_code=409, detail="existing permit claim is incomplete")
            existing_bytes = files.read(existing_request["storage_path"])
            if not hmac.compare_digest(existing_bytes, canonical):
                raise HTTPException(status_code=409, detail="permit reused with a different execution request")
            return {**job, "dispatch_status": "EXISTING", "request_artifact": existing_request}

        _save_artifact(repo, files, new_id, now_ms, job, REQUEST_ARTIFACT, "application/json", canonical)
        result = dispatch_agent_execution(job["id"])
        if result.status == "QUEUED":
            repo.update_job(
                job["id"], "RUNNING", "[agent-exec] signed one-time permit dispatched\n",
                "Device-signed Agent task dispatched", now_ms(),
            )
        else:
            repo.update_job(
                job["id"], "FAILED", f"[agent-exec] dispatch {result.status}: {result.detail}\n",
                f"Agent task dispatch {result.status}: {result.detail}", now_ms(),
            )
        current = repo.get_job(job["id"]) or job
        return {
            **current,
            "dispatch_status": result.status,
            "dispatch_detail": result.detail,
            "workflow": result.workflow,
            "repository": result.repository,
            "ref": result.ref,
            "device_identity_verified": True,
        }

    @router.get("/v1/agent-executions/{job_id}")
    def get_agent_execution(job_id: str, _token: str = Depends(verify_token)) -> dict[str, Any]:
        job = repo.get_job(job_id)
        if not job or job.get("job_type") != JOB_TYPE:
            raise HTTPException(status_code=404, detail="Agent execution job not found")
        artifacts = [
            item for item in repo.project_workspace(job["project_id"]).get("artifacts", [])
            if item.get("job_id") == job_id
        ]
        return {**job, "artifacts": artifacts}

    @router.get("/v1/internal/agent-executions/{job_id}/bundle")
    def get_agent_execution_bundle(
        job_id: str,
        x_hassan_callback_secret: str | None = Header(default=None, alias="X-Hassan-Callback-Secret"),
    ) -> Response:
        _callback_authorized(x_hassan_callback_secret)
        job = repo.get_job(job_id)
        if not job or job.get("job_type") != JOB_TYPE:
            raise HTTPException(status_code=404, detail="Agent execution job not found")
        if job.get("state") not in {"RUNNING", "QUEUED"}:
            raise HTTPException(status_code=409, detail="Agent execution job cannot provide a bundle in this state")
        artifact = _find_job_artifact(repo, job, REQUEST_ARTIFACT)
        if artifact is None:
            raise HTTPException(status_code=409, detail="Agent execution request bundle missing")
        return Response(content=files.read(artifact["storage_path"]), media_type="application/json")

    @router.post("/v1/internal/agent-executions/{job_id}/evidence")
    def publish_agent_execution_evidence(
        job_id: str,
        body: AgentExecutionEvidence,
        x_hassan_callback_secret: str | None = Header(default=None, alias="X-Hassan-Callback-Secret"),
    ) -> dict[str, Any]:
        _callback_authorized(x_hassan_callback_secret)
        job = repo.get_job(job_id)
        if not job or job.get("job_type") != JOB_TYPE:
            raise HTTPException(status_code=404, detail="Agent execution job not found")
        if job.get("state") == "CANCELLED":
            raise HTTPException(status_code=409, detail="cancelled Agent task cannot accept evidence")

        evidence_bytes = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(evidence_bytes).hexdigest()
        existing = _find_job_artifact(repo, job, EVIDENCE_ARTIFACT)
        if existing:
            if existing.get("sha256") == digest:
                raw_still_present = _find_job_artifact(repo, job, REQUEST_ARTIFACT) is not None
                return {
                    "status": "ALREADY_RECORDED",
                    "job_id": job_id,
                    "artifact": existing,
                    "raw_request_purged": not raw_still_present,
                }
            raise HTTPException(status_code=409, detail="different Agent execution evidence already recorded")

        request_artifact = _find_job_artifact(repo, job, REQUEST_ARTIFACT)
        if request_artifact is None:
            raise HTTPException(status_code=409, detail="Agent execution request bundle missing before first evidence")
        raw_request = files.read(request_artifact["storage_path"])
        try:
            request = json.loads(raw_request.decode("utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=409, detail="Agent execution request bundle is invalid JSON") from exc
        if not isinstance(request, dict):
            raise HTTPException(status_code=409, detail="Agent execution request bundle must be an object")

        reasons = _failure_reasons(body, request)
        artifact = _save_artifact(
            repo, files, new_id, now_ms, job, EVIDENCE_ARTIFACT, "application/json", evidence_bytes,
        )
        audit_bytes = build_hash_only_audit(request, raw_request_digest(raw_request))
        audit = _save_artifact(
            repo, files, new_id, now_ms, job, HASH_ONLY_AUDIT_ARTIFACT, "application/json", audit_bytes,
        )

        cleanup_error = ""
        try:
            purge_private_artifact(repo, files, request_artifact)
        except Exception as exc:
            cleanup_error = f"{type(exc).__name__}:{exc}"[:240]

        state = "COMPLETED" if not reasons else "FAILED"
        if cleanup_error:
            summary = (
                "Agent task completed but raw-request privacy cleanup failed"
                if not reasons else
                f"Agent task blocked and raw-request privacy cleanup failed: {', '.join(reasons[:3])}"
            )
            privacy_log = f"[privacy] RAW REQUEST PURGE FAILED: {cleanup_error}\n"
        else:
            summary = "Device-signed one-time ACP task completed" if not reasons else f"Agent task blocked: {', '.join(reasons[:4])}"
            privacy_log = "[privacy] raw Agent request replaced by hash-only audit and purged\n"
        repo.update_job(
            job_id, state,
            f"[agent-exec] evidence received passed={not reasons} blockers={len(reasons)}\n{privacy_log}",
            summary, now_ms(),
        )
        return {
            "status": state,
            "job_id": job_id,
            "artifact": artifact,
            "privacy_audit": audit,
            "blockers": reasons,
            "raw_request_purged": not cleanup_error,
            "privacy_cleanup_error": cleanup_error or None,
        }

    return router
