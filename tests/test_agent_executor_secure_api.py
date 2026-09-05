from __future__ import annotations

import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

import hassan_cloud.agent_executor_secure_api as secure
from hassan_cloud.agent_executor import AgentExecutionDispatchResult


class FakeFiles:
    def __init__(self):
        self.data = {}

    def save(self, artifact_id, filename, data):
        path = f"{artifact_id}/{filename}"
        self.data[path] = data
        return path, hashlib.sha256(data).hexdigest(), len(data)

    def read(self, path):
        return self.data[path]


class FakeRepo:
    def __init__(self):
        self.projects = {"p1": {"id": "p1"}}
        self.jobs = {}
        self.artifacts = []

    def get_project(self, project_id):
        return self.projects.get(project_id)

    def create_job(self, jid, project_id, conversation_id, goal, job_type, ts, idempotency_key=None):
        for existing in self.jobs.values():
            if idempotency_key and existing.get("idempotency_key") == idempotency_key:
                return dict(existing)
        row = {
            "id": jid, "project_id": project_id, "conversation_id": conversation_id,
            "goal": goal, "job_type": job_type, "state": "QUEUED", "result_summary": None,
            "log": "", "idempotency_key": idempotency_key,
        }
        self.jobs[jid] = row
        return dict(row)

    def get_job(self, job_id):
        row = self.jobs.get(job_id)
        return dict(row) if row else None

    def update_job(self, job_id, state, log_append, summary, ts):
        self.jobs[job_id]["state"] = state
        self.jobs[job_id]["log"] += log_append
        self.jobs[job_id]["result_summary"] = summary

    def create_artifact(self, row):
        self.artifacts.append(dict(row))
        return dict(row)

    def project_workspace(self, project_id):
        return {"project": self.projects.get(project_id), "artifacts": [a for a in self.artifacts if a.get("project_id") == project_id]}


class DummyIdentityStore:
    def __init__(self, repo):
        self.repo = repo


def payload():
    return {
        "project_id": "p1",
        "permit_id": "agent-task-permit-sha256:" + "a" * 64,
        "execution_request_id": "agent-exec-000000000001",
        "approval_nonce": "approval_nonce_000000000001",
        "approval_evidence_id": "approval-1",
        "comparison_evidence_id": "compare-1",
        "task_id": "task-1",
        "goal": "Read approved files and summarize them.",
        "goal_sha256": "b" * 64,
        "files": [],
        "actions": ["READ_FILES"],
        "targets_stable_directly": False,
        "cost_class": "FREE",
        "additional_spend_cents": 0,
        "agent_id": "sample-agent",
        "static_evidence_id": "static-1",
        "security_verification_job_id": "security-1",
        "benchmark_job_id": "benchmark-1",
        "shadow_job_id": "shadow-1",
        "version": "1.2.3",
        "source_url": "https://example.com/agent.tar.gz",
        "expected_sha256": "c" * 64,
        "command": "bin/agent",
        "args": ["--acp"],
        "protocol_version": 1,
        "device_id": "phone-1",
        "device_signature_base64": "signed-value",
    }


def build_client(monkeypatch, signature_ok=True):
    repo = FakeRepo()
    files = FakeFiles()
    ids = iter(["exec-1", "request-1", "exec-2", "request-2"])
    dispatches = {"count": 0}

    monkeypatch.setattr(secure, "DeviceIdentityStore", DummyIdentityStore)
    monkeypatch.setattr(secure, "_validate_request", lambda body: [])
    monkeypatch.setattr(secure, "_assert_prior_shadow_gate", lambda repo, files, body: None)

    def verify_signature(store, device_id, body, signature):
        if not signature_ok:
            raise ValueError("device signature verification failed")
        assert device_id == "phone-1"
        assert body["permit_id"].startswith("agent-task-permit-sha256:")
        assert signature == "signed-value"

    monkeypatch.setattr(secure, "verify_pinned_execution_signature", verify_signature)

    def dispatch(job_id):
        dispatches["count"] += 1
        return AgentExecutionDispatchResult("QUEUED", job_id, "owner/repo", "candidate")

    monkeypatch.setattr(secure, "dispatch_agent_execution", dispatch)
    app = FastAPI()
    app.include_router(
        secure.build_secure_agent_execution_router(
            repo=repo,
            files=files,
            verify_token=lambda: "ok",
            new_id=lambda: next(ids),
            now_ms=lambda: 1,
        )
    )
    return TestClient(app), dispatches


def test_invalid_device_signature_blocks_job_creation(monkeypatch):
    client, dispatches = build_client(monkeypatch, signature_ok=False)
    response = client.post("/v1/agent-executions", json=payload())
    assert response.status_code == 403
    assert dispatches["count"] == 0


def test_same_signed_permit_dispatches_at_most_once(monkeypatch):
    client, dispatches = build_client(monkeypatch, signature_ok=True)
    first = client.post("/v1/agent-executions", json=payload())
    assert first.status_code == 200, first.text
    assert first.json()["dispatch_status"] == "QUEUED"
    second = client.post("/v1/agent-executions", json=payload())
    assert second.status_code == 200, second.text
    assert second.json()["dispatch_status"] == "EXISTING"
    assert dispatches["count"] == 1
