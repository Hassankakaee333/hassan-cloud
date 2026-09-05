"""One-time, read-only ACP v1 task runner for Frishta P2.9.

The runner receives an already-approved private bundle from the trusted prepare job. It verifies the
permit, goal hash and every file hash again, then launches the exact Shadow-tested Binary in a
network-disabled, read-only container. It sends initialize -> session/new -> exactly one
session/prompt. Only session/update notifications are accepted; every Agent->Client request is a
blocker and is never serviced.
"""

from __future__ import annotations

import base64
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
    DEFAULT_SANDBOX_IMAGE,
    MAX_DOWNLOAD_BYTES,
    MAX_FRAME_BYTES,
    MAX_STDERR_BYTES,
    _force_remove_container,
    _initialize_request,
    _safe_command,
    download_public_https,
    inspect_archive,
    safe_extract_for_execution,
    validate_initialize_response,
)
from agent_acp_shadow_runner import _auth_methods_count, _session_new_request, _validate_session_new_response

MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_FILE_BYTES = 1024 * 1024
MAX_UPDATES_BYTES = 256 * 1024
MAX_UPDATE_FRAMES = 256
DEFAULT_TIMEOUT_SECONDS = 90
PERMIT_POLICY_ID = "frishta-agent-task-permit-v3"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_OPAQUE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def _env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise ValueError(f"{name} is required")
    return value


def _field(name: str, value: str) -> str:
    return f"{name}:{len(value.encode('utf-8'))}:{value}\n"


def _canonical_path(path: str) -> str:
    if not path or len(path) > 512 or any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        raise ValueError("unsafe file path")
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        raise ValueError("file path must be relative")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("file path escapes workspace")
    return "/".join(parts)


def _decode_files(bundle: dict) -> list[tuple[str, str, bytes]]:
    raw_files = bundle.get("files") or []
    if not isinstance(raw_files, list) or len(raw_files) > 64:
        raise ValueError("invalid file list")
    seen: set[str] = set()
    result: list[tuple[str, str, bytes]] = []
    total = 0
    for item in raw_files:
        if not isinstance(item, dict):
            raise ValueError("invalid file entry")
        path = _canonical_path(str(item.get("path") or ""))
        if path in seen:
            raise ValueError("duplicate file path")
        seen.add(path)
        digest = str(item.get("sha256") or "").lower()
        if not _SHA256.fullmatch(digest):
            raise ValueError("invalid file SHA-256")
        try:
            data = base64.b64decode(str(item.get("content_base64") or ""), validate=True)
        except Exception as exc:
            raise ValueError("invalid file base64") from exc
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("file exceeds size limit")
        total += len(data)
        if total > MAX_TOTAL_FILE_BYTES:
            raise ValueError("total file payload exceeds size limit")
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("file content SHA-256 mismatch")
        result.append((path, digest, data))
    return sorted(result, key=lambda value: value[0])


def _expected_permit_id(bundle: dict, files: list[tuple[str, str, bytes]]) -> str:
    canonical = "".join((
        _field("policy", PERMIT_POLICY_ID),
        _field("approval_nonce", str(bundle.get("approval_nonce") or "")),
        _field("agent", str(bundle.get("agent_id") or "")),
        _field("version", str(bundle.get("version") or "")),
        _field("task", str(bundle.get("task_id") or "")),
        _field("goal_sha256", str(bundle.get("goal_sha256") or "").lower()),
        _field("approval", str(bundle.get("approval_evidence_id") or "")),
        _field("comparison", str(bundle.get("comparison_evidence_id") or "")),
    ))
    for path, digest, _data in files:
        canonical += _field("file", path) + _field("file_sha256", digest)
    actions = bundle.get("actions") or []
    for action in sorted(set(str(item) for item in actions)):
        canonical += _field("action", action)
    return "agent-task-permit-sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_bundle(bundle: dict) -> list[tuple[str, str, bytes]]:
    if int(bundle.get("protocol_version") or 0) != 1:
        raise ValueError("P2.9 supports ACP v1 only")
    if bundle.get("targets_stable_directly") is not False:
        raise ValueError("Stable-direct execution forbidden")
    if str(bundle.get("cost_class") or "").upper() != "FREE" or int(bundle.get("additional_spend_cents") or 0) != 0:
        raise ValueError("paid execution forbidden")
    actions = sorted(set(str(item) for item in (bundle.get("actions") or [])))
    if actions != ["READ_FILES"]:
        raise ValueError("P2.9 supports READ_FILES only")
    execution_request_id = str(bundle.get("execution_request_id") or "")
    approval_nonce = str(bundle.get("approval_nonce") or "")
    if not _OPAQUE.fullmatch(execution_request_id) or not _OPAQUE.fullmatch(approval_nonce):
        raise ValueError("invalid one-time identifiers")
    goal = str(bundle.get("goal") or "")
    goal_sha = str(bundle.get("goal_sha256") or "").lower()
    if not goal or len(goal) > 8000 or not _SHA256.fullmatch(goal_sha):
        raise ValueError("invalid goal")
    if hashlib.sha256(goal.encode("utf-8")).hexdigest() != goal_sha:
        raise ValueError("goal SHA-256 mismatch")
    files = _decode_files(bundle)
    expected = _expected_permit_id(bundle, files)
    if expected != str(bundle.get("permit_id") or ""):
        raise ValueError("permit id mismatch")
    return files


