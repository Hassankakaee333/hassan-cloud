"""GitHub Actions job runner — cloud compute for Hassan jobs."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import httpx

from persistent_workspace_job import run_persistent_workspace_job
from agentos_android_job import run_agentos_android_job

API_URL = os.environ.get("HASSAN_API_URL", "").rstrip("/")
CALLBACK_SECRET = os.environ.get("HASSAN_CALLBACK_SECRET", "")
JOB_ID = os.environ["HASSAN_JOB_ID"]
PROJECT_ID = os.environ["HASSAN_PROJECT_ID"]
JOB_TYPE = os.environ.get("HASSAN_JOB_TYPE", "coding")
GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
RUNNER_MODE = os.environ.get("HASSAN_RUNNER_MODE", "execute")

OUT_DIR = Path("/tmp/hassan-job-out")
OUT_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR = OUT_DIR / "artifacts"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
MANIFEST_PATH = OUT_DIR / "artifact-manifest.json"
GITHUB_ARTIFACT_NAME = f"hassan-job-{JOB_ID}"

FIXTURE_SOURCE = '''def greet(name: str) -> str:
    return f"hello {name}"
'''

FIXTURE_TEST = '''from app import greet

def test_greet():
    assert greet("hassan") == "hello hassan"
'''


def callback(path: str, payload: dict) -> dict:
    if not API_URL or not CALLBACK_SECRET:
        raise RuntimeError("HASSAN_API_URL and HASSAN_CALLBACK_SECRET required")
    url = f"{API_URL}{path}"
    resp = httpx.post(
        url,
        json=payload,
        headers={"X-Hassan-Callback-Secret": CALLBACK_SECRET},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()


def update_job(**kwargs) -> None:
    callback(f"/v1/internal/jobs/{JOB_ID}/update", kwargs)


def stage_artifact(name: str, mime: str, data: bytes) -> None:
    if Path(name).name != name:
        raise ValueError(f"artifact name must be a filename: {name}")
    digest = hashlib.sha256(data).hexdigest()
    relative_path = f"artifacts/{name}"
    (ARTIFACT_DIR / name).write_bytes(data)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.exists() else []
    manifest.append(
        {
            "name": name,
            "mime_type": mime,
            "size_bytes": len(data),
            "sha256": digest,
            "path": relative_path,
        }
    )
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def register_staged_artifacts() -> None:
    if not GITHUB_RUN_ID:
        raise RuntimeError("GITHUB_RUN_ID required to register GitHub Actions artifacts")
    if not MANIFEST_PATH.exists():
        raise RuntimeError("artifact manifest missing")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not manifest:
        raise RuntimeError("artifact manifest empty")
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


def run_coding_job() -> None:
    root = OUT_DIR / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    src = root / "app.py"
    test = root / "test_app.py"
    src.write_text(FIXTURE_SOURCE, encoding="utf-8")
    test.write_text(FIXTURE_TEST, encoding="utf-8")

    update_job(state="RUNNING", log_append="[gha] planner complete\n", checkpoint_stage="planner_done")
    register_agent("Planner", "COMPLETE", "Fixture workspace prepared")

    before = src.read_text(encoding="utf-8")
    src.write_text("# Hassan Cloud workspace edit\n" + before, encoding="utf-8")
    diff = "--- before\n+++ after\n+ # Hassan Cloud workspace edit\n"
    update_job(state="CODING", log_append="[gha] coding complete\n", checkpoint_stage="coding_done")
    register_agent("Coder", "COMPLETE", "Applied workspace edit")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_app.py"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    update_job(state="TESTING", log_append=f"[gha] pytest exit={proc.returncode}\n")
    register_agent("Reviewer", "COMPLETE" if proc.returncode == 0 else "FAILED", proc.stdout[:2000])

    report = {
        "exit_code": proc.returncode,
        "tests_passed": proc.returncode == 0,
        "stdout": proc.stdout[:2000],
        "github_run_id": GITHUB_RUN_ID,
    }
    stage_artifact("coding-log.txt", "text/plain", (proc.stdout + "\n" + proc.stderr).encode("utf-8"))
    stage_artifact("changes.diff", "text/plain", diff.encode("utf-8"))
    stage_artifact("test-report.json", "application/json", json.dumps(report, indent=2).encode("utf-8"))

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in root.rglob("*"):
            if path.is_file() and ".pytest_cache" not in path.parts:
                zf.write(path, arcname=str(path.relative_to(root)))
    stage_artifact("workspace.zip", "application/zip", buffer.getvalue())

    update_job(state="VERIFYING", log_append="[gha] verifying\n", checkpoint_stage="verifier_done")
    register_agent("Verifier", "COMPLETE" if proc.returncode == 0 else "FAILED", "Tests verified")

    if proc.returncode != 0:
        update_job(
            state="FAILED",
            failure_reason="pytest failed",
            result_summary="Coding job failed: tests did not pass",
            log_append="[gha] FAILED\n",
            github_run_id=GITHUB_RUN_ID,
        )
        sys.exit(1)

    update_job(
        state="VERIFYING",
        result_summary="Coding job passed; awaiting durable artifact upload",
        log_append="[gha] waiting for GitHub artifact upload\n",
        github_run_id=GITHUB_RUN_ID,
    )


def run_android_build_job() -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "android-sample"
    update_job(state="RUNNING", log_append="[gha] Android fixture build starting\n", checkpoint_stage="android_build")
    register_agent("Planner", "COMPLETE", "Android sample fixture selected")
    proc = subprocess.run(
        ["gradle", ":app:assembleDebug", "--no-daemon"],
        cwd=str(fixture),
        capture_output=True,
        text=True,
        timeout=600,
    )
    build_log = (proc.stdout + "\n" + proc.stderr).encode("utf-8")
    stage_artifact("android-build-log.txt", "text/plain", build_log)
    if proc.returncode != 0:
        update_job(
            state="FAILED",
            failure_reason="Android fixture build failed",
            result_summary="Android build job failed",
            log_append=f"[gha] Android Gradle exit={proc.returncode}\n",
        )
        sys.exit(1)

    apk = fixture / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
    if not apk.exists():
        update_job(state="FAILED", failure_reason="APK output missing")
        sys.exit(1)
    apk_data = apk.read_bytes()
    stage_artifact("hassan-fixture-debug.apk", "application/vnd.android.package-archive", apk_data)
    stage_artifact(
        "android-build-report.json",
        "application/json",
        json.dumps(
            {
                "exit_code": proc.returncode,
                "apk_size": len(apk_data),
                "sha256": hashlib.sha256(apk_data).hexdigest(),
                "github_run_id": GITHUB_RUN_ID,
            },
            indent=2,
        ).encode("utf-8"),
    )
    register_agent("Builder", "COMPLETE", "Android sample APK assembled")
    update_job(
        state="VERIFYING",
        result_summary="Android fixture APK built; awaiting durable artifact upload",
        log_append="[gha] Android APK verified and staged\n",
        checkpoint_stage="android_artifact_upload",
    )


def finalize_job() -> None:
    register_staged_artifacts()
    if JOB_TYPE == "android_build":
        summary = "Android fixture APK completed via GitHub Actions"
    elif JOB_TYPE == "workspace_coding":
        summary = "Persistent workspace coding job completed via GitHub Actions"
    elif JOB_TYPE == "agentos_android":
        summary = "HassanTodoBenchmark AgentOS Android job completed via GitHub Actions"
    else:
        summary = "Coding job completed via GitHub Actions"
    update_job(
        state="COMPLETED",
        result_summary=summary,
        log_append="[gha] durable GitHub artifacts registered; COMPLETED\n",
        checkpoint_stage="completed",
        github_run_id=GITHUB_RUN_ID,
    )


def main() -> None:
    if RUNNER_MODE == "finalize":
        finalize_job()
        return
    if RUNNER_MODE == "failure":
        update_job(
            state="FAILED",
            failure_reason="GitHub Actions workflow or artifact upload failed",
            result_summary="Cloud job failed before durable artifact registration",
            log_append="[gha] workflow failure callback\n",
            github_run_id=GITHUB_RUN_ID,
        )
        return
    update_job(
        state="RUNNING",
        log_append=f"[gha] workflow started run={GITHUB_RUN_ID}\n",
        github_run_id=GITHUB_RUN_ID,
    )
    if JOB_TYPE in ("coding", "general"):
        run_coding_job()
    elif JOB_TYPE == "workspace_coding":
        run_persistent_workspace_job(
            job_id=JOB_ID,
            project_id=PROJECT_ID,
            github_run_id=GITHUB_RUN_ID,
            out_dir=OUT_DIR,
            update_job=update_job,
            register_agent=register_agent,
            stage_artifact=stage_artifact,
        )
    elif JOB_TYPE == "android_build":
        run_android_build_job()
    elif JOB_TYPE == "agentos_android":
        run_agentos_android_job(
            job_id=JOB_ID,
            project_id=PROJECT_ID,
            github_run_id=GITHUB_RUN_ID,
            out_dir=OUT_DIR,
            update_job=update_job,
            register_agent=register_agent,
            stage_artifact=stage_artifact,
        )
    else:
        update_job(state="FAILED", failure_reason=f"unsupported job_type: {JOB_TYPE}")
        sys.exit(1)


if __name__ == "__main__":
    main()
