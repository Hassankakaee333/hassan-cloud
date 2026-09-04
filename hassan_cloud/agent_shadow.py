"""Manual dispatcher for Frishta's post-benchmark ACP shadow session test.

The shadow path never routes through Codex/providers and never authenticates. It launches the exact
verified Binary in a no-network sandbox, sends initialize + one empty-workspace session/new, then
terminates the process without sending a prompt or granting any Agent/client request.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import httpx

WORKFLOW_FILE = "agent-acp-shadow.yml"
DEFAULT_REPO = "Hassankakaee333/hassan-cloud"
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:[/\\]")


@dataclass(frozen=True)
class AgentShadowSpec:
    agent_id: str
    static_evidence_id: str
    security_verification_job_id: str
    benchmark_job_id: str
    version: str
    source_url: str
    expected_sha256: str
    command: str
    args: tuple[str, ...] = ()
    protocol_version: int = 1

    def validate(self) -> None:
        if not _AGENT_ID.fullmatch(self.agent_id):
            raise ValueError("invalid agent_id")
        for value, name, limit in (
            (self.static_evidence_id, "static_evidence_id", 256),
            (self.security_verification_job_id, "security_verification_job_id", 128),
            (self.benchmark_job_id, "benchmark_job_id", 128),
        ):
            if not value or len(value) > limit:
                raise ValueError(f"invalid {name}")
        if not self.version or len(self.version) > 128 or any(ch.isspace() for ch in self.version):
            raise ValueError("invalid version")
        if not self.source_url.startswith("https://") or len(self.source_url) > 2048:
            raise ValueError("binary source_url must use HTTPS")
        if not _SHA256.fullmatch(self.expected_sha256):
            raise ValueError("binary expected_sha256 required")
        _validate_relative_command(self.command)
        if len(self.args) > 24:
            raise ValueError("too many Agent arguments")
        for arg in self.args:
            if len(arg) > 512:
                raise ValueError("Agent argument too long")
            if any(ord(ch) == 0 or (ord(ch) < 32 and ch != "\t") for ch in arg):
                raise ValueError("Agent argument contains control characters")
        if self.protocol_version < 1 or self.protocol_version > 65535:
            raise ValueError("invalid ACP protocol_version")
        if len(json.dumps(list(self.args), separators=(",", ":"))) > 4096:
            raise ValueError("Agent arguments payload too large")


def _validate_relative_command(command: str) -> str:
    normalized = command.strip().replace("\\", "/")
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


@dataclass(frozen=True)
class ShadowDispatchResult:
    status: str
    job_id: str
    repository: str
    ref: str
    workflow: str = WORKFLOW_FILE
    detail: str = ""


def dispatch_agent_shadow(
    job_id: str,
    spec: AgentShadowSpec,
    *,
    client: httpx.Client | None = None,
) -> ShadowDispatchResult:
    spec.validate()
    if not job_id or len(job_id) > 128:
        raise ValueError("invalid job_id")

    token = os.environ.get("HASSAN_GITHUB_ACTIONS_TOKEN", "").strip()
    repository = os.environ.get("HASSAN_AGENT_SHADOW_REPO", DEFAULT_REPO).strip() or DEFAULT_REPO
    ref = os.environ.get("HASSAN_AGENT_SHADOW_REF", "main").strip() or "main"
    if not token:
        return ShadowDispatchResult(
            status="NOT_CONFIGURED",
            job_id=job_id,
            repository=repository,
            ref=ref,
            detail="HASSAN_GITHUB_ACTIONS_TOKEN is not configured",
        )
    if repository.count("/") != 1:
        raise ValueError("invalid HASSAN_AGENT_SHADOW_REPO")

    payload = {
        "ref": ref,
        "inputs": {
            "job_id": job_id,
            "agent_id": spec.agent_id,
            "static_evidence_id": spec.static_evidence_id,
            "security_verification_job_id": spec.security_verification_job_id,
            "benchmark_job_id": spec.benchmark_job_id,
            "version": spec.version,
            "source_url": spec.source_url,
            "expected_sha256": spec.expected_sha256.lower(),
            "command": _validate_relative_command(spec.command),
            "args_json": json.dumps(list(spec.args), separators=(",", ":")),
            "protocol_version": str(spec.protocol_version),
        },
    }
    url = f"https://api.github.com/repos/{repository}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    own_client = client is None
    http = client or httpx.Client(timeout=30.0, trust_env=False)
    try:
        response = http.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Frishta-Agent-Shadow-Dispatcher/1",
            },
            json=payload,
        )
        if response.status_code != 204:
            return ShadowDispatchResult(
                status="ERROR",
                job_id=job_id,
                repository=repository,
                ref=ref,
                detail=f"GitHub workflow dispatch HTTP {response.status_code}: {response.text[:500]}",
            )
        return ShadowDispatchResult(
            status="QUEUED",
            job_id=job_id,
            repository=repository,
            ref=ref,
        )
    finally:
        if own_client:
            http.close()
