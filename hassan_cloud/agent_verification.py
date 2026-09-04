"""Structured dispatcher for Frishta Agent artifact verification.

This module only dispatches the dedicated no-exec GitHub Actions workflow. It never routes through
Codex, provider chat, or the general Agent pipeline.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import httpx

WORKFLOW_FILE = "agent-artifact-verify.yml"
DEFAULT_REPO = "Hassankakaee333/hassan-cloud"
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")


@dataclass(frozen=True)
class AgentVerificationSpec:
    agent_id: str
    static_evidence_id: str
    distribution_kind: str
    version: str
    source_url: str = ""
    expected_sha256: str = ""
    package: str = ""

    def validate(self) -> None:
        if not _AGENT_ID.fullmatch(self.agent_id):
            raise ValueError("invalid agent_id")
        if not self.static_evidence_id or len(self.static_evidence_id) > 256:
            raise ValueError("invalid static_evidence_id")
        if not self.version or len(self.version) > 128 or any(ch.isspace() for ch in self.version):
            raise ValueError("invalid version")
        kind = self.distribution_kind.lower()
        if kind not in {"binary", "npx", "uvx"}:
            raise ValueError("distribution_kind must be binary, npx, or uvx")
        if kind == "binary":
            if not self.source_url.startswith("https://"):
                raise ValueError("binary source_url must use HTTPS")
            if not _SHA256.fullmatch(self.expected_sha256):
                raise ValueError("binary expected_sha256 required")
            if self.package:
                raise ValueError("binary verification must not include package")
        else:
            if not self.package or len(self.package) > 256:
                raise ValueError("package required")
            if self.source_url or self.expected_sha256:
                raise ValueError("package verification must resolve source/integrity from registry metadata")


@dataclass(frozen=True)
class DispatchResult:
    status: str
    job_id: str
    repository: str
    ref: str
    workflow: str = WORKFLOW_FILE
    detail: str = ""


def dispatch_agent_verification(
    job_id: str,
    spec: AgentVerificationSpec,
    *,
    client: httpx.Client | None = None,
) -> DispatchResult:
    spec.validate()
    if not job_id or len(job_id) > 128:
        raise ValueError("invalid job_id")

    token = os.environ.get("HASSAN_GITHUB_ACTIONS_TOKEN", "").strip()
    repository = os.environ.get("HASSAN_AGENT_VERIFY_REPO", DEFAULT_REPO).strip() or DEFAULT_REPO
    ref = os.environ.get("HASSAN_AGENT_VERIFY_REF", "main").strip() or "main"
    if not token:
        return DispatchResult(
            status="NOT_CONFIGURED",
            job_id=job_id,
            repository=repository,
            ref=ref,
            detail="HASSAN_GITHUB_ACTIONS_TOKEN is not configured",
        )
    if repository.count("/") != 1:
        raise ValueError("invalid HASSAN_AGENT_VERIFY_REPO")

    payload = {
        "ref": ref,
        "inputs": {
            "job_id": job_id,
            "agent_id": spec.agent_id,
            "static_evidence_id": spec.static_evidence_id,
            "distribution_kind": spec.distribution_kind.lower(),
            "version": spec.version,
            "source_url": spec.source_url,
            "expected_sha256": spec.expected_sha256.lower(),
            "package": spec.package,
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
                "User-Agent": "Frishta-Agent-Verification-Dispatcher/1",
            },
            json=payload,
        )
        if response.status_code != 204:
            detail = response.text[:500]
            return DispatchResult(
                status="ERROR",
                job_id=job_id,
                repository=repository,
                ref=ref,
                detail=f"GitHub workflow dispatch HTTP {response.status_code}: {detail}",
            )
        return DispatchResult(
            status="QUEUED",
            job_id=job_id,
            repository=repository,
            ref=ref,
        )
    finally:
        if own_client:
            http.close()