def _read_frame(process: subprocess.Popen, timeout_seconds: int) -> str:
    assert process.stdout is not None
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(process.stdout.readline, MAX_FRAME_BYTES + 2)
        try:
            line = future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError as exc:
            raise TimeoutError("ACP response timed out") from exc
    if not line:
        raise ValueError("ACP subprocess closed stdout")
    if len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
        raise ValueError("invalid ACP newline frame")
    raw = line[:-1].decode("utf-8")
    if "\n" in raw or "\r" in raw:
        raise ValueError("ACP frame contains embedded newline")
    return raw


def _prompt_request(session_id: str, goal: str) -> str:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "session/prompt",
        "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": goal}]},
    }, separators=(",", ":"), ensure_ascii=False)


def _read_prompt_turn(process: subprocess.Popen, timeout_seconds: int) -> tuple[str, str, int, str]:
    updates: list[str] = []
    total = 0
    client_requests = 0
    deadline = time.monotonic() + timeout_seconds
    for _index in range(MAX_UPDATE_FRAMES + 1):
        remaining = int(max(1, deadline - time.monotonic()))
        if remaining <= 0:
            raise TimeoutError("ACP prompt turn timed out")
        raw = _read_frame(process, remaining)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            raise ValueError("invalid ACP prompt frame")
        if payload.get("id") == 3 and payload.get("method") is None:
            if payload.get("error") is not None:
                raise ValueError("ACP session/prompt returned error")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise ValueError("ACP session/prompt result missing")
            stop_reason = str(result.get("stopReason") or "").strip()
            if not stop_reason:
                raise ValueError("ACP session/prompt stopReason missing")
            return raw, "\n".join(updates), client_requests, stop_reason
        if payload.get("method") == "session/update" and payload.get("id") is None:
            encoded = raw.encode("utf-8")
            total += len(encoded) + 1
            if total > MAX_UPDATES_BYTES:
                raise ValueError("ACP session updates exceed capture limit")
            updates.append(raw)
            continue
        if payload.get("method") is not None:
            client_requests += 1
            raise ValueError(f"Agent->Client request/notification forbidden: {str(payload.get('method'))[:80]}")
        raise ValueError("unexpected ACP frame before prompt response")
    raise ValueError("too many ACP update frames")


def _write_workspace(root: Path, files: list[tuple[str, str, bytes]]) -> None:
    root.mkdir(mode=0o755)
    for rel, _digest, data in files:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o444)
    directories = sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda p: len(p.parts), reverse=True)
    for directory in directories:
        directory.chmod(0o555)
    root.chmod(0o555)


