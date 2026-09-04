"""Frishta ACP post-security sandbox micro-benchmark.

This runner deliberately executes only a Binary distribution that was already integrity-verified.
It re-downloads and re-checks the exact SHA, extracts into a temporary directory with strict path
rules, then launches the registry command inside a Docker container with no network, read-only root,
no added capabilities, no secrets, and hard resource/time limits. It sends exactly one ACP
`initialize` request over newline-delimited JSON-RPC and does not create a session or authenticate.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

import httpx

from agent_artifact_verify_runner import (
    _normalized_member_path,
    download_public_https,
    inspect_archive,
)

MAX_DOWNLOAD_BYTES = 120 * 1024 * 1024
MAX_EXTRACTED_BYTES = 250 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
MAX_FRAME_BYTES = 512 * 1024
MAX_STDERR_BYTES = 128 * 1024
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_SANDBOX_IMAGE = "ubuntu:24.04"
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:[/\\]")


def _env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise ValueError(f"{name} is required")
    return value


def _safe_command(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if not normalized or len(normalized) > 512:
        raise ValueError("invalid Agent command")
    if normalized.startswith("/") or _DRIVE_PREFIX.match(normalized):
        raise ValueError("Agent command must be relative")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("Agent command escapes artifact root")
    if any(ord(ch) < 32 for ch in normalized):
        raise ValueError("Agent command contains control characters")
    return "/".join(parts)


def _args() -> list[str]:
    raw = _env("FRISHTA_ARGS_JSON", required=False) or "[]"
    value = json.loads(raw)
    if not isinstance(value, list) or len(value) > 24:
        raise ValueError("invalid Agent args JSON")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or len(item) > 512:
            raise ValueError("invalid Agent argument")
        if any(ord(ch) == 0 or (ord(ch) < 32 and ch != "\t") for ch in item):
            raise ValueError("Agent argument contains control characters")
        result.append(item)
    return result


def _destination(root: Path, member_name: str) -> Path:
    parts = _normalized_member_path(member_name)
    if not parts:
        return root
    destination = root.joinpath(*parts)
    resolved_parent = destination.parent.resolve()
    root_resolved = root.resolve()
    if resolved_parent != root_resolved and root_resolved not in resolved_parent.parents:
        raise ValueError("archive member escapes extraction root")
    return destination


def _copy_limited(source, destination: Path, expected_size: int, total_counter: list[int]) -> None:
    if expected_size < 0 or total_counter[0] + expected_size > MAX_EXTRACTED_BYTES:
        raise ValueError("archive extracted size exceeds limit")
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with destination.open("wb") as handle:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if total_counter[0] + written > MAX_EXTRACTED_BYTES:
                raise ValueError("archive extracted size exceeds limit")
            handle.write(chunk)
    if expected_size and written != expected_size:
        raise ValueError("archive member size mismatch")
    total_counter[0] += written


def safe_extract_for_execution(artifact: Path, root: Path, command: str) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    total = [0]
    members = 0

    if zipfile.is_zipfile(artifact):
        with zipfile.ZipFile(artifact) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("zip archive has too many members")
            for info in infos:
                members += 1
                destination = _destination(root, info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ValueError("zip archive contains symlink")
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                with archive.open(info, "r") as source:
                    _copy_limited(source, destination, int(info.file_size), total)
        archive_format = "zip"
    elif tarfile.is_tarfile(artifact):
        with tarfile.open(artifact, "r:*") as archive:
            infos = archive.getmembers()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("tar archive has too many members")
            for info in infos:
                members += 1
                destination = _destination(root, info.name)
                if info.isdev() or info.isfifo() or info.issym() or info.islnk():
                    raise ValueError("execution archive contains device/fifo/link entry")
                if info.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if not info.isfile():
                    raise ValueError("execution archive contains unsupported member type")
                source = archive.extractfile(info)
                if source is None:
                    raise ValueError("tar member could not be read")
                with source:
                    _copy_limited(source, destination, int(info.size), total)
        archive_format = "tar"
    else:
        destination = root / command
        destination.parent.mkdir(parents=True, exist_ok=True)
        size = artifact.stat().st_size
        if size > MAX_EXTRACTED_BYTES:
            raise ValueError("direct binary exceeds extraction limit")
        shutil.copyfile(artifact, destination)
        total[0] = size
        members = 1
        archive_format = "direct"

    command_path = root / command
    if not command_path.exists() or not command_path.is_file() or command_path.is_symlink():
        raise ValueError("registry command not found as a regular file in verified artifact")
    resolved = command_path.resolve()
    root_resolved = root.resolve()
    if root_resolved not in resolved.parents:
        raise ValueError("registry command escapes verified artifact root")
    command_path.chmod(0o555)
    return {
        "format": archive_format,
        "members": members,
        "extracted_bytes": total[0],
        "command": command,
        "safe": True,
    }


def _initialize_request(protocol_version: int) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": protocol_version,
            "clientCapabilities": {"auth": {"terminal": False}},
            "clientInfo": {
                "name": "Frishta AI",
                "title": "Frishta AI",
                "version": "P2.3-sandbox",
            },
        },
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _container_name(job_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", job_id)[:80]
    return f"frishta-acp-bench-{safe or 'job'}"


def _force_remove_container(name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    )


def run_initialize_in_sandbox(
    root: Path,
    command: str,
    args: list[str],
    protocol_version: int,
    job_id: str,
    stderr_path: Path,
) -> tuple[str, int, int]:
    image = _env("FRISHTA_SANDBOX_IMAGE", required=False) or DEFAULT_SANDBOX_IMAGE
    timeout_seconds = int(_env("FRISHTA_HANDSHAKE_TIMEOUT_SECONDS", required=False) or DEFAULT_TIMEOUT_SECONDS)
    if timeout_seconds < 3 or timeout_seconds > 30:
        raise ValueError("handshake timeout outside safe range")
    inspect = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
    )
    if inspect.returncode != 0:
        raise RuntimeError("sandbox container image is not available")

    name = _container_name(job_id)
    _force_remove_container(name)
    request = (_initialize_request(protocol_version) + "\n").encode("utf-8")
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
        "--workdir", "/agent",
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
            assert process.stdout is not None
            process.stdin.write(request)
            process.stdin.flush()
            process.stdin.close()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(process.stdout.readline, MAX_FRAME_BYTES + 2)
                try:
                    line = future.result(timeout=timeout_seconds)
                except concurrent.futures.TimeoutError as exc:
                    _force_remove_container(name)
                    process.kill()
                    raise TimeoutError("ACP initialize response timed out") from exc
            elapsed_ms = int((time.monotonic() - start) * 1000)
            _force_remove_container(name)
            try:
                return_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                return_code = process.wait(timeout=5)
        finally:
            _force_remove_container(name)
            if process.poll() is None:
                process.kill()
            if process.stdout is not None:
                process.stdout.close()

    if len(line) > MAX_FRAME_BYTES:
        raise ValueError("ACP initialize frame exceeds size limit")
    if not line.endswith(b"\n"):
        raise ValueError("ACP stdio response is not newline-delimited")
    raw = line[:-1].decode("utf-8")
    if "\n" in raw or "\r" in raw:
        raise ValueError("ACP initialize frame contains embedded newline")
    return raw, elapsed_ms, return_code


def validate_initialize_response(raw: str, expected_protocol_version: int, expected_agent_version: str) -> dict:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("ACP initialize response must be an object")
    if payload.get("jsonrpc") != "2.0" or payload.get("id") != 1:
        raise ValueError("ACP initialize response JSON-RPC envelope mismatch")
    if payload.get("error") is not None:
        raise ValueError("ACP initialize returned JSON-RPC error")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("ACP initialize result missing")
    protocol_version = result.get("protocolVersion")
    if str(protocol_version) != str(expected_protocol_version):
        raise ValueError("ACP protocol version mismatch")
    info = result.get("agentInfo") or result.get("info") or {}
    if not isinstance(info, dict):
        info = {}
    returned_version = str(info.get("version") or "").strip()
    if returned_version and returned_version != expected_agent_version:
        raise ValueError("ACP agent version differs from verified registry version")
    return {
        "protocol_version": str(protocol_version),
        "agent_name": str(info.get("name") or info.get("title") or "")[:256],
        "agent_version": returned_version[:128],
    }


def _base_report() -> dict:
    return {
        "schema_version": 1,
        "agent_id": os.environ.get("FRISHTA_AGENT_ID", ""),
        "evidence_id": os.environ.get("GITHUB_RUN_ID", "local") or "local",
        "static_evidence_id": os.environ.get("FRISHTA_STATIC_EVIDENCE_ID", ""),
        "security_verification_job_id": os.environ.get("FRISHTA_SECURITY_VERIFICATION_JOB_ID", ""),
        "version": os.environ.get("FRISHTA_VERSION", ""),
        "artifact_sha256": "",
        "artifact_executed": False,
        "secrets_used": False,
        "network_isolated": True,
        "filesystem_read_only": True,
        "containerized": True,
        "timeout_enforced": True,
        "archive_safe": False,
        "stdout_valid": False,
        "initialize_response_json": "",
        "protocol_version": "",
        "agent_name": "",
        "agent_version": "",
        "handshake_ms": 0,
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
            version=version,
        )

        with tempfile.TemporaryDirectory(prefix="frishta-acp-benchmark-") as tmp:
            temp_dir = Path(tmp)
            artifact = temp_dir / "artifact.bin"
            with httpx.Client(timeout=httpx.Timeout(60.0, read=180.0), trust_env=False) as client:
                actual_sha = download_public_https(source_url, artifact, MAX_DOWNLOAD_BYTES, client)
            report["artifact_sha256"] = actual_sha
            if actual_sha != expected_sha:
                report["blockers"].append("sha256_mismatch")
                return report, 2

            # Repeat the no-exec archive inspection before the execution-specific strict extraction.
            archive_summary = inspect_archive(artifact)
            if not archive_summary.get("safe"):
                report["blockers"].append("archive_not_safe")
                return report, 2

            execution_root = temp_dir / "root"
            extraction = safe_extract_for_execution(artifact, execution_root, command)
            report["archive"] = extraction
            report["archive_safe"] = True
            stderr_path = temp_dir / "agent.stderr"

            # From this point execution is intentionally attempted. Mark it before launching so any
            # post-launch/launch-boundary failure can never be misreported as a no-exec observation.
            report["artifact_executed"] = True
            try:
                raw, handshake_ms, return_code = run_initialize_in_sandbox(
                    root=execution_root,
                    command=command,
                    args=args,
                    protocol_version=protocol_version,
                    job_id=job_id,
                    stderr_path=stderr_path,
                )
                report["handshake_ms"] = handshake_ms
                report["process_return_code_after_termination"] = return_code
                metadata = validate_initialize_response(raw, protocol_version, version)
                report.update(metadata)
                report["initialize_response_json"] = raw
                report["stdout_valid"] = True
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
            and report["network_isolated"]
            and report["filesystem_read_only"]
            and report["containerized"]
            and report["timeout_enforced"]
            and report["archive_safe"]
            and report["stdout_valid"]
            and not report["blockers"]
        )
        return report, 0 if report["passed"] else 2
    except Exception as exc:
        report["passed"] = False
        report["blockers"].append(f"benchmark_error:{type(exc).__name__}:{exc}")
        return report, 2


def main() -> int:
    out_dir = Path(os.environ.get("FRISHTA_BENCHMARK_OUT_DIR", "/tmp/frishta-agent-benchmark"))
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "agent-acp-benchmark.json"
    report, exit_code = run()
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
