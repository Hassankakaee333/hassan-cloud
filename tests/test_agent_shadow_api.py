from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hassan_cloud.agent_shadow_api as api_module  # noqa: E402
from hassan_cloud.agent_shadow import ShadowDispatchResult  # noqa: E402
from hassan_cloud.agent_shadow_api import build_agent_shadow_router  # noqa: E402


class FakeFiles:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def save(self, artifact_id: str, filename: str, data: bytes):
        path = f"{artifact_id}/{filename}"
        self.data[path] = data
        return path, hashlib.sha256(data).hexdigest(), len(data)

    def read(self, storage_path: str) -> bytes:
        return self.data[storage_path]


class FakeRepo:
    def __init__(self) -> None:
        self.projects = {"p1": {"id": "p1"}}
        self.jobs: dict[str, dict] = {}
        self.artifacts: list[dict] = []

    def get_project(self, project_id: str):
        return self.projects.get(project_id)

    def create_job(self, jid, project_id, conversation_id, goal, job_type, ts, idempotency_key=None):
        if idempotency_key:
            for job in self.jobs.values():
                if job.get("idempotency_key") == idempotency_key:
                    return dict(job)
        job = {
            "id": jid,
            "project_id": project_id,
            "conversation_id": conversation_id,
            "goal": goal,
            "job_type": job_type,
            "state": "QUEUED",
            "result_summary": None,
            "log": "",
            "idempotency_key": idempotency_key,
        }
        self.jobs[jid] = job
        return dict(job)

    def get_job(self, job_id: str):
        job = self.jobs.get(job_id)
        return dict(job) if job else None

    def update_job(self, job_id, state, log_append, summary, ts):
        job = self.jobs[job_id]
        job["state"] = state
        job["log"] += log_append
        if summary is not None:
            job["result_summary"] = summary

    def create_artifact(self, row: dict):
        self.artifacts.append(dict(row))
        return dict(row)

    def project_workspace(self, project_id: str):
        return {
            "project": self.projects.get(project_id),
            "artifacts": [a for a in self.artifacts if a.get("project_id") == project_id],
        }


def save_json_artifact(repo: FakeRepo, files: FakeFiles, job_id: str, artifact_id: str, name: str, payload: dict) -> None:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    storage_path, digest, size = files.save(artifact_id, name, raw)
    repo.create_artifact(
        {
            "id": artifact_id,
            "project_id": "p1",
            "job_id": job_id,
            "conversation_id": None,
            "name": name,
            "mime_type": "application/json",
            "size_bytes": size,
            "storage_path": storage_path,
            "sha256": digest,
            "created_at": 1,
        }
    )


def initialize_json() -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": 1,
                "agentInfo": {"name": "Sample Agent", "version": "1.2.3"},
                "agentCapabilities": {"session": {"list": True}},
            },
        },
        separators=(",", ":"),
    )


def session_json() -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "shadow-session-1"}},
        separators=(",", ":"),
    )


