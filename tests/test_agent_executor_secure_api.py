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
        if summary is not None:
            self.jobs[job_id]["result_summary"] = summary

    def create_artifact(self, row):
        self.artifacts.append(dict(row))
        return dict(row)

    def project_workspace(self, project_id):
        return {
            "project": self.projects.get(project_id),
            "artifacts": [a for a in self.artifacts if a.get("project_id") == project_id],
        }


class DummyIdentityStore:
    def __init__(self, repo):
        self.repo = repo


class DummyBundleClaimStore:
    def __init__(self, repo):
        self.claimed = set()

    def claim_once(self, job_id, claimed_at):
        if job_id in self.claimed:
            return False
        self.claimed.add(job_id)
        return True

    def is_claimed(self, job_id):
        return job_id in self.claimed


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
    ids = iter([f"generated-{index}" for index in range(1, 30)])
    dispatches = {"count": 0}

    monkeypatch.setattr(secure, "DeviceIdentityStore", DummyIdentityStore)
    monkeypatch.setattr(secure, "AgentExecutionBundleClaimStore", DummyBundleClaimStore)
    monkeypatch.setattr(secure, "_validate_request", lambda body: [])
    monkeypatch.setattr(secure, "_assert_prior_shadow_gate", lambda repo, files, body: None)
    monkeypatch.setattr(secure, "_callback_authorized", lambda secret: None)

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

    def purge(repo_arg, files_arg, artifact):
        files_arg.data.pop(artifact["storage_path"], None)
        repo_arg.artifacts = [item for item in repo_arg.artifacts if item.get("id") != artifact.get("id")]

    monkeypatch.setattr(secure, "purge_private_artifact", purge)

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
    return TestClient(app), dispatches, repo, files


def test_invalid_device_signature_blocks_job_creation(monkeypatch):
    client, dispatches, _repo, _files = build_client(monkeypatch, signature_ok=False)
    response = client.post("/v1/agent-executions", json=payload())
    assert response.status_code == 403
    assert dispatches["count"] == 0


def test_same_signed_permit_dispatches_at_most_once(monkeypatch):
    client, dispatches, _repo, _files = build_client(monkeypatch, signature_ok=True)
    first = client.post("/v1/agent-executions", json=payload())
    assert first.status_code == 200, first.text
    assert first.json()["dispatch_status"] == "QUEUED"
    second = client.post("/v1/agent-executions", json=payload())
    assert second.status_code == 200, second.text
    assert second.json()["dispatch_status"] == "EXISTING"
    assert dispatches["count"] == 1


def test_private_bundle_is_delivered_once_and_raw_cloud_request_is_purged(monkeypatch):
    client, dispatches, repo, _files = build_client(monkeypatch, signature_ok=True)
    created = client.post("/v1/agent-executions", json=payload())
    assert created.status_code == 200, created.text
    job_id = created.json()["id"]

    first = client.get(f"/v1/internal/agent-executions/{job_id}/bundle")
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["permit_id"] == payload()["permit_id"]
    names = [item["name"] for item in repo.artifacts]
    assert "agent-task-execution-request.json" not in names
    assert "agent-acp-task-request-audit.json" in names

    second = client.get(f"/v1/internal/agent-executions/{job_id}/bundle")
    assert second.status_code == 409
    assert "already claimed" in second.text
    assert dispatches["count"] == 1


def test_post_purge_duplicate_must_match_original_signed_request_digest(monkeypatch):
    client, dispatches, _repo, _files = build_client(monkeypatch, signature_ok=True)
    created = client.post("/v1/agent-executions", json=payload())
    assert created.status_code == 200, created.text
    job_id = created.json()["id"]
    assert client.get(f"/v1/internal/agent-executions/{job_id}/bundle").status_code == 200

    identical = client.post("/v1/agent-executions", json=payload())
    assert identical.status_code == 200, identical.text
    assert identical.json()["dispatch_status"] == "EXISTING"

    changed = payload()
    changed["execution_request_id"] = "agent-exec-000000000999"
    different = client.post("/v1/agent-executions", json=changed)
    assert different.status_code == 409
    assert "different signed execution request" in different.text
    assert dispatches["count"] == 1
