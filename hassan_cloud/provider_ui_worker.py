"""Phone-backed Gemini worker using the official Android UI and the shared Tool Gateway.

No provider API key is used. The worker drives only the already signed-in Gemini UI through
Phone Agent accessibility, asks Gemini for a tiny machine-readable tool protocol, executes
allowlisted tools server-side, and feeds bounded results back. Stable/main and secret input remain
blocked by ToolGateway.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .tool_gateway import ToolGateway, tool_catalog

_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="frishta-provider-ui")
_MARKER_TOOL = "FRISHTA_TOOL:"
_MARKER_FINAL = "FRISHTA_FINAL:"
_GEMINI_LAUNCH_PACKAGE = "com.google.android.apps.bard"
# On current Google builds the Gemini surface can be hosted by the Google app process.
_GEMINI_PACKAGES = {
    "com.google.android.apps.bard",
    "com.google.android.googlequicksearchbox",
}
_SEND_LABELS = ("إرسال", "Send", "Submit")


def _bounded(value: Any, limit: int = 12000) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return raw if len(raw) <= limit else raw[:limit] + "…"


def _extract_marker(ui_tree: str) -> tuple[str, str] | None:
    text = ui_tree.replace("\\n", "\n")
    for marker, kind in ((_MARKER_FINAL, "final"), (_MARKER_TOOL, "tool")):
        idx = text.rfind(marker)
        if idx < 0:
            continue
        tail = text[idx + len(marker):]
        start = tail.find("{")
        if start < 0:
            if kind == "final":
                line = tail.splitlines()[0].strip()
                if line:
                    return kind, line[:12000]
            continue
        depth = 0
        in_string = False
        escaped = False
        for pos, ch in enumerate(tail[start:], start=start):
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
                    return kind, tail[start:pos + 1]
    return None


def _node_center(ui_tree: str, *, editable: bool = False, labels: tuple[str, ...] = ()) -> tuple[int, int] | None:
    for line in ui_tree.splitlines():
        low = line.lower()
        if editable and "editable=true" not in low:
            continue
        if labels and not any((f"text={label}".lower() in low or f"desc={label}".lower() in low) for label in labels):
            continue
        match = re.search(r"bounds=(\d+) (\d+) (\d+) (\d+)", line)
        if match:
            x1, y1, x2, y2 = map(int, match.groups())
            return ((x1 + x2) // 2, (y1 + y2) // 2)
    return None


def _is_gemini_package(package: str | None) -> bool:
    return bool(package and package in _GEMINI_PACKAGES)


class GeminiUiWorker:
    def __init__(self, repo, new_id: Callable[[], str], now_ms: Callable[[], int]) -> None:
        self.repo = repo
        self.new_id = new_id
        self.now_ms = now_ms
        self.gateway = ToolGateway(repo, new_id, now_ms)
        self._lock = threading.Lock()
        self._active: set[str] = set()

    def start(self, job_id: str, goal: str, *, max_steps: int = 8) -> dict[str, Any]:
        with self._lock:
            if job_id in self._active:
                return {"status": "ALREADY_RUNNING", "job_id": job_id}
            self._active.add(job_id)
        _POOL.submit(self._run_guarded, job_id, goal, max(1, min(max_steps, 12)))
        return {"status": "QUEUED", "job_id": job_id, "provider": "gemini", "transport": "official-android-ui"}

    def _run_guarded(self, job_id: str, goal: str, max_steps: int) -> None:
        try:
            self._run(job_id, goal, max_steps)
        except Exception as exc:
            self.repo.update_job(job_id, "FAILED", f"[gemini-ui] {type(exc).__name__}: {str(exc)[:400]}\n", str(exc)[:1000], self.now_ms())
        finally:
            with self._lock:
                self._active.discard(job_id)

    def _phone(self, action: str, args: dict[str, Any] | None = None, timeout: float = 35.0) -> dict[str, Any]:
        queued = self.gateway.invoke("phone.command", {"action": action, "args": args or {}})
        if queued.get("status") != "OK":
            raise RuntimeError(f"phone command rejected: {queued}")
        cid = queued["data"]["command_id"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.gateway.invoke("phone.result", {"command_id": cid})
            if result.get("status") != "OK":
                raise RuntimeError(f"phone result failed: {result}")
            data = result.get("data") or {}
            if data.get("status") not in ("PENDING", None):
                if data.get("status") != "COMPLETED":
                    raise RuntimeError(f"phone action {action} failed: {data}")
                return data
            time.sleep(1.5)
        raise TimeoutError(f"phone action timed out: {action}")

    def _tree_data(self) -> dict[str, Any]:
        data = self._phone("UI_TREE")
        active = data.get("activePackage")
        if not _is_gemini_package(active):
            raise RuntimeError(f"Gemini is not foreground; active={active}")
        return data

    def _tree(self) -> str:
        return str(self._tree_data().get("uiTree") or "")

    def _open_gemini(self, timeout: float = 20.0) -> None:
        # OPEN_APP completion can report the previous foreground package on some Android builds,
        # so verify by polling UI_TREE instead of trusting the immediate command result.
        self._phone("OPEN_APP", {"packageName": _GEMINI_LAUNCH_PACKAGE})
        deadline = time.monotonic() + timeout
        last_active: str | None = None
        while time.monotonic() < deadline:
            data = self._phone("UI_TREE")
            last_active = data.get("activePackage")
            if _is_gemini_package(last_active):
                return
            time.sleep(1.0)
        raise RuntimeError(f"Gemini did not reach foreground; active={last_active}")

    def _enter_and_send(self, text: str) -> None:
        tree = self._tree()
        editable = _node_center(tree, editable=True)
        if not editable:
            raise RuntimeError("Gemini editable input not found")
        self._phone("TAP", {"x": editable[0], "y": editable[1]})
        # Re-check foreground immediately before SET_TEXT so text cannot be written into another app.
        self._tree()
        self._phone("SET_TEXT", {"targetText": "", "text": text})
        tree = self._tree()
        send = _node_center(tree, labels=_SEND_LABELS)
        if send:
            self._phone("TAP", {"x": send[0], "y": send[1]})
            return
        for label in _SEND_LABELS:
            try:
                self._phone("CLICK_TEXT", {"targetText": label}, timeout=12.0)
                self._tree()
                return
            except Exception:
                pass
        raise RuntimeError("Gemini send control not found")

    def _await_protocol(self, timeout: float = 90.0) -> tuple[str, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            marker = _extract_marker(self._tree())
            if marker:
                return marker
            time.sleep(2.5)
        raise TimeoutError("Gemini response protocol marker not found")

    def _initial_prompt(self, goal: str) -> str:
        compact = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tool_catalog()]
        return (
            "You are the Gemini worker inside Frishta AI. Use only the tool protocol below. "
            "Do not claim a tool ran unless a TOOL_RESULT is returned. Never request secrets, Stable/main writes, Codex, payments, or unsupported tools. "
            "For one tool call reply with exactly: FRISHTA_TOOL:{\"tool\":\"name\",\"arguments\":{...}} . "
            "When the task is complete reply with exactly: FRISHTA_FINAL:{\"summary\":\"...\"}. "
            "You may make one tool call per turn.\n"
            f"TOOLS={_bounded(compact, 10000)}\nTASK={goal[:8000]}"
        )

    def _run(self, job_id: str, goal: str, max_steps: int) -> None:
        self.repo.update_job(job_id, "RUNNING", "[gemini-ui] opening official Gemini app\n", None, self.now_ms())
        self._open_gemini()
        self._enter_and_send(self._initial_prompt(goal))

        for step in range(max_steps):
            kind, payload = self._await_protocol()
            if kind == "final":
                try:
                    obj = json.loads(payload)
                    summary = str(obj.get("summary") or obj)[:4000]
                except Exception:
                    summary = payload[:4000]
                self.repo.set_checkpoint(job_id, "provider_done", self.now_ms())
                self.repo.update_job(job_id, "COMPLETED", f"[gemini-ui] completed after {step + 1} turns\n", summary, self.now_ms())
                return

            try:
                call = json.loads(payload)
            except json.JSONDecodeError as exc:
                self._enter_and_send(f"TOOL_RESULT={{\"status\":\"REJECTED\",\"detail\":\"invalid JSON: {exc}\"}}")
                continue
            tool = str(call.get("tool") or "")
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            result = self.gateway.invoke(tool, arguments)
            self.repo.set_checkpoint(job_id, f"provider_tool_{step + 1}", self.now_ms())
            self._enter_and_send(f"TOOL_RESULT={_bounded(result)}")

        self.repo.update_job(job_id, "FAILED", "[gemini-ui] max tool turns reached\n", "Gemini reached the configured tool-turn limit before FRISHTA_FINAL.", self.now_ms())
