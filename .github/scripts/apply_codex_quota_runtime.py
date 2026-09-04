from pathlib import Path

runner_path = Path("scripts/codex_candidate_self_improve_job.py")
text = runner_path.read_text(encoding="utf-8")
if "def _read_codex_rate_limits(" in text:
    raise SystemExit("quota helper already present")
if "import os\n" not in text:
    text = text.replace("import json\nimport shutil", "import json\nimport os\nimport shutil", 1)

helpers = r'''def _read_codex_rate_limits(codex_home: Path) -> dict:
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


'''
anchor = "def _run_codex_edit(\n"
pos = text.find(anchor)
if pos < 0:
    raise SystemExit("_run_codex_edit anchor missing")
text = text[:pos] + helpers + text[pos:]

start_anchor = '                register_agent("CodexAuth", "COMPLETE", "Authenticated for this runner only")'
end_anchor = '\n\n                prompt = f"""'
start = text.find(start_anchor)
end = text.find(end_anchor, start)
if start < 0 or end < 0:
    raise SystemExit("post-login anchors missing")
post_login = '''                register_agent("CodexAuth", "COMPLETE", "Authenticated for this runner only")
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
                )'''
text = text[:start] + post_login + text[end:]

start_anchor = '                final_response = str(result.final_response or "")'
end_anchor = '\n            finally:'
start = text.find(start_anchor)
end = text.find(end_anchor, start)
if start < 0 or end < 0:
    raise SystemExit("turn-result anchors missing")
result_block = '''                final_response = str(result.final_response or "")
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
                }'''
text = text[:start] + result_block + text[end:]

agent_anchor = '    register_agent("Codex", "COMPLETE", f"files={applied}; {codex_summary[:240]}")'
pos = text.find(agent_anchor)
if pos < 0:
    raise SystemExit("Codex complete marker missing")
quota_artifacts = '''    quota_payload = {
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
'''
text = text[:pos] + quota_artifacts + text[pos:]

old_summary = '        result_summary="انتهى Codex من التعديل. بدأ بناء Candidate والتحقق منه.",'
new_summary = '''        result_summary=(
            "انتهى Codex من التعديل. بدأ بناء Candidate والتحقق منه. "
            + (quota_summary if quota_summary else "")
        ).strip(),'''
if text.count(old_summary) != 1:
    raise SystemExit("TESTING result summary marker missing")
text = text.replace(old_summary, new_summary, 1)
runner_path.write_text(text, encoding="utf-8")

finalizer_path = Path("scripts/github_job_runner.py")
finalizer = finalizer_path.read_text(encoding="utf-8")
old = '    elif JOB_TYPE == "codex_candidate_self_improve":\n        summary = "Frishta candidate manual Codex self-improve APK completed via GitHub Actions"'
new = '''    elif JOB_TYPE == "codex_candidate_self_improve":
        summary = "Frishta candidate manual Codex self-improve APK completed via GitHub Actions"
        quota_summary_path = OUT_DIR / "codex-quota-summary.txt"
        if quota_summary_path.exists():
            quota_summary = quota_summary_path.read_text(encoding="utf-8").strip()
            if quota_summary:
                summary += f" — {quota_summary}"'''
if finalizer.count(old) != 1:
    raise SystemExit("Codex finalizer marker missing")
finalizer_path.write_text(finalizer.replace(old, new, 1), encoding="utf-8")
