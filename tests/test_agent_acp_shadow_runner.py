from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_acp_shadow_runner as runner  # noqa: E402


def initialize_json() -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": 1,
                "agentInfo": {"name": "Shadow Agent", "version": "1.2.3"},
                "authMethods": [{"id": "oauth", "name": "OAuth"}],
                "agentCapabilities": {"session": {"list": True}},
            },
        },
        separators=(",", ":"),
    )


def session_json() -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"sessionId": "shadow-session-1"},
        },
        separators=(",", ":"),
    )


def test_shadow_request_is_session_new_only_and_empty_workspace() -> None:
    payload = json.loads(runner._session_new_request())
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 2
    assert payload["method"] == "session/new"
    assert payload["params"] == {"cwd": "/shadow", "mcpServers": []}
    serialized = json.dumps(payload).lower()
    assert "session/prompt" not in serialized
    assert "prompt" not in serialized
    assert "tool" not in serialized
    assert "permission" not in serialized
    assert "auth" not in serialized


def test_session_new_response_requires_exact_response_and_session_id() -> None:
    parsed = runner._validate_session_new_response(session_json())
    assert parsed["session_id"] == "shadow-session-1"

    with pytest.raises(ValueError, match="unexpected Agent request/notification"):
        runner._validate_session_new_response(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "session/request_permission",
                    "params": {"permission": "filesystem"},
                },
                separators=(",", ":"),
            )
        )

    with pytest.raises(ValueError, match="session/new error"):
        runner._validate_session_new_response(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "error": {"code": -32000, "message": "auth required"},
                },
                separators=(",", ":"),
            )
        )


def test_auth_methods_are_observed_but_never_attempted() -> None:
    assert runner._auth_methods_count(initialize_json()) == 1
    report = runner._base_report()
    assert report["auth_attempted"] is False
    assert report["prompt_sent"] is False
    assert report["permission_requests"] == 0
    assert report["tool_requests"] == 0
    assert report["secrets_used"] is False


def test_successful_shadow_run_preserves_no_prompt_no_auth_invariants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_sha = "a" * 64
    env = {
        "FRISHTA_AGENT_ID": "agent-x",
        "FRISHTA_STATIC_EVIDENCE_ID": "static-1",
        "FRISHTA_SECURITY_VERIFICATION_JOB_ID": "security-1",
        "FRISHTA_BENCHMARK_JOB_ID": "benchmark-1",
        "FRISHTA_VERSION": "1.2.3",
        "FRISHTA_SOURCE_URL": "https://example.com/agent.bin",
        "FRISHTA_EXPECTED_SHA256": expected_sha,
        "FRISHTA_COMMAND": "bin/agent",
        "FRISHTA_ARGS_JSON": "[]",
        "FRISHTA_PROTOCOL_VERSION": "1",
        "FRISHTA_JOB_ID": "shadow-1",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    def fake_download(url, destination, max_bytes, client):
        destination.write_bytes(b"verified")
        return expected_sha

    monkeypatch.setattr(runner, "download_public_https", fake_download)
    monkeypatch.setattr(runner, "inspect_archive", lambda path: {"safe": True, "format": "direct"})
    monkeypatch.setattr(
        runner,
        "safe_extract_for_execution",
        lambda artifact, root, command: {"safe": True, "format": "direct", "command": command},
    )
    monkeypatch.setattr(
        runner,
        "run_shadow_in_sandbox",
        lambda **kwargs: (initialize_json(), session_json(), 42, 0),
    )

    report, exit_code = runner.run()
    assert exit_code == 0
    assert report["passed"] is True
    assert report["artifact_executed"] is True
    assert report["session_created"] is True
    assert report["session_id"] == "shadow-session-1"
    assert report["auth_methods_count"] == 1
    assert report["auth_attempted"] is False
    assert report["prompt_sent"] is False
    assert report["permission_requests"] == 0
    assert report["tool_requests"] == 0
    assert report["secrets_used"] is False
    assert report["network_isolated"] is True
    assert report["filesystem_read_only"] is True


def test_unexpected_agent_request_blocks_shadow_and_never_becomes_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError):
        runner._validate_session_new_response(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "fs/write_text_file",
                    "params": {"path": "/shadow/x"},
                },
                separators=(",", ":"),
            )
        )
