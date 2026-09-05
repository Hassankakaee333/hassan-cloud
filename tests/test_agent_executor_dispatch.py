from __future__ import annotations

import os

from hassan_cloud.agent_executor import dispatch_agent_execution


class FakeResponse:
    status_code = 204
    text = ""


class FakeClient:
    def __init__(self) -> None:
        self.url = ""
        self.headers = {}
        self.payload = {}

    def post(self, url, headers=None, json=None):
        self.url = url
        self.headers = headers or {}
        self.payload = json or {}
        return FakeResponse()


def test_dispatch_sends_only_opaque_job_id(monkeypatch) -> None:
    monkeypatch.setenv("HASSAN_GITHUB_ACTIONS_TOKEN", "token")
    monkeypatch.setenv("HASSAN_AGENT_EXECUTOR_REPO", "owner/repo")
    monkeypatch.setenv("HASSAN_AGENT_EXECUTOR_REF", "candidate")
    client = FakeClient()

    result = dispatch_agent_execution("job-1234567890123456", client=client)

    assert result.status == "QUEUED"
    assert client.payload == {
        "ref": "candidate",
        "inputs": {"job_id": "job-1234567890123456"},
    }
    serialized = str(client.payload).lower()
    assert "goal" not in serialized
    assert "permit" not in serialized
    assert "prompt" not in serialized
    assert "file" not in serialized
    assert client.url.endswith("/actions/workflows/agent-acp-task.yml/dispatches")


def test_missing_actions_token_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("HASSAN_GITHUB_ACTIONS_TOKEN", raising=False)
    result = dispatch_agent_execution("job-1234567890123456")
    assert result.status == "NOT_CONFIGURED"


def test_missing_executor_ref_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("HASSAN_GITHUB_ACTIONS_TOKEN", "token")
    monkeypatch.delenv("HASSAN_AGENT_EXECUTOR_REF", raising=False)
    client = FakeClient()

    result = dispatch_agent_execution("job-1234567890123456", client=client)

    assert result.status == "NOT_CONFIGURED"
    assert "Candidate" in result.detail
    assert client.url == ""


def test_stable_executor_ref_is_forbidden(monkeypatch) -> None:
    monkeypatch.setenv("HASSAN_GITHUB_ACTIONS_TOKEN", "token")
    monkeypatch.setenv("HASSAN_AGENT_EXECUTOR_REF", "refs/heads/main")
    client = FakeClient()

    result = dispatch_agent_execution("job-1234567890123456", client=client)

    assert result.status == "NOT_CONFIGURED"
    assert "Stable/main" in result.detail
    assert client.url == ""
