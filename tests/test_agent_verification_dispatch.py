from __future__ import annotations

import json

import httpx
import pytest

from hassan_cloud.agent_verification import AgentVerificationSpec, dispatch_agent_verification


def test_binary_spec_requires_https_and_sha() -> None:
    with pytest.raises(ValueError):
        AgentVerificationSpec(
            agent_id="agent",
            static_evidence_id="static-1",
            distribution_kind="binary",
            version="1",
            source_url="http://example.com/a.tar.gz",
            expected_sha256="a" * 64,
        ).validate()


def test_not_configured_is_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HASSAN_GITHUB_ACTIONS_TOKEN", raising=False)
    result = dispatch_agent_verification(
        "job-1",
        AgentVerificationSpec(
            agent_id="agent",
            static_evidence_id="static-1",
            distribution_kind="binary",
            version="1.2.3",
            source_url="https://example.com/a.tar.gz",
            expected_sha256="a" * 64,
        ),
    )
    assert result.status == "NOT_CONFIGURED"
    assert "TOKEN" in result.detail


def test_dispatch_payload_is_structured_and_has_no_provider_or_codex_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HASSAN_GITHUB_ACTIONS_TOKEN", "test-token")
    monkeypatch.setenv("HASSAN_AGENT_VERIFY_REPO", "owner/repo")
    monkeypatch.setenv("HASSAN_AGENT_VERIFY_REF", "candidate")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = dispatch_agent_verification(
        "job-123",
        AgentVerificationSpec(
            agent_id="agent-x",
            static_evidence_id="manifest-sha",
            distribution_kind="npx",
            version="2.0.0",
            package="agent-x@2.0.0",
        ),
        client=client,
    )
    client.close()

    assert result.status == "QUEUED"
    assert result.ref == "candidate"
    inputs = captured["body"]["inputs"]
    assert inputs["job_id"] == "job-123"
    assert inputs["agent_id"] == "agent-x"
    assert inputs["package"] == "agent-x@2.0.0"
    assert "provider" not in inputs
    assert "codex" not in json.dumps(inputs).lower()
    assert captured["authorization"] == "Bearer test-token"
