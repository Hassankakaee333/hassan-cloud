"""Background job worker with lease, recovery, cancellation, checkpoints."""

from __future__ import annotations

import logging
import threading
import uuid
from typing import TYPE_CHECKING

from ..agents.pipeline import run_agent_pipeline
from ..runtime.workspace_runtime import SubprocessWorkspaceRuntime

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
        self.repo = repo
        self.files = files
        self.new_id = new_id
        self.now_ms = now_ms
        self.poll_seconds = poll_seconds
        self.worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        self.workspace = SubprocessWorkspaceRuntime()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        recovered = self.repo.recover_stale_jobs(self.now_ms())
        if recovered:
            logger.info("Recovered %d stale jobs on startup", recovered)
        self._thread = threading.Thread(target=self._loop, name="hassan-job-worker", daemon=True)
        self._thread.start()
        logger.info("Job worker started id=%s", self.worker_id)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.repo.recover_stale_jobs(self.now_ms())
                job = self.repo.claim_next_queued_job(self.worker_id, self.now_ms())
                if job:
                    self._execute(job)
            except Exception:
                logger.exception("Job worker error")
            self._stop.wait(self.poll_seconds)

    def _check_cancelled(self, job_id: str) -> bool:
        if self.repo.is_job_cancelled(job_id):
            self.repo.finalize_cancelled(job_id, self.now_ms())
            self.repo.update_job(job_id, "CANCELLED", "[worker] cancelled\n", None, self.now_ms())
            return True
        return False

    def _save_artifact(self, job: dict, name: str, mime: str, data: bytes) -> None:
        rel, digest, size = self.files.save(self.new_id(), name, data)
        self.repo.create_artifact({
            "id": self.new_id(),
            "project_id": job["project_id"],
            "job_id": job["id"],
            "conversation_id": job.get("conversation_id"),
            "name": name,
            "mime_type": mime,
            "size_bytes": size,
            "storage_path": rel,
            "sha256": digest,
            "created_at": self.now_ms(),
        })

    def _execute(self, job: dict) -> None:
        job_id = job["id"]
        goal = job["goal"]
        checkpoint = job.get("checkpoint_stage") or ""

        if self._check_cancelled(job_id):
            return

        self.repo.update_job(job_id, "RUNNING", "[worker] started\n", None, self.now_ms())
        self.repo.renew_lease(job_id, self.worker_id, self.now_ms())

        coding_result = None
        needs_coding = (
            job.get("job_type") in ("coding", "general")
            or "code" in goal.lower()
            or "اختبار" in goal
            or "test" in goal.lower()
        )
        if needs_coding and checkpoint not in ("coding_done", "agents_done"):
            if self._check_cancelled(job_id):
                return
            self.repo.update_job(job_id, "CODING", "[worker] coding workspace\n", None, self.now_ms())
            self.repo.renew_lease(job_id, self.worker_id, self.now_ms())
            try:
                coding_result = self.workspace.run_coding_job(job["project_id"], job_id)
                log_body = (coding_result.get("stdout", "") + "\n" + coding_result.get("stderr", "")).encode("utf-8")
                self._save_artifact(job, "coding-log.txt", "text/plain", log_body)
                if coding_result.get("diff"):
                    self._save_artifact(job, "changes.diff", "text/plain", coding_result["diff"].encode("utf-8"))
                import json
                report = {
                    "exit_code": coding_result.get("exit_code"),
                    "tests_passed": coding_result.get("tests_passed"),
                    "stdout": (coding_result.get("stdout") or "")[:2000],
                }
                self._save_artifact(job, "test-report.json", "application/json", json.dumps(report, indent=2).encode())
                zip_bytes = coding_result.get("zip_bytes")
                if zip_bytes:
                    self._save_artifact(job, "workspace.zip", "application/zip", zip_bytes)
            except Exception as exc:
                coding_result = {"exit_code": 1, "stdout": "", "stderr": str(exc), "tests_passed": False}
            self.repo.set_checkpoint(job_id, "coding_done", self.now_ms())

        if self._check_cancelled(job_id):
            return

        if checkpoint != "agents_done":
            self.repo.update_job(job_id, "VERIFYING", "[worker] agent pipeline\n", None, self.now_ms())
            self.repo.renew_lease(job_id, self.worker_id, self.now_ms())
            final_state, summary = run_agent_pipeline(
                self.repo, job_id, goal, coding_result, self.new_id, self.now_ms,
            )
            self.repo.set_checkpoint(job_id, "agents_done", self.now_ms())
        else:
            final_state = job.get("state", "COMPLETED")
            summary = job.get("result_summary") or ""

        if self._check_cancelled(job_id):
            return

        self.repo.update_job(
            job_id, final_state, f"[worker] done state={final_state}\n", summary, self.now_ms(),
        )
