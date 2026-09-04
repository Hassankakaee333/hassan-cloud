"""Read-only Codex account discovery for Frishta.

This module NEVER starts a Codex thread/turn and NEVER starts login. It restores an
already-authorized encrypted ChatGPT/Codex session, then asks the official local
Codex app-server for account, model catalog, and rate-limit metadata.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable


def _parse_responses(stdout: str) -> dict[int, dict]:
    responses: dict[int, dict] = {}
    for raw in stdout.splitlines():
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        request_id = message.get("id")
        if isinstance(request_id, int):
            responses[request_id] = message
    return responses


def _safe_account(result: object) -> dict:
    if not isinstance(result, dict):
        return {}
    account = result.get("account")
    safe: dict[str, object] = {"requiresOpenaiAuth": result.get("requiresOpenaiAuth")}
    if isinstance(account, dict):
        for key in ("type", "email", "planType"):
            value = account.get(key)
            if isinstance(value, (str, bool, int, float)) or value is None:
                safe[key] = value
    return safe


def _safe_models(result: object) -> list[dict]:
    if not isinstance(result, dict):
        return []
    data = result.get("data")
    if not isinstance(data, list):
        return []
    safe_models: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model: dict[str, object] = {}
        for key in (
            "id", "model", "displayName", "description", "isDefault",
            "defaultReasoningEffort", "hidden",
        ):
            value = item.get(key)
            if isinstance(value, (str, bool, int, float)) or value is None:
                model[key] = value
        efforts = item.get("supportedReasoningEfforts")
        if isinstance(efforts, list):
            model["supportedReasoningEfforts"] = [
                value for value in efforts if isinstance(value, (str, dict))
            ]
        if model.get("id") or model.get("model"):
            safe_models.append(model)
    return safe_models


def _quota_percent(value: object) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return max(0.0, min(100.0, float(value)))


def _safe_rate_limits(result: object) -> dict:
    if not isinstance(result, dict):
        return {"limits": {}}
    by_id = result.get("rateLimitsByLimitId")
    selected = by_id.get("codex") if isinstance(by_id, dict) else None
    if not isinstance(selected, dict):
        fallback = result.get("rateLimits")
        selected = fallback if isinstance(fallback, dict) else {}

    limits: dict[str, dict] = {}
    for slot in ("primary", "secondary"):
        window = selected.get(slot)
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
    return {"limits": limits, "planType": selected.get("planType")}


def _read_app_server_snapshot(codex_home: Path) -> dict:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    requests = [
        {
            "jsonrpc": "2.0", "method": "initialize", "id": 1,
            "params": {
                "clientInfo": {
                    "name": "frishta_account_runtime",
                    "title": "Frishta Account Runtime",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        },
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "method": "account/read", "id": 2, "params": {"refreshToken": False}},
        {
            "jsonrpc": "2.0", "method": "model/list", "id": 3,
            "params": {"cursor": None, "limit": 100, "includeHidden": False},
        },
        {"jsonrpc": "2.0", "method": "account/rateLimits/read", "id": 4, "params": None},
    ]
    payload = "\n".join(json.dumps(item, separators=(",", ":")) for item in requests) + "\n"
    proc = subprocess.run(
        ["timeout", "20s", "codex", "app-server", "--stdio"],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=25,
    )
    responses = _parse_responses(proc.stdout)
    for request_id in (2, 3, 4):
        message = responses.get(request_id)
        if not message:
            detail = (proc.stderr or "").strip().splitlines()[-1:] or ["no app-server response"]
            raise RuntimeError(f"Codex app-server request {request_id} unavailable: {detail[0][:180]}")
        if message.get("error"):
            raise RuntimeError(f"Codex app-server request {request_id} failed: {message['error']}")

    account = _safe_account(responses[2].get("result"))
    connected = bool(account.get("type"))
    return {
        "provider": "codex",
        "state": "CONNECTED" if connected else "DISCONNECTED",
        "account": account,
        "models": _safe_models(responses[3].get("result")),
        "usage": _safe_rate_limits(responses[4].get("result")),
        "source": "codex-app-server",
        "modelTurnUsed": False,
    }


def run_codex_account_snapshot_job(
    *,
    out_dir: Path,
    update_job: Callable[..., None],
    register_agent: Callable[[str, str, str], None],
    stage_artifact: Callable[[str, str, bytes], None],
) -> None:
    cache_raw = os.environ.get("CODEX_SESSION_CACHE", "").strip()
    secret = os.environ.get("HASSAN_CALLBACK_SECRET", "").strip()
    snapshot = {
        "provider": "codex",
        "state": "DISCONNECTED",
        "account": {},
        "models": [],
        "usage": {"limits": {}},
        "source": "codex-app-server",
        "modelTurnUsed": False,
    }

    if not cache_raw or not secret:
        snapshot["reason"] = "NO_PERSISTED_SESSION"
        stage_artifact("codex-account-snapshot.json", "application/json", json.dumps(snapshot, indent=2).encode())
        update_job(state="VERIFYING", result_summary="Codex غير متصل: لا توجد جلسة حساب محفوظة.")
        return

    from codex_persistent_auth import restore_encrypted_codex_home

    codex_home = Path(tempfile.mkdtemp(prefix="frishta-codex-readonly-"))
    try:
        restored = restore_encrypted_codex_home(Path(cache_raw), codex_home, secret)
        if not restored:
            snapshot["reason"] = "NO_VALID_PERSISTED_SESSION"
            update_job(state="VERIFYING", result_summary="Codex غير متصل: الجلسة المحفوظة غير موجودة أو غير صالحة.")
        else:
            register_agent("CodexAccountRuntime", "RUNNING", "Reading account/model/quota metadata without a model turn")
            try:
                snapshot = _read_app_server_snapshot(codex_home)
                update_job(
                    state="VERIFYING",
                    result_summary=f"Codex account snapshot جاهز: {len(snapshot['models'])} موديل/خيارات مكتشفة من الحساب.",
                    checkpoint_stage="codex_account_snapshot",
                )
                register_agent("CodexAccountRuntime", "COMPLETE", "Read-only account snapshot completed")
            except Exception as exc:
                snapshot["state"] = "DISCONNECTED"
                snapshot["reason"] = "ACCOUNT_READ_FAILED"
                snapshot["error"] = str(exc)[:240]
                update_job(state="VERIFYING", result_summary="تعذر تحديث حساب Codex بدون تنفيذ أي model turn.")
                register_agent("CodexAccountRuntime", "FAILED", str(exc)[:1000])
    finally:
        shutil.rmtree(codex_home, ignore_errors=True)

    stage_artifact(
        "codex-account-snapshot.json",
        "application/json",
        json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8"),
    )
