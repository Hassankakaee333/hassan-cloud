"""Integration tests for async job flow."""

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


def test_health(client: TestClient):
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_auth_rejects_invalid(client: TestClient):
    app.dependency_overrides.clear()
    r = client.post("/v1/jobs", headers={"Authorization": "Bearer bad"}, json={})
    assert r.status_code in (401, 403, 422)


def test_job_async_completes(client: TestClient):
    headers = {"Authorization": "Bearer test-integration-token"}
    proj_resp = client.post("/v1/projects", headers=headers, json={"name": "T", "description": ""})
    assert proj_resp.status_code == 200, proj_resp.text
    proj = proj_resp.json()
    job_resp = client.post(
        "/v1/jobs",
        headers=headers,
        json={"project_id": proj["id"], "goal": "run coding test", "job_type": "coding"},
    )
    assert job_resp.status_code == 200, job_resp.text
    job = job_resp.json()
    assert job["state"] == "QUEUED"
    job_id = job["id"]

    final = None
    for _ in range(40):
        time.sleep(0.5)
        detail = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
        if detail["state"] in ("COMPLETED", "FAILED"):
            final = detail
            break
    assert final is not None, "job did not finish in time"
    assert final["state"] == "COMPLETED", final
    arts = client.get(f"/v1/artifacts?project_id={proj['id']}", headers=headers).json()
    names = {a["name"] for a in arts}
    assert "coding-log.txt" in names
    assert "workspace.zip" in names
    assert "changes.diff" in names
    assert "test-report.json" in names
