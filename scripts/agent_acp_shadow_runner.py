"""Frishta ACP post-benchmark shadow session runner.

The exact verified Binary is launched in the same no-network/read-only sandbox used by P2.3. This
stage sends `initialize`, then one `session/new` against an empty read-only workspace. It never sends
a prompt, never authenticates, never grants permissions, and terminates immediately after the
session/new response. Any unexpected Agent->Client request/notification before the response blocks
the shadow test instead of being serviced.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from agent_acp_benchmark_runner import (
    MAX_DOWNLOAD_BYTES,
    MAX_FRAME_BYTES,
    MAX_STDERR_BYTES,
    DEFAULT_SANDBOX_IMAGE,
    _args,
    _force_remove_container,
    _initialize_request,
    _safe_command,
    download_public_https,
    inspect_archive,
    safe_extract_for_execution,
    validate_initialize_response,
)

DEFAULT_TIMEOUT_SECONDS = 15
MAX_SESSION_ID_CHARS = 512


def _env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise ValueError(f"{name} is required")
    return value


def _session_new_request() -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "session/new",
        "params": {
            "cwd": "/shadow",
            "mcpServers": [],
        },
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _read_frame(process: subprocess.Popen, timeout_seconds: int) -> str:
    assert process.stdout is not None
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(process.stdout.readline, MAX_FRAME_BYTES + 2)
        try:
            line = future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError as exc:
            raise TimeoutError("ACP response timed out") from exc
    if not line:
        raise ValueError("ACP subprocess closed stdout before response")
    if len(line) > MAX_FRAME_BYTES:
        raise ValueError("ACP frame exceeds size limit")
    if not line.endswith(b"\n"):
        raise ValueError("ACP stdio response is not newline-delimited")
    raw = line[:-1].decode("utf-8")
    if "\n" in raw or "\r" in raw:
        raise ValueError("ACP frame contains embedded newline")
    return raw


def _validate_session_new_response(raw: str) -> dict:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("ACP session/new response must be an object")
    if payload.get("jsonrpc") != "2.0" or payload.get("id") != 2:
        if payload.get("method") is not None:
            raise ValueError("unexpected Agent request/notification before session/new response")
        raise ValueError("ACP session/new JSON-RPC envelope mismatch")
    error = payload.get("error")
    if error is not None:
        if isinstance(error, dict):
            code = str(error.get("code") or "")[:32]
            message = str(error.get("message") or "")[:160]
            raise ValueError(f"ACP session/new error code={code} message={message}")
        raise ValueError("ACP session/new returned error")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("ACP session/new result missing")
    session_id = str(result.get("sessionId") or "").strip()
    if not session_id or len(session_id) > MAX_SESSION_ID_CHARS:
        raise ValueError("ACP session/new returned invalid sessionId")
    return {
        "session_id": session_id,
        "session_new_response_json": raw,
    }


def _auth_methods_count(initialize_raw: str) -> int:
    payload = json.loads(initialize_raw)
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return 0
    methods = result.get("authMethods")
    return len(methods) if isinstance(methods, list) else 0


def run_shadow_in_sandbox(
    root: Path,
    shadow_root: Path,
    command: str,
    args: list[str],
    protocol_version: int,
    job_id: str,
    stderr_path: Path,
) -> tuple[str, str, int, int]:
    image = _env("FRISHTA_SANDBOX_IMAGE", required=False) or DEFAULT_SANDBOX_IMAGE
    timeout_seconds = int(_env("FRISHTA_SHADOW_TIMEOUT_SECONDS", required=False) or DEFAULT_TIMEOUT_SECONDS)
    if timeout_seconds < 3 or timeout_seconds > 30:
        raise ValueError("shadow timeout outside safe range")
    inspect = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
    )
    if inspect.returncode != 0:
        raise RuntimeError("sandbox container image is not available")

    safe_job = re.sub(r"[^a-zA-Z0-9_.-]", "-", job_id)[:80]
    name = f"frishta-acp-shadow-{safe_job or 'job'}"
    _force_remove_container(name)
    docker_command = [
        "docker", "run", "--name", name, "--rm", "-i",
        "--network", "none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", "64",
        "--memory", "512m",
        "--cpus", "1",
        "--user", "65534:65534",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=32m",
        "--env", "HOME=/tmp",
        "--env", "LANG=C.UTF-8",
        "--env", "NO_COLOR=1",
        "--volume", f"{root.resolve()}:/agent:ro",
        "--volume", f"{shadow_root.resolve()}:/shadow:ro",
        "--workdir", "/shadow",
        image,
        f"/agent/{command}",
        *args,
    ]

    start = time.monotonic()
    with stderr_path.open("wb") as stderr_handle:
        process = subprocess.Popen(
            docker_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_handle,
        )
        try:
            assert process.stdin is not None
            process.stdin.write((_initialize_request(protocol_version) + "\n").encode("utf-8"))
            process.stdin.flush()
            initialize_raw = _read_frame(process, timeout_seconds)
            process.stdin.write((_session_new_request() + "\n").encode("utf-8"))
            process.stdin.flush()
            session_raw = _read_frame(process, timeout_seconds)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            process.stdin.close()
            _force_remove_container(name)
            try:
                return_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                return_code = process.wait(timeout=5)
            return initialize_raw, session_raw, elapsed_ms, return_code
        finally:
            _force_remove_container(name)
            if process.poll() is None:
                process.kill()
            if process.stdout is not None:
                process.stdout.close()


def _base_report() -> dict:
    return {
        "schema_version": 1,
        "agent_id": os.environ.get("FRISHTA_AGENT_ID", ""),
        "evidence_id": os.environ.get("GITHUB_RUN_ID", "local") or "local",
        "static_evidence_id": os.environ.get("FRISHTA_STATIC_EVIDENCE_ID", ""),
        "security_verification_job_id": os.environ.get("FRISHTA_SECURITY_VERIFICATION_JOB_ID", ""),
        "benchmark_job_id": os.environ.get("FRISHTA_BENCHMARK_JOB_ID", ""),
        "version": os.environ.get("FRISHTA_VERSION", ""),
        "source_url": "",
        "artifact_sha256": "",
        "command": "",
        "args": [],
        "artifact_executed": False,
        "secrets_used": False,
        "auth_attempted": False,
        "prompt_sent": False,
        "permission_requests": 0,
        "tool_requests": 0,
        "network_isolated": True,
        "filesystem_read_only": True,
        "containerized": True,
        "timeout_enforced": True,
        "archive_safe": False,
        "initialize_response_json": "",
        "protocol_version": "",
        "auth_methods_count": 0,
        "session_new_response_json": "",
        "session_id": "",
        "session_created": False,
        "shadow_ms": 0,
        "stderr_bytes": 0,
        "stderr_sha256": "",
        "passed": False,
        "blockers": [],
        "warnings": [],
    }


def run() -> tuple[dict, int]:
    report = _base_report()
    try:
        agent_id = _env("FRISHTA_AGENT_ID")
        static_evidence_id = _env("FRISHTA_STATIC_EVIDENCE_ID")
        security_job_id = _env("FRISHTA_SECURITY_VERIFICATION_JOB_ID")
        benchmark_job_id = _env("FRISHTA_BENCHMARK_JOB_ID")
        version = _env("FRISHTA_VERSION")
        source_url = _env("FRISHTA_SOURCE_URL")
        expected_sha = _env("FRISHTA_EXPECTED_SHA256").lower()
        command = _safe_command(_env("FRISHTA_COMMAND"))
        args = _args()
        protocol_version = int(_env("FRISHTA_PROTOCOL_VERSION"))
        job_id = _env("FRISHTA_JOB_ID")
        if len(expected_sha) != 64 or any(ch not in "0123456789abcdef" for ch in expected_sha):
            raise ValueError("invalid expected SHA-256")
        if protocol_version < 1 or protocol_version > 65535:
            raise ValueError("invalid ACP protocol version")

        report.update(
            agent_id=agent_id,
            static_evidence_id=static_evidence_id,
            security_verification_job_id=security_job_id,
            benchmark_job_id=benchmark_job_id,
            version=version,
            source_url=source_url,
            command=command,
            args=args,
        )

        with tempfile.TemporaryDirectory(prefix="frishta-acp-shadow-") as tmp:
            temp_dir = Path(tmp)
            artifact = temp_dir / "artifact.bin"
            with httpx.Client(timeout=httpx.Timeout(60.0, read=180.0), trust_env=False) as client:
                actual_sha = download_public_https(source_url, artifact, MAX_DOWNLOAD_BYTES, client)
            report["artifact_sha256"] = actual_sha
            if actual_sha != expected_sha:
                report["blockers"].append("sha256_mismatch")
                return report, 2
            archive_summary = inspect_archive(artifact)
            if not archive_summary.get("safe"):
                report["blockers"].append("archive_not_safe")
                return report, 2

            execution_root = temp_dir / "root"
            extraction = safe_extract_for_execution(artifact, execution_root, command)
            report["archive"] = extraction
            report["archive_safe"] = True
            shadow_root = temp_dir / "shadow"
            shadow_root.mkdir(mode=0o555)
            stderr_path = temp_dir / "agent.stderr"

            report["artifact_executed"] = True
            try:
                initialize_raw, session_raw, shadow_ms, return_code = run_shadow_in_sandbox(
                    root=execution_root,
                    shadow_root=shadow_root,
                    command=command,
                    args=args,
                    protocol_version=protocol_version,
                    job_id=job_id,
                    stderr_path=stderr_path,
                )
                metadata = validate_initialize_response(initialize_raw, protocol_version, version)
                session = _validate_session_new_response(session_raw)
                report.update(metadata)
                report.update(session)
                report["initialize_response_json"] = initialize_raw
                report["auth_methods_count"] = _auth_methods_count(initialize_raw)
                report["session_created"] = True
                report["shadow_ms"] = shadow_ms
                report["process_return_code_after_termination"] = return_code
            finally:
                if stderr_path.exists():
                    stderr_data = stderr_path.read_bytes()[:MAX_STDERR_BYTES]
                    report["stderr_bytes"] = stderr_path.stat().st_size
                    report["stderr_sha256"] = hashlib.sha256(stderr_data).hexdigest()
                    if stderr_path.stat().st_size > MAX_STDERR_BYTES:
                        report["warnings"].append("stderr_truncated_for_hash")

        report["passed"] = (
            report["artifact_executed"]
            and not report["secrets_used"]
            and not report["auth_attempted"]
            and not report["prompt_sent"]
            and report["permission_requests"] == 0
            and report["tool_requests"] == 0
            and report["network_isolated"]
            and report["filesystem_read_only"]
            and report["containerized"]
            and report["timeout_enforced"]
            and report["archive_safe"]
            and report["session_created"]
            and not report["blockers"]
        )
        return report, 0 if report["passed"] else 2
    except Exception as exc:
        report["passed"] = False
        report["blockers"].append(f"shadow_error:{type(exc).__name__}:{exc}")
        return report, 2


def main() -> int:
    out_dir = Path(os.environ.get("FRISHTA_SHADOW_OUT_DIR", "/tmp/frishta-agent-shadow"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "agent-acp-shadow.json"
    report, exit_code = run()
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
