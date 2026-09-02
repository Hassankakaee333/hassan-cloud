"""Build Frishta AI candidate APK for self-improve cloud jobs.

Looks for the real Android app sources, records the improvement goal,
bumps versionCode, runs assembleCandidateDebug, and stages the APK.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def _find_candidate_root() -> Path | None:
    env_root = os.environ.get("CANDIDATE_APP_ROOT", "").strip()
    if env_root:
        root = Path(env_root)
        if (root / "app" / "build.gradle.kts").exists():
            return root

    # Monorepo: hassan-cloud/scripts -> ../../ (Android project root)
    here = Path(__file__).resolve()
    for parent in [here.parents[2], here.parents[1], Path.cwd(), Path.cwd().parent]:
        gradle = parent / "app" / "build.gradle.kts"
        if gradle.exists() and "ai.hassan.app" in gradle.read_text(encoding="utf-8", errors="ignore"):
            return parent

    repo = os.environ.get("HASSAN_CANDIDATE_REPO", "").strip()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("HASSAN_CANDIDATE_TOKEN", "")
    if repo:
        dest = Path("/tmp/frishta-candidate-src")
        if dest.exists():
            shutil.rmtree(dest)
        url = repo if repo.startswith("http") else f"https://github.com/{repo}.git"
        if token and url.startswith("https://") and "@" not in url:
            url = url.replace("https://", f"https://x-access-token:{token}@")
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode == 0 and (dest / "app" / "build.gradle.kts").exists():
            return dest
    return None


def _bump_version(gradle_file: Path) -> tuple[int, str]:
    text = gradle_file.read_text(encoding="utf-8")
    code_match = re.search(r"versionCode\s*=\s*(\d+)", text)
    name_match = re.search(r'versionName\s*=\s*"([^"]+)"', text)
    if not code_match:
        raise RuntimeError("versionCode not found in app/build.gradle.kts")
    old_code = int(code_match.group(1))
    new_code = old_code + 1
    old_name = name_match.group(1) if name_match else "0.0.0"
    # Keep base name, append selfimprove marker if missing.
    if "+self" in old_name:
        new_name = re.sub(r"\+self\d*", f"+self{new_code}", old_name)
    else:
        # bump patch-ish: 0.5.10 -> 0.5.11 when numeric end
        parts = old_name.split(".")
        if parts and parts[-1].isdigit():
            parts[-1] = str(int(parts[-1]) + 1)
            new_name = ".".join(parts)
        else:
            new_name = f"{old_name}+self{new_code}"
    text2 = re.sub(r"versionCode\s*=\s*\d+", f"versionCode = {new_code}", text, count=1)
    text2 = re.sub(r'versionName\s*=\s*"[^"]+"', f'versionName = "{new_name}"', text2, count=1)
    gradle_file.write_text(text2, encoding="utf-8")
    return new_code, new_name


def _append_improve_log(root: Path, job_id: str, goal: str) -> str:
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    path = docs / "SELF_IMPROVE_LOG.md"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    entry = (
        f"\n## {stamp} — job `{job_id}`\n\n"
        f"- Goal: {goal.strip()[:2000]}\n"
        f"- Note: Cloud self-improve loop recorded this request and rebuilt candidate APK.\n"
    )
    prev = path.read_text(encoding="utf-8") if path.exists() else "# Frishta Self-Improve Log\n"
    path.write_text(prev + entry, encoding="utf-8")
    return path.as_posix()


def run_candidate_self_improve_job(
    *,
    job_id: str,
    project_id: str,
    github_run_id: str,
    out_dir: Path,
    update_job: Callable[..., None],
    register_agent: Callable[[str, str, str], None],
    stage_artifact: Callable[[str, str, bytes], None],
    fetch_job_context: Callable[[str], dict],
) -> None:
    context = fetch_job_context(job_id)
    goal = str(context.get("goal") or "Frishta candidate self-improve")

    update_job(
        state="RUNNING",
        log_append="[gha] candidate_self_improve starting\n",
        checkpoint_stage="locate_sources",
    )
    root = _find_candidate_root()
    if root is None:
        msg = (
            "Candidate Android sources not found on the runner. "
            "Set CANDIDATE_APP_ROOT or HASSAN_CANDIDATE_REPO, "
            "or run from a monorepo that contains app/build.gradle.kts."
        )
        register_agent("Planner", "FAILED", msg)
        update_job(
            state="FAILED",
            failure_reason="candidate_sources_missing",
            result_summary=msg,
            log_append=f"[gha] {msg}\n",
        )
        raise RuntimeError(msg)

    register_agent("Planner", "COMPLETE", f"root={root}; goal={goal[:180]}")
    log_path = _append_improve_log(root, job_id, goal)
    version_code, version_name = _bump_version(root / "app" / "build.gradle.kts")
    update_job(
        state="CODING",
        log_append=f"[gha] bumped versionCode={version_code} versionName={version_name}; log={log_path}\n",
        checkpoint_stage="version_bumped",
    )
    register_agent("Coder", "COMPLETE", f"Recorded goal and bumped to {version_name} ({version_code})")

    gradlew = root / "gradlew"
    if os.name == "nt":
        cmd = ["cmd", "/c", "gradlew.bat", ":app:assembleCandidateDebug", "--no-daemon"]
    else:
        if gradlew.exists():
            gradlew.chmod(gradlew.stat().st_mode | 0o111)
            cmd = [str(gradlew), ":app:assembleCandidateDebug", "--no-daemon"]
        else:
            cmd = ["gradle", ":app:assembleCandidateDebug", "--no-daemon"]

    update_job(state="RUNNING", log_append="[gha] assembleCandidateDebug starting\n", checkpoint_stage="building")
    proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=1200)
    build_log = (proc.stdout + "\n" + proc.stderr).encode("utf-8")
    stage_artifact("candidate-self-improve-build-log.txt", "text/plain", build_log)

    if proc.returncode != 0:
        register_agent("Builder", "FAILED", f"assembleCandidateDebug exit={proc.returncode}")
        update_job(
            state="FAILED",
            failure_reason="assembleCandidateDebug failed",
            result_summary="Candidate self-improve build failed — see build log artifact",
            log_append=f"[gha] assembleCandidateDebug exit={proc.returncode}\n",
        )
        raise RuntimeError("assembleCandidateDebug failed")

    apk_candidates = list(
        (root / "app" / "build" / "outputs" / "apk" / "candidate" / "debug").glob("*.apk")
    )
    if not apk_candidates:
        apk_candidates = list((root / "app" / "build" / "outputs" / "apk").rglob("*candidate*.apk"))
    if not apk_candidates:
        update_job(state="FAILED", failure_reason="APK missing after assembleCandidateDebug")
        raise RuntimeError("APK missing after assembleCandidateDebug")

    apk = apk_candidates[0]
    apk_data = apk.read_bytes()
    stage_artifact(
        "frishta-candidate-debug.apk",
        "application/vnd.android.package-archive",
        apk_data,
    )
    report = {
        "job_id": job_id,
        "project_id": project_id,
        "goal": goal,
        "version_code": version_code,
        "version_name": version_name,
        "apk_name": apk.name,
        "apk_size": len(apk_data),
        "sha256": hashlib.sha256(apk_data).hexdigest(),
        "github_run_id": github_run_id,
        "source_root": str(root),
    }
    stage_artifact(
        "candidate-self-improve-report.json",
        "application/json",
        json.dumps(report, indent=2).encode("utf-8"),
    )
    register_agent("Builder", "COMPLETE", f"APK {apk.name} size={len(apk_data)}")
    update_job(
        state="VERIFYING",
        result_summary=(
            f"Candidate self-improve APK ready ({version_name} / {version_code}); "
            "awaiting durable artifact upload"
        ),
        log_append="[gha] candidate APK staged\n",
        checkpoint_stage="android_artifact_upload",
    )
