"""Contract guard for the shared FastAPI and Cloudflare Worker surface."""

from __future__ import annotations

import re
from pathlib import Path

from hassan_cloud.main import app


def _normalize(path: str) -> str:
    path = re.sub(r"\{[^}]+\}", "{}", path)
    return re.sub(r":([^/]+)", "{}", path)


def test_fastapi_and_worker_share_mobile_api_contract() -> None:
    fastapi_routes = {
        (method, _normalize(route.path))
        for route in app.routes
        for method in (route.methods or set())
        if route.path.startswith("/v1/")
    }

    worker_source = (Path(__file__).parents[1] / "worker" / "src" / "index.ts").read_text(encoding="utf-8")
    worker_routes = {
        (method.upper(), _normalize(path))
        for method, path in re.findall(r'app\.(get|post|delete)\("([^"]+)"', worker_source)
        if path.startswith("/v1/") and "/internal/" not in path
    }

    required = {
        ("GET", "/v1/health"),
        ("POST", "/v1/auth/verify"),
        ("POST", "/v1/auth/tokens"),
        ("DELETE", "/v1/auth/tokens/{}"),
        ("GET", "/v1/projects"),
        ("POST", "/v1/projects"),
        ("GET", "/v1/projects/{}"),
        ("GET", "/v1/projects/{}/workspace"),
        ("GET", "/v1/jobs"),
        ("POST", "/v1/jobs"),
        ("GET", "/v1/jobs/{}"),
        ("POST", "/v1/jobs/{}/cancel"),
        ("GET", "/v1/artifacts"),
        ("GET", "/v1/files/{}"),
        ("POST", "/v1/chat"),
        ("GET", "/v1/providers"),
        ("GET", "/v1/capabilities/{}"),
        ("POST", "/v1/radar/scan"),
        ("GET", "/v1/radar/candidates"),
        ("POST", "/v1/radar/candidates/{}/evaluate"),
    }

    assert required <= fastapi_routes
    assert required <= worker_routes


def test_cloud_runner_registers_durable_github_artifacts_after_upload() -> None:
    root = Path(__file__).parents[1]
    runner = (root / "scripts" / "github_job_runner.py").read_text(encoding="utf-8")
    workflow = (root / ".github" / "workflows" / "hassan-job.yml").read_text(encoding="utf-8")

    assert '"storage_backend": "GITHUB_ACTIONS"' in runner
    assert '"storage_backend": "INLINE_POC"' not in runner
    assert workflow.index("Upload workspace artifact") < workflow.index("Register durable artifact metadata")
