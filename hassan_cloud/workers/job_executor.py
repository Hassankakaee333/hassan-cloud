"""Shared job execution — used by thread worker and async dispatcher."""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Callable

from ..agents.pipeline import run_agent_pipeline
from ..runtime.workspace_runtime import SubprocessWorkspaceRuntime

if TYPE_CHECKING:
    from ..storage.files import FileStore
    from ..storage.repository import DatabaseRepository

logger = logging.getLogger("hassan.job_executor")


class JobExecutor:
    def __init__(
        self,
        repo: "DatabaseRepository",
        files: "FileStore",
        new_id: Callable[[], str],
        now_ms: Callable[[], int],
    ) -> None:
        self.repo = repo
        self.files = files
        self.new_id = new_id
        self.now_ms = now_ms
        self.workspace = SubprocessWorkspaceRuntime()

    def process_queued(self, worker_id: str | None = None) -> bool:
        wid = worker_id or f"dispatch-{uuid.uuid4().hex[:8]}"
        self.repo.recover_stale_jobs(self.now_ms())
        job = self.repo.claim_next_queued_job(wid, self.now_ms())
        if not job:
            return False
        self.execute(job, wid)
        return True

    def process_job_id(self, job_id: str, worker_id: str | None = None) -> bool:
        wid = worker_id or f"dispatch-{uuid.uuid4().hex[:8]}"
        job = self.repo.get_job(job_id)
        if not job:
            return False
        if job.get("state") in ("COMPLETED", "FAILED", "CANCELLED"):
            return False
        if job.get("state") == "QUEUED":
            job = self.repo.claim_job_by_id(job_id, wid, self.now_ms())
            if not job:
                return False
        self.execute(job, wid)
        return True

    def _check_cancelled(self, job_id: str) -> bool:
        if self.repo.is_job_cancelled(job_id):
            self.repo.finalize_cancelled(job_id, self.now_ms())
            self.repo.update_job(job_id, "CANCELLED", "[worker] cancelled\n", None, self.now_ms())
            return True
        return False

    def _save_artifact(self, job: dict, name: str, mime: str, data: bytes) -> None:
        aid = self.new_id()
        rel, digest, size = self.files.save(aid, name, data)
        self.repo.create_artifact({
            "id": aid,
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

    def execute(self, job: dict, worker_id: str) -> None:
        job_id = job["id"]
        goal = job["goal"]
        checkpoint = job.get("checkpoint_stage") or ""

        if self._check_cancelled(job_id):
            return

        self.repo.update_job(job_id, "RUNNING", "[worker] started\n", None, self.now_ms())
        self.repo.renew_lease(job_id, worker_id, self.now_ms())

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
            self.repo.renew_lease(job_id, worker_id, self.now_ms())
            try:
                coding_result = self.workspace.run_coding_job(job["project_id"], job_id)
                log_body = (coding_result.get("stdout", "") + "\n" + coding_result.get("stderr", "")).encode("utf-8")
                self._save_artifact(job, "coding-log.txt", "text/plain", log_body)
                if coding_result.get("diff"):
                    self._save_artifact(job, "changes.diff", "text/plain", coding_result["diff"].encode("utf-8"))
                report = {
                    "exit_code": coding_result.get("exit_code"),
                    "tests_passed": coding_result.get("tests_passed"),
                    "stdout": (coding_result.get("stdout") or "")[:2000],
                }
                self._save_artifact(
                    job, "test-report.json", "application/json",
                    json.dumps(report, indent=2).encode("utf-8"),
                )
                zip_bytes = coding_result.get("zip_bytes")
                if zip_bytes:
                    self._save_artifact(job, "workspace.zip", "application/zip", zip_bytes)
            except Exception as exc:
                logger.exception("coding workspace failed job=%s", job_id)
                coding_result = {"exit_code": 1, "stdout": "", "stderr": str(exc), "tests_passed": False}
            self.repo.set_checkpoint(job_id, "coding_done", self.now_ms())

        if self._check_cancelled(job_id):
            return

        if checkpoint != "agents_done":
            self.repo.update_job(job_id, "VERIFYING", "[worker] agent pipeline\n", None, self.now_ms())
            self.repo.renew_lease(job_id, worker_id, self.now_ms())
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
