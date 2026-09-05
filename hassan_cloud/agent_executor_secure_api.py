"""Pinned-device-signed Agent execution API with one-time private bundle delivery.

P2.11 keeps the P2.9 signed execution boundary and P2.10 hash-only privacy audit, while making the
trusted prepare bundle itself single-consumption. Cloud returns raw goal/file bytes only after an
atomic claim and only after the raw Cloud artifact has been replaced by a durable hash-only audit.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import Field

from .agent_execution_bundle_claim import AgentExecutionBundleClaimStore
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


def _decode_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=409, detail=f"{label} must be an object")
    return value


def _read_audit(files: Any, artifact: dict[str, Any]) -> dict[str, Any]:
    return _decode_object(files.read(artifact["storage_path"]), "Agent execution privacy audit")


def build_secure_agent_execution_router(
    *, repo: Any, files: Any, verify_token: Callable[..., Any], new_id: Callable[[], str], now_ms: Callable[[], int],
) -> APIRouter:
    router = APIRouter()
    identity_store = DeviceIdentityStore(repo)
    bundle_claims = AgentExecutionBundleClaimStore(repo)

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
                audit_artifact = _find_job_artifact(repo, job, HASH_ONLY_AUDIT_ARTIFACT)
                if audit_artifact is not None:
                    audit = _read_audit(files, audit_artifact)
                    original_digest = str(audit.get("raw_request_sha256") or "").lower()
                    current_digest = hashlib.sha256(canonical).hexdigest()
                    if not hmac.compare_digest(original_digest, current_digest):
                        raise HTTPException(
                            status_code=409,
                            detail="permit reused with a different signed execution request after bundle consumption",
                        )
                    return {
                        **job,
                        "dispatch_status": "EXISTING",
                        "request_artifact": None,
                        "privacy_audit": audit_artifact,
                        "raw_request_purged": True,
                        "bundle_claimed": bundle_claims.is_claimed(job["id"]),
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
        return {**job, "artifacts": artifacts, "bundle_claimed": bundle_claims.is_claimed(job_id)}

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
        request_artifact = _find_job_artifact(repo, job, REQUEST_ARTIFACT)
        if request_artifact is None:
            if bundle_claims.is_claimed(job_id):
                raise HTTPException(status_code=409, detail="Agent execution bundle was already claimed")
            raise HTTPException(status_code=409, detail="Agent execution request bundle missing")

        raw_request = files.read(request_artifact["storage_path"])
        request = _decode_object(raw_request, "Agent execution request bundle")
        if not bundle_claims.claim_once(job_id, now_ms()):
            raise HTTPException(status_code=409, detail="Agent execution bundle was already claimed")

        audit_artifact = _find_job_artifact(repo, job, HASH_ONLY_AUDIT_ARTIFACT)
        try:
            if audit_artifact is None:
                audit_bytes = build_hash_only_audit(request, raw_request_digest(raw_request))
                audit_artifact = _save_artifact(
                    repo, files, new_id, now_ms, job, HASH_ONLY_AUDIT_ARTIFACT, "application/json", audit_bytes,
                )
            purge_private_artifact(repo, files, request_artifact)
        except Exception as exc:
            cleanup_error = f"{type(exc).__name__}:{exc}"[:240]
            # The one-time claim is deliberately irreversible. Best-effort privacy cleanup follows.
            try:
                if _find_job_artifact(repo, job, REQUEST_ARTIFACT) is not None:
                    purge_private_artifact(repo, files, request_artifact)
            except Exception:
                pass
            repo.update_job(
                job_id,
                "FAILED",
                f"[privacy] ONE-TIME BUNDLE CLAIM FAILED AFTER CONSUME: {cleanup_error}\n",
                "Agent bundle claim failed closed after irreversible consume",
                now_ms(),
            )
            raise HTTPException(status_code=500, detail="one-time Agent bundle privacy transition failed") from exc

        repo.update_job(
            job_id,
            job.get("state") or "RUNNING",
            "[privacy] one-time Agent bundle claimed; raw Cloud request purged before delivery\n",
            None,
            now_ms(),
        )
        return Response(
            content=raw_request,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

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
                return {
                    "status": "ALREADY_RECORDED",
                    "job_id": job_id,
                    "artifact": existing,
                    "raw_request_purged": _find_job_artifact(repo, job, REQUEST_ARTIFACT) is None,
                    "bundle_claimed": bundle_claims.is_claimed(job_id),
                }
            raise HTTPException(status_code=409, detail="different Agent execution evidence already recorded")

        if not bundle_claims.is_claimed(job_id):
            raise HTTPException(status_code=409, detail="Agent execution evidence arrived before one-time bundle claim")
        if _find_job_artifact(repo, job, REQUEST_ARTIFACT) is not None:
            raise HTTPException(status_code=409, detail="raw Agent request still retained after bundle claim")
        audit_artifact = _find_job_artifact(repo, job, HASH_ONLY_AUDIT_ARTIFACT)
        if audit_artifact is None:
            raise HTTPException(status_code=409, detail="Agent execution privacy audit missing")
        audit = _read_audit(files, audit_artifact)

        reasons = _failure_reasons(body, audit)
        artifact = _save_artifact(
            repo, files, new_id, now_ms, job, EVIDENCE_ARTIFACT, "application/json", evidence_bytes,
        )
        state = "COMPLETED" if not reasons else "FAILED"
        summary = "Device-signed one-time ACP task completed" if not reasons else f"Agent task blocked: {', '.join(reasons[:4])}"
        repo.update_job(
            job_id,
            state,
            f"[agent-exec] evidence received passed={not reasons} blockers={len(reasons)} raw_request_purged=true\n",
            summary,
            now_ms(),
        )
        return {
            "status": state,
            "job_id": job_id,
            "artifact": artifact,
            "privacy_audit": audit_artifact,
            "blockers": reasons,
            "raw_request_purged": True,
            "bundle_claimed": True,
            "privacy_cleanup_error": None,
        }

    return router
