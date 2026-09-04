"""Manual-only Codex self-improve job for Frishta candidate.

Security / quota design:
- This job is never selected by Frishta Auto.
- It is dispatched only after Hassan explicitly selects Codex and approves the plan.
- Every run performs a fresh ChatGPT device-code login on the ephemeral runner.
- Codex auth is isolated under a temporary CODEX_HOME and deleted after the run.
- No Codex auth.json, access token, refresh token, cookie, or API key is staged/uploaded.
- The runner gets workspace-write access only to the checked-out candidate repository.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from candidate_self_improve_job import (
    _append_improve_log,
    _bump_version,
    _find_candidate_root,
    _push_candidate_changes,
    _run_assemble,
    _sanitize_applied_kotlin,
)


def _git_status_paths(root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git status failed: {proc.stderr[:300]}")
    paths: list[str] = []
    for raw in proc.stdout.splitlines():
        if len(raw) < 4:
            continue
        value = raw[3:].strip()
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        value = value.strip('"').replace("\\", "/")
        if value:
            paths.append(value)
    return paths


def _allowed_source_path(path: str) -> bool:
    if not (path.startswith("app/") or path.startswith("docs/")):
        return False
    return path.endswith((".kt", ".kts", ".xml", ".md", ".properties"))


def _current_diff(root: Path) -> str:
    proc = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--binary"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _run_codex_edit(
    *,
    root: Path,
    goal: str,
    update_job: Callable[..., None],
    register_agent: Callable[[str, str, str], None],
) -> tuple[list[str], str, dict]:
    try:
        from openai_codex import Codex, CodexConfig, Sandbox
    except ImportError as exc:  # pragma: no cover - validated in GitHub Actions
        raise RuntimeError("openai-codex SDK is not installed") from exc

    if not (root / ".git").exists():
        raise RuntimeError("Manual Codex path requires an isolated git checkout of the candidate app")

    before = set(_git_status_paths(root))
    if before:
        raise RuntimeError(
            "Candidate checkout is not clean before Codex: " + ", ".join(sorted(before)[:12])
        )

    codex_home = Path(tempfile.mkdtemp(prefix="frishta-codex-home-"))
    try:
        config = CodexConfig(
            cwd=str(root),
            env={"CODEX_HOME": str(codex_home)},
        )
        with Codex(config=config) as codex:
            try:
                login = codex.login_chatgpt_device_code()
                verification_url = str(login.verification_url)
                user_code = str(login.user_code)
                # Keep this as RUNNING for current Frishta builds: active-job cards
                # show resultSummary, while WAITING_FOR_USER is rendered in a compact
                # bucket that currently hides the device-login URL and code.
                update_job(
                    state="RUNNING",
                    result_summary=(
                        "Codex بانتظار تسجيل دخولك لهذه المهمة فقط. "
                        f"افتح {verification_url} وأدخل الرمز: {user_code}"
                    ),
                    log_append="[codex] device-code login requested; isolated temporary CODEX_HOME\n",
                    checkpoint_stage="codex_device_login",
                )
                register_agent(
                    "CodexAuth",
                    "WAITING",
                    f"Open {verification_url} and enter code {user_code}. Ephemeral login only.",
                )

                login_result = login.wait()
                if not bool(getattr(login_result, "success", False)):
                    raise RuntimeError("ChatGPT device-code login for Codex did not complete successfully")

                register_agent("CodexAuth", "COMPLETE", "Authenticated for this runner only")
                update_job(
                    state="CODING",
                    result_summary="تم تسجيل دخول Codex لهذه المهمة فقط. بدأ تعديل Candidate.",
                    log_append="[codex] ephemeral ChatGPT login complete; starting workspace edit\n",
                    checkpoint_stage="codex_coding",
                )

                prompt = f"""You are editing the Frishta Android candidate repository for Hassan.

USER GOAL:
{goal.strip()[:5000]}

