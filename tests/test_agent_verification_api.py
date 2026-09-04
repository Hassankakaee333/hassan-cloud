from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hassan_cloud.agent_verification_api as api_module  # noqa: E402
from hassan_cloud.agent_verification import DispatchResult  # noqa: E402
from hassan_cloud.agent_verification_api import build_agent_verification_router  # noqa: E402


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


def build_test_client(monkeypatch: pytest.MonkeyPatch):
    repo = FakeRepo()
    files = FakeFiles()
    ids = iter(["job-1", "artifact-request", "artifact-evidence", "spare"])

    def new_id():
        return next(ids)

    def now_ms():
        return 123456789

    def verify_token():
        return "device-ok"

    monkeypatch.setenv("HASSAN_CALLBACK_SECRET", "callback-secret")
    monkeypatch.setattr(
        api_module,
        "dispatch_agent_verification",
        lambda job_id, spec: DispatchResult(
            status="QUEUED",
            job_id=job_id,
            repository="owner/repo",
            ref="candidate",
        ),
    )
    app = FastAPI()
    app.include_router(
        build_agent_verification_router(
            repo=repo,
            files=files,
            verify_token=verify_token,
            new_id=new_id,
            now_ms=now_ms,
        )
    )
    return TestClient(app), repo


def binary_request() -> dict:
    return {
        "project_id": "p1",
        "agent_id": "sample-agent",
        "static_evidence_id": "manifest-sha",
        "distribution_kind": "binary",
        "version": "1.2.3",
        "source_url": "https://example.com/agent.tar.gz",
        "expected_sha256": "a" * 64,
        "package": "",
    }


def evidence(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "agent_id": "sample-agent",
        "evidence_id": "gha-run-1",
        "static_evidence_id": "manifest-sha",
        "github_run_id": "1",
        "distribution_kind": "binary",
        "version": "1.2.3",
        "artifact_sha256": "a" * 64,
        "artifact_executed": False,
        "secrets_used": False,
        "integrity_verified": True,
        "archive_safe": True,
        "dependency_lock_complete": True,
        "passed": True,
        "blockers": [],
        "warnings": [],
    }
    base.update(overrides)
    return base


def test_zero_pc_create_and_safe_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    client, repo = build_test_client(monkeypatch)
    created = client.post("/v1/agent-verifications", json=binary_request())
    assert created.status_code == 200
    payload = created.json()
    assert payload["id"] == "job-1"
    assert payload["state"] == "RUNNING"
    assert payload["dispatch_status"] == "QUEUED"
    assert len(repo.artifacts) == 1
    assert repo.artifacts[0]["name"] == "agent-verification-request.json"

    unauthorized = client.post(
        "/v1/internal/agent-verifications/job-1/evidence",
        json=evidence(),
    )
    assert unauthorized.status_code == 401

    unsafe = client.post(
        "/v1/internal/agent-verifications/job-1/evidence",
        headers={"X-Hassan-Callback-Secret": "callback-secret"},
        json=evidence(artifact_executed=True),
    )
    assert unsafe.status_code == 409

    accepted = client.post(
        "/v1/internal/agent-verifications/job-1/evidence",
        headers={"X-Hassan-Callback-Secret": "callback-secret"},
        json=evidence(),
    )
    assert accepted.status_code == 200
    result = accepted.json()
    assert result["status"] == "COMPLETED"
    assert result["security_checked_automatically"] is False
    assert repo.jobs["job-1"]["state"] == "COMPLETED"
    assert [a["name"] for a in repo.artifacts] == [
        "agent-verification-request.json",
        "agent-artifact-verification.json",
    ]


def test_package_evidence_can_complete_job_without_claiming_security_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    client, repo = build_test_client(monkeypatch)
    request = binary_request()
    request.update(
        {
            "distribution_kind": "npx",
            "source_url": "",
            "expected_sha256": "",
            "package": "sample-agent@1.2.3",
        }
    )
    assert client.post("/v1/agent-verifications", json=request).status_code == 200
    response = client.post(
        "/v1/internal/agent-verifications/job-1/evidence",
        headers={"X-Hassan-Callback-Secret": "callback-secret"},
        json=evidence(
            distribution_kind="npx",
            dependency_lock_complete=False,
            passed=False,
            warnings=["dependency_lock_pending"],
        ),
    )
    assert response.status_code == 200
    assert response.json()["passed"] is False
    assert repo.jobs["job-1"]["state"] == "COMPLETED"
    assert "SECURITY_CHECKED remains blocked" in repo.jobs["job-1"]["result_summary"]