def seed_benchmark_gate(
    repo: FakeRepo,
    files: FakeFiles,
    *,
    state: str = "COMPLETED",
    passed: bool = True,
    command: str = "bin/agent",
) -> None:
    repo.jobs["benchmark-1"] = {
        "id": "benchmark-1",
        "project_id": "p1",
        "conversation_id": None,
        "goal": "benchmark",
        "job_type": "agent_acp_benchmark",
        "state": state,
        "result_summary": None,
        "log": "",
        "idempotency_key": None,
    }
    save_json_artifact(
        repo,
        files,
        "benchmark-1",
        "benchmark-request",
        "agent-benchmark-request.json",
        {
            "project_id": "p1",
            "conversation_id": None,
            "agent_id": "sample-agent",
            "static_evidence_id": "static-1",
            "security_verification_job_id": "security-1",
            "version": "1.2.3",
            "source_url": "https://example.com/agent.tar.gz",
            "expected_sha256": "a" * 64,
            "command": command,
            "args": ["--acp"],
            "protocol_version": 1,
            "idempotency_key": None,
        },
    )
    save_json_artifact(
        repo,
        files,
        "benchmark-1",
        "benchmark-evidence",
        "agent-acp-benchmark.json",
        {
            "schema_version": 1,
            "agent_id": "sample-agent",
            "evidence_id": "benchmark-run-1",
            "static_evidence_id": "static-1",
            "security_verification_job_id": "security-1",
            "version": "1.2.3",
            "source_url": "https://example.com/agent.tar.gz",
            "artifact_sha256": "a" * 64,
            "command": command,
            "args": ["--acp"],
            "artifact_executed": True,
            "secrets_used": False,
            "network_isolated": True,
            "filesystem_read_only": True,
            "containerized": True,
            "timeout_enforced": True,
            "archive_safe": True,
            "stdout_valid": True,
            "initialize_response_json": initialize_json(),
            "protocol_version": "1",
            "agent_name": "Sample Agent",
            "agent_version": "1.2.3",
            "handshake_ms": 50,
            "stderr_bytes": 0,
            "stderr_sha256": "",
            "passed": passed,
            "blockers": [] if passed else ["blocked"],
            "warnings": [],
        },
    )


def build_test_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    benchmark_state: str = "COMPLETED",
    benchmark_passed: bool = True,
    benchmark_command: str = "bin/agent",
):
    repo = FakeRepo()
    files = FakeFiles()
    seed_benchmark_gate(
        repo,
        files,
        state=benchmark_state,
        passed=benchmark_passed,
        command=benchmark_command,
    )
    ids = iter(["shadow-1", "shadow-request", "shadow-evidence", "spare"])

    def new_id():
        return next(ids)

    def now_ms():
        return 123456789

    def verify_token():
        return "device-ok"

    monkeypatch.setenv("HASSAN_CALLBACK_SECRET", "callback-secret")
    monkeypatch.setattr(
        api_module,
        "dispatch_agent_shadow",
        lambda job_id, spec: ShadowDispatchResult(
            status="QUEUED",
            job_id=job_id,
            repository="owner/repo",
            ref="candidate",
        ),
    )
    app = FastAPI()
    app.include_router(
        build_agent_shadow_router(
            repo=repo,
            files=files,
            verify_token=verify_token,
            new_id=new_id,
            now_ms=now_ms,
        )
    )
    return TestClient(app), repo, files


def shadow_request(**overrides) -> dict:
    base = {
        "project_id": "p1",
        "agent_id": "sample-agent",
        "static_evidence_id": "static-1",
        "security_verification_job_id": "security-1",
        "benchmark_job_id": "benchmark-1",
        "version": "1.2.3",
        "source_url": "https://example.com/agent.tar.gz",
        "expected_sha256": "a" * 64,
        "command": "bin/agent",
        "args": ["--acp"],
        "protocol_version": 1,
        "idempotency_key": "shadow:sample-agent:1",
    }
    base.update(overrides)
    return base


def shadow_evidence(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "agent_id": "sample-agent",
        "evidence_id": "shadow-run-1",
        "static_evidence_id": "static-1",
        "security_verification_job_id": "security-1",
        "benchmark_job_id": "benchmark-1",
        "version": "1.2.3",
        "source_url": "https://example.com/agent.tar.gz",
        "artifact_sha256": "a" * 64,
        "command": "bin/agent",
        "args": ["--acp"],
        "artifact_executed": True,
        "secrets_used": False,
        "auth_attempted": False,
        "prompt_sent": False,
        "permission_requests": 0,
        "tool_requests": 0,
        "network_isolated": True,
        "filesystem_read_only": True,
        "containerized": True,
        "timeout_enforced": True,
        "archive_safe": True,
        "initialize_response_json": initialize_json(),
        "protocol_version": "1",
        "agent_name": "Sample Agent",
        "agent_version": "1.2.3",
        "auth_methods_count": 0,
        "session_new_response_json": session_json(),
        "session_id": "shadow-session-1",
        "session_created": True,
        "shadow_ms": 60,
        "stderr_bytes": 0,
        "stderr_sha256": "",
        "passed": True,
        "blockers": [],
        "warnings": [],
    }
    base.update(overrides)
    return base


