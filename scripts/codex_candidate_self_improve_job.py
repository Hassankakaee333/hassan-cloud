"""Manual-only Codex self-improve job for Frishta candidate.

Security / quota design:
- This job is never selected by Frishta Auto.
- It is dispatched only after Hassan explicitly selects Codex and approves the plan.
- Device-code login is requested only when no valid encrypted persisted session exists.
- Plaintext Codex auth is isolated under a temporary CODEX_HOME and deleted after the run.
- Only AES-GCM ciphertext is persisted in GitHub Actions cache; no plaintext auth token is staged/uploaded.
- The runner gets workspace-write access only to the checked-out candidate repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from codex_persistent_auth import restore_encrypted_codex_home, save_encrypted_codex_home
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


def _read_codex_rate_limits(codex_home: Path) -> dict:
    """Read account quota through Codex app-server without issuing a model turn."""
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    requests = [
        {
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "frishta_hassan_cloud",
                    "title": "Frishta Hassan Cloud",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        },
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "method": "account/rateLimits/read", "id": 2, "params": None},
    ]
    payload = "\n".join(json.dumps(item, separators=(",", ":")) for item in requests) + "\n"
    proc = subprocess.run(
        ["timeout", "15s", "codex", "app-server", "--stdio"],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    for line in proc.stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") != 2:
            continue
        if message.get("error"):
            raise RuntimeError(f"Codex quota read failed: {message['error']}")
        result = message.get("result")
        if isinstance(result, dict):
            return result
        break
    detail = (proc.stderr or "").strip().splitlines()[-1:] or ["no app-server response"]
    raise RuntimeError(f"Codex quota unavailable: {detail[0][:220]}")


def _quota_percent(value: object) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return max(0.0, min(100.0, float(value)))


def _codex_quota_snapshot(raw: dict) -> dict:
    """Return only secret-free remaining percentages and reset metadata."""
    by_id = raw.get("rateLimitsByLimitId")
    selected = by_id.get("codex") if isinstance(by_id, dict) else None
    if not isinstance(selected, dict):
        fallback = raw.get("rateLimits")
        selected = fallback if isinstance(fallback, dict) else {}

    snapshot: dict[str, object] = {"limits": {}}
    limits = snapshot["limits"]
    assert isinstance(limits, dict)
    for key in ("primary", "secondary"):
        window = selected.get(key)
        if not isinstance(window, dict):
            continue
        used = _quota_percent(window.get("usedPercent"))
        minutes = window.get("windowDurationMins")
        if used is None or not isinstance(minutes, (int, float)):
            continue
        minutes_int = int(minutes)
        if minutes_int == 300:
            label = "five_hours"
        elif minutes_int == 1440:
            label = "daily"
        elif minutes_int == 10080:
            label = "weekly"
        elif 40320 <= minutes_int <= 44640:
            label = "monthly"
        else:
            label = f"window_{minutes_int}m"
        limits[label] = {
            "remainingPercent": round(100.0 - used, 1),
            "usedPercent": round(used, 1),
            "windowDurationMins": minutes_int,
            "resetsAt": window.get("resetsAt"),
        }

    individual = raw.get("individualLimit")
    if not isinstance(individual, dict):
        individual = selected.get("individualLimit") if isinstance(selected, dict) else None
    if isinstance(individual, dict):
        remaining = _quota_percent(individual.get("remainingPercent"))
        if remaining is not None:
            limits["monthly"] = {"remainingPercent": round(remaining, 1), "source": "individualLimit"}

    credits = raw.get("credits")
    if not isinstance(credits, dict):
        credits = selected.get("credits") if isinstance(selected, dict) else None
    if isinstance(credits, dict):
        safe_credits = {
            key: credits.get(key)
            for key in ("hasCredits", "unlimited", "balance")
            if key in credits
        }
        if safe_credits:
            snapshot["credits"] = safe_credits
    return snapshot


def _format_percent(value: object) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


def _format_codex_quota(snapshot: dict) -> str:
    limits = snapshot.get("limits")
    if not isinstance(limits, dict) or not limits:
        return "لم يرسل OpenAI نسبة رصيد متبقٍ لهذه الجلسة."
    labels = (
        ("daily", "اليومي"),
        ("weekly", "الأسبوعي"),
        ("monthly", "الشهري"),
        ("five_hours", "5 ساعات"),
    )
    parts: list[str] = []
    known = {key for key, _ in labels}
    for key, arabic in labels:
        item = limits.get(key)
        if isinstance(item, dict) and isinstance(item.get("remainingPercent"), (int, float)):
            parts.append(f"{arabic} {_format_percent(item['remainingPercent'])}%")
    for key, item in limits.items():
        if key in known or not isinstance(item, dict):
            continue
        if isinstance(item.get("remainingPercent"), (int, float)):
            parts.append(f"نافذة {item.get('windowDurationMins')} دقيقة {_format_percent(item['remainingPercent'])}%")
    return "رصيد Codex المتبقي: " + " | ".join(parts) if parts else "لم يرسل OpenAI نسبة رصيد متبقٍ لهذه الجلسة."


def _catalog_data(response: object) -> list[dict]:
    if hasattr(response, "model_dump"):
        raw = response.model_dump(mode="json", by_alias=True)
    elif isinstance(response, dict):
        raw = response
    else:
        raw = {}
    data = raw.get("data") if isinstance(raw, dict) else None
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _resolve_runtime_selection(
    catalog: list[dict],
    requested_model: object,
    requested_mode: object,
) -> tuple[str | None, str | None]:
    model_id = requested_model.strip() if isinstance(requested_model, str) else ""
    mode_id = requested_mode.strip() if isinstance(requested_mode, str) else ""
    if len(model_id) > 200 or len(mode_id) > 100:
        raise RuntimeError("Codex runtime selection is unreasonably long")
    if not model_id:
        if mode_id:
            raise RuntimeError("Codex reasoning selection requires an explicit account model")
        return None, None

    selected = next(
        (
            item for item in catalog
            if str(item.get("id") or "") == model_id or str(item.get("model") or "") == model_id
        ),
        None,
    )
    if selected is None:
        raise RuntimeError(f"Selected Codex model is no longer available to this account: {model_id}")
    if bool(selected.get("hidden")):
        raise RuntimeError(f"Selected Codex model is hidden and cannot be used: {model_id}")

    supported: set[str] = set()
    efforts = selected.get("supportedReasoningEfforts")
    if isinstance(efforts, list):
        for effort in efforts:
            if isinstance(effort, str) and effort.strip():
                supported.add(effort.strip())
            elif isinstance(effort, dict):
                value = effort.get("reasoningEffort") or effort.get("effort") or effort.get("id")
                if isinstance(value, str) and value.strip():
                    supported.add(value.strip())
    if mode_id and mode_id not in supported:
        raise RuntimeError(
            f"Selected Codex reasoning mode is no longer available for {model_id}: {mode_id}"
        )

    wire_model = str(selected.get("model") or selected.get("id") or "").strip()
    if not wire_model:
        raise RuntimeError(f"Selected Codex model has no executable model id: {model_id}")
    return wire_model, mode_id or None


def _run_codex_edit(
    *,
    root: Path,
    goal: str,
    update_job: Callable[..., None],
    register_agent: Callable[[str, str, str], None],
    requested_model: str | None = None,
    requested_mode: str | None = None,
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
    session_cache_raw = os.environ.get("CODEX_SESSION_CACHE", "").strip()
    callback_secret = os.environ.get("HASSAN_CALLBACK_SECRET", "").strip()
    session_cache = Path(session_cache_raw) if session_cache_raw else None
    restored_auth = False
    auth_reused = False
    auth_persisted = False

    if session_cache is not None and callback_secret:
        restored_auth = restore_encrypted_codex_home(session_cache, codex_home, callback_secret)
        if restored_auth:
            try:
                _read_codex_rate_limits(codex_home)
                auth_reused = True
                register_agent("CodexAuth", "COMPLETE", "Reused encrypted persisted ChatGPT/Codex session")
                update_job(
                    state="RUNNING",
                    result_summary="تمت استعادة جلسة Codex المشفرة؛ لا حاجة لرمز تسجيل دخول جديد.",
                    log_append="[codex] encrypted persisted auth restored and validated\n",
                    checkpoint_stage="codex_auth_reused",
                )
            except Exception as auth_exc:
                update_job(log_append=f"[codex] persisted auth invalid/expired; requesting fresh device login: {auth_exc}\n")
                shutil.rmtree(codex_home, ignore_errors=True)
                codex_home.mkdir(parents=True, exist_ok=True)
                restored_auth = False

    try:
        config = CodexConfig(
            cwd=str(root),
            env={"CODEX_HOME": str(codex_home)},
        )
        with Codex(config=config) as codex:
            if not auth_reused:
                login = codex.login_chatgpt_device_code()
                verification_url = str(login.verification_url)
                user_code = str(login.user_code)
                update_job(
                    state="RUNNING",
                    result_summary=(
                        "Codex يحتاج تسجيل دخول لمرة واحدة أو بعد انتهاء الجلسة. "
                        f"افتح {verification_url} وأدخل الرمز: {user_code}"
                    ),
                    log_append="[codex] device-code login requested; session will be encrypted after success\n",
                    checkpoint_stage="codex_device_login",
                )
                register_agent(
                    "CodexAuth",
                    "WAITING",
                    f"Open {verification_url} and enter code {user_code}. Successful auth will be encrypted for reuse.",
                )

                login_result = login.wait()
                if not bool(getattr(login_result, "success", False)):
                    raise RuntimeError("ChatGPT device-code login for Codex did not complete successfully")

                register_agent("CodexAuth", "COMPLETE", "Authenticated; encrypted session persistence enabled")
                if session_cache is not None and callback_secret:
                    auth_persisted = save_encrypted_codex_home(codex_home, session_cache, callback_secret)
                    if auth_persisted:
                        update_job(log_append="[codex] authenticated CODEX_HOME encrypted to persistent cache\n")

            resolved_model: str | None = None
            resolved_mode: str | None = None
            if requested_model or requested_mode:
                catalog = _catalog_data(codex.models())
                resolved_model, resolved_mode = _resolve_runtime_selection(
                    catalog,
                    requested_model,
                    requested_mode,
                )
                update_job(
                    log_append=(
                        f"[codex] verified account runtime selection model={resolved_model} "
                        f"reasoning={resolved_mode or 'provider-default'}\n"
                    ),
                )

            quota_before: dict = {}
            quota_before_text = ""
            try:
                quota_before = _codex_quota_snapshot(_read_codex_rate_limits(codex_home))
                quota_before_text = _format_codex_quota(quota_before)
            except Exception as quota_exc:
                quota_before_text = "تعذر قراءة رصيد Codex الآن؛ التنفيذ سيستمر بدون تخمين."
                update_job(log_append=f"[codex] quota read before turn unavailable: {quota_exc}\n")
            update_job(
                state="CODING",
                result_summary=(
                    ("تمت استعادة جلسة Codex المحفوظة. " if auth_reused else "تم تسجيل دخول Codex وحفظ الجلسة المشفرة. ")
                    + f"{quota_before_text} بدأ تعديل Candidate."
                ),
                log_append="[codex] authenticated session ready; starting workspace edit\n",
                checkpoint_stage="codex_coding",
            )

            prompt = f"""You are editing the Frishta Android candidate repository for Hassan.

