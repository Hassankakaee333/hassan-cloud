from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hassan_cloud.agent_executor_api as api_module
from hassan_cloud.agent_executor import AgentExecutionDispatchResult
from hassan_cloud.agent_executor_api import build_agent_execution_router


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
        row = self.jobs.get(job_id)
        return dict(row) if row else None

    def update_job(self, job_id, state, log_append, summary, ts):
        row = self.jobs[job_id]
        row["state"] = state
        row["log"] += log_append
        row["result_summary"] = summary

    def create_artifact(self, row: dict):
        self.artifacts.append(dict(row))
        return dict(row)

    def project_workspace(self, project_id: str):
        return {
            "project": self.projects.get(project_id),
            "artifacts": [a for a in self.artifacts if a.get("project_id") == project_id],
        }


def save_json(repo: FakeRepo, files: FakeFiles, job_id: str, aid: str, name: str, payload: dict) -> None:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    storage_path, digest, size = files.save(aid, name, raw)
    repo.create_artifact(
        {
            "id": aid,
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


def seed_shadow(repo: FakeRepo, files: FakeFiles) -> None:
    repo.jobs["shadow-1"] = {
        "id": "shadow-1",
        "project_id": "p1",
        "conversation_id": None,
        "goal": "shadow",
        "job_type": "agent_acp_shadow",
        "state": "COMPLETED",
        "result_summary": None,
        "log": "",
        "idempotency_key": None,
    }
    common = {
        "agent_id": "sample-agent",
        "static_evidence_id": "static-1",
        "security_verification_job_id": "security-1",
        "benchmark_job_id": "benchmark-1",
        "version": "1.2.3",
        "source_url": "https://example.com/agent.tar.gz",
        "command": "bin/agent",
        "args": ["--acp"],
    }
    save_json(
        repo, files, "shadow-1", "shadow-request", "agent-shadow-request.json",
        {**common, "expected_sha256": "a" * 64, "protocol_version": 1},
    )
    save_json(
        repo, files, "shadow-1", "shadow-evidence", "agent-acp-shadow.json",
        {
            **common,
            "artifact_sha256": "a" * 64,
            "protocol_version": "1",
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
            "session_created": True,
            "auth_methods_count": 0,
            "passed": True,
            "blockers": [],
        },
    )


def build_client(monkeypatch):
    repo = FakeRepo()
    files = FakeFiles()
    seed_shadow(repo, files)
    ids = iter(["exec-1", "request-a", "exec-2", "evidence-a", "spare"])
    dispatch_count = {"value": 0}

    def new_id():
        return next(ids)

    def verify_token():
        return "device"

    monkeypatch.setenv("HASSAN_CALLBACK_SECRET", "callback-secret")

    def dispatch(job_id):
        dispatch_count["value"] += 1
        return AgentExecutionDispatchResult(
            status="QUEUED", job_id=job_id, repository="owner/repo", ref="candidate"
        )

    monkeypatch.setattr(api_module, "dispatch_agent_execution", dispatch)
    app = FastAPI()
    app.include_router(
        build_agent_execution_router(
            repo=repo,
            files=files,
            verify_token=verify_token,
            new_id=new_id,
            now_ms=lambda: 123,
        )
    )
    return TestClient(app), repo, files, dispatch_count


def request_payload(**overrides) -> dict:
    data = b"approved bytes\n"
    digest = hashlib.sha256(data).hexdigest()
    payload = {
        "project_id": "p1",
        "permit_id": "agent-task-permit-sha256:" + "0" * 64,
        "execution_request_id": "agent-exec-000000000001",
        "approval_nonce": "approval_nonce_000000000001",
        "approval_evidence_id": "approval-1",
        "comparison_evidence_id": "compare-1",
        "task_id": "task-1",
        "goal": "Read the approved file and summarize it.",
        "goal_sha256": "",
        "files": [
            {
                "path": "docs/input.txt",
                "sha256": digest,
                "content_base64": base64.b64encode(data).decode("ascii"),
            }
        ],
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
        "expected_sha256": "a" * 64,
        "command": "bin/agent",
        "args": ["--acp"],
        "protocol_version": 1,
    }
    payload.update(overrides)
    payload["goal_sha256"] = hashlib.sha256(payload["goal"].encode()).hexdigest()
    model = api_module.AgentExecutionRequest(**payload)
    decoded = api_module._decoded_files(model)
    payload["permit_id"] = api_module._expected_permit_id(model, decoded)
    return payload


def evidence(payload: dict, **overrides) -> dict:
    base = {
        "schema_version": 1,
        "permit_id": payload["permit_id"],
        "execution_request_id": payload["execution_request_id"],
        "agent_id": payload["agent_id"],
        "version": payload["version"],
        "task_id": payload["task_id"],
        "goal_sha256": payload["goal_sha256"],
        "approval_evidence_id": payload["approval_evidence_id"],
        "comparison_evidence_id": payload["comparison_evidence_id"],
        "static_evidence_id": payload["static_evidence_id"],
        "security_verification_job_id": payload["security_verification_job_id"],
        "benchmark_job_id": payload["benchmark_job_id"],
        "shadow_job_id": payload["shadow_job_id"],
        "source_url": payload["source_url"],
        "artifact_sha256": payload["expected_sha256"],
        "command": payload["command"],
        "args": payload["args"],
        "actions": payload["actions"],
        "permit_verified": True,
        "files_verified": True,
        "file_count": 1,
        "artifact_executed": True,
        "secrets_used": False,
        "auth_attempted": False,
        "prompt_sent": True,
        "agent_client_requests": 0,
        "network_isolated": True,
        "filesystem_read_only": True,
        "containerized": True,
        "timeout_enforced": True,
        "archive_safe": True,
        "session_created": True,
        "session_id": "s1",
        "prompt_response_json": json.dumps({"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}),
        "stop_reason": "end_turn",
        "session_updates_jsonl": "",
        "updates_count": 0,
        "updates_sha256": hashlib.sha256(b"").hexdigest(),
        "execution_ms": 50,
        "stderr_bytes": 0,
        "stderr_sha256": "",
        "passed": True,
        "blockers": [],
        "warnings": [],
    }
    base.update(overrides)
    return base


def test_create_is_one_dispatch_per_permit_and_bundle_is_callback_protected(monkeypatch) -> None:
    client, repo, _files, counter = build_client(monkeypatch)
    payload = request_payload()
    first = client.post("/v1/agent-executions", json=payload)
    assert first.status_code == 200
    assert first.json()["id"] == "exec-1"
    assert first.json()["dispatch_status"] == "QUEUED"
    assert counter["value"] == 1

    second = client.post("/v1/agent-executions", json=payload)
    assert second.status_code == 200
    assert second.json()["id"] == "exec-1"
    assert second.json()["dispatch_status"] == "EXISTING"
    assert counter["value"] == 1

    assert client.get("/v1/internal/agent-executions/exec-1/bundle").status_code == 401
    bundle = client.get(
        "/v1/internal/agent-executions/exec-1/bundle",
        headers={"X-Hassan-Callback-Secret": "callback-secret"},
    )
    assert bundle.status_code == 200
    assert bundle.json()["goal"] == payload["goal"]
    assert repo.jobs["exec-1"]["state"] == "RUNNING"


def test_scope_drift_or_non_read_only_action_is_rejected_before_dispatch(monkeypatch) -> None:
    client, _repo, _files, counter = build_client(monkeypatch)
    bad_file = request_payload()
    bad_file["files"][0]["content_base64"] = base64.b64encode(b"different").decode("ascii")
    assert client.post("/v1/agent-executions", json=bad_file).status_code == 400

    write_like = request_payload(actions=["SET_TEXT"])
    assert client.post("/v1/agent-executions", json=write_like).status_code == 400
    assert counter["value"] == 0


def test_same_permit_with_different_execution_request_is_conflict(monkeypatch) -> None:
    client, _repo, _files, counter = build_client(monkeypatch)
    payload = request_payload()
    assert client.post("/v1/agent-executions", json=payload).status_code == 200
    changed = dict(payload, execution_request_id="agent-exec-000000000002")
    response = client.post("/v1/agent-executions", json=changed)
    assert response.status_code == 409
    assert counter["value"] == 1


def test_evidence_with_agent_client_request_fails_job(monkeypatch) -> None:
    client, repo, _files, _counter = build_client(monkeypatch)
    payload = request_payload()
    assert client.post("/v1/agent-executions", json=payload).status_code == 200
    blocked = client.post(
        "/v1/internal/agent-executions/exec-1/evidence",
        headers={"X-Hassan-Callback-Secret": "callback-secret"},
        json=evidence(payload, agent_client_requests=1, passed=False, blockers=["permission-request"]),
    )
    assert blocked.status_code == 200
    assert blocked.json()["status"] == "FAILED"
    assert repo.jobs["exec-1"]["state"] == "FAILED"
