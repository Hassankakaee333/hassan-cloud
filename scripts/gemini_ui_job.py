"""Gemini official-app worker for Frishta GitHub Actions.

This runner uses Hassan's already signed-in Gemini Android app as the model transport and the
private Phone Agent GitHub bridge as its hand/eye. No Gemini API key is used. Tool calls are
allowlisted and secrets/Stable writes fail closed.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx

from workspace_io import fetch_job_context

PHONE_REPO = os.environ.get("FRISHTA_PHONE_REPO", "Hassankakaee333/FMK-AI-BRIDGE").strip()
PHONE_BRANCH = os.environ.get("FRISHTA_PHONE_BRANCH", "phone-control").strip()
CANDIDATE_REPO = os.environ.get("HASSAN_CANDIDATE_REPO", PHONE_REPO).strip() or PHONE_REPO
TOKEN = os.environ.get("HASSAN_CANDIDATE_TOKEN", "").strip()
API_URL = os.environ.get("HASSAN_API_URL", "").rstrip("/")
CALLBACK_SECRET = os.environ.get("HASSAN_CALLBACK_SECRET", "")
GEMINI_LAUNCH_PACKAGE = "com.google.android.apps.bard"
GEMINI_FOREGROUND_PACKAGES = {"com.google.android.apps.bard", "com.google.android.googlequicksearchbox"}
STABLE_REFS = {"main", "master", "stable", "refs/heads/main", "refs/heads/master", "refs/heads/stable"}
MAX_TEXT = 12000
MAX_GITHUB_FILE = 128 * 1024
MAX_PHONE_RESULT = 128 * 1024
SECRET_PATH_PARTS = {
    ".env", "keystore", "keystores", "signing", "secrets", "credentials", "private-key",
    "private_key", ".ssh", ".gnupg",
}
PHONE_ACTIONS = {
    "PING", "UI_TREE", "OPEN_APP", "HOME", "BACK", "RECENTS", "NOTIFICATIONS",
    "QUICK_SETTINGS", "CLICK_TEXT", "SET_TEXT", "TAP", "SWIPE",
    "SCROLL_FORWARD", "SCROLL_BACKWARD", "SCREENSHOT",
}
FORBIDDEN_PHONE_ACTIONS = {"SET_SECRET_TEXT", "GET_SECURE_INPUT_KEY"}
TOOL_MARKER = "FRISHTA_TOOL:"
FINAL_MARKER = "FRISHTA_FINAL:"


def _gh_headers() -> dict[str, str]:
    if not TOKEN:
        raise RuntimeError("HASSAN_CANDIDATE_TOKEN is not configured")
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Frishta-Gemini-UI-Worker/1",
    }


def _gh(method: str, url: str, payload: dict[str, Any] | None = None) -> Any:
    response = httpx.request(method, url, headers=_gh_headers(), json=payload, timeout=45.0)
    if response.status_code >= 400:
        raise RuntimeError(f"GitHub HTTP {response.status_code}: {response.text[:400]}")
    return response.json() if response.content else {}


def _put_phone_file(path: str, value: dict[str, Any], message: str) -> None:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload = {
        "message": message[:160],
        "content": base64.b64encode(raw).decode("ascii"),
        "branch": PHONE_BRANCH,
    }
    _gh("PUT", f"https://api.github.com/repos/{PHONE_REPO}/contents/{quote(path, safe='/')}", payload)


def _read_phone_json(path: str) -> dict[str, Any] | None:
    url = f"https://api.github.com/repos/{PHONE_REPO}/contents/{quote(path, safe='/')}?ref={quote(PHONE_BRANCH, safe='')}"
    response = httpx.get(url, headers=_gh_headers(), timeout=30.0)
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise RuntimeError(f"GitHub HTTP {response.status_code}: {response.text[:300]}")
    row = response.json()
    raw = base64.b64decode(row.get("content", ""), validate=False)
    if len(raw) > MAX_PHONE_RESULT:
        raise RuntimeError("Phone Agent result too large")
    return json.loads(raw.decode("utf-8"))


class PhoneBridge:
    def command(self, action: str, args: dict[str, Any] | None = None, *, timeout: float = 45.0) -> dict[str, Any]:
        upper = action.strip().upper()
        if upper in FORBIDDEN_PHONE_ACTIONS or upper not in PHONE_ACTIONS:
            raise ValueError("phone action is not allowed")
        command_id = f"gw-{uuid.uuid4().hex[:20]}"
        command: dict[str, Any] = {
            "id": command_id,
            "action": upper,
            "requiresConfirmation": False,
            "expiresAtEpochMs": int(time.time() * 1000) + 120_000,
        }
        for key, value in (args or {}).items():
            if key not in {"id", "action", "requiresConfirmation", "expiresAtEpochMs"}:
                command[key] = value
        _put_phone_file(f"inbox/{command_id}.json", command, f"Frishta Gemini worker {upper} {command_id}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = _read_phone_json(f"outbox/{command_id}.json")
            if result is not None:
                return result
            time.sleep(1.5)
        raise TimeoutError(f"Phone Agent timeout: {upper}")


def _extract_protocol(ui_tree: str) -> tuple[str, str] | None:
    text = ui_tree.replace("\\n", "\n")
    found: list[tuple[int, str, str]] = []
    for marker, kind in ((TOOL_MARKER, "tool"), (FINAL_MARKER, "final")):
        start_at = 0
        while True:
            idx = text.find(marker, start_at)
            if idx < 0:
                break
            tail = text[idx + len(marker):]
            brace = tail.find("{")
            if brace >= 0:
                depth = 0
                in_string = False
                escaped = False
                for pos, ch in enumerate(tail[brace:], start=brace):
                    if in_string:
                        if escaped:
                            escaped = False
                        elif ch == "\\":
                            escaped = True
                        elif ch == '"':
                            in_string = False
                        continue
                    if ch == '"':
                        in_string = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            found.append((idx, kind, tail[brace:pos + 1]))
                            break
            elif kind == "final":
                line = tail.splitlines()[0].strip()
                if line:
                    found.append((idx, kind, line[:MAX_TEXT]))
            start_at = idx + len(marker)
    if not found:
        return None
    _, kind, payload = max(found, key=lambda item: item[0])
    return kind, payload[:MAX_TEXT]


def _editable_center(tree: str) -> tuple[int, int] | None:
    for line in tree.splitlines():
        if "editable=true" not in line.lower() or "password=true" in line.lower():
            continue
        match = re.search(r"bounds=(\d+) (\d+) (\d+) (\d+)", line)
        if match:
            x1, y1, x2, y2 = map(int, match.groups())
            return ((x1 + x2) // 2, (y1 + y2) // 2)
    return None


def _send_center(tree: str) -> tuple[int, int] | None:
    hints = ("desc=إرسال", "text=إرسال", "desc=send", "text=send", "send_button", "submit")
    for line in tree.splitlines():
        low = line.lower()
        if not any(h.lower() in low for h in hints):
            continue
        match = re.search(r"bounds=(\d+) (\d+) (\d+) (\d+)", line)
        if match:
            x1, y1, x2, y2 = map(int, match.groups())
            return ((x1 + x2) // 2, (y1 + y2) // 2)
    return None


class GeminiTransport:
    def __init__(self, phone: PhoneBridge) -> None:
        self.phone = phone

    def _tree(self, *, reopen: bool = False) -> str:
        if reopen:
            self.phone.command("OPEN_APP", {"packageName": GEMINI_LAUNCH_PACKAGE})
            time.sleep(1.5)
        last_active = ""
        for _ in range(5):
            data = self.phone.command("UI_TREE")
            last_active = str(data.get("activePackage") or "")
            if last_active in GEMINI_FOREGROUND_PACKAGES:
                return str(data.get("uiTree") or "")
            if last_active.startswith("com.android.stk"):
                # Carrier popups are not interacted with automatically. Re-open Gemini and retry.
                self.phone.command("OPEN_APP", {"packageName": GEMINI_LAUNCH_PACKAGE})
                time.sleep(1.5)
                continue
            if last_active == "com.android.systemui":
                raise RuntimeError("phone is locked; unlock the phone and resume the Gemini job")
            self.phone.command("OPEN_APP", {"packageName": GEMINI_LAUNCH_PACKAGE})
            time.sleep(1.5)
        raise RuntimeError(f"Gemini is not foreground; active={last_active}")

    def send(self, text: str) -> None:
        if not text or len(text) > MAX_TEXT:
            raise ValueError("Gemini message exceeds worker limit")
        tree = self._tree(reopen=True)
        editable = _editable_center(tree)
        if not editable:
            raise RuntimeError("Gemini safe editable field not found")
        self.phone.command("TAP", {"x": editable[0], "y": editable[1]})
        result = self.phone.command("SET_TEXT", {"targetText": "", "text": text})
        if result.get("status") != "COMPLETED":
            raise RuntimeError(f"Gemini SET_TEXT failed: {result.get('message')}")
        tree = self._tree()
        send = _send_center(tree)
        if send:
            self.phone.command("TAP", {"x": send[0], "y": send[1]})
            return
        for label in ("إرسال", "Send"):
            try:
                clicked = self.phone.command("CLICK_TEXT", {"targetText": label}, timeout=15.0)
                if clicked.get("status") == "COMPLETED":
                    return
            except Exception:
                pass
        raise RuntimeError("Gemini send control not found")

    def await_protocol(self, *, timeout: float = 120.0) -> tuple[str, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            marker = _extract_protocol(self._tree(reopen=True))
            if marker:
                return marker
            time.sleep(2.5)
        raise TimeoutError("Gemini protocol marker not found")


def _safe_candidate_ref(value: str) -> str:
    ref = value.strip()
    if ref.lower() in STABLE_REFS or not ref.startswith("frishta-"):
        raise ValueError("Candidate ref required; Stable/main is forbidden")
    return ref


def _safe_repo(value: str) -> str:
    repo = value.strip()
    allowed = {PHONE_REPO, CANDIDATE_REPO, "Hassankakaee333/hassan-cloud"}
    if repo not in allowed or repo.count("/") != 1:
        raise ValueError("repository is not allowed")
    return repo


def _safe_path(value: str) -> str:
    path = value.strip().replace("\\", "/")
    parts = path.split("/")
    if not path or path.startswith("/") or any(p in {"", ".", ".."} for p in parts):
        raise ValueError("invalid path")
    lowered = {p.lower() for p in parts}
    if lowered & SECRET_PATH_PARTS:
        raise ValueError("secret/signing path is blocked")
    return path


def _callback_get(path: str) -> Any:
    if not API_URL or not CALLBACK_SECRET:
        raise RuntimeError("Hassan Cloud callback configuration missing")
    response = httpx.get(
        f"{API_URL}{path}",
        headers={"X-Hassan-Callback-Secret": CALLBACK_SECRET},
        timeout=60.0,
    )
    response.raise_for_status()
    return response.json()


def _workspace_listing(project_id: str) -> dict[str, Any]:
    payload = _callback_get(f"/v1/internal/projects/{project_id}/workspace")
    files = [
        {"path": row.get("path"), "size_bytes": row.get("size_bytes"), "sha256": row.get("sha256")}
        for row in payload.get("files", [])
    ]
    return {"project_id": project_id, "files": files[:300], "total_bytes": payload.get("total_bytes", 0)}


def _github_read(args: dict[str, Any]) -> dict[str, Any]:
    repo = _safe_repo(str(args.get("repo") or CANDIDATE_REPO))
    ref = str(args.get("ref") or "").strip()
    path = _safe_path(str(args.get("path") or ""))
    if not ref:
        raise ValueError("ref required")
    row = _gh("GET", f"https://api.github.com/repos/{repo}/contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}")
    if row.get("type") != "file" or row.get("encoding") != "base64":
        raise ValueError("UTF-8 file required")
    raw = base64.b64decode(row.get("content", ""), validate=False)
    if len(raw) > MAX_GITHUB_FILE:
        raise ValueError("file exceeds read limit")
    return {"repo": repo, "ref": ref, "path": path, "sha": row.get("sha"), "content": raw.decode("utf-8")}


def _github_create_branch(args: dict[str, Any]) -> dict[str, Any]:
    repo = _safe_repo(str(args.get("repo") or CANDIDATE_REPO))
    base_ref = _safe_candidate_ref(str(args.get("base_ref") or ""))
    new_ref = _safe_candidate_ref(str(args.get("new_ref") or ""))
    base = _gh("GET", f"https://api.github.com/repos/{repo}/git/ref/heads/{quote(base_ref, safe='')}")
    sha = base["object"]["sha"]
    row = _gh("POST", f"https://api.github.com/repos/{repo}/git/refs", {"ref": f"refs/heads/{new_ref}", "sha": sha})
    return {"repo": repo, "base_ref": base_ref, "ref": new_ref, "sha": row.get("object", {}).get("sha", sha)}


def _github_write(args: dict[str, Any]) -> dict[str, Any]:
    repo = _safe_repo(str(args.get("repo") or CANDIDATE_REPO))
    ref = _safe_candidate_ref(str(args.get("ref") or ""))
    path = _safe_path(str(args.get("path") or ""))
    content = str(args.get("content") or "")
    if len(content.encode("utf-8")) > MAX_GITHUB_FILE:
        raise ValueError("content exceeds write limit")
    payload: dict[str, Any] = {
        "message": str(args.get("message") or "Frishta Gemini Candidate edit")[:180],
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": ref,
    }
    expected_sha = str(args.get("expected_sha") or "").strip()
    if expected_sha:
        payload["sha"] = expected_sha
    row = _gh("PUT", f"https://api.github.com/repos/{repo}/contents/{quote(path, safe='/')}", payload)
    return {"repo": repo, "ref": ref, "path": path, "commit_sha": row.get("commit", {}).get("sha"), "content_sha": row.get("content", {}).get("sha")}


def _github_open_pr(args: dict[str, Any]) -> dict[str, Any]:
    repo = _safe_repo(str(args.get("repo") or CANDIDATE_REPO))
    head = _safe_candidate_ref(str(args.get("head_ref") or ""))
    base = _safe_candidate_ref(str(args.get("base_ref") or ""))
    row = _gh("POST", f"https://api.github.com/repos/{repo}/pulls", {
        "title": str(args.get("title") or "Frishta Gemini Candidate")[:180],
        "body": str(args.get("body") or "")[:8000],
        "head": head,
        "base": base,
        "draft": False,
    })
    return {"number": row.get("number"), "url": row.get("html_url"), "head_ref": head, "base_ref": base}


def _github_runs(args: dict[str, Any]) -> dict[str, Any]:
    repo = _safe_repo(str(args.get("repo") or CANDIDATE_REPO))
    ref = _safe_candidate_ref(str(args.get("ref") or ""))
    row = _gh("GET", f"https://api.github.com/repos/{repo}/actions/runs?branch={quote(ref, safe='')}&per_page=10")
    return {"repo": repo, "ref": ref, "runs": [
        {"id": item.get("id"), "name": item.get("name"), "status": item.get("status"), "conclusion": item.get("conclusion"), "head_sha": item.get("head_sha"), "url": item.get("html_url")}
        for item in row.get("workflow_runs", [])
    ]}


def tool_catalog() -> list[dict[str, Any]]:
    return [
        {"name": "cloud.job.context", "description": "Read this Gemini job context only."},
        {"name": "cloud.workspace.list", "description": "List non-secret workspace file metadata for this project."},
        {"name": "github.file.read", "description": "Read a UTF-8 file from an allowed repository/ref; secret paths blocked."},
        {"name": "github.branch.create_candidate", "description": "Create Candidate branch from Candidate base only."},
        {"name": "github.file.write_candidate", "description": "Create/update UTF-8 file on Candidate branch only."},
        {"name": "github.pr.open_candidate", "description": "Open Candidate-to-Candidate PR only."},
        {"name": "github.workflow.runs", "description": "Read recent workflow runs for a Candidate ref."},
        {"name": "phone.command", "description": "Run one non-secret Phone Agent action and return its result."},
    ]


def execute_tool(tool: str, arguments: dict[str, Any], *, job_id: str, project_id: str, phone: PhoneBridge) -> dict[str, Any]:
    try:
        if tool == "cloud.job.context":
            return {"status": "OK", "tool": tool, "data": fetch_job_context(job_id)}
        if tool == "cloud.workspace.list":
            return {"status": "OK", "tool": tool, "data": _workspace_listing(project_id)}
        if tool == "github.file.read":
            return {"status": "OK", "tool": tool, "data": _github_read(arguments)}
        if tool == "github.branch.create_candidate":
            return {"status": "OK", "tool": tool, "data": _github_create_branch(arguments)}
        if tool == "github.file.write_candidate":
            return {"status": "OK", "tool": tool, "data": _github_write(arguments)}
        if tool == "github.pr.open_candidate":
            return {"status": "OK", "tool": tool, "data": _github_open_pr(arguments)}
        if tool == "github.workflow.runs":
            return {"status": "OK", "tool": tool, "data": _github_runs(arguments)}
        if tool == "phone.command":
            action = str(arguments.get("action") or "")
            result = phone.command(action, dict(arguments.get("args") or {}))
            return {"status": "OK", "tool": tool, "data": result}
        return {"status": "REJECTED", "tool": tool, "detail": "unknown tool"}
    except (ValueError, KeyError, TypeError) as exc:
        return {"status": "REJECTED", "tool": tool, "detail": str(exc)[:500]}
    except Exception as exc:
        return {"status": "ERROR", "tool": tool, "detail": f"{type(exc).__name__}: {str(exc)[:400]}"}


def _bounded(value: Any, limit: int = MAX_TEXT - 500) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[:limit] + "…"


def initial_prompt(goal: str) -> str:
    tools = tool_catalog()
    return (
        "You are Gemini acting as a Frishta execution brain. Use only the protocol below and only the listed tools. "
        "Never claim a tool ran unless a TOOL_RESULT is returned. Never request passwords, tokens, secrets, payments, Stable/main writes, Codex, or unsupported tools. "
        "Use one tool call per turn. For a tool call answer exactly: FRISHTA_TOOL:{\"tool\":\"name\",\"arguments\":{...}}. "
        "When finished answer exactly: FRISHTA_FINAL:{\"summary\":\"...\"}.\n"
        f"TOOLS={_bounded(tools, 5000)}\nTASK={goal[:6000]}"
    )


def run_gemini_ui_job(
    *,
    job_id: str,
    project_id: str,
    update_job: Callable[..., None],
    register_agent: Callable[[str, str, str], None],
    stage_artifact: Callable[[str, str, bytes], None],
    max_steps: int = 8,
) -> None:
    context = fetch_job_context(job_id)
    goal = str(context.get("goal") or "").strip()
    if not goal:
        raise RuntimeError("Gemini job goal is empty")
    phone = PhoneBridge()
    transport = GeminiTransport(phone)
    transcript: list[dict[str, Any]] = []

    update_job(state="RUNNING", log_append="[gemini-ui] starting official Gemini app worker\n", checkpoint_stage="gemini_open")
    register_agent("Gemini", "RUNNING", "Official Android app transport; no provider API key")
    transport.send(initial_prompt(goal))

    for step in range(max(1, min(int(max_steps), 12))):
        kind, payload = transport.await_protocol()
        transcript.append({"turn": step + 1, "kind": kind, "payload": payload[:4000]})
        if kind == "final":
            try:
                final_obj = json.loads(payload)
                summary = str(final_obj.get("summary") or final_obj)[:4000]
            except Exception:
                summary = payload[:4000]
            stage_artifact("gemini-ui-transcript.json", "application/json", json.dumps(transcript, ensure_ascii=False, indent=2).encode("utf-8"))
            register_agent("Gemini", "COMPLETE", summary)
            update_job(
                state="VERIFYING",
                result_summary=summary,
                log_append=f"[gemini-ui] FRISHTA_FINAL after {step + 1} turns\n",
                checkpoint_stage="gemini_done",
            )
            return

        try:
            call = json.loads(payload)
        except json.JSONDecodeError as exc:
            result = {"status": "REJECTED", "detail": f"invalid JSON: {exc}"}
            transport.send(f"TOOL_RESULT={_bounded(result)}")
            continue
        tool = str(call.get("tool") or "")
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        result = execute_tool(tool, arguments, job_id=job_id, project_id=project_id, phone=phone)
        transcript.append({"turn": step + 1, "tool": tool, "result": result})
        update_job(log_append=f"[gemini-ui] tool turn {step + 1}: {tool} -> {result.get('status')}\n", checkpoint_stage=f"gemini_tool_{step + 1}")
        transport.send(f"TOOL_RESULT={_bounded(result)}")

    stage_artifact("gemini-ui-transcript.json", "application/json", json.dumps(transcript, ensure_ascii=False, indent=2).encode("utf-8"))
    raise RuntimeError("Gemini reached the configured tool-turn limit before FRISHTA_FINAL")