def run_task_in_sandbox(
    root: Path,
    workspace: Path,
    command: str,
    args: list[str],
    version: str,
    goal: str,
    job_id: str,
    stderr_path: Path,
) -> tuple[str, str, str, str, int, int]:
    image = _env("FRISHTA_SANDBOX_IMAGE", required=False) or DEFAULT_SANDBOX_IMAGE
    timeout_seconds = int(_env("FRISHTA_TASK_TIMEOUT_SECONDS", required=False) or DEFAULT_TIMEOUT_SECONDS)
    if timeout_seconds < 10 or timeout_seconds > 180:
        raise ValueError("task timeout outside safe range")
    inspect = subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    if inspect.returncode != 0:
        raise RuntimeError("sandbox image unavailable")
    safe_job = re.sub(r"[^A-Za-z0-9_.-]", "-", job_id)[:80]
    name = f"frishta-acp-task-{safe_job or 'job'}"
    _force_remove_container(name)
    docker_command = [
        "docker", "run", "--name", name, "--rm", "-i",
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--pids-limit", "64",
        "--memory", "512m", "--cpus", "1", "--user", "65534:65534",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=32m",
        "--env", "HOME=/tmp", "--env", "LANG=C.UTF-8", "--env", "NO_COLOR=1",
        "--volume", f"{root.resolve()}:/agent:ro",
        "--volume", f"{workspace.resolve()}:/workspace:ro",
        "--workdir", "/workspace",
        image, f"/agent/{command}", *args,
    ]
    start = time.monotonic()
    with stderr_path.open("wb") as stderr_handle:
        process = subprocess.Popen(docker_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_handle)
        try:
            assert process.stdin is not None
            process.stdin.write((_initialize_request(1) + "\n").encode("utf-8")); process.stdin.flush()
            initialize_raw = _read_frame(process, 15)
            validate_initialize_response(initialize_raw, 1, version)
            if _auth_methods_count(initialize_raw) != 0:
                raise ValueError("Agent advertises auth methods; P2.9 forbids authentication")
            process.stdin.write((_session_new_request() + "\n").encode("utf-8")); process.stdin.flush()
            session_raw = _read_frame(process, 15)
            session = _validate_session_new_response(session_raw)
            process.stdin.write((_prompt_request(session["session_id"], goal) + "\n").encode("utf-8")); process.stdin.flush()
            prompt_raw, updates_jsonl, client_requests, stop_reason = _read_prompt_turn(process, timeout_seconds)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            process.stdin.close()
            return initialize_raw, session_raw, prompt_raw, updates_jsonl, client_requests, elapsed_ms, stop_reason
        finally:
            _force_remove_container(name)
            if process.poll() is None:
                process.kill()
            if process.stdout is not None:
                process.stdout.close()


def _base_report(bundle: dict) -> dict:
    return {
        "schema_version": 1,
        "permit_id": str(bundle.get("permit_id") or ""),
        "execution_request_id": str(bundle.get("execution_request_id") or ""),
        "agent_id": str(bundle.get("agent_id") or ""),
        "version": str(bundle.get("version") or ""),
        "task_id": str(bundle.get("task_id") or ""),
        "goal_sha256": str(bundle.get("goal_sha256") or ""),
        "approval_evidence_id": str(bundle.get("approval_evidence_id") or ""),
        "comparison_evidence_id": str(bundle.get("comparison_evidence_id") or ""),
        "static_evidence_id": str(bundle.get("static_evidence_id") or ""),
        "security_verification_job_id": str(bundle.get("security_verification_job_id") or ""),
        "benchmark_job_id": str(bundle.get("benchmark_job_id") or ""),
        "shadow_job_id": str(bundle.get("shadow_job_id") or ""),
        "source_url": str(bundle.get("source_url") or ""),
        "artifact_sha256": "",
        "command": str(bundle.get("command") or ""),
        "args": list(bundle.get("args") or []),
        "actions": list(bundle.get("actions") or []),
        "permit_verified": False, "files_verified": False, "file_count": 0,
        "artifact_executed": False, "secrets_used": False, "auth_attempted": False,
        "prompt_sent": False, "agent_client_requests": 0,
        "network_isolated": True, "filesystem_read_only": True, "containerized": True,
        "timeout_enforced": True, "archive_safe": False, "session_created": False,
        "session_id": "", "prompt_response_json": "", "stop_reason": "",
        "session_updates_jsonl": "", "updates_count": 0, "updates_sha256": "",
        "execution_ms": 0, "stderr_bytes": 0, "stderr_sha256": "",
        "passed": False, "blockers": [], "warnings": [],
    }


