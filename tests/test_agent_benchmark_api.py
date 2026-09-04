from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hassan_cloud.agent_benchmark_api as api_module  # noqa: E402
from hassan_cloud.agent_benchmark import BenchmarkDispatchResult  # noqa: E402
from hassan_cloud.agent_benchmark_api import build_agent_benchmark_router  # noqa: E402


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


def seed_security_gate(repo: FakeRepo, files: FakeFiles, *, state: str = "COMPLETED", passed: bool = True) -> None:
    repo.jobs["security-1"] = {
        "id": "security-1",
        "project_id": "p1",
        "conversation_id": None,
        "goal": "verify",
        "job_type": "agent_artifact_verify",
        "state": state,
        "result_summary": None,
        "log": "",
        "idempotency_key": None,
    }
    save_json_artifact(
        repo,
        files,
        "security-1",
        "security-request",
        "agent-verification-request.json",
        {
            "project_id": "p1",
            "conversation_id": None,
            "agent_id": "sample-agent",
            "static_evidence_id": "static-1",
            "distribution_kind": "binary",
            "version": "1.2.3",
            "source_url": "https://example.com/agent.tar.gz",
            "expected_sha256": "a" * 64,
            "package": "",
            "idempotency_key": None,
        },
    )
    save_json_artifact(
        repo,
        files,
        "security-1",
        "security-evidence",
        "agent-artifact-verification.json",
        {
            "schema_version": 1,
            "agent_id": "sample-agent",
            "evidence_id": "verify-run-1",
            "static_evidence_id": "static-1",
            "distribution_kind": "binary",
            "version": "1.2.3",
            "source_url": "https://example.com/agent.tar.gz",
            "artifact_sha256": "a" * 64,
            "artifact_executed": False,
            "secrets_used": False,
            "integrity_verified": True,
            "archive_safe": True,
            "dependency_lock_complete": True,
            "passed": passed,
            "blockers": [] if passed else ["blocked"],
            "warnings": [],
        },
    )


def build_test_client(monkeypatch: pytest.MonkeyPatch, *, security_state="COMPLETED", security_passed=True):
    repo = FakeRepo()
    files = FakeFiles()
    seed_security_gate(repo, files, state=security_state, passed=security_passed)
    ids = iter(["benchmark-1", "benchmark-request", "benchmark-evidence", "spare"])

    def new_id():
        return next(ids)

    def now_ms():
        return 123456789

    def verify_token():
        return "device-ok"

    monkeypatch.setenv("HASSAN_CALLBACK_SECRET", "callback-secret")
    monkeypatch.setattr(
        api_module,
        "dispatch_agent_benchmark",
        lambda job_id, spec: BenchmarkDispatchResult(
            status="QUEUED",
            job_id=job_id,
            repository="owner/repo",
            ref="candidate",
        ),
    )
    app = FastAPI()
    app.include_router(
        build_agent_benchmark_router(
            repo=repo,
            files=files,
            verify_token=verify_token,
            new_id=new_id,
            now_ms=now_ms,
        )
    )
    return TestClient(app), repo, files


def benchmark_request(**overrides) -> dict:
    base = {
        "project_id": "p1",
        "agent_id": "sample-agent",
        "static_evidence_id": "static-1",
        "security_verification_job_id": "security-1",
        "version": "1.2.3",
        "source_url": "https://example.com/agent.tar.gz",
        "expected_sha256": "a" * 64,
        "command": "bin/agent",
        "args": ["--acp"],
        "protocol_version": 1,
        "idempotency_key": "bench:sample-agent:1.2.3:static-1",
    }
    base.update(overrides)
    return base


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


def benchmark_evidence(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "agent_id": "sample-agent",
        "evidence_id": "benchmark-run-1",
        "static_evidence_id": "static-1",
        "security_verification_job_id": "security-1",
        "version": "1.2.3",
        "source_url": "https://example.com/agent.tar.gz",
        "artifact_sha256": "a" * 64,
        "command": "bin/agent",
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
        "passed": True,
        "blockers": [],
        "warnings": [],
    }
    base.update(overrides)
    return base


