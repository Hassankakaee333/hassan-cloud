"""Async job dispatcher — enqueue without blocking HTTP (serverless-friendly)."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from .job_executor import JobExecutor

if TYPE_CHECKING:
    from ..storage.files import FileStore
    from ..storage.repository import DatabaseRepository

logger = logging.getLogger("hassan.dispatcher")

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hassan-job")


class JobDispatcher:
    def __init__(self, repo: "DatabaseRepository", files: "FileStore", new_id, now_ms) -> None:
        self.executor = JobExecutor(repo, files, new_id, now_ms)

    def enqueue(self, job_id: str) -> None:
        _pool.submit(self._run, job_id)

    def drain(self) -> int:
        count = 0
        while self.executor.process_queued():
            count += 1
        return count

    def _run(self, job_id: str) -> None:
        try:
            if not self.executor.process_job_id(job_id):
                self.executor.process_queued()
        except Exception:
            logger.exception("dispatch failed job=%s", job_id)

    def shutdown(self) -> None:
        pass