def run() -> tuple[dict, int]:
    bundle_path = Path(_env("FRISHTA_EXECUTION_BUNDLE"))
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    report = _base_report(bundle)
    try:
        files = validate_bundle(bundle)
        report["permit_verified"] = True
        report["files_verified"] = True
        report["file_count"] = len(files)
        expected_sha = str(bundle.get("expected_sha256") or "").lower()
        if not _SHA256.fullmatch(expected_sha):
            raise ValueError("invalid Agent binary SHA-256")
        command = _safe_command(str(bundle.get("command") or ""))
        args = [str(item) for item in (bundle.get("args") or [])]
        if len(args) > 24:
            raise ValueError("too many Agent args")
        source_url = str(bundle.get("source_url") or "")
        if not source_url.startswith("https://"):
            raise ValueError("Agent source URL must use HTTPS")
        job_id = _env("FRISHTA_JOB_ID")

        with tempfile.TemporaryDirectory(prefix="frishta-acp-task-") as tmp:
            temp = Path(tmp)
            artifact = temp / "artifact.bin"
            with httpx.Client(timeout=httpx.Timeout(60.0, read=180.0), trust_env=False) as client:
                actual_sha = download_public_https(source_url, artifact, MAX_DOWNLOAD_BYTES, client)
            report["artifact_sha256"] = actual_sha
            if actual_sha != expected_sha:
                raise ValueError("Agent binary SHA-256 mismatch")
            summary = inspect_archive(artifact)
            if not summary.get("safe"):
                raise ValueError("Agent archive is not safe")
            execution_root = temp / "agent-root"
            safe_extract_for_execution(artifact, execution_root, command)
            report["archive_safe"] = True
            workspace = temp / "workspace"
            _write_workspace(workspace, files)
            stderr_path = temp / "agent.stderr"
            report["artifact_executed"] = True
            report["prompt_sent"] = True
            try:
                init_raw, session_raw, prompt_raw, updates, client_requests, elapsed_ms, stop_reason = run_task_in_sandbox(
                    execution_root, workspace, command, args, str(bundle.get("version") or ""),
                    str(bundle.get("goal") or ""), job_id, stderr_path,
                )
                session = _validate_session_new_response(session_raw)
                report["session_created"] = True
                report["session_id"] = session["session_id"]
                report["prompt_response_json"] = prompt_raw
                report["stop_reason"] = stop_reason
                report["session_updates_jsonl"] = updates
                report["updates_count"] = 0 if not updates else len(updates.splitlines())
                report["updates_sha256"] = hashlib.sha256(updates.encode("utf-8")).hexdigest()
                report["agent_client_requests"] = client_requests
                report["execution_ms"] = elapsed_ms
                report["initialize_response_sha256"] = hashlib.sha256(init_raw.encode("utf-8")).hexdigest()
            finally:
                if stderr_path.exists():
                    raw = stderr_path.read_bytes()[:MAX_STDERR_BYTES]
                    report["stderr_bytes"] = stderr_path.stat().st_size
                    report["stderr_sha256"] = hashlib.sha256(raw).hexdigest()
                    if stderr_path.stat().st_size > MAX_STDERR_BYTES:
                        report["warnings"].append("stderr_truncated_for_hash")

        report["passed"] = all((
            report["permit_verified"], report["files_verified"], report["artifact_executed"],
            report["prompt_sent"], report["network_isolated"], report["filesystem_read_only"],
            report["containerized"], report["timeout_enforced"], report["archive_safe"],
            report["session_created"], not report["secrets_used"], not report["auth_attempted"],
            report["agent_client_requests"] == 0, bool(report["prompt_response_json"]), bool(report["stop_reason"]),
            not report["blockers"],
        ))
        return report, 0 if report["passed"] else 2
    except Exception as exc:
        report["passed"] = False
        report["blockers"].append(f"task_error:{type(exc).__name__}:{exc}")
        return report, 2


def main() -> int:
    out_dir = Path(os.environ.get("FRISHTA_TASK_OUT_DIR", "/tmp/frishta-agent-task"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "agent-acp-task-execution.json"
    report, code = run()
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