def test_create_requires_completed_passing_exact_benchmark(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = build_test_client(monkeypatch)
    created = client.post("/v1/agent-shadows", json=shadow_request())
    assert created.status_code == 200
    payload = created.json()
    assert payload["id"] == "shadow-1"
    assert payload["state"] == "RUNNING"
    assert payload["dispatch_status"] == "QUEUED"
    assert payload["shadow_tested_automatically"] is False

    client2, _, _ = build_test_client(monkeypatch, benchmark_state="RUNNING")
    assert client2.post("/v1/agent-shadows", json=shadow_request()).status_code == 409

    client3, _, _ = build_test_client(monkeypatch, benchmark_passed=False)
    assert client3.post("/v1/agent-shadows", json=shadow_request()).status_code == 409

    client4, _, _ = build_test_client(monkeypatch, benchmark_command="bin/other")
    assert client4.post("/v1/agent-shadows", json=shadow_request()).status_code == 409


def test_safe_callback_records_evidence_but_never_auto_shadow_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    client, repo, _ = build_test_client(monkeypatch)
    assert client.post("/v1/agent-shadows", json=shadow_request()).status_code == 200

    unauthorized = client.post(
        "/v1/internal/agent-shadows/shadow-1/evidence",
        json=shadow_evidence(),
    )
    assert unauthorized.status_code == 401

    accepted = client.post(
        "/v1/internal/agent-shadows/shadow-1/evidence",
        headers={"X-Hassan-Callback-Secret": "callback-secret"},
        json=shadow_evidence(),
    )
    assert accepted.status_code == 200
    payload = accepted.json()
    assert payload["status"] == "COMPLETED"
    assert payload["passed"] is True
    assert payload["shadow_tested_automatically"] is False
    assert repo.jobs["shadow-1"]["state"] == "COMPLETED"


@pytest.mark.parametrize(
    "override",
    [
        {"auth_attempted": True},
        {"prompt_sent": True},
        {"permission_requests": 1},
        {"tool_requests": 1},
        {"network_isolated": False},
        {"filesystem_read_only": False},
        {"secrets_used": True},
        {"session_created": False, "session_id": "", "session_new_response_json": ""},
    ],
)
def test_any_auth_prompt_permission_tool_or_isolation_violation_fails_shadow(
    monkeypatch: pytest.MonkeyPatch,
    override: dict,
) -> None:
    client, repo, _ = build_test_client(monkeypatch)
    assert client.post("/v1/agent-shadows", json=shadow_request()).status_code == 200
    response = client.post(
        "/v1/internal/agent-shadows/shadow-1/evidence",
        headers={"X-Hassan-Callback-Secret": "callback-secret"},
        json=shadow_evidence(**override),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "FAILED"
    assert response.json()["passed"] is False
    assert response.json()["shadow_tested_automatically"] is False
    assert repo.jobs["shadow-1"]["state"] == "FAILED"


def test_failed_runner_evidence_is_audited_not_promoted(monkeypatch: pytest.MonkeyPatch) -> None:
    client, repo, _ = build_test_client(monkeypatch)
    assert client.post("/v1/agent-shadows", json=shadow_request()).status_code == 200
    response = client.post(
        "/v1/internal/agent-shadows/shadow-1/evidence",
        headers={"X-Hassan-Callback-Secret": "callback-secret"},
        json=shadow_evidence(
            passed=False,
            session_created=False,
            session_id="",
            session_new_response_json="",
            blockers=["shadow_error:TimeoutError:timeout"],
        ),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "FAILED"
    assert response.json()["shadow_tested_automatically"] is False
    assert repo.jobs["shadow-1"]["state"] == "FAILED"
