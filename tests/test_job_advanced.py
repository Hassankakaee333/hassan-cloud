"""Job idempotency, cancellation, and crash recovery tests."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from hassan_cloud.auth.tokens import hash_token
from hassan_cloud.main import app
from hassan_cloud.storage.repository import DatabaseRepository
from hassan_cloud.util import new_id, now_ms


@pytest.fixture()
def client(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("HASSAN_DATA_DIR", str(data))
    monkeypatch.setenv("HASSAN_ENV", "development")

    from hassan_cloud import config

    config.DATA_DIR = data
    config.DB_PATH = data / "hassan_cloud.db"
    config.FILE_DIR = data / "files"
    config.WORKSPACE_DIR = data / "workspaces"
    config.ensure_dirs()

    from hassan_cloud import main as main_mod
    from hassan_cloud.auth.tokens import TokenService
    from hassan_cloud.workers.job_worker import JobWorker

    main_mod.repo = DatabaseRepository(str(config.DB_PATH))
    main_mod.files.root = config.FILE_DIR
    main_mod.repo.init_schema()
    main_mod.token_service = TokenService(main_mod.repo)
    main_mod.repo.insert_token(new_id(), hash_token("test-integration-token"), "test", None, now_ms())
    main_mod.job_worker.stop()
    main_mod.job_worker = JobWorker(main_mod.repo, main_mod.files, new_id, now_ms, poll_seconds=0.5)
    main_mod.job_worker.start()

    def _verify_token() -> str:
        return "test-integration-token"

    app.dependency_overrides[main_mod.verify_token] = _verify_token
    yield TestClient(app)
    app.dependency_overrides.clear()
    main_mod.job_worker.stop()


def _create_project(client: TestClient) -> str:
    headers = {"Authorization": "Bearer test-integration-token"}
    r = client.post("/v1/projects", headers=headers, json={"name": "T", "description": ""})
    assert r.status_code == 200
    return r.json()["id"]


def test_job_idempotency(client: TestClient):
    headers = {"Authorization": "Bearer test-integration-token"}
    pid = _create_project(client)
    body = {
        "project_id": pid,
        "goal": "idempotent goal",
        "job_type": "coding",
        "idempotency_key": "idem-key-1",
    }
    r1 = client.post("/v1/jobs", headers=headers, json=body)
    r2 = client.post("/v1/jobs", headers=headers, json=body)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]


def test_job_cancel_queued(client: TestClient):
    headers = {"Authorization": "Bearer test-integration-token"}
    pid = _create_project(client)
    job = client.post(
        "/v1/jobs",
        headers=headers,
        json={"project_id": pid, "goal": "cancel me", "job_type": "general"},
    ).json()
    cancel = client.post(f"/v1/jobs/{job['id']}/cancel", headers=headers, json={})
    assert cancel.status_code == 200
    detail = client.get(f"/v1/jobs/{job['id']}", headers=headers).json()
    assert detail["state"] == "CANCELLED"


def test_stale_job_recovery(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db_path = data / "test.db"
    repo = DatabaseRepository(str(db_path))
    repo.init_schema()
    pid = new_id()
    ts = now_ms()
    repo.create_project(pid, "P", "", None, ts)
    job = repo.create_job(new_id(), pid, None, "stale", "general", ts)
    job_id = job["id"]
    repo.update_job(job_id, "RUNNING", "started\n", None, ts)
    with repo.connection() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at=?, worker_id='dead-worker' WHERE id=?",
            (ts - 60_000, job_id),
        )
    recovered = repo.recover_stale_jobs(now_ms())
    assert recovered == 1
    loaded = repo.get_job(job_id)
    assert loaded is not None
    assert loaded["state"] == "QUEUED"


def test_job_evidence_artifacts(client: TestClient):
    headers = {"Authorization": "Bearer test-integration-token"}
    pid = _create_project(client)
    job = client.post(
        "/v1/jobs",
        headers=headers,
        json={"project_id": pid, "goal": "run coding test", "job_type": "coding"},
    ).json()
    job_id = job["id"]
    final = None
    for _ in range(40):
        time.sleep(0.5)
        detail = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
        if detail["state"] in ("COMPLETED", "FAILED", "CANCELLED"):
            final = detail
            break
    assert final is not None
    assert final["state"] == "COMPLETED"
    arts = client.get(f"/v1/artifacts?project_id={pid}", headers=headers).json()
    names = {a["name"] for a in arts}
    assert "changes.diff" in names
    assert "test-report.json" in names
