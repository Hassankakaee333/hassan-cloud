"""Manual dispatcher for one-time Frishta ACP task execution.

Only an opaque Hassan Cloud job id is sent to GitHub Actions. The approved prompt/file bundle is
fetched in a separate trusted prepare job, then handed to the secret-free execution job as a private
short-retention Actions artifact.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

WORKFLOW_FILE = "agent-acp-task.yml"
DEFAULT_REPO = "Hassankakaee333/hassan-cloud"


@dataclass(frozen=True)
class AgentExecutionDispatchResult:
    status: str
    job_id: str
    repository: str
    ref: str
    workflow: str = WORKFLOW_FILE
    detail: str = ""


def dispatch_agent_execution(
    job_id: str,
    *,
    client: httpx.Client | None = None,
) -> AgentExecutionDispatchResult:
    if not job_id or len(job_id) > 128:
        raise ValueError("invalid job_id")
    token = os.environ.get("HASSAN_GITHUB_ACTIONS_TOKEN", "").strip()
    repository = os.environ.get("HASSAN_AGENT_EXECUTOR_REPO", DEFAULT_REPO).strip() or DEFAULT_REPO
    ref = os.environ.get("HASSAN_AGENT_EXECUTOR_REF", "main").strip() or "main"
    if not token:
        return AgentExecutionDispatchResult(
            status="NOT_CONFIGURED",
            job_id=job_id,
            repository=repository,
            ref=ref,
            detail="HASSAN_GITHUB_ACTIONS_TOKEN is not configured",
        )
    if repository.count("/") != 1:
        raise ValueError("invalid HASSAN_AGENT_EXECUTOR_REPO")

    payload = {"ref": ref, "inputs": {"job_id": job_id}}
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
                "User-Agent": "Frishta-Agent-Executor-Dispatcher/1",
            },
            json=payload,
        )
        if response.status_code != 204:
            return AgentExecutionDispatchResult(
                status="ERROR",
                job_id=job_id,
                repository=repository,
                ref=ref,
                detail=f"GitHub workflow dispatch HTTP {response.status_code}: {response.text[:500]}",
            )
        return AgentExecutionDispatchResult(
            status="QUEUED",
            job_id=job_id,
            repository=repository,
            ref=ref,
        )
    finally:
        if own_client:
            http.close()
