from pathlib import Path

path = Path('scripts/codex_candidate_self_improve_job.py')
text = path.read_text(encoding='utf-8')

text = text.replace(
    'from typing import Callable\n\nfrom candidate_self_improve_job import (',
    'from typing import Callable\n\nfrom codex_persistent_auth import restore_encrypted_codex_home, save_encrypted_codex_home\nfrom candidate_self_improve_job import (',
)

old = '''    codex_home = Path(tempfile.mkdtemp(prefix="frishta-codex-home-"))
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
                    log_append="[codex] device-code login requested; isolated temporary CODEX_HOME\\n",
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
                quota_before: dict = {}
                quota_before_text = ""
                try:
                    quota_before = _codex_quota_snapshot(_read_codex_rate_limits(codex_home))
                    quota_before_text = _format_codex_quota(quota_before)
                except Exception as quota_exc:
                    quota_before_text = "تعذر قراءة رصيد Codex الآن؛ التنفيذ سيستمر بدون تخمين."
                    update_job(log_append=f"[codex] quota read before turn unavailable: {quota_exc}\\n")
                update_job(
                    state="CODING",
                    result_summary=(
                        "تم تسجيل دخول Codex لهذه المهمة فقط. "
                        f"{quota_before_text} بدأ تعديل Candidate."
                    ),
                    log_append="[codex] ephemeral ChatGPT login complete; starting workspace edit\\n",
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
                quota_after: dict = {}
                try:
                    quota_after = _codex_quota_snapshot(_read_codex_rate_limits(codex_home))
                except Exception as quota_exc:
                    update_job(log_append=f"[codex] quota read after turn unavailable: {quota_exc}\\n")
                quota_summary = _format_codex_quota(quota_after or quota_before)
                usage_data = {
                    "thread_id": str(thread.id),
                    "turn_id": str(result.id),
                    "status": str(result.status),
                    "usage": str(usage) if usage is not None else None,
                    "quota_before": quota_before,
                    "quota_after": quota_after,
                    "quota_summary": quota_summary,
                }
            finally:
                try:
                    codex.logout()
                except Exception:
                    pass
    finally:
        shutil.rmtree(codex_home, ignore_errors=True)
'''

new = '''    codex_home = Path(tempfile.mkdtemp(prefix="frishta-codex-home-"))
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
                    log_append="[codex] encrypted persisted auth restored and validated\\n",
                    checkpoint_stage="codex_auth_reused",
                )
            except Exception as auth_exc:
                update_job(log_append=f"[codex] persisted auth invalid/expired; requesting fresh device login: {auth_exc}\\n")
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
                    log_append="[codex] device-code login requested; session will be encrypted after success\\n",
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
                        update_job(log_append="[codex] authenticated CODEX_HOME encrypted to persistent cache\\n")

            quota_before: dict = {}
            quota_before_text = ""
            try:
                quota_before = _codex_quota_snapshot(_read_codex_rate_limits(codex_home))
                quota_before_text = _format_codex_quota(quota_before)
            except Exception as quota_exc:
                quota_before_text = "تعذر قراءة رصيد Codex الآن؛ التنفيذ سيستمر بدون تخمين."
                update_job(log_append=f"[codex] quota read before turn unavailable: {quota_exc}\\n")
            update_job(
                state="CODING",
                result_summary=(
                    ("تمت استعادة جلسة Codex المحفوظة. " if auth_reused else "تم تسجيل دخول Codex وحفظ الجلسة المشفرة. ")
                    + f"{quota_before_text} بدأ تعديل Candidate."
                ),
                log_append="[codex] authenticated session ready; starting workspace edit\\n",
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

            thread = codex.thread_start(
                cwd=str(root),
                sandbox=Sandbox.workspace_write,
                ephemeral=True,
            )
            result = thread.run(prompt, sandbox=Sandbox.workspace_write)
            final_response = str(result.final_response or "")
            usage = getattr(result, "usage", None)
            quota_after: dict = {}
            try:
                quota_after = _codex_quota_snapshot(_read_codex_rate_limits(codex_home))
            except Exception as quota_exc:
                update_job(log_append=f"[codex] quota read after turn unavailable: {quota_exc}\\n")
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
            }
    finally:
        if session_cache is not None and callback_secret and codex_home.exists():
            try:
                if save_encrypted_codex_home(codex_home, session_cache, callback_secret):
                    auth_persisted = True
                    update_job(log_append="[codex] refreshed auth state encrypted to persistent cache\\n")
            except Exception as persist_exc:
                update_job(log_append=f"[codex] encrypted auth cache refresh failed: {persist_exc}\\n")
        shutil.rmtree(codex_home, ignore_errors=True)
'''

if old not in text:
    raise SystemExit('target Codex auth block not found')
text = text.replace(old, new)

text = text.replace(
    '        "ephemeral_auth": True,\n        "auth_persisted": False,',
    '        "ephemeral_runner_home": True,\n        "auth_reused": bool(usage_data.get("auth_reused")),\n        "auth_persisted_encrypted": bool(usage_data.get("auth_persisted_encrypted")),',
)
text = text.replace(
    '        log_append="[codex] candidate APK staged; temporary auth session deleted\\n",',
    '        log_append="[codex] candidate APK staged; plaintext CODEX_HOME deleted; encrypted auth cache retained\\n",',
)
text = text.replace(
    '- Every run performs a fresh ChatGPT device-code login on the ephemeral runner.\n- Codex auth is isolated under a temporary CODEX_HOME and deleted after the run.\n- No Codex auth.json, access token, refresh token, cookie, or API key is staged/uploaded.',
    '- Device-code login is requested only when no valid encrypted persisted session exists.\n- Plaintext Codex auth is isolated under a temporary CODEX_HOME and deleted after the run.\n- Only AES-GCM ciphertext is persisted in GitHub Actions cache; no plaintext auth token is staged/uploaded.',
)

path.write_text(text, encoding='utf-8')
print('persistent Codex auth patch applied')
