"""Persistent Hassan workspace transfer helpers for GitHub Actions."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx

API_URL = os.environ.get("HASSAN_API_URL", "").rstrip("/")
CALLBACK_SECRET = os.environ.get("HASSAN_CALLBACK_SECRET", "")
MAX_FILE_BYTES = 512 * 1024
MAX_WORKSPACE_BYTES = 5 * 1024 * 1024
MAX_FILES = 300
IGNORED_PARTS = {".git", ".gradle", "build", "__pycache__", ".pytest_cache", ".idea", ".cxx"}


def _headers() -> dict[str, str]:
    if not API_URL or not CALLBACK_SECRET:
        raise RuntimeError("HASSAN_API_URL and HASSAN_CALLBACK_SECRET required")
    return {"X-Hassan-Callback-Secret": CALLBACK_SECRET}


def _safe_relative(path: str) -> Path:
    candidate = Path(path.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ValueError(f"unsafe workspace path: {path}")
    return candidate


def fetch_workspace_optional(project_id: str, root: Path) -> set[str]:
    """Load persistent workspace files; empty workspace is allowed for first create."""
    response = httpx.get(
        f"{API_URL}/v1/internal/projects/{project_id}/workspace",
        headers=_headers(),
        timeout=120.0,
    )
    if response.status_code == 404:
        root.mkdir(parents=True, exist_ok=True)
        return set()
    response.raise_for_status()
    payload = response.json()
    files = payload.get("files", [])
    if not files:
        root.mkdir(parents=True, exist_ok=True)
        return set()
    if len(files) > MAX_FILES:
        raise RuntimeError("workspace has too many files")

    root.mkdir(parents=True, exist_ok=True)
    total = 0
    initial_paths: set[str] = set()
    for item in files:
        relative = _safe_relative(str(item["path"]))
        data = base64.b64decode(item["content_base64"], validate=True)
        if len(data) > MAX_FILE_BYTES:
            raise RuntimeError(f"workspace file too large: {relative.as_posix()}")
        total += len(data)
        if total > MAX_WORKSPACE_BYTES:
            raise RuntimeError("workspace exceeds runner limit")
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        initial_paths.add(relative.as_posix())
    return initial_paths


def fetch_workspace(project_id: str, root: Path) -> set[str]:
    paths = fetch_workspace_optional(project_id, root)
    if not paths:
        raise RuntimeError("persistent workspace is empty")
    return paths


def collect_workspace(root: Path) -> tuple[list[dict[str, str]], set[str]]:
    files: list[dict[str, str]] = []
    current_paths: set[str] = set()
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
            continue
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise RuntimeError(f"workspace file too large to sync: {relative}")
        total += len(data)
        if total > MAX_WORKSPACE_BYTES:
            raise RuntimeError("workspace exceeds sync limit")
        files.append(
            {
                "path": relative,
                "content_base64": base64.b64encode(data).decode("ascii"),
            }
        )
        current_paths.add(relative)
    if len(files) > MAX_FILES:
        raise RuntimeError("workspace has too many files to sync")
    return files, current_paths


def sync_workspace(project_id: str, root: Path, initial_paths: set[str]) -> dict:
    files, current_paths = collect_workspace(root)
    deleted_paths = sorted(initial_paths - current_paths)
    response = httpx.post(
        f"{API_URL}/v1/internal/projects/{project_id}/workspace/sync",
        headers=_headers(),
        json={"files": files, "deleted_paths": deleted_paths},
        timeout=180.0,
    )
    response.raise_for_status()
    return response.json()


def fetch_job_context(job_id: str) -> dict:
    response = httpx.get(
        f"{API_URL}/v1/internal/jobs/{job_id}/context",
        headers=_headers(),
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()
