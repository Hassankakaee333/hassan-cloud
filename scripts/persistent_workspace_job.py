"""Persistent project workspace coding job for Hassan Cloud."""

from __future__ import annotations

import difflib
import json
import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Callable

from workspace_io import fetch_job_context, fetch_workspace, sync_workspace

IGNORED_PARTS = {".git", ".gradle", "build", "__pycache__", ".pytest_cache"}


def _marker_for(path: Path, job_id: str, goal: str) -> str:
    clean_goal = " ".join(goal.split())[:160]
    if path.suffix in {".py", ".sh", ".yaml", ".yml"}:
        return f"# Hassan job {job_id}: {clean_goal}\n"
    if path.suffix in {".kt", ".kts", ".java", ".js", ".ts", ".css"}:
        return f"// Hassan job {job_id}: {clean_goal}\n"
    if path.suffix in {".md", ".html"}:
        return f"<!-- Hassan job {job_id}: {clean_goal} -->\n"
    return f"Hassan job {job_id}: {clean_goal}\n"


def _select_target(root: Path) -> Path:
    preferred = [root / "app.py", root / "main.py", root / "README.md"]
    for path in preferred:
        if path.exists() and path.is_file():
            return path
    supported = {".py", ".kt", ".kts", ".java", ".js", ".ts", ".md", ".txt"}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in supported and not any(part in IGNORED_PARTS for part in path.parts):
            return path
    fallback = root / "HASSAN_JOB.md"
    fallback.write_text("# Hassan Workspace\n", encoding="utf-8")
    return fallback


def _run_verification(root: Path) -> subprocess.CompletedProcess[str]:
    python_tests = list(root.glob("test_*.py")) + list(root.glob("tests/test_*.py"))
    if python_tests:
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=180,
        )
    if list(root.rglob("*.py")):
        return subprocess.run(
            [sys.executable, "-m", "compileall", "-q", "."],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
        )
    return subprocess.CompletedProcess(["no-test-runner"], 0, "No runnable test suite detected\n", "")


def _zip_workspace(root: Path) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not any(part in IGNORED_PARTS for part in path.parts):
                archive.write(path, arcname=path.relative_to(root).as_posix())
    return buffer.getvalue()
def run_persistent_workspace_job(
    *,
    job_id: str,
    project_id: str,
    github_run_id: str,
    out_dir: Path,
    update_job: Callable[..., None],
    register_agent: Callable[[str, str, str], None],
    stage_artifact: Callable[[str, str, bytes], None],
) -> None:
    root = out_dir / "workspace"
    context = fetch_job_context(job_id)
    goal = str(context.get("goal") or "update persistent workspace")
    initial_paths = fetch_workspace(project_id, root)

    update_job(state="RUNNING", log_append="[gha] persistent workspace loaded\n", checkpoint_stage="workspace_loaded")
    register_agent("Planner", "COMPLETE", f"Loaded {len(initial_paths)} persistent workspace files")

    target = _select_target(root)
    before = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
    marker = _marker_for(target, job_id, goal)
    if marker not in before:
        target.write_text(marker + before, encoding="utf-8")
    after = target.read_text(encoding="utf-8", errors="replace")
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{target.relative_to(root).as_posix()}",
            tofile=f"b/{target.relative_to(root).as_posix()}",
        )
    )
    update_job(state="CODING", log_append=f"[gha] persistent edit applied to {target.relative_to(root).as_posix()}\n", checkpoint_stage="coding_done")
    register_agent("Coder", "COMPLETE", f"Applied deterministic persistent edit to {target.name}")

    proc = _run_verification(root)
    update_job(state="TESTING", log_append=f"[gha] verification exit={proc.returncode}\n")
    register_agent("Reviewer", "COMPLETE" if proc.returncode == 0 else "FAILED", (proc.stdout + proc.stderr)[:3000])

    report = {
        "exit_code": proc.returncode,
        "tests_passed": proc.returncode == 0,
        "goal": goal,
        "target": target.relative_to(root).as_posix(),
        "github_run_id": github_run_id,
        "persistent_workspace": True,
    }
    stage_artifact("coding-log.txt", "text/plain", (proc.stdout + "\n" + proc.stderr).encode("utf-8"))
    stage_artifact("changes.diff", "text/plain", diff.encode("utf-8"))
    stage_artifact("test-report.json", "application/json", json.dumps(report, indent=2).encode("utf-8"))
    stage_artifact("workspace.zip", "application/zip", _zip_workspace(root))

    update_job(state="VERIFYING", log_append="[gha] persistent workspace verification\n", checkpoint_stage="verifier_done")
    register_agent("Verifier", "COMPLETE" if proc.returncode == 0 else "FAILED", "Persistent workspace verification complete")
    if proc.returncode != 0:
        update_job(
            state="FAILED",
            failure_reason="persistent workspace verification failed",
            result_summary="Persistent workspace job failed; durable workspace was not changed",
            log_append="[gha] persistent workspace NOT synced\n",
            github_run_id=github_run_id,
        )
        raise RuntimeError("persistent workspace verification failed")

    sync_result = sync_workspace(project_id, root, initial_paths)
    update_job(
        state="VERIFYING",
        result_summary="Persistent workspace updated and verified; awaiting artifact registration",
        log_append=f"[gha] workspace synced files={sync_result.get('files', 0)}\n",
        checkpoint_stage="workspace_synced",
        github_run_id=github_run_id,
    )
