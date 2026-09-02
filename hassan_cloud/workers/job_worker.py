"""Background polling worker — local dev / persistent server mode."""

from __future__ import annotations

import logging
import threading
import uuid
from typing import TYPE_CHECKING

from .job_executor import JobExecutor

if TYPE_CHECKING:
    from ..storage.files import FileStore
    from ..storage.repository import DatabaseRepository

logger = logging.getLogger("hassan.job_worker")


class JobWorker:
    def __init__(
        self,
        repo: "DatabaseRepository",
        files: "FileStore",
        new_id,
        now_ms,
        poll_seconds: float = 2.0,
    ) -> None:
        self.executor = JobExecutor(repo, files, new_id, now_ms)
        self.now_ms = now_ms
        self.poll_seconds = poll_seconds
        self.worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.executor.repo.recover_stale_jobs(self.now_ms())
        self._thread = threading.Thread(target=self._loop, name="hassan-job-worker", daemon=True)
        self._thread.start()
        logger.info("Job worker started id=%s", self.worker_id)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.executor.process_queued(self.worker_id)
            except Exception:
                logger.exception("Job worker error")
            self._stop.wait(self.poll_seconds)
