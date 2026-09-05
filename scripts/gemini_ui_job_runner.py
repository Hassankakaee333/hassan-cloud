"""GitHub Actions entrypoint for the Frishta Gemini official-app worker."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx

import gemini_ui_job as worker_module
from gemini_ui_atomic_transport import install_atomic_transport
from gemini_ui_hardening import install_runtime_hardening

API_URL = os.environ.get("HASSAN_API_URL", "").rstrip("/")
CALLBACK_SECRET = os.environ.get("HASSAN_CALLBACK_SECRET", "")
JOB_ID = os.environ["HASSAN_JOB_ID"]
PROJECT_ID = os.environ["HASSAN_PROJECT_ID"]
GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
RUNNER_MODE = os.environ.get("HASSAN_RUNNER_MODE", "execute")
OUT_DIR = Path("/tmp/hassan-job-out")
ARTIFACT_DIR = OUT_DIR / "artifacts"
MANIFEST_PATH = OUT_DIR / "artifact-manifest.json"
GITHUB_ARTIFACT_NAME = f"hassan-job-{JOB_ID}"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)


def callback(path: str, payload: dict) -> dict:
    if not API_URL or not CALLBACK_SECRET:
        raise RuntimeError("HASSAN_API_URL and HASSAN_CALLBACK_SECRET required")
    response = httpx.post(
        f"{API_URL}{path}",
        headers={"X-Hassan-Callback-Secret": CALLBACK_SECRET},
        json=payload,
        timeout=90.0,
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
            "verification_notes": "official Gemini Android UI transport; no Gemini API key",
        },
    )


def stage_artifact(name: str, mime: str, data: bytes) -> None:
    if Path(name).name != name:
        raise ValueError("artifact name must be a filename")
    digest = hashlib.sha256(data).hexdigest()
    path = ARTIFACT_DIR / name
    path.write_bytes(data)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.exists() else []
    manifest.append(
        {
            "name": name,
            "mime_type": mime,
            "size_bytes": len(data),
            "sha256": digest,
            "path": f"artifacts/{name}",
        }
    )
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def finalize() -> None:
    if not GITHUB_RUN_ID:
        raise RuntimeError("GITHUB_RUN_ID required")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.exists() else []
    for item in manifest:
        callback(
            f"/v1/internal/jobs/{JOB_ID}/artifacts",
            {
                "name": item["name"],
                "mime_type": item["mime_type"],
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
                "storage_backend": "GITHUB_ACTIONS",
                "storage_key": f"{GITHUB_RUN_ID}|{GITHUB_ARTIFACT_NAME}|{item['path']}",
                "project_id": PROJECT_ID,
            },
        )
    update_job(
        state="COMPLETED",
        log_append="[gemini-ui] durable artifacts registered; COMPLETED\n",
        checkpoint_stage="completed",
        github_run_id=GITHUB_RUN_ID,
    )


def main() -> None:
    if RUNNER_MODE == "finalize":
        finalize()
        return
    if RUNNER_MODE == "failure":
        update_job(
            state="FAILED",
            failure_reason="Gemini official-app workflow failed",
            log_append="[gemini-ui] workflow failure callback\n",
            github_run_id=GITHUB_RUN_ID,
        )
        return
    install_runtime_hardening()
    install_atomic_transport()
    try:
        worker_module.run_gemini_ui_job(
            job_id=JOB_ID,
            project_id=PROJECT_ID,
            update_job=update_job,
            register_agent=register_agent,
            stage_artifact=stage_artifact,
            max_steps=int(os.environ.get("GEMINI_UI_MAX_STEPS", "8")),
        )
    except Exception as exc:
        update_job(
            state="FAILED",
            failure_reason=f"{type(exc).__name__}: {str(exc)[:800]}",
            result_summary="Gemini official-app worker stopped before FRISHTA_FINAL",
            log_append=f"[gemini-ui] failed: {type(exc).__name__}: {str(exc)[:500]}\n",
            github_run_id=GITHUB_RUN_ID,
        )
        raise


if __name__ == "__main__":
    main()
