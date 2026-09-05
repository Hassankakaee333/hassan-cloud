"""One-time, Hassan-approved ACP task execution API.

Cloud re-validates the permit structure and exact prior Shadow-tested Binary binding. Only READ_FILES
is accepted in P2.9. Prompt/file contents stay in the private request artifact and are fetched by a
trusted prepare job; the untrusted Agent container receives no Hassan/API/GitHub secrets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field

from .agent_benchmark_api import _load_json_artifact
from .agent_executor import dispatch_agent_execution
from .agent_shadow_api import (
    EVIDENCE_ARTIFACT as SHADOW_EVIDENCE_ARTIFACT,
    JOB_TYPE as SHADOW_JOB_TYPE,
    REQUEST_ARTIFACT as SHADOW_REQUEST_ARTIFACT,
)
from .agent_verification_api import _callback_authorized, _find_job_artifact, _save_artifact

REQUEST_ARTIFACT = "agent-task-execution-request.json"
EVIDENCE_ARTIFACT = "agent-acp-task-execution.json"
JOB_TYPE = "agent_acp_task"
PERMIT_POLICY_ID = "frishta-agent-task-permit-v3"
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_FILE_BYTES = 1024 * 1024
MAX_UPDATES_BYTES = 256 * 1024
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_OPAQUE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class AgentExecutionFile(BaseModel):
    path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(min_length=64, max_length=64)
    content_base64: str = Field(default="", max_length=400000)


class AgentExecutionRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, max_length=128)
    permit_id: str = Field(min_length=32, max_length=160)
    execution_request_id: str = Field(min_length=16, max_length=128)
    approval_nonce: str = Field(min_length=16, max_length=128)
    approval_evidence_id: str = Field(min_length=1, max_length=256)
    comparison_evidence_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=8000)
    goal_sha256: str = Field(min_length=64, max_length=64)
    files: list[AgentExecutionFile] = Field(default_factory=list, max_length=64)
    actions: list[str] = Field(min_length=1, max_length=16)
    targets_stable_directly: bool = False
    cost_class: str = Field(default="FREE", max_length=32)
    additional_spend_cents: int = Field(default=0, ge=0, le=1_000_000)

    agent_id: str = Field(min_length=1, max_length=128)
    static_evidence_id: str = Field(min_length=1, max_length=256)
    security_verification_job_id: str = Field(min_length=1, max_length=128)
    benchmark_job_id: str = Field(min_length=1, max_length=128)
    shadow_job_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    source_url: str = Field(min_length=1, max_length=2048)
    expected_sha256: str = Field(min_length=64, max_length=64)
    command: str = Field(min_length=1, max_length=512)
    args: list[str] = Field(default_factory=list, max_length=24)
    protocol_version: int = Field(default=1, ge=1, le=65535)


class AgentExecutionEvidence(BaseModel):
    schema_version: int = 1
    permit_id: str = Field(min_length=1, max_length=160)
    execution_request_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    goal_sha256: str = Field(min_length=64, max_length=64)
    approval_evidence_id: str = Field(min_length=1, max_length=256)
    comparison_evidence_id: str = Field(min_length=1, max_length=256)
    static_evidence_id: str = Field(min_length=1, max_length=256)
    security_verification_job_id: str = Field(min_length=1, max_length=128)
    benchmark_job_id: str = Field(min_length=1, max_length=128)
    shadow_job_id: str = Field(min_length=1, max_length=128)
    source_url: str = Field(default="", max_length=2048)
    artifact_sha256: str = Field(default="", max_length=64)
    command: str = Field(default="", max_length=512)
    args: list[str] = Field(default_factory=list, max_length=24)
    actions: list[str] = Field(default_factory=list, max_length=16)
    permit_verified: bool
    files_verified: bool
    file_count: int = Field(default=0, ge=0, le=64)
    artifact_executed: bool
    secrets_used: bool
    auth_attempted: bool
    prompt_sent: bool
    agent_client_requests: int = Field(default=0, ge=0, le=1000)
    network_isolated: bool
    filesystem_read_only: bool
    containerized: bool
    timeout_enforced: bool
    archive_safe: bool
    session_created: bool
    session_id: str = Field(default="", max_length=512)
    prompt_response_json: str = Field(default="", max_length=131072)
    stop_reason: str = Field(default="", max_length=128)
    session_updates_jsonl: str = Field(default="", max_length=MAX_UPDATES_BYTES)
    updates_count: int = Field(default=0, ge=0, le=512)
    updates_sha256: str = Field(default="", max_length=64)
    execution_ms: int = Field(default=0, ge=0, le=300000)
    stderr_bytes: int = Field(default=0, ge=0)
    stderr_sha256: str = Field(default="", max_length=64)
    passed: bool
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    github_run_id: str | int | None = None


def _field(name: str, value: str) -> str:
    return f"{name}:{len(value.encode('utf-8'))}:{value}\n"


def _canonical_path(path: str) -> str:
    if not path or len(path) > 512 or any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        raise ValueError("unsafe file path")
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        raise ValueError("file path must be relative")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("file path escapes workspace")
    return "/".join(parts)


def _decoded_files(body: AgentExecutionRequest) -> list[tuple[str, str, bytes]]:
    seen: set[str] = set()
    total = 0
    result: list[tuple[str, str, bytes]] = []
    for item in body.files:
        path = _canonical_path(item.path)
        if path in seen:
            raise ValueError("duplicate file path")
        seen.add(path)
        digest = item.sha256.lower()
        if not _SHA256.fullmatch(digest):
            raise ValueError("invalid file SHA-256")
        try:
            data = base64.b64decode(item.content_base64, validate=True)
        except Exception as exc:
            raise ValueError("invalid file base64") from exc
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("file exceeds P2.9 size limit")
        total += len(data)
        if total > MAX_TOTAL_FILE_BYTES:
            raise ValueError("total file payload exceeds P2.9 limit")
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("file content SHA-256 mismatch")
        result.append((path, digest, data))
    return sorted(result, key=lambda item: item[0])


def _expected_permit_id(body: AgentExecutionRequest, files: list[tuple[str, str, bytes]]) -> str:
    canonical = "".join((
        _field("policy", PERMIT_POLICY_ID),
        _field("approval_nonce", body.approval_nonce),
        _field("agent", body.agent_id),
        _field("version", body.version),
        _field("task", body.task_id),
        _field("goal_sha256", body.goal_sha256.lower()),
        _field("approval", body.approval_evidence_id),
        _field("comparison", body.comparison_evidence_id),
    ))
    for path, digest, _data in files:
        canonical += _field("file", path) + _field("file_sha256", digest)
    for action in sorted(set(body.actions)):
        canonical += _field("action", action)
    return "agent-task-permit-sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_request(body: AgentExecutionRequest) -> list[tuple[str, str, bytes]]:
    if body.protocol_version != 1:
        raise HTTPException(status_code=400, detail="P2.9 executor supports ACP v1 only")
    if body.targets_stable_directly:
        raise HTTPException(status_code=400, detail="Stable-direct Agent task is forbidden")
    if body.cost_class.upper() != "FREE" or body.additional_spend_cents != 0:
        raise HTTPException(status_code=400, detail="paid Agent execution is forbidden")
    if sorted(set(body.actions)) != ["READ_FILES"]:
        raise HTTPException(status_code=400, detail="P2.9 executor permits READ_FILES only")
    if not _OPAQUE.fullmatch(body.execution_request_id) or not _OPAQUE.fullmatch(body.approval_nonce):
        raise HTTPException(status_code=400, detail="invalid one-time execution/approval id")
    if not _SHA256.fullmatch(body.goal_sha256.lower()):
        raise HTTPException(status_code=400, detail="invalid goal SHA-256")
    if hashlib.sha256(body.goal.encode("utf-8")).hexdigest() != body.goal_sha256.lower():
        raise HTTPException(status_code=400, detail="goal SHA-256 mismatch")
    try:
        decoded = _decoded_files(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    expected = _expected_permit_id(body, decoded)
    if not hmac.compare_digest(expected, body.permit_id):
        raise HTTPException(status_code=400, detail="permit id does not match approved scope")
    return decoded


def _assert_prior_shadow_gate(repo: Any, files: Any, body: AgentExecutionRequest) -> None:
    job = repo.get_job(body.shadow_job_id)
    if not job or job.get("job_type") != SHADOW_JOB_TYPE:
        raise HTTPException(status_code=409, detail="required Agent shadow job not found")
    if job.get("project_id") != body.project_id or job.get("state") != "COMPLETED":
        raise HTTPException(status_code=409, detail="Agent shadow job is not completed for this project")
    request = _load_json_artifact(repo, files, job, SHADOW_REQUEST_ARTIFACT)
    evidence = _load_json_artifact(repo, files, job, SHADOW_EVIDENCE_ARTIFACT)
    exact = {
        "agent_id": body.agent_id,
        "static_evidence_id": body.static_evidence_id,
        "security_verification_job_id": body.security_verification_job_id,
        "benchmark_job_id": body.benchmark_job_id,
        "version": body.version,
        "source_url": body.source_url,
        "command": body.command,
    }
    for key, expected in exact.items():
        if str(request.get(key) or "") != expected or str(evidence.get(key) or "") != expected:
            raise HTTPException(status_code=409, detail=f"shadow binding mismatch: {key}")
    if list(request.get("args") or []) != body.args or list(evidence.get("args") or []) != body.args:
        raise HTTPException(status_code=409, detail="shadow args mismatch")
    expected_sha = body.expected_sha256.lower()
    if str(request.get("expected_sha256") or "").lower() != expected_sha:
        raise HTTPException(status_code=409, detail="shadow request SHA mismatch")
    if str(evidence.get("artifact_sha256") or "").lower() != expected_sha:
        raise HTTPException(status_code=409, detail="shadow evidence SHA mismatch")
    if str(evidence.get("protocol_version") or "") != "1":
        raise HTTPException(status_code=409, detail="shadow protocol is not ACP v1")
    if evidence.get("passed") is not True or evidence.get("blockers"):
        raise HTTPException(status_code=409, detail="prior shadow evidence did not pass")
    for key, expected in (
        ("artifact_executed", True),
        ("secrets_used", False),
        ("auth_attempted", False),
        ("prompt_sent", False),
        ("network_isolated", True),
        ("filesystem_read_only", True),
        ("containerized", True),
        ("timeout_enforced", True),
        ("archive_safe", True),
        ("session_created", True),
    ):
        if evidence.get(key) is not expected:
            raise HTTPException(status_code=409, detail=f"prior shadow gate invalid: {key}")
    if int(evidence.get("permission_requests") or 0) != 0 or int(evidence.get("tool_requests") or 0) != 0:
        raise HTTPException(status_code=409, detail="prior shadow requested permissions/tools")
    if int(evidence.get("auth_methods_count") or 0) != 0:
        raise HTTPException(status_code=409, detail="P2.9 execution forbids Agents advertising auth methods")


def _canonical_request(body: AgentExecutionRequest) -> bytes:
    return json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")


def _failure_reasons(body: AgentExecutionEvidence, request: dict[str, Any]) -> list[str]:
    reasons = list(body.blockers)
    for key in (
        "permit_id", "execution_request_id", "agent_id", "version", "task_id", "goal_sha256",
        "approval_evidence_id", "comparison_evidence_id", "static_evidence_id",
        "security_verification_job_id", "benchmark_job_id", "shadow_job_id", "source_url", "command",
    ):
        if str(request.get(key) or "") != str(getattr(body, key)):
            reasons.append(f"{key}_mismatch")
    if list(request.get("args") or []) != body.args:
        reasons.append("args_mismatch")
    if sorted(set(request.get("actions") or [])) != sorted(set(body.actions)):
        reasons.append("actions_mismatch")
    if body.artifact_sha256.lower() != str(request.get("expected_sha256") or "").lower():
        reasons.append("artifact_sha256_mismatch")
    for key in (
        "permit_verified", "files_verified", "artifact_executed", "prompt_sent", "network_isolated",
        "filesystem_read_only", "containerized", "timeout_enforced", "archive_safe", "session_created",
    ):
        if getattr(body, key) is not True:
            reasons.append(f"{key}_false")
    if body.secrets_used:
        reasons.append("secrets_used")
    if body.auth_attempted:
        reasons.append("auth_attempted")
    if body.agent_client_requests != 0:
        reasons.append("agent_client_requests_nonzero")
    if not body.prompt_response_json or not body.stop_reason:
        reasons.append("prompt_response_missing")
    if body.updates_sha256 and not _SHA256.fullmatch(body.updates_sha256):
        reasons.append("updates_sha256_invalid")
    if body.stderr_sha256 and not _SHA256.fullmatch(body.stderr_sha256):
        reasons.append("stderr_sha256_invalid")
    if not body.passed:
        reasons.append("runner_passed_false")
    return sorted(set(reasons))


def build_agent_execution_router(
    *, repo: Any, files: Any, verify_token: Callable[..., Any], new_id: Callable[[], str], now_ms: Callable[[], int],
) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/agent-executions")
    def create_agent_execution(body: AgentExecutionRequest, _token: str = Depends(verify_token)) -> dict[str, Any]:
        if not repo.get_project(body.project_id):
            raise HTTPException(status_code=404, detail="project not found")
        _validate_request(body)
        _assert_prior_shadow_gate(repo, files, body)
        canonical = _canonical_request(body)
        requested_job_id = new_id()
        job = repo.create_job(
            requested_job_id,
            body.project_id,
            body.conversation_id,
            f"One-time ACP task: {body.agent_id}@{body.version} / {body.task_id}",
            JOB_TYPE,
            now_ms(),
            idempotency_key=f"agent-permit:{body.permit_id}",
        )
        if job.get("job_type") != JOB_TYPE:
            raise HTTPException(status_code=409, detail="permit id belongs to another job type")

        existing_request = _find_job_artifact(repo, job, REQUEST_ARTIFACT)
        if job.get("id") != requested_job_id:
            if existing_request is None:
                raise HTTPException(status_code=409, detail="existing permit claim is incomplete")
            existing_bytes = files.read(existing_request["storage_path"])
            if not hmac.compare_digest(existing_bytes, canonical):
                raise HTTPException(status_code=409, detail="permit reused with a different execution request")
            return {**job, "dispatch_status": "EXISTING", "request_artifact": existing_request}

        _save_artifact(repo, files, new_id, now_ms, job, REQUEST_ARTIFACT, "application/json", canonical)
        result = dispatch_agent_execution(job["id"])
        if result.status == "QUEUED":
            repo.update_job(job["id"], "RUNNING", "[agent-exec] one-time permit dispatched\n", "Agent task dispatched", now_ms())
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
        }

    @router.get("/v1/agent-executions/{job_id}")
    def get_agent_execution(job_id: str, _token: str = Depends(verify_token)) -> dict[str, Any]:
        job = repo.get_job(job_id)
        if not job or job.get("job_type") != JOB_TYPE:
            raise HTTPException(status_code=404, detail="Agent execution job not found")
        artifacts = [item for item in repo.project_workspace(job["project_id"]).get("artifacts", []) if item.get("job_id") == job_id]
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
        request = _load_json_artifact(repo, files, job, REQUEST_ARTIFACT)
        reasons = _failure_reasons(body, request)
        evidence_bytes = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(evidence_bytes).hexdigest()
        existing = _find_job_artifact(repo, job, EVIDENCE_ARTIFACT)
        if existing:
            if existing.get("sha256") == digest:
                return {"status": "ALREADY_RECORDED", "job_id": job_id, "artifact": existing}
            raise HTTPException(status_code=409, detail="different Agent execution evidence already recorded")
        artifact = _save_artifact(repo, files, new_id, now_ms, job, EVIDENCE_ARTIFACT, "application/json", evidence_bytes)
        state = "COMPLETED" if not reasons else "FAILED"
        summary = "One-time ACP task completed" if not reasons else f"Agent task blocked: {', '.join(reasons[:4])}"
        repo.update_job(
            job_id, state,
            f"[agent-exec] evidence received passed={not reasons} blockers={len(reasons)}\n",
            summary, now_ms(),
        )
        return {"status": state, "job_id": job_id, "artifact": artifact, "blockers": reasons}

    return router
