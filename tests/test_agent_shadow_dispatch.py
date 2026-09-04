from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hassan_cloud.agent_shadow import AgentShadowSpec, dispatch_agent_shadow  # noqa: E402


def spec(**overrides) -> AgentShadowSpec:
    values = {
        "agent_id": "agent-x",
        "static_evidence_id": "acp-static-sha256:abc",
        "security_verification_job_id": "security-job-1",
        "benchmark_job_id": "benchmark-job-1",
        "version": "1.2.3",
        "source_url": "https://example.com/agent.tar.gz",
        "expected_sha256": "a" * 64,
        "command": "bin/agent",
        "args": ("--acp",),
        "protocol_version": 1,
    }
    values.update(overrides)
    return AgentShadowSpec(**values)


def test_spec_rejects_escape_bad_sha_and_control_args() -> None:
    for command in ("../agent", "/usr/bin/agent", "C:\\agent.exe", "bin/../agent"):
        with pytest.raises(ValueError):
            spec(command=command).validate()
    with pytest.raises(ValueError):
        spec(expected_sha256="abc").validate()
    with pytest.raises(ValueError):
        spec(args=("ok", "bad\narg")).validate()


def test_not_configured_is_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HASSAN_GITHUB_ACTIONS_TOKEN", raising=False)
    result = dispatch_agent_shadow("shadow-job-1", spec())
    assert result.status == "NOT_CONFIGURED"
    assert "TOKEN" in result.detail


def test_dispatch_payload_is_manual_shadow_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HASSAN_GITHUB_ACTIONS_TOKEN", "test-token")
    monkeypatch.setenv("HASSAN_AGENT_SHADOW_REPO", "owner/repo")
    monkeypatch.setenv("HASSAN_AGENT_SHADOW_REF", "candidate")
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = dispatch_agent_shadow("shadow-job-1", spec(), client=client)
    client.close()

    assert result.status == "QUEUED"
    assert result.workflow == "agent-acp-shadow.yml"
    assert result.ref == "candidate"
    inputs = captured["body"]["inputs"]  # type: ignore[index]
    assert inputs["job_id"] == "shadow-job-1"
    assert inputs["security_verification_job_id"] == "security-job-1"
    assert inputs["benchmark_job_id"] == "benchmark-job-1"
    assert inputs["command"] == "bin/agent"
    assert json.loads(inputs["args_json"]) == ["--acp"]
    assert inputs["expected_sha256"] == "a" * 64
    serialized = json.dumps(inputs).lower()
    assert "provider" not in serialized
    assert "codex" not in serialized
    assert "prompt" not in serialized
    assert "auth" not in serialized
    assert captured["authorization"] == "Bearer test-token"