Rules you MUST follow:
- Work only inside this checked-out repository.
- Edit only app/ and docs/ source files; do not edit CI, secrets, git config, signing keys, or dependency credentials.
- Do not use OpenAI API keys or any paid metered API. You are already authenticated through Hassan's ChatGPT/Codex subscription for this one run.
- Do not persist, print, copy, inspect, or exfiltrate authentication tokens, cookies, auth.json, environment secrets, or GitHub credentials.
- Make the smallest safe implementation that satisfies the goal.
- Keep package ai.hassan.app intact and preserve the Candidate/Stable separation.
- Do not commit or push; the outer verifier will review, build, and push only after checks pass.
- Do not install anything on the phone.
- Finish with a concise summary of what you changed and what should be tested.
"""

                thread = codex.thread_start(
                    cwd=str(root),
                    sandbox=Sandbox.workspace_write,
                    ephemeral=True,
                )
                result = thread.run(prompt, sandbox=Sandbox.workspace_write)
                final_response = str(result.final_response or "")
                usage = getattr(result, "usage", None)
                usage_data = {
                    "thread_id": str(thread.id),
                    "turn_id": str(result.id),
                    "status": str(result.status),
                    "usage": str(usage) if usage is not None else None,
                }
            finally:
                try:
                    codex.logout()
                except Exception:
                    pass
    finally:
        shutil.rmtree(codex_home, ignore_errors=True)

    changed = _git_status_paths(root)
    if not changed:
        raise RuntimeError("Codex completed without changing any candidate source files")

    forbidden = [path for path in changed if not _allowed_source_path(path)]
    if forbidden:
        subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=str(root), check=False)
        subprocess.run(["git", "clean", "-fd"], cwd=str(root), check=False)
        raise RuntimeError("Codex touched forbidden paths: " + ", ".join(forbidden[:12]))

    return changed, final_response, usage_data


def run_codex_candidate_self_improve_job(
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
    goal = str(context.get("goal") or "Frishta candidate self-improve via Codex")

    update_job(
        state="RUNNING",
        result_summary="تجهيز Candidate لمسار Codex اليدوي.",
        log_append="[codex] manual-only candidate self-improve starting\n",
        checkpoint_stage="codex_locate_sources",
    )
    root = _find_candidate_root()
    if root is None:
        raise RuntimeError("Candidate Android sources not found")
    if not (root / ".git").exists():
        raise RuntimeError("Codex runner requires the isolated candidate repository checkout")

    register_agent("Planner", "COMPLETE", f"manual Codex root={root}; goal={goal[:180]}")

    try:
        applied, codex_summary, usage_data = _run_codex_edit(
            root=root,
            goal=goal,
            update_job=update_job,
            register_agent=register_agent,
        )
    except Exception as exc:
        register_agent("Codex", "FAILED", str(exc)[:500])
        update_job(
            state="FAILED",
            failure_reason="codex_manual_edit_failed",
            result_summary=f"تعذر تنفيذ Codex اليدوي: {str(exc)[:350]}",
            log_append=f"[codex] failed: {exc}\n",
        )
        raise

    sanitized = _sanitize_applied_kotlin(root)
    applied = list(dict.fromkeys(applied + sanitized))
    diff_before_build = _current_diff(root)
    stage_artifact("codex-changes.diff", "text/plain", diff_before_build.encode("utf-8"))
    stage_artifact("codex-final-response.txt", "text/plain", codex_summary.encode("utf-8"))
    stage_artifact(
        "codex-run.json",
        "application/json",
        json.dumps(usage_data, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    register_agent("Codex", "COMPLETE", f"files={applied}; {codex_summary[:240]}")

    _append_improve_log(root, job_id, goal, applied)
    version_code, version_name = _bump_version(root / "app" / "build.gradle.kts")
    update_job(
        state="TESTING",
        result_summary="انتهى Codex من التعديل. بدأ بناء Candidate والتحقق منه.",
        log_append=f"[codex] build starting version={version_name}/{version_code}\n",
        checkpoint_stage="codex_building",
    )

    _sanitize_applied_kotlin(root)
    proc = _run_assemble(root)
    build_log = (proc.stdout + "\n" + proc.stderr).encode("utf-8")
    stage_artifact("codex-candidate-build-log.txt", "text/plain", build_log)
    if proc.returncode != 0:
        register_agent("Builder", "FAILED", f"assembleCandidateDebug exit={proc.returncode}")
        update_job(
            state="FAILED",
            failure_reason="codex_candidate_build_failed",
            result_summary="Codex عدّل المصدر لكن بناء Candidate فشل؛ لم يتم دفع أو تثبيت أي نسخة.",
            log_append=f"[codex] assembleCandidateDebug exit={proc.returncode}\n",
        )
        raise RuntimeError("assembleCandidateDebug failed after Codex edit")

    apk_candidates = list(
        (root / "app" / "build" / "outputs" / "apk" / "candidate" / "debug").glob("*.apk")
    )
    if not apk_candidates:
        apk_candidates = list((root / "app" / "build" / "outputs" / "apk").rglob("*candidate*.apk"))
    if not apk_candidates:
        raise RuntimeError("Candidate APK missing after successful build")

    push_status = _push_candidate_changes(root, job_id, goal, applied)
    update_job(log_append=f"[codex] verified source push: {push_status}\n")

    apk = apk_candidates[0]
    apk_data = apk.read_bytes()
    stage_artifact(
        "frishta-candidate-codex-debug.apk",
        "application/vnd.android.package-archive",
        apk_data,
    )
    report = {
        "job_id": job_id,
        "project_id": project_id,
        "goal": goal,
        "manual_codex": True,
        "ephemeral_auth": True,
        "auth_persisted": False,
        "applied_files": applied,
        "push_status": push_status,
        "version_code": version_code,
        "version_name": version_name,
        "apk_name": apk.name,
        "apk_size": len(apk_data),
        "sha256": hashlib.sha256(apk_data).hexdigest(),
        "github_run_id": github_run_id,
        "codex": usage_data,
    }
    stage_artifact(
        "codex-candidate-report.json",
        "application/json",
        json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    register_agent("Builder", "COMPLETE", f"verified APK {apk.name} size={len(apk_data)}")
    update_job(
        state="VERIFYING",
        result_summary=f"Codex اليدوي نجح؛ Candidate {version_name}/{version_code} جاهز للتنزيل.",
        log_append="[codex] candidate APK staged; temporary auth session deleted\n",
        checkpoint_stage="codex_artifact_upload",
    )
