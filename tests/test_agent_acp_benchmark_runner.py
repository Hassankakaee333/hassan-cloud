from __future__ import annotations

import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_acp_benchmark_runner as runner  # noqa: E402


def test_safe_command_rejects_escape_and_accepts_relative() -> None:
    assert runner._safe_command("bin/agent") == "bin/agent"
    for value in ("../agent", "/agent", "C:\\agent.exe", "bin/../agent"):
        with pytest.raises(ValueError):
            runner._safe_command(value)


def test_initialize_response_requires_exact_jsonrpc_protocol_and_version() -> None:
    raw = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": 1,
                "agentInfo": {"name": "Agent X", "version": "1.2.3"},
                "agentCapabilities": {"session": {"list": True}},
            },
        },
        separators=(",", ":"),
    )
    parsed = runner.validate_initialize_response(raw, 1, "1.2.3")
    assert parsed["agent_name"] == "Agent X"
    assert parsed["agent_version"] == "1.2.3"

    with pytest.raises(ValueError):
        runner.validate_initialize_response(raw, 2, "1.2.3")
    with pytest.raises(ValueError):
        runner.validate_initialize_response(raw, 1, "9.9.9")


def test_execution_extractor_rejects_zip_traversal(tmp_path: Path) -> None:
    artifact = tmp_path / "bad.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("../escape", b"no")
    with pytest.raises(ValueError):
        runner.safe_extract_for_execution(artifact, tmp_path / "root", "bin/agent")


def test_execution_extractor_rejects_tar_symlink(tmp_path: Path) -> None:
    artifact = tmp_path / "bad.tar"
    with tarfile.open(artifact, "w") as archive:
        entry = tarfile.TarInfo("bin/agent")
        entry.type = tarfile.SYMTYPE
        entry.linkname = "/bin/sh"
        archive.addfile(entry)
    with pytest.raises(ValueError):
        runner.safe_extract_for_execution(artifact, tmp_path / "root", "bin/agent")


def test_direct_binary_is_bound_to_registry_command(tmp_path: Path) -> None:
    artifact = tmp_path / "agent.bin"
    artifact.write_bytes(b"binary")
    result = runner.safe_extract_for_execution(artifact, tmp_path / "root", "bin/agent")
    command = tmp_path / "root" / "bin" / "agent"
    assert command.read_bytes() == b"binary"
    assert result["format"] == "direct"
    assert result["command"] == "bin/agent"


def test_failure_after_execution_boundary_keeps_artifact_executed_true(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected_sha = "a" * 64
    env = {
        "FRISHTA_AGENT_ID": "agent-x",
        "FRISHTA_STATIC_EVIDENCE_ID": "static-1",
        "FRISHTA_SECURITY_VERIFICATION_JOB_ID": "security-1",
        "FRISHTA_VERSION": "1.2.3",
        "FRISHTA_SOURCE_URL": "https://example.com/agent.bin",
        "FRISHTA_EXPECTED_SHA256": expected_sha,
        "FRISHTA_COMMAND": "bin/agent",
        "FRISHTA_ARGS_JSON": "[]",
        "FRISHTA_PROTOCOL_VERSION": "1",
        "FRISHTA_JOB_ID": "bench-1",
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
        "run_initialize_in_sandbox",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("handshake failed after launch boundary")),
    )

    report, exit_code = runner.run()
    assert exit_code == 2
    assert report["artifact_executed"] is True
    assert report["passed"] is False
    assert any("handshake failed after launch boundary" in item for item in report["blockers"])
