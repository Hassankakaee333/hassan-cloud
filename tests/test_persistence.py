"""Persistence tests — jobs survive repository restart."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hassan_cloud.storage.repository import DatabaseRepository
from hassan_cloud.util import new_id, now_ms


def test_job_survives_restart() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        repo1 = DatabaseRepository(str(db_path))
        repo1.init_schema()

        pid = new_id()
        ts = now_ms()
        repo1.create_project(pid, "Test Project", "desc", None, ts)
        job = repo1.create_job(new_id(), pid, None, "test goal", "general", ts)
        job_id = job["id"]
        assert job["state"] == "QUEUED"

        repo2 = DatabaseRepository(str(db_path))
        loaded = repo2.get_job(job_id)
        assert loaded is not None
        assert loaded["goal"] == "test goal"
        assert loaded["state"] == "QUEUED"

        repo2.update_job(job_id, "COMPLETED", "done\n", "summary", now_ms())
        repo3 = DatabaseRepository(str(db_path))
        final = repo3.get_job(job_id)
        assert final is not None
        assert final["state"] == "COMPLETED"
        assert "done" in final["log"]


def test_token_hash_persistence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "tokens.db"
        repo = DatabaseRepository(str(db_path))
        repo.init_schema()
        from hassan_cloud.auth.tokens import hash_token

        raw = "test-secret-token-value"
        repo.insert_token(new_id(), hash_token(raw), "test", None, now_ms())
        repo2 = DatabaseRepository(str(db_path))
        assert repo2.is_token_valid(hash_token(raw))
        assert not repo2.is_token_valid(hash_token("wrong"))
