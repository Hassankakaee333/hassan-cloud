"""GitHub Actions job runner — cloud compute for Hassan jobs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import httpx

API_URL = os.environ.get("HASSAN_API_URL", "").rstrip("/")
CALLBACK_SECRET = os.environ.get("HASSAN_CALLBACK_SECRET", "")
JOB_ID = os.environ["HASSAN_JOB_ID"]
PROJECT_ID = os.environ["HASSAN_PROJECT_ID"]
JOB_TYPE = os.environ.get("HASSAN_JOB_TYPE", "coding")
GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID", "")

OUT_DIR = Path("/tmp/hassan-job-out")
OUT_DIR.mkdir(parents=True, exist_ok=True)

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


def register_artifact(name: str, mime: str, data: bytes) -> None:
    digest = hashlib.sha256(data).hexdigest()
    callback(
        f"/v1/internal/jobs/{JOB_ID}/artifacts",
        {
            "name": name,
            "mime_type": mime,
            "size_bytes": len(data),
            "sha256": digest,
            "storage_backend": "INLINE_POC",
            "content_base64": base64.b64encode(data).decode("ascii"),
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
    register_artifact("coding-log.txt", "text/plain", (proc.stdout + "\n" + proc.stderr).encode("utf-8"))
    register_artifact("changes.diff", "text/plain", diff.encode("utf-8"))
    register_artifact("test-report.json", "application/json", json.dumps(report, indent=2).encode("utf-8"))

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in root.rglob("*"):
            if path.is_file() and ".pytest_cache" not in path.parts:
                zf.write(path, arcname=str(path.relative_to(root)))
    register_artifact("workspace.zip", "application/zip", buffer.getvalue())

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
        state="COMPLETED",
        result_summary="Coding job completed via GitHub Actions",
        log_append="[gha] COMPLETED\n",
        checkpoint_stage="completed",
        github_run_id=GITHUB_RUN_ID,
    )


def main() -> None:
    update_job(
        state="RUNNING",
        log_append=f"[gha] workflow started run={GITHUB_RUN_ID}\n",
        github_run_id=GITHUB_RUN_ID,
    )
    if JOB_TYPE in ("coding", "general"):
        run_coding_job()
    else:
        update_job(state="FAILED", failure_reason=f"unsupported job_type: {JOB_TYPE}")
        sys.exit(1)


if __name__ == "__main__":
    main()
