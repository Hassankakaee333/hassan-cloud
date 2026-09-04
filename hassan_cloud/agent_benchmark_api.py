"""Zero-PC API for Frishta's manual post-security ACP sandbox benchmark.

A benchmark can be dispatched only after Hassan Cloud proves a prior no-exec Binary verification
job completed successfully for the same project, Agent, static evidence, version, source URL and
SHA-256. The benchmark result is evidence only; it never marks an Agent BENCHMARKED automatically.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .agent_benchmark import AgentBenchmarkSpec, dispatch_agent_benchmark
from .agent_verification_api import (
    EVIDENCE_ARTIFACT as SECURITY_EVIDENCE_ARTIFACT,
    JOB_TYPE as SECURITY_JOB_TYPE,
    REQUEST_ARTIFACT as SECURITY_REQUEST_ARTIFACT,
    _callback_authorized,
    _find_job_artifact,
    _save_artifact,
)

REQUEST_ARTIFACT = "agent-benchmark-request.json"
EVIDENCE_ARTIFACT = "agent-acp-benchmark.json"
JOB_TYPE = "agent_acp_benchmark"
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")


class AgentBenchmarkRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    static_evidence_id: str = Field(min_length=1, max_length=256)
    security_verification_job_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    source_url: str = Field(min_length=1, max_length=2048)
    expected_sha256: str = Field(min_length=64, max_length=64)
    command: str = Field(min_length=1, max_length=512)
    args: list[str] = Field(default_factory=list, max_length=24)
    protocol_version: int = Field(default=1, ge=1, le=65535)
    idempotency_key: str | None = Field(default=None, max_length=256)


class AgentBenchmarkEvidence(BaseModel):
    schema_version: int = 1
    agent_id: str = Field(min_length=1, max_length=128)
    evidence_id: str = Field(min_length=1, max_length=256)
    static_evidence_id: str = Field(min_length=1, max_length=256)
    security_verification_job_id: str = Field(min_length=1, max_length=128)
    github_run_id: str | int | None = None
    version: str = Field(min_length=1, max_length=128)
    source_url: str | None = Field(default=None, max_length=2048)
    artifact_sha256: str = Field(default="", max_length=64)
    command: str = Field(default="", max_length=512)
    args: list[str] = Field(default_factory=list, max_length=24)
    artifact_executed: bool
    secrets_used: bool
    network_isolated: bool
    filesystem_read_only: bool
    containerized: bool
    timeout_enforced: bool
    archive_safe: bool
    stdout_valid: bool
    initialize_response_json: str = Field(default="", max_length=524288)
    protocol_version: str = Field(default="", max_length=32)
    agent_name: str = Field(default="", max_length=256)
    agent_version: str = Field(default="", max_length=128)
    handshake_ms: int = Field(default=0, ge=0, le=120000)
    stderr_bytes: int = Field(default=0, ge=0)
    stderr_sha256: str = Field(default="", max_length=64)
    passed: bool
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _canonical_request(body: AgentBenchmarkRequest) -> bytes:
    return json.dumps(
        body.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _load_json_artifact(repo: Any, files: Any, job: dict[str, Any], name: str) -> dict[str, Any]:
    artifact = _find_job_artifact(repo, job, name)
    if not artifact or not artifact.get("storage_path"):
        raise HTTPException(status_code=409, detail=f"required artifact missing: {name}")
    try:
        raw = files.read(artifact["storage_path"])
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"artifact unreadable: {name}: {exc}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    stored_digest = str(artifact.get("sha256") or "").lower()
    if not stored_digest or not hmac.compare_digest(digest, stored_digest):
        raise HTTPException(status_code=409, detail=f"artifact storage digest mismatch: {name}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"artifact JSON invalid: {name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=409, detail=f"artifact JSON must be object: {name}")
    return payload


def _assert_prior_security_gate(
    repo: Any,
    files: Any,
    body: AgentBenchmarkRequest,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    job = repo.get_job(body.security_verification_job_id)
    if not job or job.get("job_type") != SECURITY_JOB_TYPE:
        raise HTTPException(status_code=409, detail="required Agent security verification job not found")
    if job.get("project_id") != body.project_id:
        raise HTTPException(status_code=409, detail="security verification project mismatch")
    if job.get("state") != "COMPLETED":
        raise HTTPException(status_code=409, detail="Agent security verification is not completed")

    request = _load_json_artifact(repo, files, job, SECURITY_REQUEST_ARTIFACT)
    evidence = _load_json_artifact(repo, files, job, SECURITY_EVIDENCE_ARTIFACT)
    expected_sha = body.expected_sha256.lower()
    bindings = {
        "agent_id": body.agent_id,
        "static_evidence_id": body.static_evidence_id,
        "version": body.version,
        "source_url": body.source_url,
    }
    for key, expected in bindings.items():
        if str(request.get(key) or "") != expected:
            raise HTTPException(status_code=409, detail=f"security verification request {key} mismatch")
    if str(request.get("distribution_kind") or "").lower() != "binary":
        raise HTTPException(status_code=409, detail="ACP benchmark requires prior Binary verification")
    if str(request.get("expected_sha256") or "").lower() != expected_sha:
        raise HTTPException(status_code=409, detail="security verification request SHA-256 mismatch")

    evidence_bindings = {
        "agent_id": body.agent_id,
        "static_evidence_id": body.static_evidence_id,
        "version": body.version,
    }
    for key, expected in evidence_bindings.items():
        if str(evidence.get(key) or "") != expected:
            raise HTTPException(status_code=409, detail=f"security verification evidence {key} mismatch")
    if str(evidence.get("distribution_kind") or "").lower() != "binary":
        raise HTTPException(status_code=409, detail="security evidence is not Binary")
    if str(evidence.get("artifact_sha256") or "").lower() != expected_sha:
        raise HTTPException(status_code=409, detail="security evidence SHA-256 mismatch")
    evidence_source = str(evidence.get("source_url") or "")
    if evidence_source and evidence_source != body.source_url:
        raise HTTPException(status_code=409, detail="security evidence source URL mismatch")
    if evidence.get("artifact_executed") is not False or evidence.get("secrets_used") is not False:
        raise HTTPException(status_code=409, detail="prior security evidence is not no-exec/no-secret")
    required_true = (
        "integrity_verified",
        "archive_safe",
        "dependency_lock_complete",
        "passed",
    )
    if any(evidence.get(key) is not True for key in required_true):
        raise HTTPException(status_code=409, detail="prior security evidence did not pass the complete Binary gate")
    if evidence.get("blockers"):
        raise HTTPException(status_code=409, detail="prior security evidence contains blockers")
    return job, request, evidence


def _parse_initialize_response(raw: str, expected_protocol_version: int, expected_agent_version: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"invalid ACP initialize JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=409, detail="ACP initialize response must be an object")
    if payload.get("jsonrpc") != "2.0" or payload.get("id") != 1:
        raise HTTPException(status_code=409, detail="ACP initialize JSON-RPC envelope mismatch")
    if payload.get("error") is not None:
        raise HTTPException(status_code=409, detail="ACP initialize response contains error")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise HTTPException(status_code=409, detail="ACP initialize result missing")
    if str(result.get("protocolVersion")) != str(expected_protocol_version):
        raise HTTPException(status_code=409, detail="ACP initialize protocol version mismatch")
    info = result.get("agentInfo") or result.get("info") or {}
    if isinstance(info, dict):
        returned_version = str(info.get("version") or "").strip()
        if returned_version and returned_version != expected_agent_version:
            raise HTTPException(status_code=409, detail="ACP initialize Agent version mismatch")
    return payload


def _evidence_failure_reasons(body: AgentBenchmarkEvidence, request: dict[str, Any]) -> list[str]:
    reasons = list(body.blockers)
    expected_sha = str(request.get("expected_sha256") or "").lower()
    if not _SHA256.fullmatch(body.artifact_sha256) or body.artifact_sha256.lower() != expected_sha:
        reasons.append("artifact_sha256_mismatch")
    if not body.artifact_executed:
        reasons.append("artifact_not_executed")
    if body.secrets_used:
        reasons.append("secrets_used")
    for key in (
        "network_isolated",
        "filesystem_read_only",
        "containerized",
        "timeout_enforced",
        "archive_safe",
        "stdout_valid",
    ):
        if getattr(body, key) is not True:
            reasons.append(f"{key}_false")
    if body.command != str(request.get("command") or ""):
        reasons.append("command_mismatch")
    if body.args != list(request.get("args") or []):
        reasons.append("args_mismatch")
    if str(body.protocol_version) != str(request.get("protocol_version")):
        reasons.append("protocol_version_mismatch")
    if body.agent_version and body.agent_version != str(request.get("version") or ""):
        reasons.append("agent_version_mismatch")
    return list(dict.fromkeys(reasons))


def build_agent_benchmark_router(
    *,
    repo: Any,
    files: Any,
    verify_token: Callable[..., Any],
    new_id: Callable[[], str],
    now_ms: Callable[[], int],
) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/agent-benchmarks")
    def create_agent_benchmark(
        body: AgentBenchmarkRequest,
        _token: str = Depends(verify_token),
    ) -> dict[str, Any]:
        if not repo.get_project(body.project_id):
            raise HTTPException(status_code=404, detail="project not found")
        _assert_prior_security_gate(repo, files, body)
        spec = AgentBenchmarkSpec(
            agent_id=body.agent_id,
            static_evidence_id=body.static_evidence_id,
            security_verification_job_id=body.security_verification_job_id,
            version=body.version,
            source_url=body.source_url,
            expected_sha256=body.expected_sha256,
            command=body.command,
            args=tuple(body.args),
            protocol_version=body.protocol_version,
        )
        try:
            spec.validate()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        canonical = _canonical_request(body)
        job = repo.create_job(
            new_id(),
            body.project_id,
            body.conversation_id,
            f"Benchmark ACP Agent: {body.agent_id}@{body.version}",
            JOB_TYPE,
            now_ms(),
            idempotency_key=body.idempotency_key,
        )
        if job.get("job_type") != JOB_TYPE:
            raise HTTPException(status_code=409, detail="idempotency key belongs to another job type")
        existing_request = _find_job_artifact(repo, job, REQUEST_ARTIFACT)
        if existing_request is None:
            request_artifact = _save_artifact(
                repo,
                files,
                new_id,
                now_ms,
                job,
                REQUEST_ARTIFACT,
                "application/json",
                canonical,
            )
        else:
            try:
                existing_bytes = files.read(existing_request["storage_path"])
            except Exception as exc:
                raise HTTPException(status_code=409, detail=f"existing benchmark request unreadable: {exc}") from exc
            if not hmac.compare_digest(existing_bytes, canonical):
                raise HTTPException(status_code=409, detail="idempotency key reused with different benchmark request")
            request_artifact = existing_request
            if job.get("state") != "QUEUED":
                return {**job, "dispatch_status": "EXISTING", "request_artifact": request_artifact}

        result = dispatch_agent_benchmark(job["id"], spec)
        if result.status == "QUEUED":
            repo.update_job(
                job["id"],
                "RUNNING",
                "[agent-benchmark] isolated ACP initialize benchmark dispatched\n",
                "Agent ACP sandbox benchmark dispatched",
                now_ms(),
            )
        else:
            repo.update_job(
                job["id"],
                "FAILED",
                f"[agent-benchmark] dispatch {result.status}: {result.detail}\n",
                f"Agent benchmark dispatch {result.status}: {result.detail}",
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
            "request_artifact": request_artifact,
            "benchmarked_automatically": False,
        }

    @router.get("/v1/agent-benchmarks/{job_id}")
    def get_agent_benchmark(
        job_id: str,
        _token: str = Depends(verify_token),
    ) -> dict[str, Any]:
        job = repo.get_job(job_id)
        if not job or job.get("job_type") != JOB_TYPE:
            raise HTTPException(status_code=404, detail="Agent benchmark not found")
        workspace = repo.project_workspace(job["project_id"])
        artifacts = [item for item in workspace.get("artifacts", []) if item.get("job_id") == job_id]
        return {**job, "artifacts": artifacts, "benchmarked_automatically": False}

    @router.post("/v1/internal/agent-benchmarks/{job_id}/evidence")
    def publish_agent_benchmark_evidence(
        job_id: str,
        body: AgentBenchmarkEvidence,
        x_hassan_callback_secret: str | None = Header(default=None, alias="X-Hassan-Callback-Secret"),
    ) -> dict[str, Any]:
        _callback_authorized(x_hassan_callback_secret)
        job = repo.get_job(job_id)
        if not job or job.get("job_type") != JOB_TYPE:
            raise HTTPException(status_code=404, detail="Agent benchmark not found")
        if job.get("state") == "CANCELLED":
            raise HTTPException(status_code=409, detail="cancelled benchmark cannot accept evidence")
        request = _load_json_artifact(repo, files, job, REQUEST_ARTIFACT)

        exact_bindings = {
            "agent_id": body.agent_id,
            "static_evidence_id": body.static_evidence_id,
            "security_verification_job_id": body.security_verification_job_id,
            "version": body.version,
        }
        for key, actual in exact_bindings.items():
            if str(request.get(key) or "") != actual:
                raise HTTPException(status_code=409, detail=f"benchmark evidence {key} mismatch")
        if body.source_url and body.source_url != str(request.get("source_url") or ""):
            raise HTTPException(status_code=409, detail="benchmark evidence source URL mismatch")

        failure_reasons = _evidence_failure_reasons(body, request)
        if body.stdout_valid or body.passed:
            _parse_initialize_response(
                body.initialize_response_json,
                int(request.get("protocol_version") or 0),
                str(request.get("version") or ""),
            )
        if body.passed and failure_reasons:
            raise HTTPException(
                status_code=409,
                detail=f"passed benchmark evidence is inconsistent: {failure_reasons[:4]}",
            )

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
                    "passed": body.passed,
                    "benchmarked_automatically": False,
                }
            raise HTTPException(status_code=409, detail="different benchmark evidence already recorded for job")

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
        passed = body.passed and not failure_reasons
        state = "COMPLETED" if passed else "FAILED"
        summary = (
            "Agent ACP sandbox benchmark passed; evidence ready for Frishta Evaluation Lab"
            if passed
            else f"Agent ACP sandbox benchmark failed: {', '.join(failure_reasons[:3] or ['runner_reported_failure'])}"
        )
        repo.update_job(
            job_id,
            state,
            f"[agent-benchmark] evidence received passed={passed} blockers={len(failure_reasons)}\n",
            summary,
            now_ms(),
        )
        return {
            "status": state,
            "job_id": job_id,
            "artifact": artifact,
            "passed": passed,
            "benchmarked_automatically": False,
        }

    return router
