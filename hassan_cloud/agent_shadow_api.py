"""Zero-PC API for Frishta's manual post-benchmark ACP shadow session test.

A shadow session can be dispatched only after Hassan Cloud proves the prior P2.3 benchmark completed
for the exact same Binary binding. Evidence is audit-only; Cloud never marks an Agent SHADOW_TESTED.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .agent_benchmark_api import (
    EVIDENCE_ARTIFACT as BENCHMARK_EVIDENCE_ARTIFACT,
    JOB_TYPE as BENCHMARK_JOB_TYPE,
    REQUEST_ARTIFACT as BENCHMARK_REQUEST_ARTIFACT,
    _load_json_artifact,
)
from .agent_shadow import AgentShadowSpec, dispatch_agent_shadow
from .agent_verification_api import _callback_authorized, _find_job_artifact, _save_artifact

REQUEST_ARTIFACT = "agent-shadow-request.json"
EVIDENCE_ARTIFACT = "agent-acp-shadow.json"
JOB_TYPE = "agent_acp_shadow"
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")


class AgentShadowRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    static_evidence_id: str = Field(min_length=1, max_length=256)
    security_verification_job_id: str = Field(min_length=1, max_length=128)
    benchmark_job_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    source_url: str = Field(min_length=1, max_length=2048)
    expected_sha256: str = Field(min_length=64, max_length=64)
    command: str = Field(min_length=1, max_length=512)
    args: list[str] = Field(default_factory=list, max_length=24)
    protocol_version: int = Field(default=1, ge=1, le=65535)
    idempotency_key: str | None = Field(default=None, max_length=256)


class AgentShadowEvidence(BaseModel):
    schema_version: int = 1
    agent_id: str = Field(min_length=1, max_length=128)
    evidence_id: str = Field(min_length=1, max_length=256)
    static_evidence_id: str = Field(min_length=1, max_length=256)
    security_verification_job_id: str = Field(min_length=1, max_length=128)
    benchmark_job_id: str = Field(min_length=1, max_length=128)
    github_run_id: str | int | None = None
    version: str = Field(min_length=1, max_length=128)
    source_url: str = Field(default="", max_length=2048)
    artifact_sha256: str = Field(default="", max_length=64)
    command: str = Field(default="", max_length=512)
    args: list[str] = Field(default_factory=list, max_length=24)
    artifact_executed: bool
    secrets_used: bool
    auth_attempted: bool
    prompt_sent: bool
    permission_requests: int = Field(default=0, ge=0, le=1000)
    tool_requests: int = Field(default=0, ge=0, le=1000)
    network_isolated: bool
    filesystem_read_only: bool
    containerized: bool
    timeout_enforced: bool
    archive_safe: bool
    initialize_response_json: str = Field(default="", max_length=524288)
    protocol_version: str = Field(default="", max_length=32)
    agent_name: str = Field(default="", max_length=256)
    agent_version: str = Field(default="", max_length=128)
    auth_methods_count: int = Field(default=0, ge=0, le=1000)
    session_new_response_json: str = Field(default="", max_length=524288)
    session_id: str = Field(default="", max_length=512)
    session_created: bool
    shadow_ms: int = Field(default=0, ge=0, le=120000)
    stderr_bytes: int = Field(default=0, ge=0)
    stderr_sha256: str = Field(default="", max_length=64)
    passed: bool
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _canonical_request(body: AgentShadowRequest) -> bytes:
    return json.dumps(
        body.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _assert_prior_benchmark_gate(repo: Any, files: Any, body: AgentShadowRequest) -> None:
    job = repo.get_job(body.benchmark_job_id)
    if not job or job.get("job_type") != BENCHMARK_JOB_TYPE:
        raise HTTPException(status_code=409, detail="required Agent benchmark job not found")
    if job.get("project_id") != body.project_id:
        raise HTTPException(status_code=409, detail="benchmark project mismatch")
    if job.get("state") != "COMPLETED":
        raise HTTPException(status_code=409, detail="Agent benchmark is not completed")

    request = _load_json_artifact(repo, files, job, BENCHMARK_REQUEST_ARTIFACT)
    evidence = _load_json_artifact(repo, files, job, BENCHMARK_EVIDENCE_ARTIFACT)
    exact = {
        "agent_id": body.agent_id,
        "static_evidence_id": body.static_evidence_id,
        "security_verification_job_id": body.security_verification_job_id,
        "version": body.version,
        "source_url": body.source_url,
        "command": body.command,
    }
    for key, expected in exact.items():
        if str(request.get(key) or "") != expected:
            raise HTTPException(status_code=409, detail=f"benchmark request {key} mismatch")
        if str(evidence.get(key) or "") != expected:
            raise HTTPException(status_code=409, detail=f"benchmark evidence {key} mismatch")
    expected_sha = body.expected_sha256.lower()
    if str(request.get("expected_sha256") or "").lower() != expected_sha:
        raise HTTPException(status_code=409, detail="benchmark request SHA-256 mismatch")
    if str(evidence.get("artifact_sha256") or "").lower() != expected_sha:
        raise HTTPException(status_code=409, detail="benchmark evidence SHA-256 mismatch")
    if list(request.get("args") or []) != body.args or list(evidence.get("args") or []) != body.args:
        raise HTTPException(status_code=409, detail="benchmark args mismatch")
    if str(request.get("protocol_version")) != str(body.protocol_version):
        raise HTTPException(status_code=409, detail="benchmark request protocol version mismatch")
    if str(evidence.get("protocol_version")) != str(body.protocol_version):
        raise HTTPException(status_code=409, detail="benchmark evidence protocol version mismatch")

    if evidence.get("artifact_executed") is not True or evidence.get("secrets_used") is not False:
        raise HTTPException(status_code=409, detail="prior benchmark execution/secrets evidence invalid")
    for key in (
        "network_isolated",
        "filesystem_read_only",
        "containerized",
        "timeout_enforced",
        "archive_safe",
        "stdout_valid",
        "passed",
    ):
        if evidence.get(key) is not True:
            raise HTTPException(status_code=409, detail=f"prior benchmark gate missing: {key}")
    if evidence.get("blockers"):
        raise HTTPException(status_code=409, detail="prior benchmark contains blockers")
    if not str(evidence.get("initialize_response_json") or ""):
        raise HTTPException(status_code=409, detail="prior benchmark initialize evidence missing")


def _shadow_failure_reasons(body: AgentShadowEvidence, request: dict[str, Any]) -> list[str]:
    reasons = list(body.blockers)
    exact = {
        "agent_id": body.agent_id,
        "static_evidence_id": body.static_evidence_id,
        "security_verification_job_id": body.security_verification_job_id,
        "benchmark_job_id": body.benchmark_job_id,
        "version": body.version,
        "source_url": body.source_url,
        "command": body.command,
    }
    for key, actual in exact.items():
        if str(request.get(key) or "") != str(actual):
            reasons.append(f"{key}_mismatch")
    if body.args != list(request.get("args") or []):
        reasons.append("args_mismatch")
    expected_sha = str(request.get("expected_sha256") or "").lower()
    if not _SHA256.fullmatch(body.artifact_sha256) or body.artifact_sha256.lower() != expected_sha:
        reasons.append("artifact_sha256_mismatch")
    if str(body.protocol_version) != str(request.get("protocol_version")):
        reasons.append("protocol_version_mismatch")
    if not body.artifact_executed:
        reasons.append("artifact_not_executed")
    if body.secrets_used:
        reasons.append("secrets_used")
    if body.auth_attempted:
        reasons.append("auth_attempted")
    if body.prompt_sent:
        reasons.append("prompt_sent")
    if body.permission_requests != 0:
        reasons.append("permission_requests_nonzero")
    if body.tool_requests != 0:
        reasons.append("tool_requests_nonzero")
    for key in (
        "network_isolated",
        "filesystem_read_only",
        "containerized",
        "timeout_enforced",
        "archive_safe",
        "session_created",
    ):
        if getattr(body, key) is not True:
            reasons.append(f"{key}_false")
    if not body.initialize_response_json:
        reasons.append("initialize_response_missing")
    if not body.session_new_response_json or not body.session_id:
        reasons.append("session_new_evidence_missing")
    if body.agent_version and body.agent_version != str(request.get("version") or ""):
        reasons.append("agent_version_mismatch")
    if body.stderr_sha256 and not _SHA256.fullmatch(body.stderr_sha256):
        reasons.append("stderr_sha256_invalid")
    if not body.passed:
        reasons.append("runner_passed_false")
    return sorted(set(reasons))


def build_agent_shadow_router(
    *,
    repo: Any,
    files: Any,
    verify_token: Callable[..., Any],
    new_id: Callable[[], str],
    now_ms: Callable[[], int],
) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/agent-shadows")
    def create_agent_shadow(body: AgentShadowRequest, _token: str = Depends(verify_token)) -> dict[str, Any]:
        if not repo.get_project(body.project_id):
            raise HTTPException(status_code=404, detail="project not found")
        try:
            spec = AgentShadowSpec(
                agent_id=body.agent_id,
                static_evidence_id=body.static_evidence_id,
                security_verification_job_id=body.security_verification_job_id,
                benchmark_job_id=body.benchmark_job_id,
                version=body.version,
                source_url=body.source_url,
                expected_sha256=body.expected_sha256,
                command=body.command,
                args=tuple(body.args),
                protocol_version=body.protocol_version,
            )
            spec.validate()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        _assert_prior_benchmark_gate(repo, files, body)
        canonical = _canonical_request(body)
        job = repo.create_job(
            new_id(),
            body.project_id,
            body.conversation_id,
            f"Shadow-test ACP session: {body.agent_id}@{body.version}",
            JOB_TYPE,
            now_ms(),
            idempotency_key=body.idempotency_key,
        )
        if job.get("job_type") != JOB_TYPE:
            raise HTTPException(status_code=409, detail="idempotency key belongs to another job type")
        existing_request = _find_job_artifact(repo, job, REQUEST_ARTIFACT)
        if existing_request is None:
            _save_artifact(repo, files, new_id, now_ms, job, REQUEST_ARTIFACT, "application/json", canonical)
        else:
            existing_bytes = files.read(existing_request["storage_path"])
            if not hmac.compare_digest(existing_bytes, canonical):
                raise HTTPException(status_code=409, detail="idempotency key reused with different shadow request")
            if job.get("state") != "QUEUED":
                return {
                    **job,
                    "dispatch_status": "EXISTING",
                    "request_artifact": existing_request,
                    "shadow_tested_automatically": False,
                }

        result = dispatch_agent_shadow(job["id"], spec)
        if result.status == "QUEUED":
            repo.update_job(
                job["id"],
                "RUNNING",
                "[agent-shadow] isolated no-prompt session test dispatched\n",
                "Agent ACP shadow session dispatched",
                now_ms(),
            )
        else:
            repo.update_job(
                job["id"],
                "FAILED",
                f"[agent-shadow] dispatch {result.status}: {result.detail}\n",
                f"Agent shadow dispatch {result.status}: {result.detail}",
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
            "shadow_tested_automatically": False,
        }

    @router.get("/v1/agent-shadows/{job_id}")
    def get_agent_shadow(job_id: str, _token: str = Depends(verify_token)) -> dict[str, Any]:
        job = repo.get_job(job_id)
        if not job or job.get("job_type") != JOB_TYPE:
            raise HTTPException(status_code=404, detail="Agent shadow job not found")
        artifacts = [
            item for item in repo.project_workspace(job["project_id"]).get("artifacts", [])
            if item.get("job_id") == job_id
        ]
        return {**job, "artifacts": artifacts, "shadow_tested_automatically": False}

    @router.post("/v1/internal/agent-shadows/{job_id}/evidence")
    def publish_agent_shadow_evidence(
        job_id: str,
        body: AgentShadowEvidence,
        x_hassan_callback_secret: str | None = Header(default=None, alias="X-Hassan-Callback-Secret"),
    ) -> dict[str, Any]:
        _callback_authorized(x_hassan_callback_secret)
        job = repo.get_job(job_id)
        if not job or job.get("job_type") != JOB_TYPE:
            raise HTTPException(status_code=404, detail="Agent shadow job not found")
        if job.get("state") == "CANCELLED":
            raise HTTPException(status_code=409, detail="cancelled shadow job cannot accept evidence")
        request = _load_json_artifact(repo, files, job, REQUEST_ARTIFACT)
        reasons = _shadow_failure_reasons(body, request)
        evidence_bytes = json.dumps(
            body.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        evidence_digest = hashlib.sha256(evidence_bytes).hexdigest()
        existing = _find_job_artifact(repo, job, EVIDENCE_ARTIFACT)
        if existing:
            if existing.get("sha256") == evidence_digest:
                return {
                    "status": "ALREADY_RECORDED",
                    "job_id": job_id,
                    "artifact": existing,
                    "shadow_tested_automatically": False,
                }
            raise HTTPException(status_code=409, detail="different shadow evidence already recorded for job")

        artifact = _save_artifact(
            repo, files, new_id, now_ms, job, EVIDENCE_ARTIFACT, "application/json", evidence_bytes,
        )
        state = "COMPLETED" if not reasons else "FAILED"
        summary = (
            "ACP shadow session passed; evidence ready for Frishta Evaluation Lab"
            if not reasons
            else f"ACP shadow session blocked: {', '.join(reasons[:4])}"
        )
        repo.update_job(
            job_id,
            state,
            f"[agent-shadow] evidence received passed={not reasons} blockers={len(reasons)}\n",
            summary,
            now_ms(),
        )
        return {
            "status": state,
            "job_id": job_id,
            "artifact": artifact,
            "passed": not reasons,
            "shadow_tested_automatically": False,
        }

    return router