def test_create_requires_completed_passing_binary_security_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    client, repo, _ = build_test_client(monkeypatch)
    created = client.post("/v1/agent-benchmarks", json=benchmark_request())
    assert created.status_code == 200
    payload = created.json()
    assert payload["id"] == "benchmark-1"
    assert payload["state"] == "RUNNING"
    assert payload["dispatch_status"] == "QUEUED"
    assert payload["benchmarked_automatically"] is False

    client2, _, _ = build_test_client(monkeypatch, security_state="RUNNING")
    blocked = client2.post("/v1/agent-benchmarks", json=benchmark_request())
    assert blocked.status_code == 409

    client3, _, _ = build_test_client(monkeypatch, security_passed=False)
    blocked = client3.post("/v1/agent-benchmarks", json=benchmark_request())
    assert blocked.status_code == 409


def test_create_rejects_sha_or_agent_drift_from_security_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = build_test_client(monkeypatch)
    assert client.post(
        "/v1/agent-benchmarks",
        json=benchmark_request(expected_sha256="b" * 64),
    ).status_code == 409

    client, _, _ = build_test_client(monkeypatch)
    assert client.post(
        "/v1/agent-benchmarks",
        json=benchmark_request(agent_id="other-agent"),
    ).status_code == 409


def test_safe_callback_records_evidence_but_never_auto_benchmarks(monkeypatch: pytest.MonkeyPatch) -> None:
    client, repo, _ = build_test_client(monkeypatch)
    assert client.post("/v1/agent-benchmarks", json=benchmark_request()).status_code == 200

    unauthorized = client.post(
        "/v1/internal/agent-benchmarks/benchmark-1/evidence",
        json=benchmark_evidence(),
    )
    assert unauthorized.status_code == 401

    accepted = client.post(
        "/v1/internal/agent-benchmarks/benchmark-1/evidence",
        headers={"X-Hassan-Callback-Secret": "callback-secret"},
        json=benchmark_evidence(),
    )
    assert accepted.status_code == 200
    payload = accepted.json()
    assert payload["status"] == "COMPLETED"
    assert payload["passed"] is True
    assert payload["benchmarked_automatically"] is False
    assert repo.jobs["benchmark-1"]["state"] == "COMPLETED"


def test_passed_callback_rejects_broken_sandbox_or_command_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = build_test_client(monkeypatch)
    assert client.post("/v1/agent-benchmarks", json=benchmark_request()).status_code == 200
    broken = client.post(
        "/v1/internal/agent-benchmarks/benchmark-1/evidence",
        headers={"X-Hassan-Callback-Secret": "callback-secret"},
        json=benchmark_evidence(network_isolated=False),
    )
    assert broken.status_code == 409

    client, _, _ = build_test_client(monkeypatch)
    assert client.post("/v1/agent-benchmarks", json=benchmark_request()).status_code == 200
    wrong_command = client.post(
        "/v1/internal/agent-benchmarks/benchmark-1/evidence",
        headers={"X-Hassan-Callback-Secret": "callback-secret"},
        json=benchmark_evidence(command="bin/other"),
    )
    assert wrong_command.status_code == 409


def test_failed_runner_evidence_is_audited_as_failed_not_promoted(monkeypatch: pytest.MonkeyPatch) -> None:
    client, repo, _ = build_test_client(monkeypatch)
    assert client.post("/v1/agent-benchmarks", json=benchmark_request()).status_code == 200
    response = client.post(
        "/v1/internal/agent-benchmarks/benchmark-1/evidence",
        headers={"X-Hassan-Callback-Secret": "callback-secret"},
        json=benchmark_evidence(
            passed=False,
            stdout_valid=False,
            initialize_response_json="",
            blockers=["benchmark_error:TimeoutError:timeout"],
        ),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "FAILED"
    assert response.json()["passed"] is False
    assert repo.jobs["benchmark-1"]["state"] == "FAILED"