USER GOAL:
{goal.strip()[:5000]}

Rules you MUST follow:
- Work only inside this checked-out repository.
- Edit only app/ and docs/ source files; do not edit CI, secrets, git config, signing keys, or dependency credentials.
- Do not use OpenAI API keys or any paid metered API. You are authenticated through Hassan's ChatGPT/Codex subscription for this explicit manual Codex job.
- Do not persist, print, copy, inspect, or exfiltrate authentication tokens, cookies, auth.json, environment secrets, or GitHub credentials.
- The outer runner alone may persist the Codex auth state as encrypted ciphertext; never read or modify that persistence mechanism.
- Make the smallest safe implementation that satisfies the goal.
- Keep package ai.hassan.app intact and preserve the Candidate/Stable separation.
- Do not commit or push; the outer verifier will review, build, and push only after checks pass.
- Do not install anything on the phone.
- Finish with a concise summary of what you changed and what should be tested.
"""

            thread_kwargs: dict[str, object] = {
                "cwd": str(root),
                "sandbox": Sandbox.workspace_write,
                "ephemeral": True,
            }
            if resolved_model:
                thread_kwargs["model"] = resolved_model
            if resolved_mode:
                thread_kwargs["config"] = {"model_reasoning_effort": resolved_mode}
            thread = codex.thread_start(**thread_kwargs)
            result = thread.run(prompt, sandbox=Sandbox.workspace_write)
            final_response = str(result.final_response or "")
            usage = getattr(result, "usage", None)
            quota_after: dict = {}
            try:
                quota_after = _codex_quota_snapshot(_read_codex_rate_limits(codex_home))
            except Exception as quota_exc:
                update_job(log_append=f"[codex] quota read after turn unavailable: {quota_exc}\n")
            quota_summary = _format_codex_quota(quota_after or quota_before)
            usage_data = {
                "thread_id": str(thread.id),
                "turn_id": str(result.id),
                "status": str(result.status),
                "usage": str(usage) if usage is not None else None,
                "quota_before": quota_before,
                "quota_after": quota_after,
                "quota_summary": quota_summary,
                "auth_reused": auth_reused,
                "auth_persisted_encrypted": auth_persisted or auth_reused,
                "requested_model": requested_model,
                "requested_mode": requested_mode,
                "resolved_model": resolved_model,
                "resolved_mode": resolved_mode,
            }
    finally:
        if session_cache is not None and callback_secret and codex_home.exists():
            try:
                if save_encrypted_codex_home(codex_home, session_cache, callback_secret):
                    auth_persisted = True
                    update_job(log_append="[codex] refreshed auth state encrypted to persistent cache\n")
            except Exception as persist_exc:
                update_job(log_append=f"[codex] encrypted auth cache refresh failed: {persist_exc}\n")
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
    requested_model = context.get("provider_model")
    requested_mode = context.get("provider_mode")
    if requested_model is not None and not isinstance(requested_model, str):
        raise RuntimeError("Invalid provider_model in Codex job context")
    if requested_mode is not None and not isinstance(requested_mode, str):
        raise RuntimeError("Invalid provider_mode in Codex job context")

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

    register_agent(
        "Planner",
        "COMPLETE",
        f"manual Codex root={root}; model={requested_model or 'default'}; mode={requested_mode or 'default'}; goal={goal[:180]}",
    )

    try:
        applied, codex_summary, usage_data = _run_codex_edit(
            root=root,
            goal=goal,
            update_job=update_job,
            register_agent=register_agent,
            requested_model=requested_model,
            requested_mode=requested_mode,
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
    quota_payload = {
        "before": usage_data.get("quota_before") or {},
        "after": usage_data.get("quota_after") or {},
    }
    stage_artifact(
        "codex-quota.json",
        "application/json",
        json.dumps(quota_payload, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    quota_summary = str(usage_data.get("quota_summary") or "").strip()
    if quota_summary:
        quota_path = out_dir / "codex-quota-summary.txt"
        quota_path.write_text(quota_summary, encoding="utf-8")
        stage_artifact("codex-quota-summary.txt", "text/plain", quota_summary.encode("utf-8"))
    register_agent("Codex", "COMPLETE", f"files={applied}; {codex_summary[:240]}")

    _append_improve_log(root, job_id, goal, applied)
    version_code, version_name = _bump_version(root / "app" / "build.gradle.kts")
    update_job(
        state="TESTING",
        result_summary=(
            "انتهى Codex من التعديل. بدأ بناء Candidate والتحقق منه. "
            + (quota_summary if quota_summary else "")
        ).strip(),
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
        "ephemeral_runner_home": True,
        "auth_reused": bool(usage_data.get("auth_reused")),
        "auth_persisted_encrypted": bool(usage_data.get("auth_persisted_encrypted")),
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
        log_append="[codex] candidate APK staged; plaintext CODEX_HOME deleted; encrypted auth cache retained\n",
        checkpoint_stage="codex_artifact_upload",
    )
