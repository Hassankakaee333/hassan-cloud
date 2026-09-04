"""Standalone GitHub Actions entrypoint for the read-only Codex account snapshot job."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx

from codex_account_snapshot_job import run_codex_account_snapshot_job

API_URL = os.environ.get("HASSAN_API_URL", "").rstrip("/")
CALLBACK_SECRET = os.environ.get("HASSAN_CALLBACK_SECRET", "")
JOB_ID = os.environ["HASSAN_JOB_ID"]
PROJECT_ID = os.environ["HASSAN_PROJECT_ID"]
GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
RUNNER_MODE = os.environ.get("HASSAN_RUNNER_MODE", "execute")
OUT_DIR = Path("/tmp/hassan-job-out")
ARTIFACT_DIR = OUT_DIR / "artifacts"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_NAME = f"hassan-job-{JOB_ID}"
MANIFEST_PATH = OUT_DIR / "codex-account-manifest.json"
_staged: list[dict] = []


def callback(path: str, payload: dict) -> dict:
    if not API_URL or not CALLBACK_SECRET:
        raise RuntimeError("HASSAN_API_URL and HASSAN_CALLBACK_SECRET required")
    response = httpx.post(
        f"{API_URL}{path}",
        json=payload,
        headers={"X-Hassan-Callback-Secret": CALLBACK_SECRET},
        timeout=60.0,
    )
    response.raise_for_status()
    return response.json()


def update_job(**kwargs) -> None:
    callback(f"/v1/internal/jobs/{JOB_ID}/update", kwargs)


def register_agent(agent_role: str, status: str, output: str) -> None:
    callback(
        f"/v1/internal/jobs/{JOB_ID}/agent-runs",
        {
            "agent_id": agent_role.lower(),
            "agent_role": agent_role,
            "status": status,
            "input_text": JOB_ID,
            "output_text": output[:4000],
        },
    )


def stage_artifact(name: str, mime: str, data: bytes) -> None:
    if Path(name).name != name:
        raise ValueError("artifact name must be a filename")
    path = ARTIFACT_DIR / name
    path.write_bytes(data)
    _staged.append(
        {
            "name": name,
            "mime_type": mime,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "path": f"artifacts/{name}",
        }
    )
    MANIFEST_PATH.write_text(json.dumps(_staged, indent=2), encoding="utf-8")


def register_staged_artifacts() -> None:
    if not GITHUB_RUN_ID:
        raise RuntimeError("GITHUB_RUN_ID required")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(manifest, list) or not manifest:
        raise RuntimeError("Codex account snapshot manifest missing or empty")
    for item in manifest:
        callback(
            f"/v1/internal/jobs/{JOB_ID}/artifacts",
            {
                "name": item["name"],
                "mime_type": item["mime_type"],
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
                "storage_backend": "GITHUB_ACTIONS",
                "storage_key": f"{GITHUB_RUN_ID}|{ARTIFACT_NAME}|{item['path']}",
                "project_id": PROJECT_ID,
            },
        )


def finalize() -> None:
    register_staged_artifacts()
    snapshot_path = ARTIFACT_DIR / "codex-account-snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    state = snapshot.get("state", "DISCONNECTED")
    models = snapshot.get("models") if isinstance(snapshot.get("models"), list) else []
    update_job(
        state="COMPLETED",
        result_summary=f"Codex Account Runtime: {state}; models={len(models)}; model turn used=false",
        log_append="[codex-account] uploaded snapshot registered; COMPLETED\n",
        checkpoint_stage="completed",
        github_run_id=GITHUB_RUN_ID,
    )


def execute() -> None:
    update_job(
        state="RUNNING",
        log_append=f"[codex-account] read-only snapshot started run={GITHUB_RUN_ID}; no model turn\n",
        github_run_id=GITHUB_RUN_ID,
        checkpoint_stage="codex_account_readonly",
    )
    run_codex_account_snapshot_job(
        out_dir=OUT_DIR,
        update_job=update_job,
        register_agent=register_agent,
        stage_artifact=stage_artifact,
    )
    if not _staged:
        raise RuntimeError("Codex account snapshot artifact was not produced")
    update_job(
        state="VERIFYING",
        result_summary="Codex Account Runtime snapshot staged; waiting for durable GitHub artifact upload.",
        log_append="[codex-account] snapshot staged; waiting for upload\n",
        checkpoint_stage="codex_account_upload",
        github_run_id=GITHUB_RUN_ID,
    )


def main() -> None:
    if RUNNER_MODE == "finalize":
        finalize()
    else:
        execute()


if __name__ == "__main__":
    main()
