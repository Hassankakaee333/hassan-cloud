"""Workspace runtime — subprocess prototype with container hook for later."""

from __future__ import annotations

from ..coding.workspace import run_coding_workspace


class SubprocessWorkspaceRuntime:
    """MVP: subprocess + temp dir. NOT a secure production sandbox."""

    def run_coding_job(self, project_id: str, job_id: str) -> dict:
        return run_coding_workspace(project_id, job_id)


class ContainerWorkspaceRuntime:
    """Placeholder for future Docker/container isolation."""

    def run_coding_job(self, project_id: str, job_id: str) -> dict:
        raise NotImplementedError("Container runtime not configured on this host")
