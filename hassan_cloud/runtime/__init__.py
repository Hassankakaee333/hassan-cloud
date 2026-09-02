"""Runtime abstractions — swap hosting/infrastructure without changing API routes."""

from __future__ import annotations

from typing import Protocol

from ..storage.files import FileStore
from ..storage.repository import DatabaseRepository


class JobRepository(Protocol):
    def create_job(self, jid: str, project_id: str, conversation_id: str | None, goal: str, job_type: str, ts: int) -> dict: ...
    def claim_next_queued_job(self) -> dict | None: ...
    def update_job(self, job_id: str, state: str, log_append: str, summary: str | None, ts: int) -> None: ...
    def get_job(self, job_id: str) -> dict | None: ...


class ArtifactStore(Protocol):
    def save(self, artifact_id: str, filename: str, data: bytes) -> tuple[str, str, int]: ...
    def read(self, storage_path: str) -> bytes: ...


class WorkerRuntime(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


class WorkspaceRuntime(Protocol):
    """Prototype isolation — subprocess today, container later."""

    def run_coding_job(self, project_id: str, job_id: str) -> dict: ...


# Concrete bindings used by Hassan Cloud today:
DatabaseRepository  # implements JobRepository
FileStore           # implements ArtifactStore
